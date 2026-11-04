"""UTS #39 skeleton for impersonation checks (rules I01, I02, I03, and the lookalike detector).

The skeleton of a string is NFD, then the confusables map on each code point,
then NFD again (Unicode Technical Standard #39, section 4). Two strings that
look the same have the same skeleton. The data file is the official Unicode
``confusables.txt``. Its SHA-256 is in ``data/confusables.lock``, and the
loader stops if the file does not agree with the lock.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
LOCK_FILE = DATA_DIR / "confusables.lock"


class ConfusablesError(RuntimeError):
    """The confusables data is missing, changed, or not in the expected form."""


def read_lock(path: Path = LOCK_FILE) -> dict[str, str]:
    if not path.exists():
        raise ConfusablesError(f"{path} not found")
    out: dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    for k in ("file", "version", "sha256"):
        if k not in out:
            raise ConfusablesError(f"{path}: key '{k}' is missing")
    return out


def parse_confusables(text: str) -> tuple[str | None, dict[str, str]]:
    """Return (version, map) from the text of confusables.txt."""
    version: str | None = None
    table: dict[str, str] = {}
    for raw in text.splitlines():
        if raw.startswith("# Version:"):
            version = raw.split(":", 1)[1].strip()
            continue
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ConfusablesError(f"bad confusables line: {raw[:60]!r}")
        src = chr(int(parts[0], 16))
        table[src] = "".join(chr(int(h, 16)) for h in parts[1].split())
    return version, table


@lru_cache(maxsize=1)
def load_table() -> tuple[str, dict[str, str]]:
    """Read, verify, and parse the locked confusables data. Returns (version, map)."""
    lock = read_lock()
    path = DATA_DIR / lock["file"]
    if not path.exists():
        raise ConfusablesError(f"{path} not found")
    blob = path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != lock["sha256"].lower():
        raise ConfusablesError(
            f"{path.name}: SHA-256 {digest} does not agree with the lock ({lock['sha256']})"
        )
    version, table = parse_confusables(blob.decode("utf-8-sig"))
    if version != lock["version"]:
        raise ConfusablesError(f"{path.name}: version {version!r} does not agree with the lock ({lock['version']!r})")
    if len(table) < 1000:
        raise ConfusablesError(f"{path.name}: only {len(table)} entries")
    return version, table


def data_version() -> str:
    return load_table()[0]


def skeleton(text: str) -> str:
    """UTS #39 skeleton(X): NFD, map each code point with confusables, NFD."""
    table = load_table()[1]
    nfd = unicodedata.normalize("NFD", text)
    return unicodedata.normalize("NFD", "".join(table.get(ch, ch) for ch in nfd))


@lru_cache(maxsize=65536)
def match_key(text: str) -> str:
    """A comparison key for handles and display names.

    It removes a leading '@', white space, and format characters (for example
    zero-width spaces), then takes the skeleton. X handles do not use case, so
    the key also case-folds and takes the skeleton again. Last, it removes the
    combining marks (category Mn): the skeleton of some homoglyphs is a base
    letter with a mark (Greek eta gives ``n`` with a vertical line below), and
    a mark is easy to miss on the screen. The result is cached: the lookalike
    detector and rules I01 and I02 ask for the same keys many times.
    """
    return _key(text, fold_first=False)


def _key(text: str, fold_first: bool) -> str:
    t = text.strip()
    if t.startswith("@"):
        t = t[1:]
    t = "".join(ch for ch in t if not ch.isspace() and unicodedata.category(ch) != "Cf")
    if fold_first:
        t = t.casefold()
    return "".join(ch for ch in skeleton(skeleton(t).casefold()) if unicodedata.category(ch) != "Mn")


@lru_cache(maxsize=65536)
def match_keys(text: str) -> frozenset[str]:
    """``match_key`` and the key with case folding before the skeleton.

    A capital I has two readings: the skeleton maps it to ``l`` (``RonDiIley``
    looks like ``RonDilley``), and after case folding it is ``i`` (``CTI``
    copied as ``CT`` with a Cyrillic small i). The skeleton maps each capital
    I-like letter to ``l`` and each small one to ``i``, so one key cannot hold
    both readings. Two strings look alike if any of their keys are equal.
    """
    return frozenset({_key(text, fold_first=False), _key(text, fold_first=True)})


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (insert, delete, substitute)."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---------------------------------------------------------------------------
# Lookalike names (rule I02 and the pattern detector "lookalike")
# ---------------------------------------------------------------------------

