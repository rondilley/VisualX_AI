"""Signal engine: state bands, TSC-15, TSC-16, TSC-18, TSC-26, TSC-28, and the stage records."""

from __future__ import annotations

import copy
import json

import pytest
import yaml

from tests.unit.test_rules_kit import (
    ACC,
    EGO,
    RULES_PATH,
    default_rules,
    ego_profile,
    interaction,
    iso_days_before,
    make_store,
    profile,
    score,
)
from visualx.score.engine import RuleHit, compute_state, ruleset_from_data
from visualx.store import StaleError

T0 = "2026-10-01T12:00:00.000Z"


def hit(rid: str, family: str, points: float, hard: bool = False) -> RuleHit:
    return RuleHit(rid, family, points, "{}", "cause", hard)


# -- band logic (AD-08) -------------------------------------------------------


@pytest.mark.parametrize(
    "hits, gate, override, expected",
    [
        ([], False, None, "no_signals"),
        ([hit("P03", "profile", 1), hit("P07", "profile", 1)], False, None, "no_signals"),
        ([hit("P01", "profile", 3)], False, None, "some_signals"),
        ([hit("P01", "profile", 3), hit("P05", "profile", 2), hit("P06", "profile", 2)], False, None, "some_signals"),
        # 6 points, but only 1 behavior point (bands.many_signals_min_family_points = 3).
        ([hit("P01", "profile", 3), hit("P05", "profile", 2), hit("B05", "behavior", 1)], False, None, "some_signals"),
        ([hit("P01", "profile", 3), hit("P05", "profile", 2), hit("B01", "behavior", 3)], False, None, "many_signals"),
        ([hit("B01", "behavior", 3), hit("B07", "behavior", 3), hit("T03", "trust", -3)], False, None, "some_signals"),
        ([hit("I01", "impersonation", 6, hard=True), hit("T02", "trust", -2)], False, None, "many_signals"),
        ([hit("N01", "gate", 0)], True, None, "not_scored"),
        ([hit("N03", "gate", 0)], True, {"state": "many_signals"}, "not_scored"),
        ([hit("I01", "impersonation", 6, hard=True)], False, {"state": "no_signals"}, "no_signals"),
        ([], False, {"state": "some_signals"}, "some_signals"),
    ],
)
def test_state_bands(hits, gate, override, expected):
    state, _ = compute_state(default_rules(), hits, gate, override)
    assert state == expected


# -- overlap group: one fact counts one time (B09 and B10 on new accounts, 2026-11-03) ----------


def test_account_age_rules_count_up_to_the_group_maximum():
    rs = default_rules()
    new_in_burst = [hit("P01", "profile", 3), hit("B08", "behavior", 1), hit("B09", "behavior", 2)]
    assert compute_state(rs, new_in_burst, False, None) == ("some_signals", 5)
    new_in_cluster = [hit("P01", "profile", 3), hit("B08", "behavior", 1), hit("B10", "behavior", 2),
                      hit("B09", "behavior", 2)]
    assert compute_state(rs, new_in_cluster, False, None) == ("some_signals", 5)
    # One independent signal more gives many_signals.
    assert compute_state(rs, new_in_burst + [hit("B13", "behavior", 2)], False, None) == ("many_signals", 7)
    # A trust rule still reduces the total after the cap.
    assert compute_state(rs, new_in_burst + [hit("T03", "trust", -3)], False, None)[1] == 2
    # Below the maximum nothing changes.
    assert compute_state(rs, [hit("P01", "profile", 2), hit("B09", "behavior", 2)], False, None)[1] == 4


def test_many_signals_needs_two_behavior_points():
    rs = default_rules()
    assert rs.bands.many_signals_min_family_points == 2
    # Profile points and one 1-point behavior rule: some_signals (FR-35).
    assert compute_state(rs, [hit("P01", "profile", 3), hit("P04", "profile", 2), hit("P05", "profile", 2),
                              hit("B08", "behavior", 1)], False, None)[0] == "some_signals"
    # The age group adds only its largest behavior rule. A rule outside the group adds in full.
    from visualx.score.engine import family_points

    assert family_points(rs.bands, [hit("B08", "behavior", 1), hit("B10", "behavior", 2)]) == 2
    assert family_points(rs.bands, [hit("B08", "behavior", 1), hit("B13", "behavior", 2)]) == 3
    # A behavior signal from another fact gives many_signals.
    assert compute_state(rs, [hit("P01", "profile", 3), hit("B09", "behavior", 2), hit("B13", "behavior", 2)],
                         False, None) == ("many_signals", 7)
    assert compute_state(rs, [hit("B01", "behavior", 3), hit("P05", "profile", 3)], False, None)[0] == "many_signals"
    # A hard rule is not affected.
    assert compute_state(rs, [hit("I01", "impersonation", 6, hard=True)], False, None)[0] == "many_signals"


