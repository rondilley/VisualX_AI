"""Mapping rules of the X API v2 adapter (AD-06 type table, FR-21, DR-02, TSC-33) and the HTTP client."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests.unit.xapi_test_server import XApiTestServer, fixture
from visualx.collect.xapi import (
    POST_PARAMS,
    XApiAuthError,
    XApiClient,
    XApiError,
    endpoint_template,
    map_post,
    map_retweeted_by,
    map_user,
)
from visualx.logs import RunLog
from visualx.store import Store

EGO = "1000"
EP = "/2/users/:id/tweets"


def by_id(name: str) -> tuple[dict[str, dict], dict]:
    body = fixture(name)
    return {p["id"]: p for p in body["data"]}, body.get("includes", {})


def edges(items) -> set[tuple[str, str, str]]:
    return {(i.type, i.source_user_id, i.target_user_id) for i in items}


# -- FR-21 type rules --------------------------------------------------------


def test_original_post_gives_mention_with_text_urls_and_time():
    posts, inc = by_id("owner_tweets_p1.json")
    out = map_post(posts["3001"], EGO, EP, "r1", includes=inc)
    assert edges(out) == {("mention", "1000", "2001")}
    i = out[0]
    assert i.source_post_id == "3001" and i.target_post_id is None
    assert i.interaction_created_at == "2026-10-20T09:00:00.000Z"
    assert i.urls == ["https://example.org/post"]
    assert i.field_status["target_post_id"] == "absent"
    assert i.field_status["text"] == "present" and i.text_hash


def test_reply_gives_reply_and_no_mention_of_same_user():
    posts, inc = by_id("owner_tweets_p1.json")
    out = map_post(posts["3002"], EGO, EP, "r1", includes=inc)
    assert edges(out) == {("reply", "1000", "2002"), ("mention", "1000", "2003")}
    reply = next(i for i in out if i.type == "reply")
    assert reply.target_post_id == "9001"
    # entities is in the post and has no URL entity: the post has no URLs.
    assert reply.urls == [] and reply.field_status["urls"] == "present"


def test_repost_gives_only_repost_no_mention_from_rt_text():
    posts, inc = by_id("owner_tweets_p2.json")
    out = map_post(posts["3003"], EGO, EP, "r1", includes=inc)
    assert edges(out) == {("repost", "1000", "2003")}
    assert out[0].target_post_id == "9002"
    assert out[0].interaction_created_at == "2026-10-10T08:00:00.000Z"


def test_quote_gives_quote_and_no_mention_of_same_user():
    posts, inc = by_id("owner_tweets_p2.json")
    out = map_post(posts["3004"], EGO, EP, "r1", includes=inc)
    assert edges(out) == {("quote", "1000", "2004"), ("mention", "1000", "2005")}


def test_reply_to_owner_from_mentions_has_no_extra_mention():
    posts, inc = by_id("mentions_p1.json")
    out = map_post(posts["4001"], EGO, "/2/users/:id/mentions", "r1", includes=inc)
    assert edges(out) == {("reply", "2005", "1000")}


def test_each_mentioned_user_gives_a_mention():
    posts, inc = by_id("mentions_p1.json")
    out = map_post(posts["4002"], EGO, "/2/users/:id/mentions", "r1", includes=inc)
    assert edges(out) == {("mention", "2001", "1000"), ("mention", "2001", "2006"), ("mention", "2001", "2008")}
    assert len({i.interaction_id for i in out}) == 3


def test_quote_of_owner_post_uses_owner_post_ids_when_includes_has_no_tweet():
    posts, inc = by_id("quote_tweets_3001.json")
    out = map_post(posts["5001"], EGO, "/2/tweets/:id/quote_tweets", "r1", includes=inc, owner_post_ids=("3001",))
    assert edges(out) == {("quote", "2002", "1000")}


def test_unknown_quote_author_gives_no_invented_quote_edge():
    posts, inc = by_id("quote_tweets_3001.json")
    out = map_post(posts["5001"], EGO, "/2/tweets/:id/quote_tweets", "r1", includes=inc)
    # The quoted author is not in the response, so no quote edge. The mention stays.
    assert edges(out) == {("mention", "2002", "1000")}


def test_no_self_edges():
    post = {
        "id": "7001", "author_id": "1000", "in_reply_to_user_id": "1000",
        "referenced_tweets": [{"type": "replied_to", "id": "3001"}],
        "text": "@owner thread part 2",
        "entities": {"mentions": [{"username": "owner", "id": "1000"}]},
    }
    assert map_post(post, EGO, EP, "r1") == []
    rt_self = {"id": "7002", "author_id": "1000", "referenced_tweets": [{"type": "retweeted", "id": "3001"}]}
    assert map_post(rt_self, EGO, EP, "r1", owner_post_ids=("3001",)) == []


def test_mention_without_id_resolves_by_username_from_includes_only():
    post = {"id": "7003", "author_id": "2001", "text": "@Carol @nobody",
            "entities": {"mentions": [{"username": "Carol"}, {"username": "nobody"}]}}
    inc = {"users": [{"id": "2003", "username": "carol"}]}
    assert edges(map_post(post, EGO, EP, "r1", includes=inc)) == {("mention", "2001", "2003")}


def test_reply_target_from_includes_when_in_reply_to_user_id_is_missing():
    post = {"id": "7004", "author_id": "2001", "referenced_tweets": [{"type": "replied_to", "id": "9001"}]}
    inc = {"tweets": [{"id": "9001", "author_id": "2002"}]}
    out = map_post(post, EGO, EP, "r1", includes=inc)
    assert edges(out) == {("reply", "2001", "2002")}
    i = out[0]
    for f in ("interaction_created_at", "conversation_id", "text", "urls"):
        assert i.field_status[f] == "absent" and getattr(i, f) is None


def test_post_without_author_gives_nothing():
    assert map_post({"id": "7005", "text": "x"}, EGO, EP, "r1") == []


# -- TSC-33 ------------------------------------------------------------------


def test_retweeted_by_marks_post_id_and_time_absent_and_store_keeps_it(tmp_path: Path):
    users = fixture("retweeted_by_3001.json")["data"] + [{"id": EGO, "username": "owner"}]
    out = map_retweeted_by(users, "3001", EGO, "/2/tweets/:id/retweeted_by", "r1", observed_at="2026-11-02T12:00:00.000000Z")
    assert edges(out) == {("repost", "2007", "1000"), ("repost", "2001", "1000")}
    for i in out:
        assert i.source_post_id is None and i.interaction_created_at is None
        assert i.field_status["source_post_id"] == "absent"
        assert i.field_status["interaction_created_at"] == "absent"
        assert i.target_post_id == "3001" and i.field_status["target_post_id"] == "present"
        assert i.observed_at == "2026-11-02T12:00:00.000000Z"
    store = Store(tmp_path / "s.sqlite")
    assert store.put_interactions(out) == 2
    rows = store.interactions()
    assert {(r["source_user_id"], r["target_user_id"]) for r in rows} == {("2007", "1000"), ("2001", "1000")}
    for r in rows:
        assert r["source_post_id"] is None and r["interaction_created_at"] is None
        assert r["observed_at"] == "2026-11-02T12:00:00.000000Z"
        assert r["field_status"]["source_post_id"] == "absent"
        assert r["field_status"]["interaction_created_at"] == "absent"
    store.close()


# -- Profiles ----------------------------------------------------------------


def test_map_user_marks_missing_fields_absent():
    _p, inc = by_id("owner_tweets_p2.json")
    dave = next(u for u in inc["users"] if u["id"] == "2004")
    p = map_user(dave, "/2/users")
    # Slice 0a: X leaves out an empty location, so a full user object gives "" (present).
    assert p.location == ("" if "created_at" in dave else None)
    assert p.handle == "dave" and p.followers_count == 10
    assert p.verified_type is None and p.field_status["verified_type"] == "absent"
    for f in ("description", "url", "profile_image_url"):
        assert p.field_status[f] == "absent"
    me = map_user(fixture("users_me.json")["data"], "/2/users/me")
    assert me.field_status["verified_type"] == "present" and me.verified_type == "none"
    assert me.protected is False and me.field_status["protected"] == "present"
    bare = map_user({"id": "42"}, "/2/users")
    assert all(bare.field_status[f] == "absent" for f in bare.field_status)


def test_endpoint_template_hides_ids():
    assert endpoint_template("/2/users/1000/tweets") == ("/2/users/:id/tweets", ["1000"])
    assert endpoint_template("/2/tweets/3001/quote_tweets") == ("/2/tweets/:id/quote_tweets", ["3001"])
    assert endpoint_template("/2/users/me") == ("/2/users/me", [])


# -- HTTP client (real local server) ------------------------------------------


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, s: float) -> None:
        self.calls.append(s)


def make_client(srv: XApiTestServer, tmp_path: Path, **kw) -> tuple[XApiClient, Sleeps, Path]:
    sleeps = Sleeps()
    log_path = tmp_path / "log.jsonl"
    c = XApiClient(srv.base_url, token_provider=lambda: "tok-a", log=RunLog(log_path, b"k" * 32), sleep=sleeps, now=lambda: 1_000_000.0, **kw)
    return c, sleeps, log_path


def test_client_waits_for_rate_limit_reset_then_retries(tmp_path: Path):
    with XApiTestServer() as srv:
        srv.once("GET", "/2/users/1000/mentions", 429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": str(1_000_000 + 30)})
        c, sleeps, log_path = make_client(srv, tmp_path)
        body, headers = c.get("/2/users/1000/mentions", POST_PARAMS)
        assert body["data"][0]["id"] == "4001"
        assert sleeps.calls == [31.0]
        assert srv.count("/2/users/1000/mentions") == 2
        # Same token both times: no change of token to go around the limit (NFR-05).
        assert {r["authorization"] for r in srv.requests} == {"Bearer tok-a"}
        events = [json.loads(x)["event"] for x in log_path.read_text(encoding="utf-8").splitlines()]
        assert events == ["x_api_rate_limited", "x_api_call"]


def test_client_refreshes_once_on_401_then_stops(tmp_path: Path):
    with XApiTestServer() as srv:
        refreshed: list[int] = []

        def on_refresh() -> str:
            refreshed.append(1)
            return "tok-b"

        srv.once("GET", "/2/users/me", 401, {"title": "Unauthorized"})
        c, _s, _l = make_client(srv, tmp_path, on_refresh=on_refresh)
        body, _h = c.get("/2/users/me", {})
        assert body["data"]["id"] == "1000" and refreshed == [1]
        assert [r["authorization"] for r in srv.requests] == ["Bearer tok-a", "Bearer tok-b"]

        srv.once("GET", "/2/users/me", 401, {})
        srv.once("GET", "/2/users/me", 401, {})
        with pytest.raises(XApiAuthError, match="auth login"):
            c.get("/2/users/me", {})
        assert len(refreshed) == 2


def test_client_401_without_refresh_hook_stops(tmp_path: Path):
    with XApiTestServer() as srv:
        srv.once("GET", "/2/users/me", 401, {})
        c, _s, _l = make_client(srv, tmp_path)
        with pytest.raises(XApiAuthError):
            c.get("/2/users/me", {})


def test_client_retries_5xx_three_times_then_raises(tmp_path: Path):
    with XApiTestServer() as srv:
        for _ in range(4):
            srv.once("GET", "/2/users/me", 503, {"title": "Service Unavailable"})
        c, sleeps, _l = make_client(srv, tmp_path)
        with pytest.raises(XApiError) as ei:
            c.get("/2/users/me", {})
        assert ei.value.status == 503
        assert srv.count("/2/users/me") == 4
        assert sleeps.calls == [2.0, 4.0, 8.0]


def test_client_recovers_after_one_5xx(tmp_path: Path):
    with XApiTestServer() as srv:
        srv.once("GET", "/2/users/me", 500, {})
        c, sleeps, _l = make_client(srv, tmp_path)
        body, _h = c.get("/2/users/me", {})
        assert body["data"]["id"] == "1000" and sleeps.calls == [2.0]


def test_client_other_4xx_raises_with_status(tmp_path: Path):
    with XApiTestServer() as srv:
        c, _s, _l = make_client(srv, tmp_path)
        with pytest.raises(XApiError) as ei:
            c.get("/2/tweets/999/quote_tweets", {})
        assert ei.value.status == 404


def test_client_log_has_no_raw_ids_text_or_token(tmp_path: Path):
    with XApiTestServer() as srv:
        c, _s, log_path = make_client(srv, tmp_path)
        c.get("/2/users/1000/tweets", POST_PARAMS, unit_price=0.001)
        c.get("/2/users", {"ids": "2006,2008", "user.fields": "created_at"}, unit_price=0.01)
        text = log_path.read_text(encoding="utf-8")
        recs = [json.loads(x) for x in text.splitlines()]
        assert recs[0]["endpoint"] == "/2/users/:id/tweets"
        assert recs[0]["resources"] == 2 and recs[0]["cost"] == pytest.approx(0.002)
        assert recs[0]["status"] == 200
        assert recs[1]["resources"] == 1
        for raw in ('"1000"', '"2006"', '"2008"', "2006,2008", "tok-a", "write-up", "alice"):
            assert raw not in text


def test_empty_location_left_out_by_x_is_empty_when_fields_requested():
    """Slice 0a probe: X leaves out an empty location but sends url and description as ''."""
    from visualx.collect.xapi import map_user

    full = map_user({"id": "9", "username": "a", "created_at": "2020-01-01T00:00:00.000Z", "url": "", "description": ""}, "e")
    assert full.location == "" and full.field_status["location"] == "present"
    partial = map_user({"id": "9", "username": "a"}, "e")
    assert partial.location is None and partial.field_status["location"] == "absent"


class _ScriptedTransport(httpx.BaseTransport):
    """Raises the given errors in order, then answers 200 with an empty user body."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def handle_request(self, request):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return httpx.Response(200, json={"data": {"id": "1", "username": "x"}})


def test_client_retries_only_errors_before_the_request_is_sent():
    # A-07 (review 20261103): a connect error never reached X, so a retry is safe.
    req = httpx.Request("GET", "https://api.x.com/2/users/me")
    tr = _ScriptedTransport([httpx.ConnectError("refused", request=req)])
    client = XApiClient(token_provider=lambda: "t", sleep=lambda s: None, http=httpx.Client(transport=tr))
    body, _ = client.get("/2/users/me", {})
    assert body["data"]["id"] == "1" and tr.calls == 2


@pytest.mark.parametrize("err", [httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError])
def test_client_does_not_retry_after_the_request_was_sent(err):
    # A-07: X can bill a request that it got. A retry would be a second billed
    # call under one reservation.
    req = httpx.Request("GET", "https://api.x.com/2/users/me")
    tr = _ScriptedTransport([err("lost", request=req)])
    client = XApiClient(token_provider=lambda: "t", sleep=lambda s: None, http=httpx.Client(transport=tr))
    with pytest.raises(XApiError, match="not retried") as ei:
        client.get("/2/users/me", {})
    assert ei.value.status is None and tr.calls == 1
