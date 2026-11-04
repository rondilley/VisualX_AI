"""OAuth 2.0 PKCE, the loopback listener, and keyring storage (AD-05, IR-03, SR-01, TSC-27).

The listener tests use a real socket on 127.0.0.1. The keyring is an
in-memory backend; no test touches the Windows Credential Manager. The token
endpoint is the local test server, never api.x.com.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import keyring
import keyring.errors
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail

from tests.unit.xapi_test_server import XApiTestServer, default_router
from visualx import auth
from visualx.auth import AuthError, CallbackListener
from visualx.collect.xapi import XApiClient
from visualx.config import Config


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.data:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.data[(service, username)]


@pytest.fixture
def kr() -> Iterator[MemoryKeyring]:
    prev = keyring.get_keyring()
    mem = MemoryKeyring()
    keyring.set_keyring(mem)
    try:
        yield mem
    finally:
        keyring.set_keyring(prev)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    raw = {"owner": {"client_id": "test-client", "redirect_port": 0, "scopes": ["tweet.read", "users.read", "offline.access"]}}
    return Config(root=tmp_path, raw=raw)


def stored(mem: MemoryKeyring) -> dict[str, Any] | None:
    v = mem.data.get(("visualx", "x_oauth"))
    return None if v is None else json.loads(v)


def _get(url: str) -> httpx.Response:
    return httpx.get(url, timeout=5.0)


# -- Listener (TSC-27) ---------------------------------------------------------


def test_listener_binds_loopback_only():
    lis = CallbackListener(state="s", port=0)
    try:
        assert lis.host == "127.0.0.1"
        assert lis.redirect_uri.startswith("http://127.0.0.1:")
    finally:
        lis.stop()
        lis._server.server_close()
    with pytest.raises(AuthError):
        CallbackListener(state="s", port=0, host="0.0.0.0")


def test_listener_rejects_each_bad_request_then_accepts_one_and_stops():
    lis = CallbackListener(state="S1", port=0).start()
    base = f"http://127.0.0.1:{lis.port}"
    try:
        assert _get(f"{base}/other?code=c&state=S1").status_code == 404
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            r = httpx.request(method, f"{base}/callback?code=c&state=S1", timeout=5.0)
            assert r.status_code == 405, method
        assert _get(f"{base}/callback?state=S1").status_code == 400  # missing code
        assert _get(f"{base}/callback?code=&state=S1").status_code == 400  # empty code
        assert _get(f"{base}/callback?code=c&state=WRONG").status_code == 400  # wrong state
        assert _get(f"{base}/callback?code=c").status_code == 400  # no state
        assert not lis.stopped
        r = _get(f"{base}/callback?code=GOOD&state=S1")
        assert r.status_code == 200
        assert lis.wait(5) == "GOOD"
        assert lis.stopped
        # A second callback finds no listener (or a refusal) and does not change the code.
        try:
            second = _get(f"{base}/callback?code=EVIL&state=S1")
            assert second.status_code != 200
        except httpx.HTTPError:
            pass
        assert lis._code == "GOOD"
        assert {"path", "method POST", "missing code", "state"} <= set(lis.rejected)
    finally:
        lis.stop()


def test_listener_refuses_second_callback_inside_the_handler():
    lis = CallbackListener(state="S2", port=0)
    try:
        assert lis._handle_get("/callback?code=A&state=S2")[0] == 200
        status, _body = lis._handle_get("/callback?code=B&state=S2")
        assert status == 409
        assert lis._code == "A" and "second callback" in lis.rejected
    finally:
        lis._server.server_close()


def test_listener_times_out_and_stops():
    lis = CallbackListener(state="S3", port=0).start()
    with pytest.raises(AuthError, match="No sign-in callback"):
        lis.wait(0.3)
    assert lis.stopped


def test_listener_error_from_x_with_good_state_stops_with_error():
    lis = CallbackListener(state="S4", port=0).start()
    assert _get(f"http://127.0.0.1:{lis.port}/callback?error=access_denied&state=S4").status_code == 400
    with pytest.raises(AuthError, match="access_denied"):
        lis.wait(5)


def test_pkce_s256():
    v, c = auth.make_pkce()
    assert 43 <= len(v) <= 128
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


# -- Login, refresh, revoke, status --------------------------------------------


class TokenRouter:
    def __init__(self) -> None:
        self.token_status = 200
        self.revoke_status = 200
        self.n = 0

    def __call__(self, method: str, path: str, query: dict[str, str], form: dict[str, str]):
        if method == "POST" and path == "/2/oauth2/token":
            self.n += 1
            if self.token_status != 200:
                return self.token_status, {}, {"error": "invalid_grant", "error_description": "bad"}
            if form.get("grant_type") == "authorization_code":
                assert form["code"] == "GOOD" and form["client_id"] == "test-client"
                assert form["redirect_uri"].startswith("http://127.0.0.1:")
                assert form["code_verifier"]
                return 200, {}, {"token_type": "bearer", "access_token": "acc-1", "refresh_token": "ref-1",
                                 "expires_in": 7200, "scope": "tweet.read users.read offline.access"}
            if form.get("grant_type") == "refresh_token":
                assert form["refresh_token"] == "ref-1"
                return 200, {}, {"token_type": "bearer", "access_token": "acc-2", "refresh_token": "ref-2",
                                 "expires_in": 7200, "scope": "tweet.read users.read offline.access"}
        if method == "POST" and path == "/2/oauth2/revoke":
            return self.revoke_status, {}, {"revoked": self.revoke_status == 200}
        return default_router(method, path, query, form)


def test_login_end_to_end_saves_one_json_value_in_keyring(kr: MemoryKeyring, cfg: Config, tmp_path: Path):
    router = TokenRouter()
    seen: dict[str, str] = {}
    with XApiTestServer(router) as srv:

        def browser(url: str) -> bool:
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
            seen.update(q)
            assert url.startswith("http://authorize.test/")
            assert q["code_challenge_method"] == "S256" and q["client_id"] == "test-client"
            assert q["scope"] == "tweet.read users.read offline.access"
            r = _get(f"{q['redirect_uri']}?code=GOOD&state={urllib.parse.quote(q['state'])}")
            assert r.status_code == 200
            return True

        summary = auth.login(cfg, browser, authorize_url="http://authorize.test/", token_url=f"{srv.base_url}/2/oauth2/token",
                             timeout=10, now=lambda: 1000.0)
        token_req = next(r for r in srv.requests if r["path"] == "/2/oauth2/token")
    verifier = token_req["form"]["code_verifier"]
    assert seen["code_challenge"] == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert "acc-1" not in json.dumps(summary) and summary["has_refresh_token"] is True
    rec = stored(kr)
    assert rec is not None and rec["access_token"] == "acc-1" and rec["refresh_token"] == "ref-1"
    assert rec["expires_at"] == 8200.0 and rec["scopes"] == ["tweet.read", "users.read", "offline.access"]
    assert list(kr.data) == [("visualx", "x_oauth")]
    # No token in a file anywhere under the project root of this test.
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert b"acc-1" not in p.read_bytes()


def test_get_access_token_uses_stored_token_then_refreshes_when_expired(kr: MemoryKeyring, cfg: Config):
    kr.set_password("visualx", "x_oauth", json.dumps({"access_token": "acc-1", "refresh_token": "ref-1", "expires_at": 5000.0, "scopes": []}))
    router = TokenRouter()
    with XApiTestServer(router) as srv:
        url = f"{srv.base_url}/2/oauth2/token"
        assert auth.get_access_token(cfg, token_url=url, now=lambda: 1000.0) == "acc-1"
        assert router.n == 0
        assert auth.get_access_token(cfg, token_url=url, now=lambda: 4990.0) == "acc-2"
        assert router.n == 1
    rec = stored(kr)
    assert rec is not None and rec["refresh_token"] == "ref-2" and rec["expires_at"] == 4990.0 + 7200


def test_refresh_error_tells_owner_to_sign_in(kr: MemoryKeyring, cfg: Config):
    kr.set_password("visualx", "x_oauth", json.dumps({"access_token": "acc-1", "refresh_token": "ref-1", "expires_at": 0.0}))
    router = TokenRouter()
    router.token_status = 400
    with XApiTestServer(router) as srv:
        with pytest.raises(AuthError, match="visualx auth login") as ei:
            auth.get_access_token(cfg, token_url=f"{srv.base_url}/2/oauth2/token", now=lambda: 1000.0)
    assert "ref-1" not in str(ei.value) and "acc-1" not in str(ei.value)


def test_no_token_tells_owner_to_sign_in(kr: MemoryKeyring, cfg: Config):
    with pytest.raises(AuthError, match="visualx auth login"):
        auth.get_access_token(cfg)


@pytest.mark.parametrize("revoke_status", [200, 500])
def test_revoke_deletes_keyring_entry_even_when_call_fails(kr: MemoryKeyring, cfg: Config, revoke_status: int):
    kr.set_password("visualx", "x_oauth", json.dumps({"access_token": "acc-1", "refresh_token": "ref-1", "expires_at": 0.0}))
    router = TokenRouter()
    router.revoke_status = revoke_status
    with XApiTestServer(router) as srv:
        out = auth.revoke(cfg, revoke_url=f"{srv.base_url}/2/oauth2/revoke")
        forms = [r["form"] for r in srv.requests if r["path"] == "/2/oauth2/revoke"]
    assert out["deleted_from_keyring"] is True and stored(kr) is None
    assert out["revoked_at_x"] is (revoke_status == 200)
    assert forms[0]["token"] == "ref-1" and forms[0]["client_id"] == "test-client"


def test_revoke_with_unreachable_endpoint_still_deletes(kr: MemoryKeyring, cfg: Config):
    kr.set_password("visualx", "x_oauth", json.dumps({"access_token": "acc-1", "refresh_token": "ref-1"}))
    lis = CallbackListener(state="x", port=0)
    port = lis.port
    lis._server.server_close()  # a port with nothing on it
    out = auth.revoke(cfg, revoke_url=f"http://127.0.0.1:{port}/2/oauth2/revoke")
    assert out["revoked_at_x"] is False and out["deleted_from_keyring"] is True and stored(kr) is None


def test_no_keyring_backend_stops_and_writes_no_file(cfg: Config, tmp_path: Path):
    prev = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    opened: list[str] = []
    try:
        with pytest.raises(AuthError, match="keyring"):
            auth.login(cfg, opened.append, timeout=1)
        with pytest.raises(AuthError, match="keyring"):
            auth.get_access_token(cfg)
    finally:
        keyring.set_keyring(prev)
    assert opened == []  # stopped before the browser opened
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


def test_status_calls_users_me(kr: MemoryKeyring, cfg: Config):
    with XApiTestServer() as srv:
        client = XApiClient(srv.base_url, token_provider=lambda: "acc-1")
        assert auth.status(cfg, client) == {"user_id": "1000", "handle": "owner"}
        assert [r["path"] for r in srv.requests] == ["/2/users/me"]
        client.close()


def test_empty_client_id_is_an_error(kr: MemoryKeyring, tmp_path: Path):
    with pytest.raises(AuthError, match="client_id"):
        auth.login(Config(root=tmp_path, raw={"owner": {"client_id": ""}}), lambda u: True, timeout=1)


# -- Owner check for commands that write the owner (review 20261103, A-06) --


def _owner_env(tmp_path: Path):
    from tests.helpers import make_env

    cfg, store, _cache, _log = make_env(tmp_path)
    cfg.raw.setdefault("owner", {})["handle"] = "Ron_Dilley"
    return cfg, store


def test_record_owner_refuses_a_handle_that_is_not_the_config_owner(tmp_path: Path):
    cfg, store = _owner_env(tmp_path)
    with pytest.raises(AuthError, match="owner.handle"):
        auth.record_owner(cfg, store, "555", "someone_else")
    assert store.ego_id is None


def test_record_owner_refuses_a_store_of_a_different_account(tmp_path: Path):
    cfg, store = _owner_env(tmp_path)
    store.set_meta("ego_user_id", "100")
    with pytest.raises(AuthError, match="owner account of this store"):
        auth.record_owner(cfg, store, "555", "Ron_Dilley")
    assert store.ego_id == "100"


def test_record_owner_writes_the_owner_and_rekeys_a_provisional_owner(tmp_path: Path):
    cfg, store = _owner_env(tmp_path)
    store.set_meta("ego_user_id", "h:ron_dilley")
    auth.record_owner(cfg, store, "100", "Ron_Dilley")
    assert store.ego_id == "100" and store.get_meta("ego_handle") == "Ron_Dilley"
    auth.record_owner(cfg, store, "100", "Ron_Dilley")  # the same owner again is allowed
    assert store.ego_id == "100"


# -- P3 items of review 20261103 ------------------------------------------------


def test_listener_serves_the_callback_after_a_client_that_sends_nothing():
    # A-17: a connection with no request no longer blocks the callback until wait() stops.
    import socket
    import threading

    lst = CallbackListener(state="s1").start()
    try:
        idle = socket.create_connection(("127.0.0.1", lst.port))
        t = threading.Thread(target=lambda: _get(f"{lst.redirect_uri}?code=c1&state=s1"), daemon=True)
        t.start()
        assert lst.wait(30) == "c1"
        idle.close()
    finally:
        lst.stop()


def test_listener_port_in_use_is_an_auth_error():
    # A-29: the OSError of a used port becomes a message.
    first = CallbackListener(state="s1").start()
    try:
        with pytest.raises(AuthError, match="cannot use"):
            CallbackListener(state="s2", port=first.port)
    finally:
        first.stop()


class PlaintextKeyring(MemoryKeyring):
    """A backend that has the name of a backend that writes secrets to a file."""


def test_keyring_that_writes_a_file_is_refused(cfg: Config):
    # A-20: tokens go only into the Credential Manager.
    prev = keyring.get_keyring()
    keyring.set_keyring(PlaintextKeyring())
    try:
        with pytest.raises(AuthError, match="keyring"):
            auth.get_access_token(cfg)
    finally:
        keyring.set_keyring(prev)


def test_cli_revoke_reports_what_happened(kr: MemoryKeyring, tmp_path: Path):
    # A-18: with no stored token, the command does not say "Tokens removed".
    from typer.testing import CliRunner

    from tests.helpers import make_env
    from visualx.cli import app

    make_env(tmp_path)
    r = CliRunner().invoke(app, ["auth", "revoke", "--config", str(tmp_path / "config")])
    assert r.exit_code == 0, r.output
    assert "Tokens removed" not in r.output and "No token was in the Credential Manager." in r.output
