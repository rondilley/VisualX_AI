"""AD-10, DR-04, TSC-09: deletion of a user reaches the pattern findings."""

from __future__ import annotations

import json

from tests.unit.test_patterns import POST, burst_fixture, fast_fixture, run, uid
from tests.unit.test_rules_kit import EGO
from visualx.cache import Cache
from visualx.delete import delete_ids, find_id


def rows(store) -> dict[str, dict]:
    out = {}
    for r in store.q("SELECT * FROM findings"):
        d = dict(r)
        d["members"] = json.loads(d["members_json"])
        d["evidence"] = json.loads(d["evidence_json"])
        out[d["detector"]] = d
    return out


def test_deleted_user_leaves_group_finding_and_single_finding(tmp_path):
    profs, items, posts = burst_fixture(5, 2, young=True)
    fprofs, fitems, fposts = fast_fixture(4, 4)
    s = run(tmp_path, profs, items + fitems, posts + fposts)
    before = rows(s.store)
    assert len(before["reply_burst"]["members"]) == 5
    assert before["fast_reply"]["members"] == [uid(0)]
    # uid(0) also sent 5 contacts with no answer: a one-way finding about it.
    assert before["one_way"]["members"] == [uid(0)]
    cache = Cache(s.store, tmp_path / "cache")

    # uid(0) is in the burst (5 members) and is the subject of the fast-reply finding.
    rep = delete_ids(s.store, cache, tmp_path / "config", [uid(0)], "user")
    after = rows(s.store)
    assert "fast_reply" not in after and "one_way" not in after
    assert sorted(after["reply_burst"]["members"]) == [uid(k) for k in range(1, 5)]
    assert uid(0) not in after["reply_burst"]["evidence_json"]
    assert rep.findings_changed == 1 and rep.findings_removed == 2
    assert find_id(s.store, cache, uid(0)) == []


def test_group_finding_with_one_member_left_is_deleted(tmp_path):
    from tests.unit.test_patterns import acct
    from tests.unit.test_rules_kit import interaction

    profs = [acct(0, handle="ron_dilley"), acct(1)]
    items = [interaction(src=uid(0)), interaction(src=uid(1), tgt=uid(0), type="mention")]
    s = run(tmp_path, profs, items)
    assert rows(s.store)["lookalike"]["members"] == [uid(0), EGO]
    cache = Cache(s.store, tmp_path / "cache")
    rep = delete_ids(s.store, cache, tmp_path / "config", [uid(0)], "user")
    assert "lookalike" not in rows(s.store)
    assert rep.findings_removed == 1
    assert find_id(s.store, cache, uid(0)) == []


def test_deleted_user_in_evidence_only_is_removed(tmp_path):
    # uid(1) interacted with the lookalike: its ID is in the evidence, not in the members.
    from tests.unit.test_patterns import acct
    from tests.unit.test_rules_kit import interaction

    profs = [acct(0, handle="ron_dilley"), acct(1)]
    items = [interaction(src=uid(0)), interaction(src=uid(1), tgt=uid(0), type="mention")]
    s = run(tmp_path, profs, items)
    cache = Cache(s.store, tmp_path / "cache")
    delete_ids(s.store, cache, tmp_path / "config", [uid(1)], "user")
    f = rows(s.store)["lookalike"]
    assert f["members"] == [uid(0), EGO]
    assert f["evidence"]["interacting_accounts"] == []
    assert find_id(s.store, cache, uid(1)) == []


def test_deleted_post_is_redacted_in_findings(tmp_path):
    profs, items, posts = burst_fixture(4, 2, young=True)
    s = run(tmp_path, profs, items, posts)
    assert rows(s.store)["reply_burst"]["evidence"]["anchor_post_id"] == POST
    cache = Cache(s.store, tmp_path / "cache")
    delete_ids(s.store, cache, tmp_path / "config", [POST], "post")
    assert find_id(s.store, cache, POST) == []