_SEPARATORS = frozenset("_.-")
_TRAILING_DIGITS = re.compile(r"\d+$")


@lru_cache(maxsize=65536)
def loose_key(text: str) -> str:
    """``match_key`` without the separators ``_ . -``.

    ``Jane_Doe`` and ``JaneDoe`` get the same loose key. X handles can hold
    only letters, digits, and ``_``, so a separator is the cheapest way to make
    a new handle that looks like an old one.
    """
    return "".join(ch for ch in match_key(text) if ch not in _SEPARATORS)


@lru_cache(maxsize=65536)
def loose_keys(text: str) -> frozenset[str]:
    """``match_keys`` without the separators (see ``loose_key``). Empty keys are removed."""
    return frozenset(k for k in ("".join(ch for ch in m if ch not in _SEPARATORS) for m in match_keys(text)) if k)


def allowed_distance(a: str, b: str, params: dict[str, Any]) -> int:
    """The edit distance that counts as a lookalike for two loose handle keys.

    Short handles are near many other handles by chance, so the distance
    grows with the length of the shorter key: 0 below ``min_handle_length``,
    1 from there, 2 from ``long_handle_min_length``. Two handles that both end
    in 4 or more digits (the X generated form) get 0, because a different digit
    tail is not a lookalike.
    """
    short = min(len(a), len(b))
    if _TRAILING_DIGITS.search(a) and _TRAILING_DIGITS.search(b):
        ta = _TRAILING_DIGITS.search(a).group(0)  # type: ignore[union-attr]
        tb = _TRAILING_DIGITS.search(b).group(0)  # type: ignore[union-attr]
        if len(ta) >= 4 and len(tb) >= 4:
            return 0
    d = 0
    if short >= int(params.get("min_handle_length", 5)):
        d = 1
    if short >= int(params.get("long_handle_min_length", 9)):
        d = 2
    return min(d, int(params.get("max_handle_edit_distance", 2)))


def lookalike_match(
    handle_a: str, name_a: str | None, handle_b: str, name_b: str | None, params: dict[str, Any]
) -> tuple[str, int] | None:
    """How the account names A and B look alike, or None.

    Returns (kind, distance). The kinds, strongest first:

    - ``handle_skeleton``: the loose handle keys are equal.
    - ``name_skeleton``: the display-name keys are equal (key length at or
      above ``min_name_key_length``, because short names such as "Mike" are
      shared by many persons).
    - ``handle_name_skeleton``: the handle of one is the display name of the other.
    - ``handle_edit_distance``: the loose handle keys are near (``allowed_distance``).
    """
    ha, hb = loose_key(handle_a), loose_key(handle_b)
    if not ha or not hb:
        return None
    hsa, hsb = loose_keys(handle_a), loose_keys(handle_b)
    if hsa & hsb:
        return "handle_skeleton", 0
    min_name = int(params.get("min_name_key_length", 8))
    nsa = frozenset(k for k in loose_keys(name_a) if len(k) >= min_name) if name_a else frozenset()
    nsb = frozenset(k for k in loose_keys(name_b) if len(k) >= min_name) if name_b else frozenset()
    if nsa & nsb:
        return "name_skeleton", 0
    if (nsa & hsb) or (nsb & hsa):
        return "handle_name_skeleton", 0
    limit = allowed_distance(ha, hb, params)
    if limit and abs(len(ha) - len(hb)) <= limit:
        dist = edit_distance(ha, hb)
        if dist <= limit:
            return "handle_edit_distance", dist
    return None


def names_alike(name_a: str | None, name_b: str | None, params: dict[str, Any]) -> bool:
    """True if two display names of ``min_name_key_length`` or more have an equal key or keys one edit apart.

    One edit, not ``allowed_distance``: the benchmark (tests/validation) showed
    that two edits match different persons with one family name ("Marta
    Abbott" and "Maya Abbott"). A copy of a name is usually exact or uses
    look-alike letters, which the skeleton already makes equal.
    """
    if not name_a or not name_b:
        return False
    a, b = loose_key(name_a), loose_key(name_b)
    min_name = int(params.get("min_name_key_length", 8))
    if not a or not b or min(len(a), len(b)) < min_name:
        return False
    if loose_keys(name_a) & loose_keys(name_b):
        return True
    return abs(len(a) - len(b)) <= 1 and edit_distance(a, b) <= 1


