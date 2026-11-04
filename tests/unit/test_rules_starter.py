"""TR-01, FR-56, FR-57: the v0.2 starter rules P02, P04, B08, I02 (B02 is in test_patterns.py, I03 in test_mixed_script.py)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.unit.test_rules_kit import ACC, EGO, ego_profile, interaction, make_store, profile, score
from visualx.score.skeleton import allowed_distance, lookalike_match, loose_key

T0 = datetime(2026, 10, 1, 12, 0, 0)
DEFAULT_IMG = "https://abs.twimg.com/sticky/default_profile_images/default_profile_normal.png"


def at(minutes: float = 0) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def days_before(days: float) -> str:
    return (T0 - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# -- P02 default profile image ------------------------------------------------


def test_p02_fires_for_default_image(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(profile_image_url=DEFAULT_IMG)])
    assert "P02" in s.fired()
    assert s.hit("P02")["cause"] == "Account uses the default profile image"
    assert s.hit("P02")["points"] == 1
    assert s.states[ACC] == "no_signals"


def test_p02_does_not_fire_for_own_image(tmp_path):
    s = score(tmp_path)
    assert "P02" not in s.fired()


def test_p02_has_no_input_without_image_url(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(profile_image_url=None)])
    assert "P02" not in s.fired()
    assert s.skipped()["P02"] == {"profile_image_url"}


# -- P04 generated handle -----------------------------------------------------


@pytest.mark.parametrize("handle, digits", [("user48213907", 8), ("Jane_Doe123456", 6)])
def test_p04_fires_for_generated_handle(tmp_path, handle, digits):
    s = score(tmp_path, profiles=[ego_profile(), profile(handle=handle)])
    assert "P04" in s.fired()
    assert s.hit("P04")["cause"] == f"Handle ends in {digits} digits"


@pytest.mark.parametrize("handle", ["quietreader", "reader42", "user12345", "1234567abc", "abc_1234567x"])
def test_p04_does_not_fire(tmp_path, handle):
    s = score(tmp_path, profiles=[ego_profile(), profile(handle=handle)])
    assert "P04" not in s.fired()


def test_p04_has_no_input_without_handle(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(handle=None)])
    assert s.skipped()["P04"] == {"handle"}


# -- B08 new account on a high-reach post -------------------------------------


def owner_posts(store, n: int = 10, top: str = "700000009"):
    for k in range(n):
        pid = f"70000000{k}"
        store.put_post(
            {"id": pid, "author_id": EGO, "created_at": at(-600 + k),
             "public_metrics": {"impression_count": 5000 if pid == top else 50 + k, "reply_count": 1}},
            "test", "run_test",
        )


def b08(tmp_path, created: str, target: str, n_posts: int = 10):
    st = make_store(tmp_path)
    owner_posts(st, n_posts)
    return score(tmp_path, profiles=[ego_profile(), profile(created_at=created)],
                 interactions=[interaction(created_at=at(0), target_post_id=target)], store=st)


def test_b08_fires_for_new_account_on_top_post(tmp_path):
    s = b08(tmp_path, days_before(5), "700000009")
    assert "B08" in s.fired()
    assert s.hit("B08")["cause"] == "New account (5 days old) replied to a high-reach post"
    assert "P01" in s.fired()


def test_b08_does_not_fire_on_low_reach_post(tmp_path):
    s = b08(tmp_path, days_before(5), "700000002")
    assert "B08" not in s.fired()


def test_b08_does_not_fire_for_old_account(tmp_path):
    s = b08(tmp_path, days_before(400), "700000009")
    assert "B08" not in s.fired()


def test_b08_has_no_input_with_too_few_owner_posts(tmp_path):
    s = b08(tmp_path, days_before(5), "700000003", n_posts=5)
    assert "B08" not in s.fired()
    assert s.skipped()["B08"] == {"ego_posts"}


def test_b08_is_not_skipped_for_account_without_reply_to_owner_post(tmp_path):
    s = score(tmp_path)  # the reply goes to a post that is not an owner post
    assert "B08" not in s.skipped()


# -- I02 impersonation of a known account -------------------------------------

KNOWN = "300"
# A copy is a new account (60 days before FETCHED). The validation of 2026-11-03
# (background/2026-11-03_detector_validation.md) changed I02: an old account with
# the same skeleton, or a near handle with another display name, does not fire it.
NEW_ACCOUNT = "2026-09-02T00:00:00.000Z"


def i02(tmp_path, handle: str, name: str = "A Reader", allow: bool = True, known_handle: str = "alice_secops",
        contact: bool = False, created_at: str = NEW_ACCOUNT, known_created_at: str = "2015-01-01T00:00:00.000Z"):
    profs = [ego_profile(), profile(KNOWN, handle=known_handle, name="Alice Example", created_at=known_created_at),
             profile(handle=handle, name=name, created_at=created_at)]
    items = [interaction(), interaction(src=KNOWN)]
    if contact:
        items += [interaction(src=EGO, tgt=KNOWN), interaction(src=EGO, tgt=KNOWN, type="mention")]
    return score(tmp_path, profiles=profs, interactions=items, allowlist=[KNOWN] if allow else None)


@pytest.mark.parametrize("handle, name", [
    ("alicesecops", "A Reader"), ("aIice_secops", "A Reader"), ("alice_secops_", "A Reader"),
    # A near handle (not an equal skeleton) needs a copied display name too (validation 2026-11-03).
    ("alice_secop", "Alice Example"),
])
def test_i02_fires_for_lookalike_of_allowlisted_account(tmp_path, handle, name):
    s = i02(tmp_path, handle, name=name)
    assert "I02" in s.fired()
    assert s.hit("I02")["cause"] == "Name looks like @alice_secops"
    assert s.hit("I02")["points"] == 4
    assert s.hit("I02")["family"] == "impersonation"


def test_i02_fires_for_display_name_of_known_account(tmp_path):
    s = i02(tmp_path, "someone_new", name="Alice  Example")
    assert "I02" in s.fired()
    assert '"match":"name_skeleton"' in s.hit("I02")["value_json"]


def test_i02_fires_for_lookalike_of_top_contact(tmp_path):
    # Not on the allowlist: a close contact (the owner replies to it) is a known account.
    s = i02(tmp_path, "alice_secops1", name="Alice Example", allow=False, contact=True)
    assert "I02" in s.fired()
    assert '"role":"contact"' in s.hit("I02")["value_json"]
    # The contact itself does not fire I02.
    assert "I02" not in s.fired(KNOWN)


def test_i02_needs_owner_interaction_for_a_contact(tmp_path):
    s = i02(tmp_path, "alice_secops1", name="Alice Example", allow=False, contact=False)
    assert "I02" not in s.fired()


@pytest.mark.parametrize("handle, known_handle", [
    ("quietreader", "alice_secops"),  # different
    ("bob_j", "bob_k"),  # short handles one letter apart
    ("alice_sec", "alice_secops"),  # three letters apart
])
def test_i02_does_not_fire(tmp_path, handle, known_handle):
    s = i02(tmp_path, handle, known_handle=known_handle)
    assert "I02" not in s.fired()


@pytest.mark.parametrize("handle, name, created_at, known_created_at, fires", [
    # Same skeleton: a new account fires; an old account (often a second person with the name) does not.
    ("alicesecops", "A Reader", NEW_ACCOUNT, "2015-01-01T00:00:00.000Z", True),
    ("alicesecops", "A Reader", "2016-01-01T00:00:00.000Z", "2015-01-01T00:00:00.000Z", False),
    ("someone_new", "Alice  Example", "2018-01-01T00:00:00.000Z", "2015-01-01T00:00:00.000Z", False),
    # Near handle: fires only with a display name that also looks alike, young or old.
    ("alice_secop", "A Reader", "2018-01-01T00:00:00.000Z", "2015-01-01T00:00:00.000Z", False),
    ("alice_secop", "A Reader", NEW_ACCOUNT, "2015-01-01T00:00:00.000Z", False),
    ("alice_secop", "Alice Exampl", "2018-01-01T00:00:00.000Z", "2015-01-01T00:00:00.000Z", True),
    ("alice_secop", "Alice Exampl", NEW_ACCOUNT, "2015-01-01T00:00:00.000Z", True),
    # Two edits in the display name is another person with the same family name.
    ("alice_secop", "Alina Example", NEW_ACCOUNT, "2015-01-01T00:00:00.000Z", False),
    # An equal skeleton ignores the creation order: a bought, renamed account can copy a newer one.
    ("alicesecops", "A Reader", NEW_ACCOUNT, "2026-10-01T00:00:00.000Z", True),
    # A near handle on an account made before the known account did not copy it.
    ("alice_secop", "Alice Exampl", "2014-01-01T00:00:00.000Z", "2015-01-01T00:00:00.000Z", False),
])
def test_i02_age_and_name_limits(tmp_path, handle, name, created_at, known_created_at, fires):
    s = i02(tmp_path, handle, name=name, created_at=created_at, known_created_at=known_created_at)
    assert ("I02" in s.fired()) is fires


def test_i02_has_no_input_without_handle(tmp_path):
    s = i02(tmp_path, None)  # type: ignore[arg-type]
    assert s.skipped()["I02"] == {"handle"}


def test_lookalike_helpers():
    assert loose_key("@Ron_Dilley") == loose_key("RonDilley")
    assert allowed_distance("abcd", "abce", {}) == 0
    assert allowed_distance("abcde", "abcdf", {}) == 1
    assert allowed_distance("abcdefghi", "abcdefghj", {}) == 2
    assert allowed_distance("anna1234", "anna1235", {}) == 0
    assert lookalike_match("ron_dilley", None, "RonDilley", None, {}) == ("handle_skeleton", 0)
    assert lookalike_match("rondilley", None, "someone", "Ron Dilley", {}) == ("handle_name_skeleton", 0)
    assert lookalike_match("tom_q", "Mike", "zed_k", "Mike", {}) is None  # short names are not a match
    assert lookalike_match("mike_a", None, "mike_b", None, {}) == ("handle_edit_distance", 1)
