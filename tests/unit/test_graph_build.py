"""Slice 4 gate: TSC-06, TSC-07, TSC-08 (build) and TSC-10 (metrics)."""

from __future__ import annotations

import math

from tests.helpers import interaction, make_env, mark_stage, profile
from visualx.analyze import layout, metrics, run_analyze
from visualx.build import edge_weight, run_build

T = "2026-10-01T00:00:00Z"


def _env(tmp_path):
    cfg, store, cache, log = make_env(tmp_path)
    store.set_meta("ego_user_id", "ego")
    mark_stage(store, "collect")
    return cfg, store, cache, log


def test_missing_profile_nodes_tsc06(tmp_path):
    cfg, store, _, log = _env(tmp_path)
    store.put_interactions([
        interaction("reply", "a", "ego", spost="1", tpost="0", at=T),
        interaction("mention", "b", "ego", spost="2", at=T),
    ])
    store.put_profiles([profile("a", handle="aa", name="A")])
    counts = run_build(cfg, store, log)
    assert counts["interactions"] == 2
    assert counts["missing_profiles"] == 1
    st = {r["user_id"]: r["profile_status"] for r in store.q("SELECT user_id, profile_status FROM nodes")}
    assert st == {"a": "present", "b": "missing", "ego": "missing"} or st["b"] == "missing"
    # All events stay, also for the account with no profile.
    assert {r["source"] for r in store.q("SELECT source FROM edges")} == {"a", "b"}


def test_counts_match_events_tsc08(tmp_path):
    cfg, store, _, log = _env(tmp_path)
    items = [
        interaction("reply", "a", "ego", spost="1", tpost="0", at=T),
        interaction("reply", "a", "ego", spost="2", tpost="0", at=T),
        interaction("quote", "a", "ego", spost="3", tpost="0", at=T),
        interaction("repost", "c", "ego", tpost="0"),
        interaction("mention", "ego", "c", spost="4", at=T),
        interaction("mention", "a", "c", spost="1", at=T),
    ]
    store.put_interactions(items)
    run_build(cfg, store, log)
    for t in ("reply", "quote", "mention", "repost"):
        edge_total = store.q1(f"SELECT SUM({t}_count) AS s FROM edges")["s"] or 0
        events = store.q1("SELECT COUNT(*) AS n FROM interactions WHERE type=?", (t,))["n"]
        assert edge_total == events, t
    e = dict(store.q1("SELECT * FROM edges WHERE source='a' AND target='ego'"))
    assert (e["reply_count"], e["quote_count"]) == (2, 1)
    assert math.isclose(e["weight"], 3.0 * (1 + math.log(2)) + 2.0)


def test_reciprocity(tmp_path):
    cfg, store, _, log = _env(tmp_path)
    store.put_interactions([
        interaction("reply", "a", "ego", spost="1", tpost="0", at=T),
        interaction("reply", "ego", "a", spost="5", tpost="1", at=T),
        interaction("mention", "b", "ego", spost="2", at=T),
    ])
    run_build(cfg, store, log)
    pw = {(r["source"], r["target"]): r["pair_weight"] for r in store.q("SELECT * FROM edges")}
    assert math.isclose(pw[("a", "ego")], (3.0 + 3.0) * 1.5)
    assert math.isclose(pw[("b", "ego")], 1.5)


def test_edge_weight_formula():
    a = {"reply": 3.0, "quote": 2.0, "mention": 1.5, "repost": 1.0}
    assert edge_weight({"reply": 1, "quote": 0, "mention": 0, "repost": 0}, a) == 3.0
    assert math.isclose(edge_weight({"reply": 0, "quote": 0, "mention": 3, "repost": 1}, a), 1.5 * (1 + math.log(3)) + 1.0)


def test_betweenness_without_ego_tsc10():
    """Ego connects to all. Two clusters join only through 'bridge'. The bridge must rank 1."""
    left = [f"l{i}" for i in range(5)]
    right = [f"r{i}" for i in range(5)]
    nodes = ["ego", "bridge", *left, *right]
    edges = []

    def add(s, t, w=1.0):
        edges.append({"source": s, "target": t, "weight": w, "pair_weight": w})

    for u in nodes:
        if u != "ego":
            add(u, "ego")
            add("ego", u)
    for grp in (left, right):
        for a in grp:
            for b in grp:
                if a != b:
                    add(a, b)
    add("l0", "bridge")
    add("bridge", "l0")
    add("r0", "bridge")
    add("bridge", "r0")
    m = metrics(nodes, edges, "ego")
    ranked = sorted((u for u in nodes if u != "ego"), key=lambda u: -m[u]["betweenness"])
    assert ranked[0] == "bridge"
    assert m["ego"]["betweenness"] is None
    assert m["ego"]["degree"] == len(nodes) - 1


def test_layout_ego_at_center_and_strong_ties_near():
    nodes = ["ego", "strong", "weak", "x"]
    edges = [
        {"source": "strong", "target": "x", "weight": 1.0, "pair_weight": 1.0},
        {"source": "weak", "target": "x", "weight": 1.0, "pair_weight": 1.0},
    ]
    pos = layout(nodes, edges, "ego", {"strong": 50.0, "weak": 0.5, "x": 0.0}, seed=1, iterations=200)
    assert pos["ego"] == (0.0, 0.0)
    d = {u: math.hypot(*pos[u]) for u in nodes}
    assert all(d[u] > 0 for u in ("strong", "weak", "x"))
    pos2 = layout(nodes, edges, "ego", {"strong": 50.0, "weak": 0.5, "x": 0.0}, seed=1, iterations=200)
    assert pos == pos2  # fixed random seed (MR-04)


def test_run_analyze_writes_metrics(tmp_path):
    cfg, store, _, log = _env(tmp_path)
    store.put_interactions([
        interaction("reply", "a", "ego", spost="1", tpost="0", at=T),
        interaction("reply", "b", "a", spost="2", tpost="1", at=T),
        interaction("mention", "c", "b", spost="3", at=T),
    ])
    run_build(cfg, store, log)
    run_analyze(cfg, store, log)
    rows = {r["user_id"]: dict(r) for r in store.q("SELECT * FROM nodes")}
    assert rows["ego"]["x"] == 0.0 and rows["ego"]["y"] == 0.0
    assert rows["b"]["betweenness"] > 0  # c -> b -> a
    assert all(r["pagerank"] is not None for u, r in rows.items() if u != "ego")
    assert store.stale_downstream("analyze") == []
