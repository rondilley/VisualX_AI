"""UTS #39 skeleton and the locked Unicode confusables data.

This file can hold literal non-ASCII test text (project hook allowlist).
"""

from __future__ import annotations

import hashlib

import pytest

from visualx.score import skeleton as sk


def test_data_file_agrees_with_lock():
    lock = sk.read_lock()
    blob = (sk.DATA_DIR / lock["file"]).read_bytes()
    assert hashlib.sha256(blob).hexdigest() == lock["sha256"]
    assert sk.data_version() == lock["version"] == "18.0.0"


def test_license_notice_is_present():
    text = (sk.DATA_DIR / "LICENSE-Unicode.txt").read_text(encoding="ascii")
    assert "UNICODE LICENSE V3" in text
    assert "confusables.txt" in text


def test_loader_refuses_changed_data(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "confusables.txt").write_bytes((sk.DATA_DIR / "confusables.txt").read_bytes() + b"\n# changed\n")
    (data / "confusables.lock").write_text((sk.LOCK_FILE).read_text(encoding="ascii"), encoding="ascii")
    monkeypatch.setattr(sk, "DATA_DIR", data)
    monkeypatch.setattr(sk, "LOCK_FILE", data / "confusables.lock")
    sk.load_table.cache_clear()
    try:
        with pytest.raises(sk.ConfusablesError, match="SHA-256"):
            sk.load_table()
    finally:
        monkeypatch.undo()
        sk.load_table.cache_clear()


@pytest.mark.parametrize(
    "fake",
    [
        "rоndilley",      # Cyrillic о
        "rondіlley",      # Cyrillic і
        "r0ndilley",      # digit zero
        "rondiIley",      # capital I for l
        "ｒｏｎｄｉｌｌｅｙ",  # full-width letters
        "ron​dilley",  # zero-width space
        "@RonDilley",
    ],
)
def test_lookalikes_have_the_ego_key(fake):
    assert sk.match_key(fake) == sk.match_key("rondilley")


@pytest.mark.parametrize("other", ["rondilly", "ronaldilley", "dilleyron", "ron_dilley"])
def test_different_strings_have_a_different_key(other):
    assert sk.match_key(other) != sk.match_key("rondilley")


def test_skeleton_maps_cyrillic_a_to_latin_a():
    assert sk.skeleton("а") == sk.skeleton("a")


def test_display_name_with_greek_and_cyrillic_letters():
    assert sk.match_key("Rοn Dіlley") == sk.match_key("Ron Dilley")


def test_edit_distance():
    assert sk.edit_distance("rondilley", "rondilley") == 0
    assert sk.edit_distance("rondilley", "rondiley") == 1
    assert sk.edit_distance("rondilley", "rondiley_") == 2
    assert sk.edit_distance("rondilley", "dilley") == 3
    assert sk.edit_distance("", "abc") == 3
