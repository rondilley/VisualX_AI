"""Build stage: interactions -> nodes and edges (ARCHITECTURE AD-06, FR-17 to FR-23).

One directed edge for each (source, target) pair, with a count and the known
timestamps for each interaction type. The adapter already gave each post one
type for each target (FR-21); this stage aggregates and weights.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Any

from .config import Config
from .logs import RunLog
from .model import INTERACTION_TYPES, dumps
from .store import Store


def _count_term(c: float) -> float:
    """1 + ln(c) for c >= 1, and c below 1. The two parts meet at c = 1 with the same slope."""
    return 1.0 + math.log(c) if c >= 1.0 else c


def edge_weight(counts: dict[str, float], alpha: dict[str, float]) -> float:
    """w(u->v) = sum over types of alpha_t * (1 + ln(count_t)) for count_t > 0.

    With recency decay (FR-20) count_t is the sum of the decay factors, and a sum
    below 1 counts linearly (``_count_term``). With no decay the formula is unchanged.
    """
    return sum(alpha[t] * _count_term(c) for t, c in counts.items() if c > 0)


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else None


def decayed_counts(e: dict[str, Any], ref: datetime | None, half_life_days: float) -> dict[str, float]:
    """Counts for each type with the FR-20 decay. An interaction with no time counts 1 (DR-02)."""
    if half_life_days <= 0 or ref is None:
        return {t: float(c) for t, c in e["counts"].items()}
    out: dict[str, float] = {}
    for t, c in e["counts"].items():
        timed = [d for d in (_parse(x) for x in e["times"].get(t, [])) if d is not None]
        untimed = c - len(timed)
        out[t] = untimed + sum(0.5 ** (max(0.0, (ref - d).total_seconds()) / 86400.0 / half_life_days) for d in timed)
    return out


def aggregate(interactions: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for it in interactions:
        s, t = it["source_user_id"], it["target_user_id"]
        if s == t:
            continue
        e = edges.get((s, t))
        if e is None:
            e = {"counts": dict.fromkeys(INTERACTION_TYPES, 0), "times": defaultdict(list)}
            edges[(s, t)] = e
        e["counts"][it["type"]] += 1
        if it.get("interaction_created_at"):
            e["times"][it["type"]].append(it["interaction_created_at"])
    return edges


def run_build(cfg: Config, store: Store, log: RunLog, run_id: str | None = None) -> dict[str, int]:
    inputs = store.check_fresh("build")
    merged = store.merge_provisional()  # Grok h:<handle> users that now have an X profile
    if merged:
        log.write("stage", stage="build", provisional_ids_merged=merged)
    ego = store.ego_id
    if not ego:
        raise RuntimeError("The store has no owner account ID. Run 'visualx collect' first.")
    alpha = {t: float(cfg.get(f"weights.{t}")) for t in INTERACTION_TYPES}
    recip = float(cfg.get("weights.reciprocity_factor", 1.0))

    interactions = store.interactions()
    profiles = store.profiles()
    edges = aggregate(interactions)

    half_life = float(cfg.get("weights.half_life_days", 0) or 0)
    if half_life < 0 or not math.isfinite(half_life):
        raise ValueError("weights.half_life_days must be 0 (off) or more")
    stamps = [d for d in (_parse(i["interaction_created_at"]) for i in interactions
                          if i.get("interaction_created_at")) if d is not None]
    ref = max(stamps) if stamps else None  # the newest interaction, so a rebuild gives the same weights
    w = {k: edge_weight(decayed_counts(v, ref, half_life), alpha) for k, v in edges.items()}
    pair: dict[tuple[str, str], float] = {}
    for (s, t), wst in w.items():
        wts = w.get((t, s), 0.0)
        pair[(s, t)] = (wst + wts) * recip if wts > 0 else wst

    users: set[str] = {ego}
    for s, t in edges:
        users.add(s)
        users.add(t)
    ego_w: dict[str, float] = defaultdict(float)
    for (s, t), pw in pair.items():
        if s == ego:
            ego_w[t] = max(ego_w[t], pw)
        elif t == ego:
            ego_w[s] = max(ego_w[s], pw)

    sid = store.begin_stage("build", run_id, inputs, cfg.hash())
    missing = 0
    with store.tx() as c:
        c.execute("DELETE FROM nodes")
        c.execute("DELETE FROM edges")
        for u in sorted(users):
            p = profiles.get(u)
            status = "present" if p is not None else "missing"
            if p is None and u != ego:
                missing += 1
            c.execute(
                "INSERT INTO nodes(user_id, handle, name, profile_status, is_ego, ego_weight, stage_run_id) VALUES (?,?,?,?,?,?,?)",
                (u, p["handle"] if p else None, p["name"] if p else None, status, int(u == ego), ego_w.get(u, 0.0), sid),
            )
        for (s, t), e in sorted(edges.items()):
            cnt = e["counts"]
            c.execute(
                "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?,?)",
                (s, t, cnt["reply"], cnt["quote"], cnt["mention"], cnt["repost"],
                 dumps({k: sorted(v) for k, v in e["times"].items()}), w[(s, t)], pair[(s, t)], sid),
            )
        counts = {
            "interactions": len(interactions),
            "nodes": len(users),
            "edges": len(edges),
            "missing_profiles": missing,
            "half_life_days": half_life,
            **{f"{t}_interactions": sum(1 for i in interactions if i["type"] == t) for t in INTERACTION_TYPES},
        }
        store.finish_stage(sid, counts, conn=c)
    log.write("stage", stage="build", counts=counts)
    return counts
