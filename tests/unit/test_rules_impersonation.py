"""TR-01, TSC-14: impersonation rule I01 (hard rule, UTS #39 skeleton).

I01 gives many_signals alone, so it fires only for a strong match: a handle
that looks the same as the owner handle at any age, or a young account with a
near handle or the same name (review 20261103, B-01).
"""

from __future__ import annotations

from tests.unit.test_rules_kit import ACC, EGO_HANDLE, ego_profile, make_store, profile, score

YOUNG = "2026-09-01T00:00:00.000Z"  # 61 days before FETCHED


def test_i01_fires_for_confusable_handle(tmp_path):
    # Cyrillic small o (U+043E) and capital I for l.
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="rоndiIley")])
    assert "I01" in s.fired()
    assert s.states[ACC] == "many_signals"
    hit = s.hit("I01")
    assert hit["hard"] == 1
    assert hit["points"] == 6
    assert hit["cause"] == f"Name looks like your account @{EGO_HANDLE}"


def test_i01_fires_for_handle_with_only_a_separator_changed_at_any_age(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="ron_dilley", name="Someone")])
    assert "I01" in s.fired()
    assert '"match":"handle_skeleton"' in s.hit("I01")["value_json"]


def test_i01_fires_for_young_account_with_display_name_equal_to_ego_name(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="someone_else_42", name="Ron  DiIley",
                                                         created_at=YOUNG)])
    assert "I01" in s.fired()
    assert '"match":"name_skeleton"' in s.hit("I01")["value_json"]


def test_i01_does_not_fire_for_old_account_with_the_same_name(tmp_path):
    # A second person with the owner's name (account made in 2015). Not a hard signal.
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="jdoe_writes", name="Ron Dilley")])
    assert "I01" not in s.fired()
    assert s.states[ACC] != "many_signals"


def test_i01_fires_for_young_account_with_handle_within_edit_distance(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="rondiley_", created_at=YOUNG)])
    assert "I01" in s.fired()
    assert '"match":"handle_edit_distance"' in s.hit("I01")["value_json"]


def test_i01_does_not_fire_for_old_account_with_near_handle_and_other_name(tmp_path):
    # Two edits from the owner handle, made in 2010, with many followers: a different person.
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="Ron_Riley", name="Ron Riley",
                                                         created_at="2010-05-01T00:00:00.000Z",
                                                         followers_count=5000)])
    assert "I01" not in s.fired()
    assert s.states[ACC] != "many_signals"


def test_i01_near_handle_without_created_at_does_not_fire(tmp_path):
    # Invariant 10: a missing age is not young, so only the strong test can fire.
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="rondiley_", created_at=None)])
    assert "I01" not in s.fired()


def test_i01_does_not_fire_for_different_handle(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="rondilleyfan", name="A Reader")])
    assert "I01" not in s.fired()


def test_i01_does_not_fire_for_the_ego_itself(tmp_path):
    s = score(tmp_path)
    assert "I01" not in s.fired("100")


def test_i01_has_no_input_without_ego_handle(tmp_path):
    st = make_store(tmp_path)
    st.conn.execute("DELETE FROM meta WHERE key='ego_handle'")
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="rоndilley")], store=st)
    assert "I01" not in s.fired()
    assert s.skipped()["I01"] == {"ego_handle"}


def test_i01_hard_rule_overrides_trust_credit(tmp_path):
    # T01 and T02 credit does not cancel a hard rule.
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="rondi1ley", verified_type="business",
                                                         followers_count=9000, listed_count=90)])
    assert {"I01", "T01", "T02"} <= s.fired()
    assert s.states[ACC] == "many_signals"