def lookalike_grade(
    kind: str,
    look_name: str | None,
    ref_name: str | None,
    look_age_days: float | None,
    reference_is_owner: bool,
    params: dict[str, Any],
    created_before_reference: bool = False,
) -> str:
    """How strong a lookalike of the owner or of a known account is: "high", "notable", or "info".

    ``kind`` comes from ``lookalike_match``. The rules (validation 2026-11-03,
    background/2026-11-03_detector_validation.md):

    - For an equal skeleton the creation order is not used: an old account
      that is bought and renamed can copy a newer account exactly.
    - An equal skeleton (handle, display name, or handle with display name) is
      "high" if the lookalike is younger than ``young_account_days`` or the
      reference is the owner. An old account with the same name as a contact
      is often a second person with the same name: "notable".
    - A near handle alone is common by chance (``jsmith`` and ``ksmith``,
      ``amberlabs`` and ``emberlabs``). A reader confuses two accounts when the
      display name also looks the same, so a near handle is "high" only if the
      display names look alike (``names_alike``), or if the lookalike is young
      and the reference is the owner (one reference, and the most important
      one). A young lookalike of a known account with another display name is
      "notable". A near handle on an account made before the reference is
      "info": it did not copy the reference, and a renamed account copies the
      name exactly (the benchmark showed ``sue_acosta`` of 2011 against
      ``suecosta`` of 2024). Else "info".

    A missing age counts as not young.
    """
    young = look_age_days is not None and look_age_days < float(params.get("young_account_days", 365))
    if kind != "handle_edit_distance":
        return "high" if (young or reference_is_owner) else "notable"
    if created_before_reference:
        return "info"
    if names_alike(look_name, ref_name, params):
        return "high"
    if young:
        return "high" if reference_is_owner else "notable"
    return "info"


# ---------------------------------------------------------------------------
# Mixed script (rule I03, UTS #39 section 5.1)
# ---------------------------------------------------------------------------

# Letter names that do not name a script. These letters are Script=Common in
# Unicode (for example the bold and italic letters that some users put in a
# display name), so they do not make a token mixed-script.
_COMMON_NAME_WORDS = frozenset({
    "MATHEMATICAL", "MODIFIER", "SUPERSCRIPT", "SUBSCRIPT", "CIRCLED", "PARENTHESIZED",
    "SQUARED", "NEGATIVE", "REGIONAL", "KATAKANA-HIRAGANA", "IDEOGRAPHIC",
})
_WIDTH_PREFIXES = ("FULLWIDTH ", "HALFWIDTH ")
# UTS #39 augmented script sets: these script groups are one writing system.
_SCRIPT_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"Han", "Hiragana", "Katakana"}),  # Japanese
    frozenset({"Han", "Bopomofo"}),  # Chinese with Bopomofo
    frozenset({"Han", "Hangul"}),  # Korean
)


def char_script(ch: str) -> str | None:
    """The script of a letter, from its Unicode character name. None for a non-letter or a Common letter.

    Python has no Script property, so the first word of the character name is
    the script (``CYRILLIC SMALL LETTER O`` gives ``Cyrillic``). This agrees
    with the Script property for the letters that occur in names and hashtags.
    """
    if not unicodedata.category(ch).startswith("L"):
        return None
    name = unicodedata.name(ch, "")
    for p in _WIDTH_PREFIXES:
        if name.startswith(p):
            name = name[len(p):]
    if not name:
        return None
    first = name.split(" ", 1)[0]
    if first in _COMMON_NAME_WORDS:
        return None
    if first == "CJK":
        return "Han"
    return first.title()


def _tokens(text: str) -> list[str]:
    """Split text into tokens of letters, marks, and digits."""
    out: list[str] = []
    cur: list[str] = []
    for ch in text:
        if unicodedata.category(ch)[0] in "LMN":
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def token_scripts(token: str) -> frozenset[str]:
    return frozenset(s for s in (char_script(ch) for ch in token) if s is not None)


def is_mixed(scripts: frozenset[str]) -> bool:
    if len(scripts) < 2:
        return False
    return not any(scripts <= g for g in _SCRIPT_GROUPS)


def mixed_script_token(text: str) -> tuple[str, frozenset[str]] | None:
    """The first token of ``text`` that mixes scripts, with its scripts. None if no token mixes scripts.

    The test is on each token (word or hashtag), not on the full text. A name
    with one Cyrillic word and one Latin word is a multilingual name. One word
    with Latin and Cyrillic letters is the confusable form that UTS #39 warns
    about. The rule gives no points for a script or a language as such (TR-06).
    """
    for tok in _tokens(text):
        scripts = token_scripts(tok)
        if is_mixed(scripts):
            return tok, scripts
    return None
