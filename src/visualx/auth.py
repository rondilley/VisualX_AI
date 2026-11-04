"""OAuth 2.0 Authorization Code with PKCE for the owner's X account (AD-05, IR-03, SR-01).

The owner signs in one time. The tool opens the browser at the X
authorization URL and starts a listener on ``127.0.0.1`` only. The listener
accepts exactly one ``GET /callback`` with a ``code`` and the correct
``state``. It rejects other paths, other methods, a missing code, a wrong
state, and a second callback, and then it stops.

Tokens (access token, refresh token, expiry, scopes) go into the OS keyring
as one JSON value (service ``visualx``, username ``x_oauth``). If the keyring
has no usable backend, the tool stops with a clear error. It never writes a
token to a file.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from typing import Any

import httpx
import keyring
import keyring.errors
from keyring.backends import chainer as _kr_chainer
from keyring.backends import fail as _kr_fail

try:  # the null backend is in keyring 25; keep the check optional
    from keyring.backends import null as _kr_null
except ImportError:  # pragma: no cover
    _kr_null = None  # type: ignore[assignment]

from .config import Config

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
REVOKE_URL = "https://api.x.com/2/oauth2/revoke"
KEYRING_SERVICE = "visualx"
KEYRING_USER = "x_oauth"
LOOPBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
HTTP_TIMEOUT = 30.0
# Refresh the access token when it expires in less than this many seconds.
EXPIRY_MARGIN = 60.0

LOGIN_HINT = "Run 'visualx auth login' to sign in again."


class AuthError(RuntimeError):
    """A sign-in, token, or keyring operation did not succeed."""


# -- PKCE ---------------------------------------------------------------


def make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for the S256 method (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    challenge: str,
    authorize_url: str = AUTHORIZE_URL,
) -> str:
    q = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{authorize_url}?{urllib.parse.urlencode(q, quote_via=urllib.parse.quote)}"


# -- Loopback listener --------------------------------------------------


class _Server(http.server.HTTPServer):
    listener: CallbackListener
    # HTTPServer sets SO_REUSEADDR. On Windows that lets a second program bind the
    # same port and receive the callback with the authorization code. The
    # listener asks for exclusive use of the port (review 20261103, A-29).
    allow_reuse_address = False

    def server_bind(self) -> None:
        excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if excl is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, excl, 1)
        super().server_bind()


class _Handler(http.server.BaseHTTPRequestHandler):
    server: _Server
    server_version = "visualx-callback"
    sys_version = ""
    # A socket timeout for each connection. The server is single-threaded, so a
    # local client that connects and sends nothing would block the callback
    # until wait() stops (review 20261103, A-17).
    timeout = 5

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # The request line holds the authorization code. Do not print it.
        return None

    def _reply(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _reject_method(self) -> None:
        self.server.listener._note_rejected(f"method {self.command}")
        self._reply(405, "Method not allowed.")

    do_POST = _reject_method
    do_PUT = _reject_method
    do_DELETE = _reject_method
    do_PATCH = _reject_method
    do_HEAD = _reject_method
    do_OPTIONS = _reject_method

    def do_GET(self) -> None:
        status, body = self.server.listener._handle_get(self.path)
        self._reply(status, body)


class CallbackListener:
    """A one-shot OAuth callback listener on 127.0.0.1.

    ``start()`` binds the socket and serves in a thread. ``wait()`` returns
    the authorization code after one good callback, or raises AuthError.
    """

    def __init__(self, state: str, port: int = 0, host: str = LOOPBACK_HOST, path: str = CALLBACK_PATH):
        if host != LOOPBACK_HOST:
            raise AuthError(f"The callback listener binds to {LOOPBACK_HOST} only, not {host!r}.")
        self.state = state
        self.path = path
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._stopped = threading.Event()
        self._code: str | None = None
        self._error: str | None = None
        self.rejected: list[str] = []
        try:
            self._server = _Server((LOOPBACK_HOST, port), _Handler)
        except OSError as e:
            # A-29: a port that another program uses is a message, not a traceback.
            raise AuthError(
                f"The callback listener cannot use {LOOPBACK_HOST}:{port} ({type(e).__name__}). Close the program "
                "that uses the port, or change owner.redirect_port and the callback URL of the X app."
            ) from e
        self._server.listener = self
        self._server.timeout = 0.2
        self._thread: threading.Thread | None = None

    # The bound address, for tests and for the redirect URI.
    @property
    def host(self) -> str:
        return str(self._server.server_address[0])

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def redirect_uri(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}{self.path}"

    def _note_rejected(self, why: str) -> None:
        with self._lock:
            self.rejected.append(why)

    def _handle_get(self, raw_path: str) -> tuple[int, str]:
        parts = urllib.parse.urlsplit(raw_path)
        if parts.path != self.path:
            self._note_rejected("path")
            return 404, "Not found."
        q = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        with self._lock:
            if self._done.is_set():
                self.rejected.append("second callback")
                return 409, "This sign-in is already complete."
            state = q.get("state", [""])
            if len(state) != 1 or not secrets.compare_digest(state[0].encode(), self.state.encode()):
                self.rejected.append("state")
                return 400, "The state value is not correct."
            if "error" in q:
                self._error = q["error"][0][:100]
                self._done.set()
                return 400, "X did not give permission. You can close this window."
            code = q.get("code", [""])
            if len(code) != 1 or not code[0]:
                self.rejected.append("missing code")
                return 400, "The callback has no code."
            self._code = code[0]
            self._done.set()
        return 200, "Sign-in is complete. You can close this window."

    def _serve(self) -> None:
        try:
            while not self._done.is_set() and not self._stopped.is_set():
                self._server.handle_request()
        finally:
            self._server.server_close()
            self._stopped.set()

    def start(self) -> CallbackListener:
        self._thread = threading.Thread(target=self._serve, name="visualx-oauth-callback", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set() and (self._thread is None or not self._thread.is_alive())

    def wait(self, timeout: float) -> str:
        """Wait for one good callback. Return the code. The listener stops in all cases."""
        got = self._done.wait(timeout)
        self.stop()
        if not got:
            raise AuthError(f"No sign-in callback came in {timeout:.0f} seconds. {LOGIN_HINT}")
        if self._error is not None:
            raise AuthError(f"X returned an error to the callback ({self._error}). {LOGIN_HINT}")
        assert self._code is not None
        return self._code


# -- Keyring ------------------------------------------------------------


def _check_keyring() -> None:
    kr = keyring.get_keyring()
    bad = isinstance(kr, _kr_fail.Keyring) or (_kr_null is not None and isinstance(kr, _kr_null.Keyring))
    if isinstance(kr, _kr_chainer.ChainerBackend) and not kr.backends:
        bad = True
    # A-20 (review 20261103): a backend that writes the secret to a file (the
    # keyrings.alt plaintext and file backends) is not the Credential Manager.
    kinds = [kr, *(getattr(kr, "backends", None) or [])]
    if any(type(b).__module__.startswith("keyrings.alt") or "Plaintext" in type(b).__name__ for b in kinds):
        bad = True
    if bad:
        raise AuthError(
            "No usable keyring backend (Windows Credential Manager) is available. "
            "VisualX_AI keeps tokens only in the keyring and does not write them to a file."
        )


def _load_record() -> dict[str, Any] | None:
    _check_keyring()
    try:
        raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except keyring.errors.KeyringError as e:
        raise AuthError(f"The keyring read did not succeed ({type(e).__name__}).") from e
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except ValueError as e:
        raise AuthError(f"The stored token value is not valid. {LOGIN_HINT}") from e
    return rec if isinstance(rec, dict) else None


def _save_record(rec: dict[str, Any]) -> None:
    _check_keyring()
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, json.dumps(rec, sort_keys=True))
    except keyring.errors.KeyringError as e:
        raise AuthError(f"The keyring write did not succeed ({type(e).__name__}). No token was saved.") from e


def _delete_record() -> bool:
    _check_keyring()
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        return True
    except keyring.errors.PasswordDeleteError:
        return False
    except keyring.errors.KeyringError as e:
        raise AuthError(f"The keyring delete did not succeed ({type(e).__name__}).") from e


def _record_from_response(body: dict[str, Any], old: dict[str, Any] | None, now: float) -> dict[str, Any]:
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthError(f"The token response has no access token. {LOGIN_HINT}")
    refresh = body.get("refresh_token") or (old or {}).get("refresh_token")
    expires_in = body.get("expires_in")
    scope = body.get("scope")
    scopes = scope.split() if isinstance(scope, str) else list((old or {}).get("scopes", []))
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": now + float(expires_in) if isinstance(expires_in, (int, float)) else None,
        "scopes": scopes,
        "token_type": body.get("token_type", "bearer"),
        "saved_at": now,
    }


def _summary(rec: dict[str, Any]) -> dict[str, Any]:
    """Non-secret facts about a token record."""
    return {
        "scopes": list(rec.get("scopes", [])),
        "expires_at": rec.get("expires_at"),
        "has_refresh_token": bool(rec.get("refresh_token")),
    }


# -- Token endpoint -----------------------------------------------------


def _client_id(cfg: Config) -> str:
    cid = str(cfg.get("owner.client_id", "") or "")
    if not cid:
        raise AuthError("owner.client_id is empty. Set it in config/visualx.local.toml to the X app client ID.")
    return cid


def _post_form(http: httpx.Client | None, url: str, data: dict[str, str], what: str) -> dict[str, Any]:
    own = http is None
    c = http or httpx.Client(timeout=HTTP_TIMEOUT)
    try:
        try:
            r = c.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as e:
            raise AuthError(f"The {what} request did not succeed ({type(e).__name__}).") from e
    finally:
        if own:
            c.close()
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200:
        err = body.get("error") if isinstance(body, dict) else None
        raise AuthError(f"The {what} request returned HTTP {r.status_code} ({err or 'no error code'}).")
    return body if isinstance(body, dict) else {}


# -- Public API ---------------------------------------------------------


def login(
    cfg: Config,
    open_browser: Callable[[str], Any] = webbrowser.open,
    *,
    http: httpx.Client | None = None,
    authorize_url: str = AUTHORIZE_URL,
    token_url: str = TOKEN_URL,
    timeout: float = 300.0,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Sign in with PKCE and save the tokens in the keyring.

    Returns non-secret facts (scopes, expiry, refresh token present). The
    tokens themselves go only into the keyring.
    """
    client_id = _client_id(cfg)
    _check_keyring()  # stop before the browser opens if tokens cannot be kept
    scopes = list(cfg.get("owner.scopes", ["tweet.read", "users.read", "offline.access"]))
    port = int(cfg.get("owner.redirect_port", 8765))
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(32)
    listener = CallbackListener(state=state, port=port).start()
    try:
        url = build_authorize_url(client_id, listener.redirect_uri, scopes, state, challenge, authorize_url)
        open_browser(url)
        code = listener.wait(timeout)
    finally:
        listener.stop()
    body = _post_form(
        http,
        token_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": listener.redirect_uri,
            "code_verifier": verifier,
            "client_id": client_id,
        },
        "token",
    )
    rec = _record_from_response(body, None, now())
    _save_record(rec)
    return _summary(rec)


