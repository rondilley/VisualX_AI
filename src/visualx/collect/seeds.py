"""Seed accounts for collection (FR-07, AD-04 step 6).

The seed accounts are the owner's top contacts by tie weight. Step 6 of the
plan collects their recent posts, so that links between the owner's contacts
(alter-alter edges) go into the store. Without them the map is a star: each
edge touches the owner, and communities and the coordination detectors have
little to work on.

This is part of the owner's map, not a mode to map another account (FR-54):
the seeds come only from the owner's own interactions in the store, and the
plan has no parameter for a target account.

The tie weight is the build-stage value (AD-06): the edge weight
``sum alpha_t * (1 + ln count_t)`` with the reciprocity factor for a pair
with edges in both directions, and for each contact the largest pair weight
of an edge to or from the owner (``nodes.ego_weight``).

A contact is not a seed when:

- it is the owner;
- its ID is not a numeric X user ID (for example a provisional ``h:<handle>``
  ID from the Grok source), because the timeline endpoint needs an X ID;
- its stored profile says ``protected``, because the endpoint gives no posts
  of a protected account to a non-follower;
- a profile lookup for it failed (``profile_errors``: not found or suspended).

Contacts on the allowlist can be seeds. The allowlist changes the signal
state of an account (score stage); it does not change what the collector
reads, and the posts of a trusted contact give useful alter-alter edges.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..build import aggregate, edge_weight
from ..config import Config
from ..model import INTERACTION_TYPES
from ..store import Store


@dataclass(frozen=True)
class Seed:
    user_id: str
    weight: float
    handle: str | None


def tie_weights(cfg: Config, store: Store, ego: str) -> dict[str, float]:
    """Tie weight to the owner for each contact (the build-stage ``ego_weight``)."""
    alpha = {t: float(cfg.get(f"weights.{t}")) for t in INTERACTION_TYPES}
    recip = float(cfg.get("weights.reciprocity_factor", 1.0))
    rows = [
        dict(r)
        for r in store.q(
            "SELECT type, source_user_id, target_user_id, interaction_created_at FROM interactions "
            "WHERE source_user_id=? OR target_user_id=?",
            (ego, ego),
        )
    ]
    edges = aggregate(rows)
    w = {k: edge_weight(v["counts"], alpha) for k, v in edges.items()}
    out: dict[str, float] = {}
    for (s, t), wst in w.items():
        wts = w.get((t, s), 0.0)
        pw = (wst + wts) * recip if wts > 0 else wst
        other = t if s == ego else s
        if other == ego:
            continue
        out[other] = max(out.get(other, 0.0), pw)
    return out


def select_seeds(cfg: Config, store: Store, ego: str | None, n: int) -> list[Seed]:
    """The top ``n`` contacts by tie weight. Ties go to the lower numeric user ID."""
    if not ego or n <= 0:
        return []
    weights = tie_weights(cfg, store, ego)
    protected = {r["user_id"] for r in store.q("SELECT user_id FROM profiles WHERE protected=1")}
    failed = {r["user_id"] for r in store.q("SELECT user_id FROM profile_errors")}
    handles = {r["user_id"]: r["handle"] for r in store.q("SELECT user_id, handle FROM profiles")}
    ok = [
        (uid, wt)
        for uid, wt in weights.items()
        if uid != ego and uid.isdigit() and wt > 0 and uid not in protected and uid not in failed
    ]
    ok.sort(key=lambda x: (-x[1], int(x[0])))
    return [Seed(uid, wt, handles.get(uid)) for uid, wt in ok[:n]]
