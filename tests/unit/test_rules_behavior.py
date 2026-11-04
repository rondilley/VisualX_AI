"""TR-01, TSC-14: behavior rules B01, B03, B04, B05, B07."""

from __future__ import annotations

from datetime import datetime, timedelta

from tests.unit.test_rules_kit import ACC, EGO, ego_profile, interaction, profile, score

T0 = datetime(2026, 10, 1, 12, 0, 0)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# -- B01 duplicate replies ----------------------------------------------------

SPAM = "Great insight here, my profile has the best trading signals for you"


def test_b01_fires_for_three_identical_replies(tmp_path):
    items = [interaction(text=SPAM) for _ in range(3)]
    s = score(tmp_path, interactions=items)
    assert "B01" in s.fired()
    assert s.hit("B01")["cause"] == "Sent 3 near-identical replies"
    assert s.hit("B01")["points"] == 3


def test_b01_fires_for_near_identical_replies(tmp_path):
    # Only the mention, the link, and the punctuation are different.
    items = [
        interaction(text="@alice " + SPAM + " https://example.org/1"),
        interaction(text="@bob " + SPAM.upper() + "!!"),
        interaction(text=SPAM + " https://example.org/2"),
    ]
    s = score(tmp_path, interactions=items)
    assert "B01" in s.fired()


def test_b01_does_not_fire_for_two_duplicates(tmp_path):
    items = [interaction(text=SPAM), interaction(text=SPAM), interaction(text="A different reply about routing tables")]
    s = score(tmp_path, interactions=items)
    assert "B01" not in s.fired()


def test_b01_has_no_input_when_reply_text_unreadable(tmp_path):
    items = [interaction(unreadable=("text",)) for _ in range(3)]
    s = score(tmp_path, interactions=items)
    assert "B01" not in s.fired()
    assert "text" in s.skipped()["B01"]


# -- B03 reply spike ----------------------------------------------------------


def test_b03_fires_for_ten_replies_in_one_hour(tmp_path):
    items = [interaction(text=f"reply number {n}", created_at=at(n * 5)) for n in range(10)]
    s = score(tmp_path, interactions=items)
    assert "B03" in s.fired()
    assert s.hit("B03")["cause"] == "10 replies to you in 60 minutes"


def test_b03_does_not_fire_when_spread_over_more_than_one_hour(tmp_path):
    items = [interaction(text=f"reply number {n}", created_at=at(n * 7)) for n in range(10)]
    s = score(tmp_path, interactions=items)
    assert "B03" not in s.fired()


def test_b03_counts_only_replies_to_the_ego(tmp_path):
    items = [interaction(tgt="555", text=f"reply number {n}", created_at=at(n)) for n in range(12)]
    items.append(interaction(text="one reply to the ego", created_at=at(1)))
    s = score(tmp_path, interactions=items)
    assert "B03" not in s.fired()


def test_b03_uses_only_interactions_with_time(tmp_path):
    items = [interaction(text=f"reply {n}", created_at=at(n), unreadable=("interaction_created_at",)) for n in range(12)]
    s = score(tmp_path, interactions=items)
    assert "B03" not in s.fired()
    assert s.skipped()["B03"] == {"interaction_created_at"}


# -- B04 scam phrases ---------------------------------------------------------


def test_b04_fires_for_phrase_in_reply(tmp_path):
    s = score(tmp_path, interactions=[interaction(text="Free AIRDROP today, claim it now")])
    assert "B04" in s.fired()
    assert s.hit("B04")["cause"] == "Reply or bio contains scam phrase 'airdrop'"


def test_b04_fires_for_phrase_in_bio(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(description="Crypto expert. DM me for help.")])
    assert "B04" in s.fired()
    assert "'dm me'" in s.hit("B04")["cause"]


def test_b04_does_not_fire_for_ordinary_text(tmp_path):
    s = score(tmp_path, interactions=[interaction(text="My wallets are full of receipts, ha")])
    assert "B04" not in s.fired()


def test_b04_has_no_input_when_no_text_and_no_bio(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(description=None)],
              interactions=[interaction(unreadable=("text",))])
    assert "B04" not in s.fired()
    assert s.skipped()["B04"] == {"text+description"}


# -- B05 messenger or shortener link ------------------------------------------


def test_b05_fires_for_telegram_link_and_skips_escalation(tmp_path):
    s = score(tmp_path, interactions=[interaction(urls=["https://t.me/fastprofits"])])
    assert "B05" in s.fired()
    assert s.hit("B05")["points"] == 1  # base points; follow data is not collected
    assert s.hit("B05")["cause"] == "Links to t.me"
    assert s.skipped()["B05"] == {"follows_ego"}
    assert any(r["event"] == "rule_no_input" and r["rule_id"] == "B05" for r in s.log.records)


def test_b05_fires_for_shortener_in_bio(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(description="Read this https://bit.ly/abc123 now")])
    assert "B05" in s.fired()
    assert s.hit("B05")["cause"] == "Links to bit.ly"


def test_b05_does_not_fire_for_ordinary_link(tmp_path):
    s = score(tmp_path, interactions=[interaction(urls=["https://example.org/t.me"])])
    assert "B05" not in s.fired()
    assert "B05" not in s.skipped()


# -- B07 AI authorship phrase -------------------------------------------------


def test_b07_fires_for_language_model_phrase(tmp_path):
    s = score(tmp_path, interactions=[interaction(text="As an AI language model, I can not have opinions.")])
    assert "B07" in s.fired()
    assert s.hit("B07")["cause"] == "Text contains phrase 'as an ai language model'"
    assert s.hit("B07")["points"] == 3


def test_b07_does_not_fire_for_ordinary_text(tmp_path):
    s = score(tmp_path, interactions=[interaction(text="I read about AI models and language today.")])
    assert "B07" not in s.fired()


def test_behavior_rules_do_not_read_other_accounts(tmp_path):
    # The ego posts the phrase. The account under test does not fire B07.
    items = [interaction(), interaction(src=EGO, tgt=ACC, text="As an AI language model, ha")]
    s = score(tmp_path, interactions=items)
    assert "B07" not in s.fired()
