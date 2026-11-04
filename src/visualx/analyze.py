"""Analyze stage: metrics with the ego removed, the layout (ARCHITECTURE AD-07), and the
groups of ``groups.py`` (Leiden communities with stability, hubs, bridges, topics; FR-26
to FR-28, FR-30) in the ``node_groups`` table.

igraph calculates PageRank, weighted betweenness, and the Fruchterman-Reingold
layout. rustworkx has no weighted node betweenness, and FR-25 requires
distance = 1/w, so igraph does all three (about 1-5 s at 10k nodes, research §4
section 9).
"""

from __future__ import annotations

import math
import random
from typing import Any

import igraph as ig

from .config import Config
from .groups import run_groups
from .logs import RunLog
from .model import dumps
from .store import Store


def _ego_removed(nodes: list[str], edges: list[dict[str, Any]], ego: str) -> tuple[ig.Graph, list[str]]:
    alters = [n for n in nodes if n != ego]
    idx = {u: i for i, u in enumerate(alters)}
    es = [(idx[e["source"]], idx[e["target"]]) for e in edges if e["source"] != ego and e["target"] != ego]
    ws = [e["pair_weight"] for e in edges if e["source"] != ego and e["target"] != ego]
    g = ig.Graph(n=len(alters), edges=es, directed=True)
    g.es["weight"] = ws
    g.es["distance"] = [1.0 / w if w > 0 else math.inf for w in ws]
    return g, alters


def metrics(nodes: list[str], edges: list[dict[str, Any]], ego: str) -> dict[str, dict[str, float]]:
    """Degree (with ego), weighted degree (with ego), PageRank and betweenness (ego removed)."""
    out: dict[str, dict[str, float]] = {u: {"in_degree": 0, "out_degree": 0, "weighted_degree": 0.0} for u in nodes}
    nbrs: dict[str, set[str]] = {u: set() for u in nodes}
    for e in edges:
        s, t, w = e["source"], e["target"], e["weight"]
        out[s]["out_degree"] += 1
        out[t]["in_degree"] += 1
        out[s]["weighted_degree"] += w
        out[t]["weighted_degree"] += w
        nbrs[s].add(t)
        nbrs[t].add(s)
    for u in nodes:
        out[u]["degree"] = len(nbrs[u])

    g, alters = _ego_removed(nodes, edges, ego)
    if g.vcount():
        pr = g.pagerank(weights="weight" if g.ecount() else None, directed=True)
        bt = g.betweenness(directed=True, weights="distance" if g.ecount() else None)
    else:
        pr, bt = [], []
    for i, u in enumerate(alters):
        out[u]["pagerank"] = float(pr[i])
        out[u]["betweenness"] = float(bt[i])
    out[ego]["pagerank"] = None  # the ego is not part of the ego-removed metrics
    out[ego]["betweenness"] = None
    return out


def layout(nodes: list[str], edges: list[dict[str, Any]], ego: str, ego_weight: dict[str, float],
           seed: int, iterations: int) -> dict[str, tuple[float, float]]:
    """AD-07 ego layout procedure (FR-55)."""
    g, alters = _ego_removed(nodes, edges, ego)
    pos: dict[str, tuple[float, float]] = {ego: (0.0, 0.0)}
    if not alters:
        return pos
    ug = g.as_undirected(combine_edges="sum") if g.ecount() else g.as_undirected()
    rng = random.Random(seed)
    # igraph draws from its own generator. A start-position matrix turns off the
    # grid speed-up (about 100 s at 10k nodes), so set the generator instead.
    ig.set_random_number_generator(random.Random(seed))
    try:
        lay = ug.layout_fruchterman_reingold(
            weights="weight" if ug.ecount() else None, niter=iterations, grid="auto"
        )
    finally:
        ig.set_random_number_generator(random)
    xs = [p[0] for p in lay.coords]
    ys = [p[1] for p in lay.coords]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    pts = [(x - cx, y - cy) for x, y in zip(xs, ys)]
    radius = max((math.hypot(x, y) for x, y in pts), default=1.0) or 1.0
    max_lw = max((math.log1p(ego_weight.get(u, 0.0)) for u in alters), default=0.0)
    for (x, y), u in zip(pts, alters):
        r = math.hypot(x, y)
        if r == 0:
            ang = rng.uniform(0, 2 * math.pi)
            x, y, r = math.cos(ang), math.sin(ang), 1.0
        # Strong ties to the ego sit closer: scale factor from 1.0 (no tie) down to 0.3.
        tie = math.log1p(ego_weight.get(u, 0.0)) / max_lw if max_lw > 0 else 0.0
        factor = 1.0 - 0.7 * tie
        # Keep a minimum distance from the ego so nodes do not cover it.
        target = max(r / radius, 0.15) * factor * 1000.0
        pos[u] = (x / r * target, y / r * target)
    return pos


def run_analyze(cfg: Config, store: Store, log: RunLog, run_id: str | None = None) -> dict[str, Any]:
    inputs = store.check_fresh("analyze")
    ego = store.ego_id
    if not ego:
        raise RuntimeError("The store has no owner account ID.")
    nodes = [r["user_id"] for r in store.q("SELECT user_id FROM nodes ORDER BY user_id")]
    edges = [dict(r) for r in store.q("SELECT source, target, weight, pair_weight FROM edges")]
    ego_weight = {r["user_id"]: r["ego_weight"] or 0.0 for r in store.q("SELECT user_id, ego_weight FROM nodes")}
    m = metrics(nodes, edges, ego)
    pos = layout(nodes, edges, ego, ego_weight, int(cfg.get("analyze.random_seed", 42)),
                 int(cfg.get("analyze.layout_iterations", 500)))
    descriptions = {u: p.get("description") for u, p in store.profiles().items()}
    groups, gcounts = run_groups(nodes, edges, ego, descriptions,
                                 cfg.topics, cfg.get("analyze.groups", {}) or {})
    sid = store.begin_stage("analyze", run_id, inputs, cfg.hash())
    with store.tx() as c:
        c.execute("DELETE FROM node_groups")
        for u in nodes:
            gr = groups[u]
            c.execute(
                "INSERT INTO node_groups VALUES (?,?,?,?,?,?,?,?,?,?)",
                (u, gr["community"], gr["community_size"], gr["stability"], gr["is_hub"], gr["is_bridge"],
                 gr["bridge_reason"], gr["bridge_communities"], dumps(gr["topics"]), sid),
            )
        for u in nodes:
            mm = m[u]
            x, y = pos[u]
            c.execute(
                """UPDATE nodes SET in_degree=?, out_degree=?, degree=?, weighted_degree=?,
                pagerank=?, betweenness=?, x=?, y=? WHERE user_id=?""",
                (mm["in_degree"], mm["out_degree"], mm["degree"], mm["weighted_degree"],
                 mm["pagerank"], mm["betweenness"], x, y, u),
            )
        alter_edges = sum(1 for e in edges if e["source"] != ego and e["target"] != ego)
        counts = {"nodes": len(nodes), "edges": len(edges), "alter_edges": alter_edges, **gcounts}
        store.finish_stage(sid, counts, conn=c)
    log.write("stage", stage="analyze", counts=counts)
    return counts