def refresh_access_token(
    cfg: Config,
    *,
    http: httpx.Client | None = None,
    token_url: str = TOKEN_URL,
    now: Callable[[], float] = time.time,
) -> str:
    """Use the refresh token now. Return the new access token, or raise AuthError."""
    rec = _load_record()
    if rec is None:
        raise AuthError(f"No stored X token. {LOGIN_HINT}")
    refresh = rec.get("refresh_token")
    if not refresh:
        raise AuthError(f"The stored X token has no refresh token. {LOGIN_HINT}")
    try:
        body = _post_form(
            http,
            token_url,
            {"grant_type": "refresh_token", "refresh_token": str(refresh), "client_id": _client_id(cfg)},
            "token refresh",
        )
    except AuthError as e:
        raise AuthError(f"The token refresh did not succeed: {e} {LOGIN_HINT}") from e
    new = _record_from_response(body, rec, now())
    _save_record(new)
    return str(new["access_token"])


def get_access_token(
    cfg: Config,
    *,
    http: httpx.Client | None = None,
    token_url: str = TOKEN_URL,
    now: Callable[[], float] = time.time,
) -> str:
    """Return a valid access token. Refresh it first when it has expired."""
    rec = _load_record()
    if rec is None:
        raise AuthError(f"No stored X token. {LOGIN_HINT}")
    exp = rec.get("expires_at")
    if isinstance(exp, (int, float)) and exp - EXPIRY_MARGIN > now():
        return str(rec["access_token"])
    if exp is None and rec.get("access_token"):
        return str(rec["access_token"])
    return refresh_access_token(cfg, http=http, token_url=token_url, now=now)


