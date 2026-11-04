"""TR-01, TSC-14: profile rules P01, P03, P05, P06, P07."""

from __future__ import annotations

from tests.unit.test_rules_kit import ACC, EGO, FETCHED, ego_profile, interaction, iso_days_before, profile, score

T0 = "2026-10-01T12:00:00.000Z"


# -- P01 new account at first interaction ------------------------------------


def test_p01_fires_with_two_points_under_30_days(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=iso_days_before(T0, 20))],
              interactions=[interaction(created_at=T0)])
    assert "P01" in s.fired()
    assert s.hit("P01")["points"] == 2
    assert s.hit("P01")["cause"] == "Account created 20 days before first interaction"


def test_p01_escalates_to_three_points_under_7_days(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=iso_days_before(T0, 3))],
              interactions=[interaction(created_at=T0)])
    assert s.hit("P01")["points"] == 3
    assert s.states[ACC] == "some_signals"


def test_p01_uses_earliest_timed_interaction(tmp_path):
    created = iso_days_before(T0, 40)
    items = [interaction(created_at=T0), interaction(created_at=iso_days_before(T0, 15))]
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=created)], interactions=items)
    assert "P01" in s.fired()
    assert '"value":25' in s.hit("P01")["value_json"]


def test_p01_does_not_fire_for_old_account(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=iso_days_before(T0, 31))],
              interactions=[interaction(created_at=T0)])
    assert "P01" not in s.fired()


def test_p01_uses_interactions_where_the_account_is_the_target(tmp_path):
    """Owner decision 2026-11-03: an account that only gets replies from the owner has P01 input."""
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=iso_days_before(T0, 10))],
              interactions=[interaction(src=EGO, tgt=ACC, created_at=T0)])
    assert "P01" in s.fired()
    assert s.hit("P01")["cause"] == "Account created 10 days before first interaction"
    assert "P01" not in s.skipped()


def test_p01_earliest_time_over_both_directions(tmp_path):
    items = [interaction(created_at=T0), interaction(src=EGO, tgt=ACC, created_at=iso_days_before(T0, 20))]
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=iso_days_before(T0, 45))], interactions=items)
    assert '"value":25' in s.hit("P01")["value_json"]


def test_p01_ignores_reposts_and_has_no_input_without_time(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=iso_days_before(T0, 2))],
              interactions=[interaction(type="repost")])
    assert "P01" not in s.fired()
    assert s.skipped()["P01"] == {"interaction_created_at"}


# -- P03 empty profile --------------------------------------------------------


def test_p03_fires_for_empty_bio_url_location(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(description="", url="", location=" ")])
    assert "P03" in s.fired()
    assert s.hit("P03")["points"] == 1


def test_p03_does_not_fire_with_a_bio(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(url="", location="")])
    assert "P03" not in s.fired()


def test_p03_absent_location_is_no_input_not_empty(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(description="", url="", location=None)])
    assert "P03" not in s.fired()
    assert s.skipped()["P03"] == {"location"}


# -- P05 low follower ratio ---------------------------------------------------


def test_p05_fires_for_few_followers_many_following(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(followers_count=9, following_count=200)])
    assert "P05" in s.fired()
    assert s.hit("P05")["cause"] == "9 followers, 200 following"


def test_p05_does_not_fire_at_ten_followers(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(followers_count=10, following_count=900)])
    assert "P05" not in s.fired()


# -- P06 high post rate -------------------------------------------------------


def test_p06_fires_above_144_posts_each_day(tmp_path):
    created = iso_days_before(FETCHED, 100)
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=created, tweet_count=20000)])
    assert "P06" in s.fired()
    assert s.hit("P06")["cause"] == "Average of 200 posts each day"


def test_p06_does_not_fire_at_normal_rate(tmp_path):
    created = iso_days_before(FETCHED, 100)
    s = score(tmp_path, profiles=[ego_profile(), profile(created_at=created, tweet_count=14400)])
    assert "P06" not in s.fired()


# -- P07 no social graph ------------------------------------------------------


def test_p07_fires_for_zero_followers_and_following(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(followers_count=0, following_count=0)])
    assert "P07" in s.fired()
    assert s.hit("P07")["cause"] == "No followers and follows no accounts"


def test_p07_does_not_fire_with_one_follower(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(followers_count=1, following_count=0)])
    assert "P07" not in s.fired()
