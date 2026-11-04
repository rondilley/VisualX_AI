"""Snapshots: what changed in the network between two runs (VISION §5, v0.2).

Each render takes a snapshot of the stage outputs that it shows: for each account
the signal state, the total points, the rules that fired, the community, and the
tie weights, and for each finding its detector and members. A render of the same
analyze and score outputs uses the snapshot that is already there.

``diff`` compares two snapshots:

* **new** accounts and accounts that are **gone**,
* **state** changes (for example ``no_signals`` to ``some_signals``),
* **community moves**: the community numbers of two Leiden runs are not the same
  numbers, so each new community is matched to the earlier community that holds
  most of its accounts. An account moved if its earlier community is not the match,
* **tie weight** changes to the owner,
* **new findings**: a finding whose detector and members were not in the earlier
  snapshot.

The snapshot tables hold signal states, so the same limits apply as for the score
tables (invariant 8, BSC-05): the details go only into the private HTML file, and
``visualx snapshot diff`` prints counts only. Deletion removes a user from every
snapshot (AD-10).
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .model import dumps
from .store import Store, new_id, now_iso


class SnapshotError(RuntimeError):
    pass


def _latest(store: Store, stage: str) -> str | None:
    row = store.latest_stage_run(stage)
    return None if row is None else row["stage_run_id"]


def take(store: Store) -> str:
    """Record a snapshot of the newest analyze and score outputs. Return its ID.

    If the newest snapshot has the same analyze and score stage runs, return its ID.
    """
    analyze, score = _latest(store, "analyze"), _latest(store, "score")
    if analyze is None or score is None:
        raise SnapshotError("a snapshot needs analyze and score outputs")
    old = store.q1("SELECT snapshot_id FROM snapshots WHERE analyze_stage=? AND score_stage=?", (analyze, score))
    if old is not None:
        return old["snapshot_id"]
    collect = store.latest_stage_run("collect")
    sid = new_id("snap")
    rules: dict[str, list[str]] = {}
    for r in store.q("SELECT user_id, rule_id FROM rule_results ORDER BY user_id, rule_id"):
        rules.setdefault(r["user_id"], []).append(r["rule_id"])
    groups = {r["user_id"]: r["community"] for r in store.q("SELECT user_id, community FROM node_groups")}
    states = {r["user_id"]: r for r in store.q("SELECT user_id, state, total_points FROM node_states")}
    nodes = store.q("SELECT user_id, weighted_degree, ego_weight FROM nodes")
    with store.tx() as c:
        c.execute(
            "INSERT INTO snapshots(snapshot_id, created_at, run_id, analyze_stage, score_stage, counts_json) "
            "VALUES (?,?,?,?,?,?)",
            (sid, now_iso(), collect["run_id"] if collect is not None else None, analyze, score,
             dumps({"accounts": len(nodes)})),
        )
        for n in nodes:
            u = n["user_id"]
            st = states.get(u)
            c.execute(
                "INSERT INTO snapshot_nodes VALUES (?,?,?,?,?,?,?,?)",
                (sid, u, st["state"] if st else "not_scored", st["total_points"] if st else None,
                 groups.get(u), n["weighted_degree"], n["ego_weight"], dumps(rules.get(u, []))),
            )
        for f in store.q("SELECT detector, severity, members_json FROM findings"):
            try:
                members = sorted(str(m) for m in json.loads(f["members_json"] or "[]"))
            except json.JSONDecodeError:
                members = []
            c.execute("INSERT INTO snapshot_findings VALUES (?,?,?,?)",
                      (sid, f["detector"], f["severity"], dumps(members)))
    return sid


def list_snapshots(store: Store) -> list[dict[str, Any]]:
    out = []
    for r in store.q("SELECT s.snapshot_id, s.created_at, s.run_id, COUNT(n.user_id) AS accounts FROM snapshots s "
                     "LEFT JOIN snapshot_nodes n USING(snapshot_id) GROUP BY s.snapshot_id ORDER BY s.created_at"):
        out.append(dict(r))
    return out


def previous(store: Store, sid: str) -> str | None:
    """The snapshot before ``sid`` (by time), or None."""
    row = store.q1("SELECT created_at FROM snapshots WHERE snapshot_id=?", (sid,))
    if row is None:
        return None
    p = store.q1("SELECT snapshot_id FROM snapshots WHERE created_at < ? OR (created_at = ? AND snapshot_id < ?) "
                 "ORDER BY created_at DESC, snapshot_id DESC LIMIT 1", (row["created_at"], row["created_at"], sid))
    return None if p is None else p["snapshot_id"]


def _nodes(store: Store, sid: str) -> dict[str, dict[str, Any]]:
    return {r["user_id"]: dict(r) for r in store.q("SELECT * FROM snapshot_nodes WHERE snapshot_id=?", (sid,))}


def _findings(store: Store, sid: str) -> set[tuple[str, str]]:
    return {(r["detector"], r["members_json"]) for r in
            store.q("SELECT detector, members_json FROM snapshot_findings WHERE snapshot_id=?", (sid,))}


def community_match(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> dict[int, int | None]:
    """For each new community: the earlier community that holds most of its accounts (or None)."""
    votes: dict[int, Counter[int]] = {}
    for u, n in new.items():
        c = n["community"]
        o = old.get(u, {}).get("community")
        if c is None:
            continue
        votes.setdefault(c, Counter())
        if o is not None:
            votes[c][o] += 1
    return {c: (v.most_common(1)[0][0] if v else None) for c, v in votes.items()}


def diff(store: Store, base: str, head: str, min_tie_change: float = 1.0) -> dict[str, Any]:
    """Changes from snapshot ``base`` to snapshot ``head``. Per-user details and counts."""
    old, new = _nodes(store, base), _nodes(store, head)
    if not new:
        raise SnapshotError(f"snapshot {head} has no accounts")
    match = community_match(old, new)
    users: dict[str, dict[str, Any]] = {}
    for u, n in new.items():
        o = old.get(u)
        if o is None:
            users[u] = {"new": True}
            continue
        ch: dict[str, Any] = {}
        if o["state"] != n["state"]:
            ch["state"] = [o["state"], n["state"]]
        if n["community"] is not None and o["community"] is not None and match.get(n["community"]) != o["community"]:
            ch["moved"] = True
        dw = (n["ego_weight"] or 0.0) - (o["ego_weight"] or 0.0)
        if abs(dw) >= min_tie_change:
            ch["tie"] = round(dw, 3)
        added = sorted(set(json.loads(n["rules_json"] or "[]")) - set(json.loads(o["rules_json"] or "[]")))
        if added:
            ch["rules_added"] = added
        if ch:
            users[u] = ch
    gone = sorted(set(old) - set(new))
    new_f = sorted(_findings(store, head) - _findings(store, base))
    transitions = Counter(f"{c['state'][0]}->{c['state'][1]}" for c in users.values() if "state" in c)
    counts = {
        "accounts_base": len(old), "accounts_head": len(new),
        "new_accounts": sum(1 for c in users.values() if c.get("new")), "gone_accounts": len(gone),
        "state_changes": sum(transitions.values()), "community_moves": sum(1 for c in users.values() if c.get("moved")),
        "tie_stronger": sum(1 for c in users.values() if c.get("tie", 0) > 0),
        "tie_weaker": sum(1 for c in users.values() if c.get("tie", 0) < 0),
        "new_findings": len(new_f),
        "new_findings_by_detector": dict(Counter(d for d, _ in new_f)),
        "state_transitions": dict(sorted(transitions.items())),
    }
    return {"base": base, "head": head, "users": users, "gone": gone, "new_findings": new_f, "counts": counts}


def diff_text(d: dict[str, Any], base_at: str, head_at: str) -> str:
    """The counts only: no handle, ID, or state of a person (invariant 8)."""
    c = d["counts"]
    lines = [f"Changes from {d['base']} ({base_at}) to {d['head']} ({head_at})",
             f"  accounts:          {c['accounts_base']} -> {c['accounts_head']}",
             f"  new accounts:      {c['new_accounts']}",
             f"  gone accounts:     {c['gone_accounts']}",
             f"  state changes:     {c['state_changes']}"]
    lines += [f"    {k}: {v}" for k, v in c["state_transitions"].items()]
    lines += [f"  community moves:   {c['community_moves']}",
              f"  tie to you:        {c['tie_stronger']} stronger, {c['tie_weaker']} weaker",
              f"  new findings:      {c['new_findings']}"]
    lines += [f"    {k}: {v}" for k, v in sorted(c["new_findings_by_detector"].items())]
    lines.append("Open the map (Overview, Changes) for the accounts.")
    return "\n".join(lines)