def token_summary() -> dict[str, Any] | None:
    """Non-secret facts about the stored token, or None if there is no token."""
    rec = _load_record()
    return None if rec is None else _summary(rec)


def status(cfg: Config, client: Any) -> dict[str, str]:
    """Return the owner ID and handle from ``GET /2/users/me`` (one live call).

    ``client`` is an ``XApiClient`` (or an object with the same ``get``).
    """
    body, _headers = client.get("/2/users/me", {})
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not data.get("id"):
        raise AuthError("GET /2/users/me returned no user ID.")
    return {"user_id": str(data["id"]), "handle": str(data.get("username", ""))}


def owner_mismatch(cfg: Config, store: Any, user_id: str, handle: str) -> str | None:
    """Why the signed-in account is not the owner of config and of the store, or None (FR-54).

    The same rule as the collect and sweep steps: the handle must be
    ``owner.handle`` from config, and the store owner (if set) must be this user
    ID or the provisional ID ``h:<handle>`` of this handle. Commands that write
    the owner use this check first (review 20261103, A-06).
    """
    expected = str(cfg.get("owner.handle", "") or "").lstrip("@").casefold()
    got = handle.lstrip("@").casefold()
    if expected and got != expected:
        return f"the signed-in account is not @{cfg.get('owner.handle')} from config owner.handle (FR-54)"
    known = store.ego_id
    if known is not None and known != user_id and not (known.startswith("h:") and known[2:].casefold() == got):
        return "the signed-in account is not the owner account of this store (FR-54); use a new data folder"
    return None