@pytest.mark.parametrize("overlap, msg", [
    ({"g": {"rules": ["P01"], "max_points": 3}}, "2 or more"),
    ({"g": {"rules": ["P01", "ZZ9"], "max_points": 3}}, "unknown rule"),
    ({"g": {"rules": ["P01", "B08"], "max_points": 3}, "h": {"rules": ["B08", "B09"], "max_points": 3}},
     "another group"),
    ({"g": {"rules": ["P01", "B08"]}}, "max_points"),
])
def test_bad_overlap_groups_are_refused(overlap, msg):
    data = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    data["bands"]["overlap"] = overlap
    with pytest.raises(Exception, match=msg):
        ruleset_from_data(data)


def test_trust_points_reduce_total(tmp_path):
    created = iso_days_before(T0, 3)
    items = [interaction(created_at=T0), interaction(src=EGO, tgt=ACC, type="reply")]
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=created)], interactions=items)
    assert s.fired() == {"P01", "T03"}
    assert s.states[ACC] == "no_signals"  # 3 - 3 = 0


# -- TSC-16: profile rules alone never give many_signals ----------------------


def test_profile_rules_alone_give_at_most_some_signals(tmp_path):
    created = iso_days_before(T0, 3)
    p = profile(created_at=created, followers_count=2, following_count=900, tweet_count=100000,
                description="", url="", location="")
    s = score(tmp_path, profiles=[ego_profile(), p], interactions=[interaction(created_at=T0)])
    assert {"P01", "P03", "P05", "P06"} <= s.fired()
    total = s.store.q1("SELECT total_points FROM node_states WHERE user_id=?", (ACC,))["total_points"]
    assert total >= 6
    assert s.states[ACC] == "some_signals"


def test_behavior_rule_with_total_six_gives_many_signals(tmp_path):
    created = iso_days_before(T0, 3)
    items = [interaction(created_at=T0, text="As an AI language model I like this airdrop")]
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=created)], interactions=items)
    assert {"P01", "B04", "B07"} <= s.fired()
    assert s.states[ACC] == "many_signals"


# -- TSC-15: each state has its rule results ----------------------------------


def test_each_state_has_its_rule_results(tmp_path):
    profiles = [ego_profile()]
    items = []
    for n in range(12):
        uid = str(300 + n)
        profiles.append(profile(uid, handle=f"acct{n}xyz", created_at=iso_days_before(T0, n * 4),
                                followers_count=n, following_count=50 * n, description="" if n % 3 else "Hello"))
        items.append(interaction(src=uid, created_at=T0, text="airdrop now" if n % 4 == 0 else f"text {n}"))
    items.append(interaction(src="999", created_at=T0))  # no profile: N01
    s = score(tmp_path, profiles=profiles, interactions=items)
    assert set(s.states) == {EGO, "999", *(str(300 + n) for n in range(12))}
    seen = set()
    for uid, state in s.states.items():
        seen.add(state)
        res = s.results.get(uid, {})
        if state in ("some_signals", "many_signals"):
            assert any(r["points"] > 0 or r["hard"] for r in res.values()), uid
        if state == "not_scored":
            assert any(r["family"] == "gate" for r in res.values()), uid
        for r in res.values():
            assert r["cause"] and r["value_json"] is not None
    assert {"not_scored", "some_signals"} <= seen


# -- TSC-18: owner overrides persist ------------------------------------------


def test_override_applies_and_persists_across_runs(tmp_path):
    created = iso_days_before(T0, 3)
    p = profile(created_at=created, followers_count=2, following_count=900)
    ov = [{"user_id": ACC, "state": "no_signals", "note": "Known colleague", "date": "2026-11-02"}]
    s1 = score(tmp_path, profiles=[ego_profile(), p], interactions=[interaction(created_at=T0)], overrides=ov)
    assert s1.states[ACC] == "no_signals"
    assert s1.notes[ACC] == "Known colleague"
    assert {"P01", "P05"} <= s1.fired()  # the rule results stay visible
    assert s1.summary["overrides_applied"] == 1
    # Second run with the same overrides.yaml: the override stays in effect.
    s2 = score(tmp_path, profiles=[], interactions=[], overrides=ov, store=s1.store)
    assert s2.states[ACC] == "no_signals"
    assert s2.notes[ACC] == "Known colleague"
    assert {"P01", "P05"} <= s2.fired()


