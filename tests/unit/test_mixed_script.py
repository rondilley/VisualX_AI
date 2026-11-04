"""TR-01, FR-56: rule I03 (mixed-script display name or hashtag, UTS #39).

This file can hold literal non-ASCII test text (project hook allowlist).
"""

from __future__ import annotations

import pytest

from tests.unit.test_rules_kit import ACC, ego_profile, interaction, profile, score
from visualx.score.skeleton import char_script, mixed_script_token


def test_char_script():
    assert char_script("a") == "Latin"
    assert char_script("о") == "Cyrillic"  # U+043E
    assert char_script("ι") == "Greek"
    assert char_script("山") == "Han"
    assert char_script("Ａ") == "Latin"  # fullwidth
    assert char_script("𝐀") is None  # mathematical bold: Common
    assert char_script("1") is None
    assert char_script("_") is None


@pytest.mark.parametrize("text, scripts", [
    ("Pаypal Support", {"Latin", "Cyrillic"}),  # Cyrillic a in a Latin word
    ("Dιzzιєє", {"Latin", "Greek", "Cyrillic"}),  # decorative mix seen in live data
    ("Αpple", {"Greek", "Latin"}),  # Greek capital alpha
])
def test_mixed_token_found(text, scripts):
    m = mixed_script_token(text)
    assert m is not None and set(m[1]) == scripts


@pytest.mark.parametrize("text", [
    "Иван Petrov",  # two words, two scripts: a multilingual name
    "山田たろう",  # Han with Hiragana: Japanese
    "김민준 金",  # Hangul, Han
    "𝐉𝐨𝐡𝐧 Smith",  # mathematical bold letters
    "Ron Dilley 🔐",
    "José Müller",  # Latin with marks
])
def test_single_script_tokens(text):
    assert mixed_script_token(text) is None


def test_i03_fires_for_mixed_script_name(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(name="Pаypal Support")])
    assert "I03" in s.fired()
    assert s.hit("I03")["cause"] == "Name mixes Cyrillic and Latin letters in one word"
    assert s.hit("I03")["points"] == 2
    assert s.states[ACC] == "no_signals"


def test_i03_fires_for_mixed_script_hashtag(tmp_path):
    s = score(tmp_path, interactions=[interaction(text="Join now #сrypto #airdrops")])
    assert "I03" in s.fired()
    assert s.hit("I03")["cause"] == "Hashtag mixes Cyrillic and Latin letters in one word"


@pytest.mark.parametrize("name", ["Иван Petrov", "山田たろう", "Quiet Reader"])
def test_i03_does_not_fire_for_single_script_words(tmp_path, name):
    s = score(tmp_path, profiles=[ego_profile(), profile(name=name)],
              interactions=[interaction(text="Привет, thanks for the #graphs post")])
    assert "I03" not in s.fired()


def test_i03_has_no_input_without_name_and_text(tmp_path):
    s = score(tmp_path, profiles=[ego_profile(), profile(name=None)],
              interactions=[interaction(text=None)])
    assert s.skipped()["I03"] == {"name+text"}
