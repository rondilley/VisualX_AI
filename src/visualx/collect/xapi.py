"""Official X API v2 adapter: HTTP client and pure mapping functions (AD-02, AD-06, IR-01).

The client sends GET requests with the owner's user token. It waits for the
``x-rate-limit-reset`` time on HTTP 429 and never changes token or app to go
around a limit (NFR-05). It tries one token refresh on HTTP 401, and three
retries with backoff on HTTP 5xx or a network error. It writes one log line
for each call (IR-06, OR-01).

The mapping functions turn X objects into ``Interaction`` and ``Profile``
records with the type rules of FR-21. They never make up a field value: a
field that is not in the response is ``absent`` in ``field_status``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from ..logs import RunLog
from ..model import (
    INTERACTION_OPTIONAL_FIELDS,
    PROFILE_OPTIONAL_FIELDS,
    Interaction,
    Profile,
    status_for,
)
from ..store import now_iso

ADAPTER = "xapi"
ADAPTER_VERSION = "1"
BASE_URL = "https://api.x.com"

# IR-01: the fields and expansions that give the edge type and the counterparty profile.
TWEET_FIELDS = "created_at,author_id,conversation_id,in_reply_to_user_id,referenced_tweets,entities,public_metrics"
EXPANSIONS = "author_id,referenced_tweets.id,referenced_tweets.id.author_id,entities.mentions.username,in_reply_to_user_id"
USER_FIELDS = "created_at,description,entities,location,protected,public_metrics,url,verified_type,profile_image_url"

POST_PARAMS: dict[str, str] = {"tweet.fields": TWEET_FIELDS, "expansions": EXPANSIONS, "user.fields": USER_FIELDS}
USER_PARAMS: dict[str, str] = {"user.fields": USER_FIELDS}

# Request parameters that must never reach a log line.
SECRET_PARAMS = frozenset({"access_token", "refresh_token", "token", "client_secret", "code", "code_verifier"})

# A numeric path segment after the first one (the first is the API version "2").
_NUMERIC_SEGMENT = re.compile(r"(?<=.)/(\d+)(?=/|$)")

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
SERVER_RETRIES = 3
MAX_RATE_LIMIT_WAIT = 3600.0
MAX_RATE_LIMIT_HITS = 20


class XApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class XApiAuthError(XApiError):
    """The token is not accepted after one refresh. The owner must sign in again."""


def endpoint_template(path: str) -> tuple[str, list[str]]:
    """Return the path with numeric IDs replaced by ``:id``, and the IDs."""
    ids = _NUMERIC_SEGMENT.findall(path)
    return _NUMERIC_SEGMENT.sub("/:id", path), ids


def count_resources(body: dict[str, Any], expansions_billed: bool = False) -> int:
    """Billable resources in a response: the ``data`` objects, plus ``includes`` if billed."""
    data = body.get("data") if isinstance(body, dict) else None
    n = len(data) if isinstance(data, list) else (1 if isinstance(data, dict) else 0)
    if expansions_billed:
        inc = body.get("includes") or {}
        if isinstance(inc, dict):
            n += sum(len(v) for v in inc.values() if isinstance(v, list))
    return n


def _log_params(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if k in SECRET_PARAMS:
            continue
        if k == "ids" and isinstance(v, str):
            out["ids"] = v.split(",")
        else:
            out[k] = v
    return out


class XApiClient:
    """GET client for X API v2 with the owner's user token."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        token_provider: Callable[[], str] | None = None,
        log: RunLog | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        on_refresh: Callable[[], str] | None = None,
        *,
        http: httpx.Client | None = None,
        now: Callable[[], float] = time.time,
        max_rate_limit_wait: float = MAX_RATE_LIMIT_WAIT,
    ):
        if token_provider is None:
            raise ValueError("XApiClient needs a token_provider")
        self.base_url = base_url.rstrip("/")
        self.token_provider = token_provider
        self.log = log
        self.sleep = sleep
        self.on_refresh = on_refresh
        self.now = now
        self.max_rate_limit_wait = max_rate_limit_wait
        self._own_http = http is None
        self._http = http or httpx.Client(timeout=HTTP_TIMEOUT)
        self.requests_sent = 0

    def close(self) -> None:
        if self._own_http:
            self._http.close()

    def __enter__(self) -> XApiClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _log(self, event: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.write(event, **fields)

    def _send(self, url: str, params: dict[str, Any], token: str) -> httpx.Response:
        self.requests_sent += 1
        return self._http.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}", "User-Agent": "visualx/0.1"},
            timeout=HTTP_TIMEOUT,
        )

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        unit_price: float = 0.0,
        expansions_billed: bool = False,
        step: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """GET ``path``. Return (JSON body, response headers).

        ``unit_price`` and ``expansions_billed`` are only for the log line
        (resource count and cost). The plan keeps the cost ledger.
        """
        params = dict(params or {})
        url = f"{self.base_url}{path}"
        template, path_ids = endpoint_template(path)
        token = self.token_provider()
        refreshed = False
        server_tries = 0
        rate_hits = 0
        while True:
            try:
                r = self._send(url, params, token)
            except httpx.HTTPError as e:
                server_tries += 1
                self._log("x_api_retry", endpoint=template, id=path_ids, status=None, error=type(e).__name__, attempt=server_tries, step=step)
                if server_tries > SERVER_RETRIES:
                    raise XApiError(f"{template}: network error after {SERVER_RETRIES} retries ({type(e).__name__})") from e
                self.sleep(2.0 ** server_tries)
                continue
            code = r.status_code
            if code == 429:
                rate_hits += 1
                reset_raw = r.headers.get("x-rate-limit-reset")
                try:
                    wait = max(1.0, float(reset_raw) - self.now() + 1.0) if reset_raw else 60.0
                except ValueError:
                    wait = 60.0
                self._log("x_api_rate_limited", endpoint=template, id=path_ids, status=429, reset=reset_raw, wait_s=round(wait, 1), step=step)
                if wait > self.max_rate_limit_wait or rate_hits > MAX_RATE_LIMIT_HITS:
                    raise XApiError(f"{template}: rate limit reset is {wait:.0f} s away; stopped (the cursor stays)", 429)
                self.sleep(wait)
                continue
            if code == 401:
                self._log("x_api_unauthorized", endpoint=template, id=path_ids, status=401, refreshed=refreshed, step=step)
                if refreshed or self.on_refresh is None:
                    raise XApiAuthError(f"{template}: HTTP 401. Run 'visualx auth login' to sign in again.", 401)
                token = self.on_refresh()
                refreshed = True
                continue
            if code >= 500:
                server_tries += 1
                self._log("x_api_retry", endpoint=template, id=path_ids, status=code, attempt=server_tries, step=step)
                if server_tries > SERVER_RETRIES:
                    raise XApiError(f"{template}: HTTP {code} after {SERVER_RETRIES} retries (the cursor stays)", code)
                self.sleep(2.0 ** server_tries)
                continue
            try:
                body = r.json()
            except ValueError as e:
                self._log("x_api_call", endpoint=template, id=path_ids, params=_log_params(params), status=code, resources=0, cost=0.0, step=step)
                raise XApiError(f"{template}: HTTP {code} with a body that is not JSON", code) from e
            if code != 200:
                self._log("x_api_call", endpoint=template, id=path_ids, params=_log_params(params), status=code, resources=0, cost=0.0, step=step)
                raise XApiError(f"{template}: HTTP {code}", code)
            if not isinstance(body, dict):
                raise XApiError(f"{template}: the response is not a JSON object", code)
            n = count_resources(body, expansions_billed)
            self._log(
                "x_api_call", endpoint=template, id=path_ids, params=_log_params(params),
                status=code, resources=n, cost=round(n * unit_price, 6), step=step,
            )
            return body, dict(r.headers)


