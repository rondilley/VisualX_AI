"""TR-06, TSC-17, TSC-29, FR-32, FR-36: the rule file and its policy controls."""

from __future__ import annotations

import copy
import re

import pytest
import yaml

from tests.unit.test_rules_kit import RULES_PATH, score
from visualx.model import SIGNAL_STATES, STATE_LABELS
from visualx.score.engine import (
    FAMILIES,
    FORBIDDEN_WORDS,
    V02_RULE_IDS,
    RuleFileError,
    load_rules,
    policy_errors,
    ruleset_from_data,
)
from visualx.score.signals import SIGNALS


def rules_data() -> dict:
    with RULES_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# -- TR-06 on the real rule file ----------------------------------------------


def test_rule_file_has_exactly_the_v02_rules():
    # 17 v0.1 rules, the 7 starter rules of FR-56 (B06 reads the owner's threat feed), B09 to B12,
    # and B13, B14, I04.
    ids = [r["id"] for r in rules_data()["rules"]]
    assert sorted(ids) == sorted(V02_RULE_IDS)
    assert len(ids) == 31


def test_every_rule_has_a_family_and_the_fr32_keys():
    for r in rules_data()["rules"]:
        assert r.get("family") in FAMILIES, r["id"]
        for k in ("id", "signal", "when", "cause"):
            assert r.get(k), (r["id"], k)
        if r["family"] != "gate":
            assert isinstance(r.get("points"), (int, float)), r["id"]


def test_rule_file_has_no_sensitive_category_input_or_keyword():
    assert policy_errors(rules_data()) == []


def test_rule_file_loads():
    rs = load_rules(RULES_PATH)
    assert len(rs.rules) == 31
    assert rs.bands.many_signals_families <= {"behavior", "impersonation"}


# -- TSC-17: the check fails for each bad fixture -----------------------------

BASE_RULE = """
bands: {some_signals_min: 3, many_signals_min: 6, many_signals_families: [behavior]}
rules:
"""

BAD_FIXTURES = {
    "no_family": """
  - id: X01
    signal: scam_phrase
    when: {eq: true}
    points: 2
    params: {phrases: [airdrop]}
    cause: "Contains '{phrase}'"
""",
    "religion_keyword": """
  - id: X02
    family: behavior
    signal: scam_phrase
    when: {eq: true}
    points: 2
    params: {phrases: [airdrop, church donation]}
    cause: "Contains '{phrase}'"
""",
    "politics_keyword": """
  - id: X03
    family: behavior
    signal: ai_authorship_phrase
    when: {eq: true}
    points: 2
    params: {phrases: [vote republican]}
    cause: "Contains '{phrase}'"
""",
    "health_keyword": """
  - id: X04
    family: behavior
    signal: scam_phrase
    when: {eq: true}
    points: 2
    params: {phrases: [cancer cure]}
    cause: "Contains '{phrase}'"
""",
    "ethnicity_field": """
  - id: X05
    family: profile
    signal: verified_type
    when: {eq: true}
    points: 1
    params: {field: ethnicity}
    cause: "Field"
""",
    "sexuality_keyword": """
  - id: X06
    family: behavior
    signal: scam_phrase
    when: {eq: true}
    points: 2
    params: {phrases: [gay]}
    cause: "Contains '{phrase}'"
""",
    "trade_union_keyword": """
  - id: X07
    family: behavior
    signal: scam_phrase
    when: {eq: true}
    points: 2
    params: {phrases: [join the union]}
    cause: "Contains '{phrase}'"
""",
    "sensitive_signal_name": """
  - id: X08
    family: profile
    signal: religious_affiliation
    when: {eq: true}
    points: 2
    cause: "Field"
""",
    "muslim_keyword": """
  - id: X09
    family: behavior
    signal: scam_phrase
    when: {eq: true}
    points: 2
    params: {phrases: [Muslims]}
    cause: "Contains '{phrase}'"
""",
    "forbidden_word_in_cause": """
  - id: X10
    family: behavior
    signal: scam_phrase
    when: {eq: true}
    points: 2
    params: {phrases: [airdrop]}
    cause: "Probable bot: '{phrase}'"
""",
    "forbidden_word_risky": """
  - id: X11
    family: behavior
    signal: scam_phrase
    when: {eq: true}
    points: 2
    params: {phrases: [airdrop]}
    cause: "Risky account"
""",
}


