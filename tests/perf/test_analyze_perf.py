"""TSC-12 and TSC-13: analyze and layout on a synthetic 10k-node, 50k-edge graph (slow).

Synthetic data proves the time limit only. The live run must record its own timings (TR-05).
"""

from __future__ import annotations

import random
import time

import pytest

from visualx.analyze import layout, metrics


def synthetic(n: int = 10_000, m: int = 50_000, seed: int = 7):
    rng = random.Random(seed)
    nodes = ["ego"] + [f"u{i}" for i in range(n - 1)]
    edges = {}
    alters = nodes[1:]
    for u in alters[: n // 2]:
        edges[(u, "ego")] = rng.uniform(1, 10)
    while len(edges) < m:
        # preferential-ish: half the targets from a small hub set
        s = rng.choice(alters)
        t = rng.choice(alters[:500]) if rng.random() < 0.5 else rng.choice(alters)
        if s != t:
            edges[(s, t)] = rng.uniform(1, 10)
    es = [{"source": s, "target": t, "weight": w, "pair_weight": w} for (s, t), w in edges.items()]
    ego_w = {s: w for (s, t), w in edges.items() if t == "ego"}
    return nodes, es, ego_w


@pytest.mark.slow
def test_analyze_and_layout_10k():
    nodes, edges, ego_w = synthetic()
    t0 = time.perf_counter()
    metrics(nodes, edges, "ego")
    t_metrics = time.perf_counter() - t0
    t0 = time.perf_counter()
    layout(nodes, edges, "ego", ego_w, seed=42, iterations=500)
    t_layout = time.perf_counter() - t0
    print(f"\nTSC-12 metrics: {t_metrics:.1f} s, TSC-13 layout: {t_layout:.1f} s ({len(nodes)} nodes, {len(edges)} edges)")
    assert t_metrics < 120
    assert t_layout < 120
