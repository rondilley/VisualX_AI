"""TR-01, TSC-14: trust rules T01, T02, T03."""

from __future__ import annotations

from tests.unit.test_rules_kit import ACC, EGO, FETCHED, ego_profile, interaction, iso_days_before, profile, score


def test_t01_fires_for_business_verification(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(verified_type="business")])
    assert "T01" in s.fired()
    assert s.hit("T01")["points"] == -2


def test_t01_fires_for_government_verification(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(verified_type="government")])
    assert "T01" in s.fired()


def test_t01_does_not_fire_for_paid_blue(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(verified_type="blue")])
    assert "T01" not in s.fired()


def test_t02_fires_for_established_account(tmp_path):
    created = iso_days_before(FETCHED, 365 * 5 + 2)
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=created, followers_count=1000, listed_count=10)])
    assert "T02" in s.fired()
    assert s.hit("T02")["cause"] == "Account is 5 years old and is on 10 lists"
    assert s.hit("T02")["points"] == -2


def test_t02_does_not_fire_with_few_lists(tmp_path):
    created = iso_days_before(FETCHED, 365 * 5)
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=created, followers_count=5000, listed_count=9)])
    assert "T02" not in s.fired()


def test_t02_does_not_fire_for_young_account(tmp_path):
    created = iso_days_before(FETCHED, 700)
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=created, followers_count=5000, listed_count=50)])
    assert "T02" not in s.fired()


def test_t03_fires_when_ego_replied(tmp_path):
    items = [interaction(), interaction(src=EGO, tgt=ACC, type="reply")]
    s = score(tmp_path, interactions=items)
    assert "T03" in s.fired()
    assert s.hit("T03")["points"] == -3
    assert s.hit("T03")["cause"] == "You interacted with this account"


def test_t03_fires_when_ego_quoted(tmp_path):
    items = [interaction(), interaction(src=EGO, tgt=ACC, type="quote")]
    s = score(tmp_path, interactions=items)
    assert "T03" in s.fired()


def test_t03_does_not_fire_for_ego_mention_or_repost(tmp_path):
    items = [interaction(), interaction(src=EGO, tgt=ACC, type="mention"), interaction(src=EGO, tgt=ACC, type="repost")]
    s = score(tmp_path, interactions=items)
    assert "T03" not in s.fired()


def test_t03_does_not_fire_without_ego_interaction(tmp_path):
    s = score(tmp_path)
    assert "T03" not in s.fired()