def test_override_removed_from_yaml_stops_on_next_run(tmp_path):
    created = iso_days_before(T0, 3)
    p = profile(created_at=created, followers_count=2, following_count=900)
    ov = [
        {"user_id": ACC, "state": "no_signals", "note": "Known colleague"},
        {"user_id": "999", "state": "some_signals", "note": "Other"},
    ]
    s1 = score(tmp_path, profiles=[ego_profile(), p], interactions=[interaction(created_at=T0)], overrides=ov)
    assert s1.states[ACC] == "no_signals"
    # The owner removes the entry for ACC from overrides.yaml.
    s2 = score(tmp_path, profiles=[], interactions=[], overrides=ov[1:], store=s1.store)
    assert s2.states[ACC] == "some_signals"  # P01 3 + P05 2, computed again
    assert s2.notes[ACC] is None
    rows = {r["user_id"] for r in s2.store.q("SELECT user_id FROM overrides")}
    assert rows == {"999"}
    # An empty overrides.yaml removes all rows.
    s3 = score(tmp_path, profiles=[], interactions=[], overrides=[], store=s1.store)
    assert s3.store.q("SELECT user_id FROM overrides") == []
    assert s3.summary["overrides_applied"] == 0


def test_override_updates_from_config(tmp_path):
    ov1 = [{"user_id": ACC, "state": "some_signals", "note": "first"}]
    s1 = score(tmp_path, overrides=ov1)
    assert s1.states[ACC] == "some_signals"
    ov2 = [{"user_id": ACC, "state": "many_signals", "note": "second"}]
    s2 = score(tmp_path, profiles=[], interactions=[], overrides=ov2, store=s1.store)
    assert s2.states[ACC] == "many_signals"
    assert s2.notes[ACC] == "second"


# -- TSC-26: a threshold change in the rule file changes the states -----------


def test_threshold_change_in_rule_file_changes_state(tmp_path):
    with RULES_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    p = profile(followers_count=7, following_count=250, description="", url="", location="")
    s1 = score(tmp_path / "a", profiles=[ego_profile(), p], ruleset=ruleset_from_data(data))
    assert s1.fired() == {"P03", "P05"}
    assert s1.states[ACC] == "some_signals"

    changed = copy.deepcopy(data)
    for r in changed["rules"]:
        if r["id"] == "P05":
            r["params"]["followers_below"] = 5
    s2 = score(tmp_path / "b", profiles=[ego_profile(), p], ruleset=ruleset_from_data(changed))
    assert s2.fired() == {"P03"}
    assert s2.states[ACC] == "no_signals"


def test_band_change_in_rule_file_changes_state(tmp_path):
    with RULES_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["bands"]["some_signals_min"] = 1
    s = score(tmp_path, profiles=[ego_profile(), profile(description="", url="", location="")],
              ruleset=ruleset_from_data(data))
    assert s.fired() == {"P03"}
    assert s.states[ACC] == "some_signals"


def test_run_score_reads_rule_file_from_path(tmp_path):
    from visualx.logs import NullLog
    from visualx.score.engine import run_score
    from tests.unit.test_rules_kit import make_config

    with RULES_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["bands"]["some_signals_min"] = 1
    rp = tmp_path / "rules.yaml"
    rp.write_text(yaml.safe_dump(data), encoding="utf-8")
    st = make_store(tmp_path)
    st.put_profiles([ego_profile(), profile(description="", url="", location="")])
    st.put_interactions([interaction()])
    run_score(make_config(tmp_path), st, NullLog(), rules_path=rp)
    row = st.q1("SELECT state FROM node_states WHERE user_id=?", (ACC,))
    assert row["state"] == "some_signals"


# -- TSC-28: a missing input gives no result and one log entry ----------------


def test_missing_input_gives_no_result_one_skip_one_log(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(verified_type=None)])
    assert "T01" not in s.fired()
    rows = s.store.q("SELECT * FROM rule_skips WHERE user_id=? AND rule_id='T01'", (ACC,))
    assert len(rows) == 1 and rows[0]["missing_field"] == "verified_type"
    logs = [r for r in s.log.records if r["event"] == "rule_no_input" and r["rule_id"] == "T01"]
    assert len(logs) == 1
    assert logs[0]["missing_field"] == "verified_type"
    assert logs[0]["user_id"] != ACC  # the log holds an HMAC, not the raw ID
    assert s.summary["rules_no_input"] == 1