# -- Mapping ----------------------------------------------------------------


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def map_user(user: dict[str, Any], endpoint: str, fetched_at: str | None = None) -> Profile:
    """Map an X v2 user object to a Profile. Fields not in the object are ``absent``."""
    pm = user.get("public_metrics") if isinstance(user.get("public_metrics"), dict) else {}
    values: dict[str, Any] = {
        "handle": user.get("username"),
        "name": user.get("name"),
        "description": user.get("description"),
        "url": user.get("url"),
        # The Slice 0a probe (background/2026-11-03_api_probe.md) showed that X sends an empty
        # url and description as "", but leaves out an empty location. When the object holds
        # the requested user.fields (created_at is there), a missing location is empty.
        "location": user.get("location", "" if "created_at" in user else None),
        "created_at": user.get("created_at"),
        "followers_count": _int_or_none(pm.get("followers_count")),
        "following_count": _int_or_none(pm.get("following_count")),
        "tweet_count": _int_or_none(pm.get("tweet_count")),
        "listed_count": _int_or_none(pm.get("listed_count")),
        "verified_type": user.get("verified_type"),
        "protected": user.get("protected") if isinstance(user.get("protected"), bool) else None,
        "profile_image_url": user.get("profile_image_url"),
    }
    return Profile(
        user_id=str(user["id"]),
        fetched_at=fetched_at or now_iso(),
        source_endpoint=endpoint,
        adapter=ADAPTER,
        adapter_version=ADAPTER_VERSION,
        field_status=status_for(values, PROFILE_OPTIONAL_FIELDS),
        **values,
    )