@pytest.mark.parametrize("name", sorted(BAD_FIXTURES))
def test_policy_check_fails_for_bad_fixture(tmp_path, name):
    p = tmp_path / f"{name}.yaml"
    p.write_text(BASE_RULE + BAD_FIXTURES[name], encoding="utf-8")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert policy_errors(data), name
    with pytest.raises(RuleFileError):
        load_rules(p)


def test_good_fixture_passes(tmp_path):
    p = tmp_path / "good.yaml"
    p.write_text(BASE_RULE + """
  - id: X20
    family: behavior
    signal: scam_phrase
    when: {eq: true}
    points: 2
    params: {phrases: [airdrop, transfer, reunion]}
    cause: "Contains '{phrase}'"
""", encoding="utf-8")
    assert len(load_rules(p).rules) == 1


def test_whole_word_match_does_not_flag_transfer_or_trace():
    data = {"rules": [{"id": "Z", "family": "behavior", "params": {"phrases": ["transfer", "trace", "goodness"]}}]}
    assert policy_errors(data) == []


# -- other contract errors ----------------------------------------------------


def _mutated(**changes) -> dict:
    data = copy.deepcopy(rules_data())
    for rid, patch in changes.items():
        for r in data["rules"]:
            if r["id"] == rid:
                r.update(patch)
    return data


def test_loader_refuses_profile_family_in_many_band():
    data = copy.deepcopy(rules_data())
    data["bands"]["many_signals_families"] = ["behavior", "profile"]
    with pytest.raises(RuleFileError, match="FR-35"):
        ruleset_from_data(data)


def test_loader_refuses_hard_profile_rule():
    with pytest.raises(RuleFileError, match="FR-35"):
        ruleset_from_data(_mutated(P05={"hard": True}))


def test_loader_refuses_unknown_signal():
    with pytest.raises(RuleFileError, match="signal"):
        ruleset_from_data(_mutated(P05={"signal": "no_such_signal"}))


def test_loader_refuses_unknown_operator():
    with pytest.raises(RuleFileError, match="operator"):
        ruleset_from_data(_mutated(P06={"when": {"about": 144}}))


def test_loader_refuses_missing_bands():
    data = copy.deepcopy(rules_data())
    del data["bands"]
    with pytest.raises(RuleFileError, match="bands"):
        ruleset_from_data(data)


def test_loader_refuses_attribute_access_in_cause():
    with pytest.raises(RuleFileError, match="plain name"):
        ruleset_from_data(_mutated(P07={"cause": "{value.__class__}"}))


def test_signal_inputs_have_no_sensitive_field():
    from visualx.score.engine import sensitive_hits

    for name, sdef in SIGNALS.items():
        for f in (name, *sdef.inputs):
            assert sensitive_hits(f) == [], (name, f)


# -- TSC-29 -------------------------------------------------------------------

WORD = re.compile("|".join(FORBIDDEN_WORDS), re.IGNORECASE)


def test_state_names_and_labels_have_no_forbidden_word():
    for s in SIGNAL_STATES:
        assert not WORD.search(s)
        assert not WORD.search(STATE_LABELS[s])


def test_cause_templates_have_no_forbidden_word():
    for r in rules_data()["rules"]:
        assert not WORD.search(r["cause"]), r["id"]


def test_rendered_causes_have_no_forbidden_word(tmp_path):
    from tests.unit.test_rules_kit import ego_profile, interaction, profile

    items = [interaction(text="As an AI language model airdrop", urls=["https://t.me/x"]) for _ in range(3)]
    s = score(tmp_path, profiles=[ego_profile(), profile(handle="r0ndilley", followers_count=0, following_count=0)],
              interactions=items)
    causes = [r["cause"] for r in s.store.q("SELECT cause FROM rule_results")]
    assert len(causes) >= 5
    for c in causes:
        assert not WORD.search(c), c
