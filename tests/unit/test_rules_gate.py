"""TR-01, TSC-14: gate rules N01, N02, N03 (FR-34)."""

from __future__ import annotations

from tests.unit.test_rules_kit import ACC, EGO, ego_profile, interaction, profile, score


def test_baseline_account_fires_no_rule(tmp_path):
    s = score(tmp_path)
    assert s.fired() == set()
    assert s.states[ACC] == "no_signals"
    assert s.skipped() == {}


def test_n01_fires_when_no_profile(tmp_path):
    s = score(tmp_path, profiles=[ego_profile()])
    assert s.fired() == {"N01"}
    assert s.states[ACC] == "not_scored"
    assert s.hit("N01")["cause"] == "Profile data not available"
    assert s.hit("N01")["points"] == 0


def test_n01_fires_when_metrics_absent(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(followers_count=None)])
    assert s.fired() == {"N01"}
    assert s.states[ACC] == "not_scored"


def test_n01_does_not_fire_with_full_profile(tmp_path):
    s = score(tmp_path)
    assert "N01" not in s.fired()


def test_n01_gate_stops_other_rules(tmp_path):
    # A missing profile must not create skips for every profile rule.
    s = score(tmp_path, profiles=[ego_profile()])
    assert s.skipped() == {}


def test_n02_fires_on_lookup_error(tmp_path):
    s = score(tmp_path, profiles=[ego_profile()], errors={ACC: "Not Found Error"})
    # N01 is first in file order and also fires for a missing profile.
    assert s.states[ACC] == "not_scored"
    assert s.fired() & {"N01", "N02"}


def test_n02_fires_on_lookup_error_with_old_profile(tmp_path):
    from tests.unit.test_rules_kit import make_store

    st = make_store(tmp_path)
    st.put_profiles([ego_profile(), profile()])
    st.put_profile_error(ACC, "Forbidden")  # a later lookup failed
    s = score(tmp_path, profiles=[], store=st)
    assert s.fired() == {"N02"}
    assert s.hit("N02")["cause"] == "Account is protected or not available"


def test_n02_fires_for_protected_account_with_no_interactions(tmp_path):
    # The ego mentions a protected account: the account has no interactions of its own.
    items = [interaction(src=EGO, tgt=ACC, type="mention")]
    s = score(tmp_path, profiles=[ego_profile(), profile(protected=True)], interactions=items)
    assert s.fired() == {"N02"}
    assert s.states[ACC] == "not_scored"


def test_n02_does_not_fire_for_protected_account_with_interactions(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(protected=True)])
    assert "N02" not in s.fired()
    assert s.states[ACC] == "no_signals"


def test_n03_fires_for_ego(tmp_path):
    s = score(tmp_path)
    assert s.fired(EGO) == {"N03"}
    assert s.states[EGO] == "not_scored"
    assert s.hit("N03", EGO)["cause"] == "Account is you or is on your allowlist"


def test_n03_fires_for_allowlist(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at="2026-09-30T00:00:00.000Z")], allowlist=[ACC])
    assert s.fired() == {"N03"}
    assert s.states[ACC] == "not_scored"


def test_n03_does_not_fire_for_other_account(tmp_path):
    s = score(tmp_path, allowlist=["999"])
    assert "N03" not in s.fired()