def test_unreadable_input_is_not_zero(tmp_path):
    # Unreadable counts must not turn into 0 and fire P07.
    s = score(tmp_path, profiles=[ego_profile(), profile(unreadable=("followers_count", "following_count"))])
    # N01 needs the public metrics, so the account is not scored.
    assert s.fired() == {"N01"}
    assert "P07" not in s.fired()


def test_missing_ego_user_id_skips_ego_rules(tmp_path):
    st = make_store(tmp_path)
    st.conn.execute("DELETE FROM meta WHERE key='ego_user_id'")
    s = score(tmp_path, store=st)
    assert s.skipped()["T03"] == {"ego_user_id"}
    assert s.skipped()["B03"] == {"ego_user_id"}
    assert s.skipped()["I01"] == {"ego_user_id"}


# -- stage records ------------------------------------------------------------


def test_stage_run_record_and_summary(tmp_path):
    s = score(tmp_path)
    row = s.store.latest_stage_run("score")
    assert row is not None and row["finished_at"]
    assert row["stage_run_id"] == s.summary["stage_run_id"]
    assert json.loads(row["inputs_json"])["build"].startswith("build_")
    counts = json.loads(row["counts_json"])
    assert counts["states"]["not_scored"] == 1 and counts["states"]["no_signals"] == 1
    assert s.summary["accounts"] == 2
    assert s.summary["rules_fired"] == 1  # N03 for the ego
    assert s.summary["rules_no_input"] == 0
    for table in ("rule_results", "node_states"):
        ids = {r["stage_run_id"] for r in s.store.q(f"SELECT stage_run_id FROM {table}")}
        assert ids == {row["stage_run_id"]}


def test_second_run_replaces_rows(tmp_path):
    s1 = score(tmp_path, profiles=[ego_profile(), profile(verified_type="business")])
    assert "T01" in s1.fired()
    s2 = score(tmp_path, profiles=[ego_profile(), profile(verified_type="none")], interactions=[], store=s1.store)
    assert "T01" not in s2.fired()
    ids = {r["stage_run_id"] for r in s2.store.q("SELECT stage_run_id FROM rule_results")}
    assert ids == {s2.summary["stage_run_id"]}
    assert s2.store.q1("SELECT COUNT(*) AS n FROM stage_runs WHERE stage='score'")["n"] == 2


def test_score_needs_build_stage(tmp_path):
    st = make_store(tmp_path, with_build=False)
    with pytest.raises(StaleError):
        score(tmp_path, store=st)


def test_override_to_some_signals_has_a_cause_row(tmp_path):
    # Invariant 5 (review 20261103, B-06): an override that sets some_signals on an
    # account with no rule result now has the owner's decision as its cause.
    ov = [{"user_id": ACC, "state": "some_signals", "note": "Asked me for money"}]
    s = score(tmp_path, overrides=ov)
    assert s.states[ACC] == "some_signals"
    hit = s.hit("OVERRIDE")
    assert hit["points"] == 0 and hit["family"] == "owner"
    assert hit["cause"] == "You set this state in overrides.yaml: Asked me for money"
    assert all(r["points"] == 0 for r in s.results[ACC].values())  # no rule fired, only the cause row


def test_override_note_with_a_forbidden_word_stays_out_of_the_cause(tmp_path):
    ov = [{"user_id": ACC, "state": "many_signals", "note": "a bot, I think"}]
    s = score(tmp_path, overrides=ov)
    assert s.hit("OVERRIDE")["cause"] == "You set this state in overrides.yaml"
    assert s.notes[ACC] == "a bot, I think"


def test_missing_source_with_no_hit_is_a_missing_input(tmp_path):
    # Invariant 10 (B-07): B04 reads the bio and the replies. The replies have no
    # phrase, but the bio is absent, so "did not fire" would be a made-up result.
    p = profile(description=None)
    s = score(tmp_path, profiles=[ego_profile(), p], interactions=[interaction(text="nice talk today")])
    assert "B04" not in s.fired()
    assert "description" in s.skipped().get("B04", set())
    # A hit in the source that is there still fires the rule.
    s2 = score(tmp_path / "b", profiles=[ego_profile(), p],
               interactions=[interaction(text="claim your crypto giveaway now")])
    assert "B04" in s2.fired()