def record_owner(cfg: Config, store: Any, user_id: str, handle: str) -> None:
    """Write the verified owner into the store. Raise AuthError if it is not the owner (FR-54)."""
    err = owner_mismatch(cfg, store, user_id, handle)
    if err is not None:
        raise AuthError(err)
    known = store.ego_id
    if known is not None and known != user_id:
        store.rekey_user(known, user_id)  # the provisional h:<handle> owner of an earlier Grok run
    store.set_meta("ego_user_id", user_id)
    if handle:
        store.set_meta("ego_handle", handle)


def revoke(
    cfg: Config,
    *,
    http: httpx.Client | None = None,
    revoke_url: str = REVOKE_URL,
) -> dict[str, Any]:
    """Revoke the tokens at X, then delete the keyring entry even if the call fails."""
    remote_ok = False
    remote_error: str | None = None
    try:
        rec = _load_record()
    except AuthError as e:
        rec = None
        remote_error = str(e)
    try:
        if rec is not None:
            cid = _client_id(cfg)
            for kind in ("refresh_token", "access_token"):
                tok = rec.get(kind)
                if tok:
                    _post_form(http, revoke_url, {"token": str(tok), "token_type_hint": kind, "client_id": cid}, "revoke")
            remote_ok = True
    except AuthError as e:
        remote_error = str(e)
    finally:
        deleted = _delete_record()
    return {"revoked_at_x": remote_ok, "deleted_from_keyring": deleted, "error": remote_error}