def _refs(post: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in post.get("referenced_tweets") or []:
        if isinstance(r, dict) and r.get("type") and r.get("id"):
            out.setdefault(str(r["type"]), str(r["id"]))
    return out


def _includes_index(includes: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
    """(post ID -> author ID, lower-case username -> user ID) from an ``includes`` block."""
    authors: dict[str, str] = {}
    users: dict[str, str] = {}
    if isinstance(includes, dict):
        for t in includes.get("tweets") or []:
            if isinstance(t, dict) and t.get("id") and t.get("author_id"):
                authors[str(t["id"])] = str(t["author_id"])
        for u in includes.get("users") or []:
            if isinstance(u, dict) and u.get("id") and u.get("username"):
                users[str(u["username"]).lower()] = str(u["id"])
    return authors, users


def _urls(post: dict[str, Any]) -> list[str] | None:
    ent = post.get("entities")
    if not isinstance(ent, dict):
        return None
    out = []
    for u in ent.get("urls") or []:
        if isinstance(u, dict):
            v = u.get("expanded_url") or u.get("url")
            if isinstance(v, str) and v:
                out.append(v)
    return out


def map_post(
    post: dict[str, Any],
    ego_id: str,
    endpoint: str,
    run_id: str,
    *,
    includes: dict[str, Any] | None = None,
    owner_post_ids: Iterable[str] = (),
    observed_at: str | None = None,
    raw_request_hash: str | None = None,
) -> list[Interaction]:
    """Map one X v2 post to interactions with the FR-21 type rules.

    - A repost (``referenced_tweets`` type ``retweeted``) gives only a repost
      to the reposted author. The "RT @user" text gives no mention.
    - A reply gives a reply to ``in_reply_to_user_id`` and no mention of that user.
    - A quote gives a quote to the quoted author and no mention of that user.
    - Each other user in ``entities.mentions`` gives a mention.
    - No self-edges.

    The author of a referenced post comes from ``includes.tweets``. For a
    referenced post in ``owner_post_ids`` the author is ``ego_id``. If the
    author is not known, the post gives no edge for that reference.
    """
    source = post.get("author_id")
    if not source or not post.get("id"):
        return []
    source = str(source)
    post_id = str(post["id"])
    authors, users_by_name = _includes_index(includes)
    for pid in owner_post_ids:
        authors.setdefault(str(pid), ego_id)
    refs = _refs(post)
    observed = observed_at or now_iso()
    text = post.get("text") if isinstance(post.get("text"), str) else None
    urls = _urls(post)

    def make(kind: str, target: str, target_post: str | None) -> Interaction:
        values = {
            "source_post_id": post_id,
            "target_post_id": target_post,
            "interaction_created_at": post.get("created_at"),
            "conversation_id": post.get("conversation_id"),
            "text": text,
            "urls": urls,
        }
        return Interaction(
            type=kind,
            source_user_id=source,
            target_user_id=target,
            observed_at=observed,
            source_endpoint=endpoint,
            adapter=ADAPTER,
            adapter_version=ADAPTER_VERSION,
            run_id=run_id,
            field_status=status_for(values, INTERACTION_OPTIONAL_FIELDS),
            raw_request_hash=raw_request_hash,
            **values,
        )

    out: list[Interaction] = []
    if "retweeted" in refs:
        target = authors.get(refs["retweeted"])
        if target and target != source:
            out.append(make("repost", target, refs["retweeted"]))
        return out

    covered: set[str] = {source}
    reply_target = post.get("in_reply_to_user_id")
    if not reply_target and "replied_to" in refs:
        reply_target = authors.get(refs["replied_to"])
    if reply_target:
        reply_target = str(reply_target)
        if reply_target not in covered:
            out.append(make("reply", reply_target, refs.get("replied_to")))
        covered.add(reply_target)
    if "quoted" in refs:
        q_target = authors.get(refs["quoted"])
        if q_target:
            if q_target not in covered:
                out.append(make("quote", q_target, refs["quoted"]))
            covered.add(q_target)
    ent = post.get("entities")
    for m in (ent.get("mentions") or []) if isinstance(ent, dict) else []:
        if not isinstance(m, dict):
            continue
        uid = m.get("id")
        if not uid and isinstance(m.get("username"), str):
            uid = users_by_name.get(m["username"].lower())
        if not uid:
            continue
        uid = str(uid)
        if uid in covered:
            continue
        covered.add(uid)
        out.append(make("mention", uid, None))
    return out


def map_retweeted_by(
    users: Iterable[dict[str, Any]],
    owner_post_id: str,
    ego_id: str,
    endpoint: str,
    run_id: str,
    *,
    observed_at: str | None = None,
    raw_request_hash: str | None = None,
) -> list[Interaction]:
    """Reposts of an owner post seen through ``retweeted_by`` (TSC-33).

    The endpoint gives user objects only: no repost post ID and no repost
    time. Those fields are ``absent``; the record has no made-up value.
    """
    observed = observed_at or now_iso()
    out: list[Interaction] = []
    seen: set[str] = set()
    for u in users:
        if not isinstance(u, dict) or not u.get("id"):
            continue
        uid = str(u["id"])
        if uid == ego_id or uid in seen:
            continue
        seen.add(uid)
        values = {
            "source_post_id": None,
            "target_post_id": str(owner_post_id),
            "interaction_created_at": None,
            "conversation_id": None,
            "text": None,
            "urls": None,
        }
        out.append(
            Interaction(
                type="repost",
                source_user_id=uid,
                target_user_id=ego_id,
                observed_at=observed,
                source_endpoint=endpoint,
                adapter=ADAPTER,
                adapter_version=ADAPTER_VERSION,
                run_id=run_id,
                field_status=status_for(values, INTERACTION_OPTIONAL_FIELDS),
                raw_request_hash=raw_request_hash,
                **values,
            )
        )
    return out
