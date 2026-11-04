"""Snapshots and the changes between runs (snapshot.py, VISION §5)."""

from __future__ import annotations

import json

from tests.helpers import mark_stage
from tests.unit.test_v02 import EGO, pipeline
from visualx import snapshot as snap
from visualx.delete import decode_payloads, delete_ids, find_id
from visualx.render import run_render


def add_finding(store, fid: str, members: list[str]) -> None:
    store.conn.execute("INSERT INTO findings VALUES (?,?,?,?,?,?,?,?)",
                       (fid, "creation_cluster", "Accounts made in the same hours", "notable", "A summary.",
                        json.dumps(members), "{}", "s"))


def second_run(store) -> None:
    """Change the stage outputs as a new collection would, then mark new analyze and score runs."""
    c = store.conn
    c.execute("UPDATE node_states SET state='some_signals', total_points=3 WHERE user_id='20'")
    c.execute("INSERT INTO rule_results VALUES ('20','B10','behavior',2,'{}','A cause',0,'s')")
    c.execute("UPDATE nodes SET ego_weight = ego_weight + 4 WHERE user_id='21'")
    c.execute("DELETE FROM nodes WHERE user_id='25'")
    c.execute("DELETE FROM edges WHERE source='25' OR target='25'")
    c.execute("DELETE FROM node_states WHERE user_id='25'")
    c.execute("INSERT INTO nodes(user_id, handle, name, profile_status, is_ego, x, y, stage_run_id) "
              "VALUES ('40','acct40','Name 40','present',0,1.0,1.0,'s')")
    # Leiden numbers the groups again: swap the two community numbers (no account moved) ...
    c.execute("UPDATE node_groups SET community = CASE community WHEN 0 THEN 1 WHEN 1 THEN 0 END "
              "WHERE community IS NOT NULL")
    # ... and move one account to the other group.
    other = c.execute("SELECT community FROM node_groups WHERE user_id='20'").fetchone()[0]
    c.execute("UPDATE node_groups SET community=? WHERE user_id='13'", (other,))
    add_finding(store, "f_new", ["20", "21", "22"])
    mark_stage(store, "analyze")
    mark_stage(store, "score")


def test_snapshot_diff_finds_each_kind_of_change(tmp_path):
    cfg, store, cache, log = pipeline(tmp_path)
    add_finding(store, "f_old", ["10", "11", "12"])
    a = snap.take(store)
    assert snap.take(store) == a  # same analyze and score outputs: the same snapshot
    second_run(store)
    b = snap.take(store)
    assert b != a and snap.previous(store, b) == a
    d = snap.diff(store, a, b)
    u = d["users"]
    assert u["40"] == {"new": True}
    # The pipeline accounts have thin profiles, so gate N01 gives not_scored first.
    assert u["20"]["state"] == ["not_scored", "some_signals"] and u["20"]["rules_added"] == ["B10"]
    assert u["21"]["tie"] == 4.0
    assert u["13"]["moved"] is True
    assert not any(u.get(x, {}).get("moved") for x in ("10", "11", "22", "23"))  # renumbered, not moved
    assert d["gone"] == ["25"]
    c = d["counts"]
    assert (c["new_accounts"], c["gone_accounts"], c["community_moves"], c["new_findings"]) == (1, 1, 1, 1)
    assert c["state_transitions"] == {"not_scored->some_signals": 1}
    # The snapshot IDs hold the clock time, which can contain "13" or "25".
    text = snap.diff_text(d, "a", "b").replace(a, "").replace(b, "")
    for x in ("acct", "Name ", '"20"', " 20 ", "13", "25"):
        assert x not in text  # counts only (invariant 8)


def test_the_map_shows_the_changes_and_the_redacted_map_does_not(tmp_path):
    cfg, store, cache, log = pipeline(tmp_path)
    p1 = run_render(cfg, store, log)
    d1 = decode_payloads(p1.read_text(encoding="utf-8"))[0]
    assert d1["meta"]["changes"] is None  # no earlier snapshot
    second_run(store)
    p2 = run_render(cfg, store, log)
    d2 = decode_payloads(p2.read_text(encoding="utf-8"))[0]
    ch = d2["meta"]["changes"]
    assert ch["counts"]["new_accounts"] == 1 and ch["base_at"]
    ids = d2["n"]["id"]
    assert d2["n"]["cn"][ids.index("40")] == 1
    assert d2["n"]["cps"][ids.index("20")] == 0  # index of not_scored
    assert d2["n"]["cra"][ids.index("20")] == ["B10"]
    assert d2["n"]["ctw"][ids.index("21")] == 4
    new = [f for f in d2["f"] if f["new"] == 1]
    assert len(new) == 1 and new[0]["id"] == "f_new"  # the score stage findings were there before
    assert all("_k" not in f for f in d2["f"])
    # A redacted render of the same outputs uses the same snapshot and shows no change.
    n_snap = len(snap.list_snapshots(store))
    red = run_render(cfg, store, log, redact=True)
    dr = decode_payloads(red.read_text(encoding="utf-8"))[0]
    assert dr["meta"]["changes"] is None and set(dr["n"]["cn"]) == {None}
    assert len(snap.list_snapshots(store)) == n_snap


def test_deletion_removes_the_user_from_every_snapshot(tmp_path):
    cfg, store, cache, log = pipeline(tmp_path)
    add_finding(store, "f_old", ["20", "21", "22"])
    add_finding(store, "f_two", ["20", "23"])
    snap.take(store)
    second_run(store)
    snap.take(store)
    delete_ids(store, cache, cfg.config_dir, ["20"], "user")
    assert store.q("SELECT * FROM snapshot_nodes WHERE user_id='20'") == []
    rows = [json.loads(r["members_json"]) for r in store.q("SELECT members_json FROM snapshot_findings")]
    assert all("20" not in m for m in rows) and ["21", "22"] in rows
    assert ["23"] not in rows  # a group finding with one member left is deleted
    assert find_id(store, cache, "20") == []
    assert store.q("SELECT * FROM snapshot_nodes WHERE user_id=?", (EGO,))
