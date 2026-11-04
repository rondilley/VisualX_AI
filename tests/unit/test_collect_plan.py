"""The budgeted collection plan against a real local HTTP server (AD-04, TSC-03, TSC-04, TSC-05)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.unit.xapi_test_server import XApiTestServer
from visualx import config as config_mod
from visualx.cache import Cache
from visualx.collect.plan import CollectResult, run_collect
from visualx.collect.xapi import XApiClient
from visualx.config import Config
from visualx.logs import NullLog, RunLog
from visualx.store import Store

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
NOW_TS = NOW.timestamp()
FULL_COST = 0.01 + 0.004 + 0.002 + 0.005 + 0.02 + 0.01  # me, posts, mentions, quote, reposters, one profile
ALL_EDGES = {
    ("mention", "1000", "2001"), ("reply", "1000", "2002"), ("mention", "1000", "2003"),
    ("repost", "1000", "2003"), ("quote", "1000", "2004"), ("mention", "1000", "2005"),
    ("reply", "2005", "1000"), ("mention", "2001", "1000"), ("mention", "2001", "2006"),
    ("mention", "2001", "2008"), ("quote", "2002", "1000"), ("repost", "2007", "1000"),
    ("repost", "2001", "1000"),
}


def make_cfg(tmp_path: Path, **collect: Any) -> Config:
    handle = collect.pop("_handle", "")
    base = config_mod.load(ROOT / "config", root=ROOT)
    raw = copy.deepcopy(base.raw)
    raw["data"]["dir"] = str(tmp_path / "data")
    raw["collect"].update(collect)
    raw["owner"]["handle"] = handle
    return Config(root=tmp_path, raw=raw)


class Env:
    def __init__(self, tmp_path: Path, srv: XApiTestServer | None, log: RunLog | None = None, **collect: Any):
        self.cfg = make_cfg(tmp_path, **collect)
        self.store = Store(self.cfg.store_path)
        self.cache = Cache(self.store, self.cfg.cache_dir)
        self.log = log or NullLog()
        self.sleeps: list[float] = []
        self.texts: list[str] = []
        self.client = None
        if srv is not None:
            self.client = XApiClient(srv.base_url, token_provider=lambda: "tok", log=self.log,
                                     sleep=self.sleeps.append, now=lambda: NOW_TS)

    def approve_all(self, text: str) -> bool:
        self.texts.append(text)
        return True

    def run(self, run_id: str, approve=None, dry_run: bool = False, **kw: Any) -> CollectResult:
        return run_collect(self.cfg, self.store, self.cache, self.log, None if dry_run else self.client,
                           approve or self.approve_all, dry_run=dry_run, run_id=run_id, now=NOW, **kw)

    def edges(self) -> set[tuple[str, str, str]]:
        return {(r["type"], r["source_user_id"], r["target_user_id"]) for r in self.store.interactions()}

    def ledger(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store.q("SELECT * FROM cost_ledger WHERE run_id=? ORDER BY id", (run_id,))]


def test_full_run_all_five_steps(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv)
        res = env.run("run_a")
        assert res.complete, res.stopped_reason
        assert env.edges() == ALL_EDGES
        assert len(env.store.interactions()) == 13
        # Each endpoint once; page 2 of the owner timeline once; skip posts with a count of zero.
        assert srv.count("/2/users/me") == 1
        assert srv.count("/2/users/1000/tweets") == 2
        assert srv.count("/2/users/1000/mentions") == 1
        assert srv.count("/2/tweets/3001/quote_tweets") == 1
        assert srv.count("/2/tweets/3001/retweeted_by") == 1
        assert srv.count("/2/users") == 1
        assert len(srv.requests) == 7 == env.client.requests_sent
        assert not any("/3002/" in r["path"] or "/3003/" in r["path"] or "/3004/" in r["path"] for r in srv.requests)
        lookup = next(r for r in srv.requests if r["path"] == "/2/users")
        assert sorted(lookup["query"]["ids"].split(",")) == ["2006", "2008"]
        tl = next(r for r in srv.requests if r["path"] == "/2/users/1000/tweets")
        assert tl["query"]["start_time"] == "2026-08-04T12:00:00Z"
        assert "referenced_tweets.id.author_id" in tl["query"]["expansions"]
        assert "verified_type" in tl["query"]["user.fields"]
        # Owner identity, profiles, posts.
        assert env.store.get_meta("ego_user_id") == "1000" and env.store.get_meta("ego_handle") == "owner"
        profiles = env.store.profiles()
        assert {"1000", "2001", "2002", "2003", "2004", "2005", "2006", "2007"} <= set(profiles)
        assert env.store.profile_errors() == {"2008": "Not Found Error"}
        assert env.store.q1("SELECT COUNT(*) AS n FROM posts")["n"] == 6
        # Ledger: cost, reservations, response codes.
        assert res.total_cost == pytest.approx(FULL_COST)
        rows = env.ledger("run_a")
        assert [r["step"] for r in rows] == [0, 1, 1, 2, 3, 4, 5]
        assert all(r["cost"] <= r["reserved"] + 1e-12 and r["response_code"] == 200 for r in rows)
        assert [r["resources"] for r in rows] == [1, 2, 2, 2, 1, 2, 1]
        # Estimates vs actual.
        assert res.step(1).est_resources == 3200 and res.step(1).actual_resources == 4
        assert res.step(3).est_resources == 1 and res.step(3).est_kind.startswith("exact")
        assert res.step(4).est_resources == 2  # the owner's repost 3003 does not count
        assert res.step(5).est_resources == 2 and res.step(5).est_kind == "exact"
        # Approvals: one for each paid step; step 5 is the second approval with the exact count.
        assert len(env.texts) == 5
        assert env.texts[0].startswith("Step 1 of 5") and "/2/users/me" in env.texts[0]
        for t in env.texts[:2]:
            assert "OQ-02" in t and "OQ-01" in t and "If this is wrong" in t and "owned_read $0.0010" in t
        assert "Step 5 (profiles" in env.texts[1]  # the remaining plan shows step 5
        assert "maximum" in env.texts[2] and "Step 5" in env.texts[2]
        assert "second approval" in env.texts[4] and "maximum of 3 users" in env.texts[4]
        # stage_runs record and the cursor.
        sr = env.store.latest_stage_run("collect")
        counts = json.loads(sr["counts_json"])
        assert counts["complete"] is True and counts["interactions_total"] == 13
        assert all(r["done"] == 1 for r in env.store.q("SELECT done FROM cursor"))
        assert env.store.get_meta("collect_window_start") == ""


def test_dry_run_makes_zero_http_calls(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv)
        res = env.run("dry_1", approve=lambda t: pytest.fail("dry run asked for approval"), dry_run=True)
        assert srv.requests == []
        assert res.dry_run and res.ego_user_id is None
        assert any("owner ID is not known" in w for w in res.warnings)
        assert len(res.estimates) == 5 and res.step(1).est_resources == 3200 and res.step(2).est_resources == 800
        assert res.step(3).est_resources is None
        assert env.store.q("SELECT * FROM cost_ledger") == [] and env.store.latest_stage_run("collect") is None

        env.run("run_a")  # fill the store
        before = len(srv.requests)
        inc = run_collect(env.cfg, env.store, env.cache, env.log, None, lambda t: False, dry_run=True, run_id="dry_i", now=NOW)
        assert inc.incremental and inc.step(3).est_resources is None  # new posts only, known after step 1
        res2 = run_collect(env.cfg, env.store, env.cache, env.log, None, lambda t: False, dry_run=True, run_id="dry_2",
                           now=NOW, full=True)
        assert len(srv.requests) == before
        assert res2.ego_user_id == "1000"
        assert res2.step(3).est_resources == 1 and res2.step(4).est_resources == 2
        assert env.ledger("dry_2") == []


@pytest.mark.parametrize("cap", [0.005, 0.05, 0.111, 0.12, 0.2, 0.62, 0.7, 5.0])
def test_cap_is_never_exceeded(tmp_path: Path, cap: float):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, cap_usd=cap)
        res = env.run("run_cap")
        spent = env.store.ledger_total("run_cap")
        assert spent <= cap + 1e-12
        assert res.total_cost == pytest.approx(spent)
        rows = env.ledger("run_cap")
        # One ledger row for each request: no request went out without a reservation.
        assert len(rows) == len(srv.requests)
        if res.cap_reached is not None:
            assert res.stopped_reason and "Spend cap reached before step" in res.stopped_reason
            assert res.remaining_plan
            nxt = res.cap_reached
            assert nxt.spent + nxt.reserve > cap
        else:
            assert res.complete and spent == pytest.approx(FULL_COST)


def test_cap_stop_names_step_then_resume_completes_without_paying_twice(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, cap_usd=0.12)
        res = env.run("run_1")
        assert res.cap_reached is not None and res.cap_reached.step == 3
        assert res.step(1).status == "done" and res.step(2).status == "done" and res.step(3).status == "stopped"
        assert any(line.startswith("Step 3") for line in res.remaining_plan)
        assert any(line.startswith("Step 5") for line in res.remaining_plan)
        assert srv.count("/2/tweets/3001/quote_tweets") == 0
        assert env.store.get_meta("collect_window_start") == "2026-08-04T12:00:00Z"

        env.cfg.raw["collect"]["cap_usd"] = 5.0
        n_before = len(env.texts)
        res2 = env.run("run_2")
        assert res2.complete, res2.stopped_reason
        assert srv.count("/2/users/1000/tweets") == 2 and srv.count("/2/users/1000/mentions") == 1
        run2_texts = env.texts[n_before:]
        assert run2_texts[0].startswith("Step 1 of 5") and "already collected" in run2_texts[0]
        assert env.edges() == ALL_EDGES and len(env.store.interactions()) == 13


def test_rate_limit_429_waits_and_gives_same_event_count(tmp_path: Path):
    with XApiTestServer() as srv:
        base = Env(tmp_path / "a", srv)
        base.run("run_ok")
        expected = base.edges()
        n_expected = len(base.store.interactions())

    with XApiTestServer() as srv:
        srv.once("GET", "/2/users/1000/tweets", 429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": str(int(NOW_TS) + 30)})
        srv.once("GET", "/2/users/1000/mentions", 429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": str(int(NOW_TS) + 120)})
        env = Env(tmp_path / "b", srv)
        res = env.run("run_429")
        assert res.complete, res.stopped_reason
        assert env.sleeps == [31.0, 121.0]
        assert len(env.store.interactions()) == n_expected == 13
        assert env.edges() == expected
        assert res.total_cost == pytest.approx(FULL_COST)  # a 429 is not a billed page
        assert len({r["authorization"] for r in srv.requests}) == 1


def test_cached_page_is_reused_after_a_crash_before_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv)
        real = env.store.put_interactions

        def crash_on_quotes(items, conn=None):
            items = list(items)
            if any(i.source_endpoint == "/2/tweets/:id/quote_tweets" for i in items):
                raise RuntimeError("simulated crash after the response was cached")
            return real(items, conn=conn)

        monkeypatch.setattr(env.store, "put_interactions", crash_on_quotes)
        with pytest.raises(RuntimeError):
            env.run("run_crash")
        monkeypatch.setattr(env.store, "put_interactions", real)
        assert srv.count("/2/tweets/3001/quote_tweets") == 1

        res = env.run("run_after")
        assert res.complete, res.stopped_reason
        assert srv.count("/2/tweets/3001/quote_tweets") == 1  # the cached page, no second payment
        assert res.step(3).cached_pages == 1 and res.step(3).actual_cost == 0.0
        assert env.edges() == ALL_EDGES


def test_server_error_stops_and_keeps_cursor(tmp_path: Path):
    with XApiTestServer() as srv:
        for _ in range(4):
            srv.once("GET", "/2/tweets/3001/retweeted_by", 503, {"title": "Service Unavailable"})
        env = Env(tmp_path, srv)
        res = env.run("run_5xx")
        assert res.stopped_reason and "503" in res.stopped_reason
        assert res.step(4).status == "stopped" and res.step(5).status == "not_run"
        assert env.store.get_cursor(4, "3001") == (None, False)
        assert env.store.get_cursor(3, "3001") == (None, True)
        assert env.sleeps == [2.0, 4.0, 8.0]
        assert env.store.ledger_total("run_5xx") <= env.cfg.get("collect.cap_usd")
        res2 = env.run("run_5xx_resume")
        assert res2.complete and env.edges() == ALL_EDGES


def test_denied_approval_stops_cleanly(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv)
        answers = iter([True, False])
        res = env.run("run_deny", approve=lambda t: next(answers))
        assert res.stopped_reason and "did not approve step 2" in res.stopped_reason
        assert srv.count("/2/users/1000/mentions") == 0
        assert res.step(1).status == "done" and res.step(2).status == "not_run"


def test_denied_first_approval_makes_no_call(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv)
        res = env.run("run_no", approve=lambda t: False)
        assert srv.requests == [] and res.stopped_reason and "step 1" in res.stopped_reason


def test_mention_limit_warning_names_oldest_mention(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, mention_limit=2)
        res = env.run("run_lim")
        assert res.complete
        assert any("2-mention limit cut the window" in w and "2026-10-12T13:00:00.000Z" in w for w in res.warnings)


def test_store_of_a_different_owner_stops(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv)
        env.store.set_meta("ego_user_id", "999")
        res = env.run("run_other")
        assert res.stopped_reason and "FR-54" in res.stopped_reason
        assert srv.count("/2/users/1000/tweets") == 0
        assert env.store.get_meta("ego_user_id") == "999"


def test_run_log_has_no_raw_ids_or_text(tmp_path: Path):
    with XApiTestServer() as srv:
        log = RunLog(tmp_path / "runs" / "log.jsonl", b"k" * 32)
        env = Env(tmp_path, srv, log=log)
        env.run("run_log")
        text = (tmp_path / "runs" / "log.jsonl").read_text(encoding="utf-8")
        events = [json.loads(x)["event"] for x in text.splitlines()]
        assert events.count("x_api_call") == 7
        for raw in ('"1000"', '"3001"', '"2006"', "write-up", "alice", "Not Found Error"):
            assert raw not in text


def test_configured_handle_must_match_signed_in_account(tmp_path: Path):
    """FR-54: config owner.handle = Ron_Dilley, the token belongs to @owner -> stop after /users/me."""
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, _handle="Ron_Dilley")
        res = env.run("run_h")
        assert not res.complete
        assert "owner.handle" in (res.stopped_reason or "")
        assert srv.count("/2/users/me") == 1 and len(srv.requests) == 1
        assert env.store.ego_id is None
    with XApiTestServer() as srv:
        env = Env(tmp_path / "b", srv, _handle="@OWNER")
        assert env.run("run_i").complete


def test_incremental_second_run_uses_since_id(tmp_path: Path):
    """FR-08: after a complete run, steps 1 and 2 send since_id, and old posts get no new quote or repost calls."""
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv)
        assert env.run("run_full").complete
        first = len(srv.requests)
        res = env.run("run_inc")
        assert res.complete and res.incremental
        new = srv.requests[first:]
        tl = [r for r in new if r["path"] == "/2/users/1000/tweets"]
        mn = [r for r in new if r["path"] == "/2/users/1000/mentions"]
        assert tl and all("since_id" in r["query"] and "start_time" not in r["query"] for r in tl)
        assert mn and all("since_id" in r["query"] for r in mn)
        newest_owner = max((p for p in env.store.q("SELECT post_id FROM posts WHERE author_id='1000'")), key=lambda r: int(r[0]))[0]
        assert tl[0]["query"]["since_id"] == newest_owner
        assert not any("/quote_tweets" in r["path"] or "/retweeted_by" in r["path"] for r in new)
        assert env.edges() == ALL_EDGES  # no duplicate and no lost interaction
        assert "incremental" in "\n".join(res.summary_lines())


def test_full_flag_and_longer_window_collect_everything(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv)
        assert env.run("r1").complete
        n = len(srv.requests)
        res = run_collect(env.cfg, env.store, env.cache, env.log, env.client, env.approve_all,
                          dry_run=False, run_id="r2", now=NOW, full=True)
        assert res.complete and not res.incremental
        tl = [r for r in srv.requests[n:] if r["path"] == "/2/users/1000/tweets"]
        assert all("start_time" in r["query"] and "since_id" not in r["query"] for r in tl)
        # A longer window than the last complete run is not incremental.
        env.cfg.raw["collect"]["window_days"] = 400
        res3 = env.run("r3")
        assert not res3.incremental
        assert any("full window" in w for w in res3.warnings)
