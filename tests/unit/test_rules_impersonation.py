"""TR-01, TSC-14: impersonation rule I01 (hard rule, UTS #39 skeleton)."""

from __future__ import annotations

from tests.unit.test_rules_kit import ACC, EGO_HANDLE, ego_profile, make_store, profile, score


def test_i01_fires_for_confusable_handle(tmp_path):
    # Cyrillic small o (U+043E) and capital I for l.
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="rоndiIley")])
    assert "I01" in s.fired()
    assert s.states[ACC] == "many_signals"
    hit = s.hit("I01")
    assert hit["hard"] == 1
    assert hit["points"] == 6
    assert hit["cause"] == f"Name looks like your account @{EGO_HANDLE}"


def test_i01_fires_for_display_name_equal_to_ego_name(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="someone_else_42", name="Ron  DiIley")])
    assert "I01" in s.fired()
    assert '"match":"skeleton"' in s.hit("I01")["value_json"]


def test_i01_fires_for_handle_within_edit_distance_two(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="rondiley_")])
    assert "I01" in s.fired()
    assert '"match":"edit_distance"' in s.hit("I01")["value_json"]


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
