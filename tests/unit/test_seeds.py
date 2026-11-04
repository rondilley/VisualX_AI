"""Seed-account collection (FR-07, plan step 6) against the real local test server.

The seed timelines are hand-made fixtures (tests/fixtures/handmade/README.md).
After the five-step run of the base fixtures, the tie weights to the owner are:
bob 2002 = 7.5, erin 2005 = 6.75, alice 2001 = 6.0, carol 2003 = 2.5,
dave 2004 = 2.0, gina 2007 = 1.0. Frank 2006 and hank 2008 have no edge to
or from the owner.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.unit.test_collect_plan import ALL_EDGES, FULL_COST, NOW, Env
from tests.unit.xapi_test_server import XApiTestServer
from visualx.build import run_build
from visualx.collect.plan import META_SEED_NEWEST, META_SEED_PLAN, run_collect
from visualx.collect.seeds import select_seeds, tie_weights
from visualx.delete import delete_ids
from visualx.logs import NullLog

SEED_START = "2026-10-03T12:00:00Z"  # NOW - 30 days; later than the 90-day window start
SEED_EDGES = {
    ("quote", "2002", "2005"), ("mention", "2002", "2001"), ("mention", "2002", "2009"),
    ("reply", "2002", "2003"), ("mention", "2005", "2010"), ("repost", "2005", "2001"),
}
STEPS_0_TO_4 = 0.01 + 0.004 + 0.002 + 0.005 + 0.02
SEED_COST = 3 * 0.005 + 3 * 0.005  # bob 3 posts, erin 3 posts, alice none
SEED_RUN_COST = STEPS_0_TO_4 + SEED_COST + 2 * 0.01  # step 5: 2006 and 2010 (2008 is not found)


def seed_paths(srv: XApiTestServer, start: int = 0) -> list[dict]:
    return [r for r in srv.requests[start:] if r["path"].endswith("/tweets") and r["path"] != "/2/users/1000/tweets"]


def test_seed_selection_order_and_exclusions(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv)
        assert env.run("base").complete
    w = tie_weights(env.cfg, env.store, "1000")
    assert w == pytest.approx({"2002": 7.5, "2005": 6.75, "2001": 6.0, "2003": 2.5, "2004": 2.0, "2007": 1.0})
    assert [s.user_id for s in select_seeds(env.cfg, env.store, "1000", 10)] == ["2002", "2005", "2001", "2003", "2004", "2007"]
    assert [s.handle for s in select_seeds(env.cfg, env.store, "1000", 2)] == ["bob", "erin"]
    assert select_seeds(env.cfg, env.store, "1000", 0) == [] and select_seeds(env.cfg, env.store, None, 5) == []

    # Not seeds: a protected account, a provisional h: ID, an account with a failed lookup.
    env.store.conn.execute("UPDATE profiles SET protected=1 WHERE user_id='2005'")
    hi = dict(env.store.interactions()[0])
    for uid in ("h:zed", "2008"):
        env.store.conn.execute(
            "INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"x{uid}", "reply", uid, "1000", None, None, None, hi["observed_at"], None, None, None, None,
             "/2/users/:id/mentions", None, "xapi", "1", "t", json.dumps(hi["field_status"])),
        )
    assert {"h:zed", "2008"} <= set(tie_weights(env.cfg, env.store, "1000"))
    ids = [s.user_id for s in select_seeds(env.cfg, env.store, "1000", 10)]
    assert ids == ["2002", "2001", "2003", "2004", "2007"]
    # Allowlisted contacts can be seeds: the allowlist changes the score, not the collection.
    env.cfg.allowlist = ["2002"]
    assert select_seeds(env.cfg, env.store, "1000", 1)[0].user_id == "2002"


def test_seed_run_order_cost_approvals_and_alter_edges(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, seeds=3)
        res = env.run("run_s")
        assert res.complete, res.stopped_reason
        assert [s.step for s in res.steps] == [1, 2, 3, 4, 6, 5]
        # The seeds are bob, erin, alice: one page each (seed_pages = 1), from the seed window start.
        sp = seed_paths(srv)
        assert [r["path"] for r in sp] == ["/2/users/2002/tweets", "/2/users/2005/tweets", "/2/users/2001/tweets"]
        assert all(r["query"]["start_time"] == SEED_START and "since_id" not in r["query"] for r in sp)
        assert all(r["query"]["max_results"] == "100" and "referenced_tweets.id.author_id" in r["query"]["expansions"] for r in sp)
        # Step 6 runs before step 5: the profile lookup includes 2010, an account from a seed post.
        lookup = [r for r in srv.requests if r["path"] == "/2/users"]
        assert len(lookup) == 1 and sorted(lookup[0]["query"]["ids"].split(",")) == ["2006", "2008", "2010"]
        assert srv.requests.index(lookup[0]) > srv.requests.index(sp[-1])
        # Ledger: step numbers 0 to 5 as before, and 6 for the seed calls; post_read price.
        rows = env.ledger("run_s")
        assert [r["step"] for r in rows] == [0, 1, 1, 2, 3, 4, 6, 6, 6, 5]
        seed_rows = [r for r in rows if r["step"] == 6]
        assert all(r["billing_class"] == "post_read" and r["unit_price"] == 0.005 for r in seed_rows)
        assert [r["resources"] for r in seed_rows] == [3, 3, 0]
        assert all(r["cost"] <= r["reserved"] + 1e-12 for r in rows)
        assert res.total_cost == pytest.approx(SEED_RUN_COST)
        assert res.step(6).actual_resources == 6 and res.step(6).actual_cost == pytest.approx(SEED_COST)
        # Approvals: 1, 2, 3, 4, 6, then the second approval of step 5.
        assert len(env.texts) == 6
        assert env.texts[0].startswith("Step 1 of 6")
        t6 = env.texts[4]
        assert t6.startswith("Step 6 of 6: seed posts (/2/users/:id/tweets)")
        assert "Seeds: 3 accounts, the top 3 of 6 contacts" in t6 and "@bob 7.50, @erin 6.75, @alice 6.00" in t6
        assert "3 seeds x 1 page(s) x 100 posts x $0.0050 (post_read) = $1.5000" in t6
        assert "OQ-01" in t6 and "OQ-02" not in t6
        assert "Step 5 (profiles" in t6  # the remaining plan after step 6
        t5 = env.texts[5]
        assert t5.startswith("Step 5 of 6") and "after steps 3, 4, and 6" in t5 and "3 users" in t5
        # The seed edges are in the store, with no duplicate of the reply 4001 (a mention and a seed post).
        assert env.edges() == ALL_EDGES | SEED_EDGES
        assert len(env.store.interactions()) == 13 + 6
        # A seed post that is also a mention keeps the mentions row (the step 2 since_id stays correct).
        assert env.store.q1("SELECT source_endpoint FROM posts WHERE post_id='4001'")[0] == "/2/users/:id/mentions"
        assert env.store.q1("SELECT source_endpoint FROM posts WHERE post_id='6003'")[0] == "/2/users/:id/tweets"
        assert json.loads(env.store.get_meta(META_SEED_NEWEST)) == {"2002": "6003", "2005": "6102"}
        assert env.store.get_meta(META_SEED_PLAN) == ""
        counts = json.loads(env.store.latest_stage_run("collect")["counts_json"])
        assert counts["steps"]["6"]["status"] == "done" and counts["steps"]["6"]["calls"] == 3

    # Build: edges between the owner's contacts appear (the map is no longer a star).
    out = run_build(env.cfg, env.store, NullLog(), "b1")
    alter = {(r["source"], r["target"]) for r in env.store.q("SELECT source, target FROM edges WHERE source!='1000' AND target!='1000'")}
    assert {("2002", "2005"), ("2002", "2001"), ("2002", "2003"), ("2005", "2001")} <= alter
    assert out["edges"] > len({(s, t) for _k, s, t in ALL_EDGES})


def test_seeds_off_is_the_five_step_plan(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, seeds=0)
        res = env.run("run_off")
        assert res.complete and [s.step for s in res.steps] == [1, 2, 3, 4, 5]
        assert seed_paths(srv) == [] and res.total_cost == pytest.approx(FULL_COST)


def test_seed_pages_limit(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, seeds=1)
        assert env.run("p1").complete
        assert [r["path"] for r in seed_paths(srv)] == ["/2/users/2002/tweets"]  # next_token ignored: 1 page
    with XApiTestServer() as srv:
        env = Env(tmp_path / "b", srv, seeds=1, seed_pages=2)
        assert env.run("p2").complete
        sp = seed_paths(srv)
        assert len(sp) == 2 and sp[1]["query"]["pagination_token"] == "s2002p2"
        assert ("mention", "2002", "2003") in env.edges()


def test_denied_seed_approval_stops_before_any_seed_call(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, seeds=3)
        answers = iter([True, True, True, True, False])
        res = env.run("run_deny", approve=lambda t: next(answers))
        assert res.stopped_reason and "did not approve step 6" in res.stopped_reason
        assert seed_paths(srv) == [] and srv.count("/2/users") == 0
        assert res.step(6).status == "not_run" and res.step(5).status == "not_run"
        assert any(line.startswith("Step 6") for line in res.remaining_plan)
        assert any(line.startswith("Step 5") for line in res.remaining_plan)


def test_cap_stops_before_a_seed_call_then_resume_uses_the_same_seeds(tmp_path: Path):
    # page_size 5: a seed call reserves 5 x $0.005. Steps 0 to 4 spend $0.041; bob's page spends $0.015.
    # The call for erin would reserve $0.025 on top of $0.056 > $0.075.
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, seeds=3, page_size=5, cap_usd=0.075)
        res = env.run("run_c1")
        assert res.cap_reached is not None and res.cap_reached.step == 6
        assert "before step 6 (seed posts)" in (res.stopped_reason or "")
        assert [r["path"] for r in seed_paths(srv)] == ["/2/users/2002/tweets"]
        assert res.step(6).status == "stopped" and res.step(5).status == "not_run"
        assert [line.split(" ")[1] for line in res.remaining_plan] == ["6", "5"]
        assert env.store.ledger_total("run_c1") <= 0.075 + 1e-12
        plan = json.loads(env.store.get_meta(META_SEED_PLAN))
        assert [e["id"] for e in plan["seeds"]] == ["2002", "2005", "2001"]
        assert env.store.get_cursor(6, "2002")[1] is True  # one page done (seed_pages = 1)

        env.cfg.raw["collect"]["cap_usd"] = 5.0
        n = len(env.texts)
        res2 = env.run("run_c2")
        assert res2.complete, res2.stopped_reason
        t6 = next(t for t in env.texts[n:] if t.startswith("Step 6"))
        assert "continues the seed plan" in t6 and "1 seeds are complete from an earlier run" in t6
        assert "@bob" not in t6 and "@erin 6.75, @alice 6.00" in t6
        assert [r["path"] for r in seed_paths(srv)] == ["/2/users/2002/tweets", "/2/users/2005/tweets", "/2/users/2001/tweets"]
        assert env.edges() == ALL_EDGES | SEED_EDGES


def test_estimate_warns_when_the_seed_maximum_is_above_the_cap(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, seeds=3, cap_usd=1.2)  # 3 seeds x 100 x $0.005 = $1.50
        answers = iter([True, True, True, True, False])
        env.run("run_w", approve=lambda t: (env.texts.append(t), next(answers))[1])
        t6 = env.texts[-1]
        assert t6.startswith("Step 6") and "The maximum is above the cap ($1.20)" in t6


@pytest.mark.parametrize("cap", [0.05, 0.07, 0.09, 0.1, 0.12, 0.2])
def test_cap_is_never_exceeded_with_seeds(tmp_path: Path, cap: float):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, seeds=3, page_size=5, cap_usd=cap)
        res = env.run("run_cap")
        spent = env.store.ledger_total("run_cap")
        assert spent <= cap + 1e-12
        assert len(env.ledger("run_cap")) == len(srv.requests)
        if res.cap_reached is not None:
            assert res.cap_reached.spent + res.cap_reached.reserve > cap
        else:
            assert res.complete


def test_incremental_seeds_use_since_id_per_seed(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, seeds=2)
        assert env.run("inc_1").complete
        assert json.loads(env.store.get_meta(META_SEED_NEWEST)) == {"2002": "6003", "2005": "6102"}
        first = len(srv.requests)
        env.cfg.raw["collect"]["seeds"] = 3  # alice is a new seed
        res = env.run("inc_2")
        assert res.complete and res.incremental
        sp = {r["path"]: r["query"] for r in seed_paths(srv, first)}
        assert sp["/2/users/2002/tweets"]["since_id"] == "6003" and "start_time" not in sp["/2/users/2002/tweets"]
        assert sp["/2/users/2005/tweets"]["since_id"] == "6102"
        assert sp["/2/users/2001/tweets"]["start_time"] == SEED_START and "since_id" not in sp["/2/users/2001/tweets"]
        t6 = [t for t in env.texts if t.startswith("Step 6")][-1]
        assert "Incremental: 2 seeds ask only for posts newer" in t6 and "1 new seeds read the full seed window" in t6
        assert ("mention", "2002", "2005") in env.edges()
        assert json.loads(env.store.get_meta(META_SEED_NEWEST))["2002"] == "6010"

        # A --full run reads the seed window again, with no since_id.
        n = len(srv.requests)
        res3 = env.run("inc_3", full=True)
        assert res3.complete and not res3.incremental
        assert all("since_id" not in r["query"] and r["query"]["start_time"] == SEED_START for r in seed_paths(srv, n))


def test_dry_run_with_seeds_makes_zero_http_calls(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, seeds=3)
        res = env.run("dry_0", approve=lambda t: pytest.fail("dry run asked for approval"), dry_run=True)
        assert srv.requests == [] and res.step(6).est_resources == 0
        assert any("owner ID is not known, so the store gives no seeds" in t for t in res.estimates)

        env.cfg.raw["collect"]["seeds"] = 0
        assert env.run("fill").complete
        before = len(srv.requests)
        env.cfg.raw["collect"]["seeds"] = 40
        res = env.run("dry_1", approve=lambda t: pytest.fail("dry run asked for approval"), dry_run=True)
        assert len(srv.requests) == before
        assert env.ledger("dry_1") == [] and env.store.get_meta(META_SEED_PLAN) in (None, "")
        assert [s.step for s in res.steps] == [1, 2, 3, 4, 6, 5]
        s6 = res.step(6)
        assert s6.est_resources == 600 and s6.est_cost == pytest.approx(3.0)
        t6 = next(t for t in res.estimates if t.startswith("Step 6"))
        assert "Seeds: 6 accounts, the top 6 of 6 contacts by tie weight to the owner (collect.seeds = 40)" in t6
        assert "6 seeds x 1 page(s) x 100 posts x $0.0050 (post_read) = $3.0000" in t6
        assert "Incremental: 0 seeds ask only for posts newer" in t6
        text = "\n".join(res.summary_lines())
        assert "Step 6 of 6: seed posts" in text and "Estimates" in text


def test_seed_http_404_skips_that_seed(tmp_path: Path):
    with XApiTestServer() as srv:
        srv.once("GET", "/2/users/2005/tweets", 404, {"title": "Not Found Error"})
        env = Env(tmp_path, srv, seeds=3)
        res = env.run("run_404")
        assert res.complete, res.stopped_reason
        assert any("HTTP 404" in w for w in res.warnings)
        assert srv.count("/2/users/2001/tweets") == 1
        assert ("quote", "2002", "2005") in env.edges() and ("repost", "2005", "2001") not in env.edges()
        row = [r for r in env.ledger("run_404") if r["response_code"] == 404]
        assert len(row) == 1 and row[0]["cost"] == 0


def test_old_store_without_seed_state_keeps_working(tmp_path: Path):
    """A store from before step 6: cursor rows for steps 1 to 5 only, no seed meta keys."""
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, cap_usd=0.012)  # five-step plan, stops in step 2 with an open window
        res = env.run("old_1")
        assert not res.complete and env.store.get_meta("collect_window_start")
        env.store.conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (META_SEED_PLAN, META_SEED_NEWEST))
        assert {r["step"] for r in env.store.q("SELECT step FROM cursor")} <= {1, 2, 3, 4, 5}

        env.cfg.raw["collect"].update(cap_usd=5.0, seeds=2)
        res2 = env.run("old_2")
        assert res2.complete, res2.stopped_reason
        assert srv.count("/2/users/2002/tweets") == 1 and srv.count("/2/users/2005/tweets") == 1
        assert env.edges() >= ALL_EDGES

    with XApiTestServer() as srv:
        env = Env(tmp_path / "b", srv)
        assert env.run("old_a").complete
        # A store from before FR-08 and FR-07: no complete-window meta and no seed meta.
        env.store.conn.execute("DELETE FROM meta WHERE key IN ('collect_complete_window_start', ?, ?)",
                               (META_SEED_PLAN, META_SEED_NEWEST))
        env.cfg.raw["collect"]["seeds"] = 2
        n = len(srv.requests)
        res = env.run("old_b")
        assert res.complete and res.incremental
        assert all("start_time" in r["query"] for r in seed_paths(srv, n))  # new seeds: full seed window


def test_delete_removes_a_seed_from_the_seed_state(tmp_path: Path):
    with XApiTestServer() as srv:
        env = Env(tmp_path, srv, seeds=2)
        assert env.run("del_1").complete
    rep = delete_ids(env.store, env.cache, tmp_path, ["2002"], "user")
    assert rep.remaining == []
    newest = json.loads(env.store.get_meta(META_SEED_NEWEST))
    assert "2002" not in newest and newest.get("2005") == "6102"
    # The next dry run reads the redacted state without an error.
    res = run_collect(env.cfg, env.store, env.cache, env.log, None, lambda t: False, dry_run=True, run_id="d", now=NOW)
    assert res.step(6).est_resources is not None
