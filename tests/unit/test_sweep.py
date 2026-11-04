"""DR-05 sweep against a real local HTTP server on 127.0.0.1 (AD-04, AD-10, FR-54).

No test contacts api.x.com. The router plays ``GET /2/tweets?ids=`` with the
X API v2 error shapes: "Not Found Error" (resource-not-found) for a deleted
post, "Authorization Error" (not-authorized-for-resource) for a protected or
suspended author.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import make_env
from tests.unit.xapi_test_server import Reply, XApiTestServer, default_router
from visualx.analyze import run_analyze
from visualx.build import run_build
from visualx.collect.plan import run_collect
from visualx.collect.sweep import META_CURSOR, classify, local_post_ids, run_sweep, select_ids
from visualx.collect.xapi import XApiClient
from visualx.delete import decode_payloads, find_id
from visualx.render.render import run_render
from visualx.score.engine import run_score

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
NOW_TS = NOW.timestamp()
BASE = 1850000000000000000  # X post IDs are 64-bit and increase with time


def not_found(pid: str) -> dict[str, Any]:
    return {
        "value": pid, "detail": f"Could not find tweet with ids: [{pid}].", "title": "Not Found Error",
        "resource_type": "tweet", "parameter": "ids", "resource_id": pid,
        "type": "https://api.twitter.com/2/problems/resource-not-found",
    }


def not_authorized(pid: str) -> dict[str, Any]:
    return {
        "value": pid, "detail": f"Sorry, you are not authorized to see the Tweet with ids: [{pid}].",
        "title": "Authorization Error", "resource_type": "tweet", "parameter": "ids", "resource_id": pid,
        "type": "https://api.twitter.com/2/problems/not-authorized-for-resource",
    }


class Router:
    """``default_router`` plus ``GET /2/tweets?ids=`` with configurable gone and protected IDs."""

    def __init__(self, gone: set[str] = frozenset(), protected: set[str] = frozenset(), silent: set[str] = frozenset()):
        self.gone, self.protected, self.silent = set(gone), set(protected), set(silent)

    def __call__(self, method: str, path: str, query: dict[str, str], form: dict[str, str]) -> Reply | None:
        if method == "GET" and path == "/2/tweets":
            ids = [i for i in query.get("ids", "").split(",") if i]
            body: dict[str, Any] = {}
            data = [{"id": i, "text": "post text", "edit_history_tweet_ids": [i]} for i in ids
                    if i not in self.gone | self.protected | self.silent]
            errs = [not_found(i) for i in ids if i in self.gone] + [not_authorized(i) for i in ids if i in self.protected]
            if data:
                body["data"] = data
            if errs:
                body["errors"] = errs
            return 200, {}, body
        return default_router(method, path, query, form)


class Env:
    def __init__(self, tmp_path: Path, srv: XApiTestServer | None, extra_toml: str = ""):
        self.cfg, self.store, self.cache, self.log = make_env(tmp_path, extra_toml)
        self.config_dir = tmp_path / "config"
        self.texts: list[str] = []
        self.sleeps: list[float] = []
        self.client = None
        if srv is not None:
            self.client = XApiClient(srv.base_url, token_provider=lambda: "tok", log=self.log,
                                     sleep=self.sleeps.append, now=lambda: NOW_TS)

    def approve_all(self, text: str) -> bool:
        self.texts.append(text)
        return True

    def add_posts(self, n: int) -> list[str]:
        ids = [str(BASE + i) for i in range(n)]
        with self.store.tx() as c:
            for pid in ids:
                self.store.put_post({"id": pid, "author_id": "2001", "text": "t"}, "/2/users/:id/tweets", "r0", conn=c)
        return ids

    def pipeline(self) -> Path:
        run_build(self.cfg, self.store, self.log)
        run_analyze(self.cfg, self.store, self.log)
        run_score(self.cfg, self.store, self.log)
        return run_render(self.cfg, self.store, self.log)

    def sweep(self, run_id: str, approve=None, **kw: Any):
        dry = kw.pop("dry_run", False)
        return run_sweep(self.cfg, self.store, self.cache, self.log, None if dry else self.client,
                         approve or self.approve_all, dry_run=dry, run_id=run_id, config_dir=self.config_dir, **kw)

    def ledger(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store.q("SELECT * FROM cost_ledger WHERE run_id=? ORDER BY id", (run_id,))]


def tweet_batches(srv: XApiTestServer) -> list[list[str]]:
    return [r["query"]["ids"].split(",") for r in srv.requests if r["path"] == "/2/tweets"]


def test_batches_of_100_and_ledger_rows(tmp_path: Path):
    with XApiTestServer(Router()) as srv:
        env = Env(tmp_path, srv)
        ids = env.add_posts(250)
        res = env.sweep("sw_1")
        assert res.complete, res.stopped_reason
        batches = tweet_batches(srv)
        assert [len(b) for b in batches] == [100, 100, 50]
        assert sum(batches, []) == ids  # oldest first, each ID once
        assert srv.count("/2/users/me") == 1 and len(srv.requests) == 4
        assert set(srv.requests[1]["query"]) == {"ids"}  # no fields, no expansions
        assert res.checked == 250 and res.on_x == 250 and res.gone == [] and not res.kept
        rows = env.ledger("sw_1")
        assert [(r["step"], r["endpoint"], r["billing_class"]) for r in rows] == [
            (0, "/2/users/me", "user_read")] + [(7, "/2/tweets", "post_read")] * 3
        assert [r["resources"] for r in rows] == [1, 100, 100, 50]
        assert all(r["response_code"] == 200 and r["cost"] == pytest.approx(r["reserved"]) for r in rows)
        assert res.total_cost == pytest.approx(0.010 + 250 * 0.005)
        assert env.store.latest_stage_run("sweep") is not None
        assert len(env.texts) == 1 and "250 posts x $0.0050" in env.texts[0] and "$1.2600" in env.texts[0]
        assert env.store.q("SELECT COUNT(*) AS n FROM cache_index")[0]["n"] == 0  # the sweep caches nothing


def test_gone_posts_are_deleted_from_store_cache_and_render(tmp_path: Path):
    gone, protected = "9002", "3002"
    with XApiTestServer(Router(gone={gone}, protected={protected})) as srv:
        env = Env(tmp_path, srv)
        assert run_collect(env.cfg, env.store, env.cache, env.log, env.client, lambda t: True, run_id="c1", now=NOW).complete
        html = env.pipeline()
        assert gone in local_post_ids(env.store)[0] and protected in local_post_ids(env.store)[0]
        assert env.cache.files_with({gone})
        # The current page holds no post IDs. Put the gone ID into the file, so that the render
        # step of the deletion (file scan, new render) is exercised.
        page = html.read_text(encoding="utf-8")
        html.write_text(page.replace("</body>", f"<!-- post {gone} --></body>"), encoding="utf-8")
        n_int = env.store.q1("SELECT COUNT(*) AS n FROM interactions WHERE source_post_id=? OR target_post_id=?",
                             (gone, gone))["n"]
        assert n_int > 0

        res = env.sweep("sw_del", rerender=lambda: str(env.pipeline()), orphans=lambda: [])
        assert res.complete, res.stopped_reason
        assert res.gone == [gone]
        assert dict(res.kept) == {"Authorization Error": 1}
        rep = res.delete_report
        assert rep is not None and rep.kind == "post" and rep.remaining == []
        assert rep.store_rows >= n_int and rep.cache_files_scrubbed >= 1
        assert rep.renders_deleted and not html.exists() and rep.rerendered and Path(rep.rerendered).exists()
        assert find_id(env.store, env.cache, gone) == []
        assert gone not in str(decode_payloads(Path(rep.rerendered).read_text(encoding="utf-8")))
        # The ambiguous post stays.
        assert protected in local_post_ids(env.store)[0]
        assert env.store.q1("SELECT 1 FROM posts WHERE post_id=?", (protected,)) is not None
        text = "\n".join(res.summary_lines())
        assert "gone and deleted:       1" in text and "Authorization Error 1" in text
        assert env.store.get_meta(META_CURSOR) == ""  # a complete sweep clears the cursor


def test_ambiguous_and_missing_answers_delete_nothing(tmp_path: Path):
    with XApiTestServer(Router(protected={str(BASE + 1)}, silent={str(BASE + 2)})) as srv:
        env = Env(tmp_path, srv)
        env.add_posts(3)
        res = env.sweep("sw_amb")
        assert res.complete and res.gone == [] and res.delete_report is None
        assert dict(res.kept) == {"Authorization Error": 1, "not returned": 1}
        assert len(local_post_ids(env.store)[0]) == 3


def test_classify_error_shapes():
    batch = ["1", "2", "3", "4", "5"]
    body = {
        "data": [{"id": "1", "text": "x"}],
        "errors": [
            not_found("2"),
            not_authorized("3"),
            {"value": "4", "title": "Not Found Error", "resource_type": "user", "resource_id": "4",
             "type": "https://api.twitter.com/2/problems/resource-not-found"},
            {"value": "99", "title": "Not Found Error", "resource_type": "tweet", "resource_id": "99"},
            "not an object",
        ],
    }
    on_x, gone, unclear = classify(body, batch)
    assert on_x == {"1"} and gone == {"2"}
    assert unclear == {"3": "Authorization Error", "4": "Not Found Error", "5": "not returned"}
    # The x.com problem URL and an error with only "value" also count as resource-not-found.
    _o, g2, _u = classify({"errors": [{"value": "7", "type": "https://api.x.com/2/problems/resource-not-found"}]}, ["7"])
    assert g2 == {"7"}


@pytest.mark.parametrize("cap, requests", [(0.005, 0), (0.5, 1), (0.52, 2)])
def test_cap_stops_before_a_request(tmp_path: Path, cap: float, requests: int):
    with XApiTestServer(Router()) as srv:
        env = Env(tmp_path, srv)
        env.add_posts(250)
        res = env.sweep("sw_cap", cap=cap)
        assert len(srv.requests) == requests
        assert res.stopped_reason and "Spend cap reached" in res.stopped_reason
        spent = env.store.ledger_total("sw_cap")
        assert spent <= cap + 1e-12 and res.total_cost == pytest.approx(spent)
        assert len(env.ledger("sw_cap")) == requests  # one reservation for each request, none for the stopped call
        assert "above the cap" in env.texts[0]


def test_dry_run_makes_no_request(tmp_path: Path):
    with XApiTestServer(Router()) as srv:
        env = Env(tmp_path, srv)
        env.add_posts(150)
        res = env.sweep("sw_dry", approve=lambda t: pytest.fail("a dry run asked for approval"), dry_run=True)
        assert srv.requests == [] and res.dry_run and not res.complete
        assert env.store.q("SELECT * FROM cost_ledger") == [] and env.store.latest_stage_run("sweep") is None
        assert res.selected == 150 and res.calls_planned == 2 and res.est_cost == pytest.approx(0.76)
        assert "150 posts x $0.0050" in res.estimate and "Authorization Error" in res.estimate
        assert "Sweep sw_dry (dry run, no API calls)" in "\n".join(res.summary_lines())


def test_refused_approval_makes_no_request(tmp_path: Path):
    with XApiTestServer(Router()) as srv:
        env = Env(tmp_path, srv)
        env.add_posts(5)
        res = env.sweep("sw_no", approve=lambda t: False)
        assert srv.requests == [] and res.approved is False
        assert res.stopped_reason == "the owner did not approve the sweep"
        assert env.store.q("SELECT * FROM cost_ledger") == []


def test_limit_rotates_through_all_posts(tmp_path: Path):
    with XApiTestServer(Router(gone={str(BASE + 99)})) as srv:
        env = Env(tmp_path, srv)
        ids = env.add_posts(250)
        r1 = env.sweep("sw_l1", limit=100)
        assert tweet_batches(srv)[-1] == ids[:100] and r1.gone == [ids[99]]
        # The last checked post was gone, so the cursor is the last checked post that stays.
        assert env.store.get_meta(META_CURSOR) == ids[98]
        r2 = env.sweep("sw_l2", limit=100)
        assert tweet_batches(srv)[-1] == ids[100:200] and r2.complete
        assert env.store.get_meta(META_CURSOR) == ids[199]
        env.sweep("sw_l3", limit=100)
        assert tweet_batches(srv)[-1] == ids[200:] + ids[:50]  # wraps to the oldest; the deleted post is gone
        assert "from after the last post" in env.texts[-1]


def test_select_ids_rotation():
    ids = ["1", "2", "3", "4"]
    assert select_ids(ids, None, None) == ids
    assert select_ids(ids, "2", 3) == ["3", "4", "1"]
    assert select_ids(ids, "[deleted]", 2) == ["1", "2"]


def test_owner_check_fr54(tmp_path: Path):
    with XApiTestServer(Router()) as srv:
        env = Env(tmp_path, srv, extra_toml="")
        env.cfg.raw["owner"]["handle"] = "Ron_Dilley"  # the token belongs to @owner
        env.add_posts(5)
        res = env.sweep("sw_h")
        assert res.stopped_reason and "FR-54" in res.stopped_reason
        assert srv.count("/2/users/me") == 1 and srv.count("/2/tweets") == 0
    with XApiTestServer(Router()) as srv:
        env = Env(tmp_path / "b", srv)
        env.store.set_meta("ego_user_id", "999")
        env.add_posts(5)
        res = env.sweep("sw_o")
        assert res.stopped_reason and "owner account of this store" in res.stopped_reason
        assert srv.count("/2/tweets") == 0


def test_http_error_and_rate_limit(tmp_path: Path):
    with XApiTestServer(Router(gone={str(BASE)})) as srv:
        srv.once("GET", "/2/tweets", 429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": str(int(NOW_TS) + 30)})
        env = Env(tmp_path, srv)
        env.add_posts(150)
        res = env.sweep("sw_429")
        assert res.complete and env.sleeps == [31.0] and res.gone == [str(BASE)]
        assert res.total_cost == pytest.approx(0.010 + 150 * 0.005)  # a 429 is not a billed call
    with XApiTestServer(Router(gone={str(BASE)})) as srv:
        srv.once("GET", "/2/tweets", 400, {"title": "Invalid Request"})
        env = Env(tmp_path / "b", srv)
        env.add_posts(150)
        res = env.sweep("sw_400")
        assert res.stopped_reason and "HTTP 400" in res.stopped_reason
        assert res.gone == [] and res.delete_report is None and len(local_post_ids(env.store)[0]) == 150
        rows = env.ledger("sw_400")
        assert rows[-1]["response_code"] == 400 and rows[-1]["cost"] == 0
        assert len(tweet_batches(srv)) == 1  # no later batch after the error


def test_no_post_ids_makes_no_request(tmp_path: Path):
    with XApiTestServer(Router()) as srv:
        env = Env(tmp_path, srv)
        res = env.sweep("sw_empty", approve=lambda t: pytest.fail("asked for approval with nothing to check"))
        assert srv.requests == [] and res.stopped_reason == "the store holds no post IDs to check"
