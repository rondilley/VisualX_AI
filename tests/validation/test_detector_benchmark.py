"""Labeled benchmark for the pattern detectors: a fast subset of tools/validate_detectors.py.

The full measurement (32 runs, up to 2000 contacts) is in the report
background/2026-11-03_detector_validation.md. This module runs 6 scenarios of
300 to 700 contacts with seeds that the tuning did not use, and checks that
the detectors still find the planted campaigns and stay quiet on background
traffic. The limits are lower than the measured values, with a margin for
the variation between seeds. A test that fails here means a detector change
that the full report must measure again.

The module also checks the policy invariants on realistic data: FR-31 words,
no handle in finding text, invariant 5 (each state has its causes), and
FR-35 (profile rules alone never give many_signals).
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.validation import scenarios as S
from visualx.score.engine import FORBIDDEN_WORDS

pytestmark = pytest.mark.validation

PLANTED = (("tune", 701, 300), ("tune", 702, 700), ("holdout", 801, 300), ("holdout", 802, 700))
BACKGROUND = ((901, 300), (902, 700))


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[tuple[S.Scenario, S.RunResult]]]:
    out: dict[str, list[tuple[S.Scenario, S.RunResult]]] = {"planted": [], "background": []}
    for form, seed, n in PLANTED:
        scn = S.build_scenario(seed, n, S.DETECTORS, holdout=(form == "holdout"))
        out["planted"].append((scn, S.run_scenario(scn, tmp_path_factory.mktemp(scn.name))))
    for seed, n in BACKGROUND:
        scn = S.build_scenario(seed, n, ())
        out["background"].append((scn, S.run_scenario(scn, tmp_path_factory.mktemp(scn.name))))
    return out


def merged(runs: dict[str, Any], key: str) -> dict[str, Any]:
    return S.merge([S.evaluate(scn, res) for scn, res in runs[key]])


def test_scenarios_are_deterministic():
    a = S.build_scenario(5, 300)
    b = S.build_scenario(5, 300)
    assert [i.interaction_id for i in a.interactions] == [i.interaction_id for i in b.interactions]
    assert [(t.variant, t.members) for t in a.truth] == [(t.variant, t.members) for t in b.truth]


@pytest.mark.parametrize("detector, min_recall", [
    ("reply_burst", 0.5),
    ("fast_reply", 1.0),
    ("shared_content", 0.9),
    ("lookalike", 0.75),
    ("one_way", 1.0),
    ("tight_group", 1.0),
    ("co_action", 0.8),
    ("profile_change", 0.8),
    ("mass_tagging", 0.75),
    ("community_anomaly", 0.75),
])
def test_planted_campaigns_are_found(runs, detector, min_recall):
    d = merged(runs, "planted")["detectors"][detector]
    assert d["pos"] > 0
    assert S.recall(d) >= min_recall, (detector, d["tp"], d["pos"], d["missed"])


def test_owner_impersonation_is_always_high(runs):
    for scn, res in runs["planted"]:
        flagged = S.flagged_accounts(res, "lookalike")
        for t in scn.truth:
            if t.detector == "lookalike" and t.positive and t.variant.startswith("owner_"):
                assert set(t.members) <= flagged, (scn.name, t.variant)


def test_extreme_profiles_are_listed(runs):
    for scn, res in runs["planted"]:
        listed = {f["members"][0] for f in res.findings if f["detector"] == "profile_outliers"}
        for t in scn.truth:
            if t.detector == "profile_outliers" and t.variant in ("posts_per_day_extreme", "age_days_1to5", "age_days_2to10"):
                assert t.members[0] in listed, (scn.name, t.variant)


def test_hard_negatives_are_not_flagged(runs):
    # neg_three_accounts of reply_burst is a known near-threshold case: an organic
    # reply in the same minutes can make it 3 young accounts of 4 (see the report).
    hits = []
    for scn, res in runs["planted"]:
        ev = S.evaluate(scn, res)
        for det, d in ev["detectors"].items():
            hits += [(scn.name, det, v) for v in d["neg_hits"] if v != "neg_three_accounts"]
    assert hits == []


def test_coordination_detectors_are_quiet_on_background(runs):
    m = merged(runs, "background")
    for det in ("reply_burst", "creation_cluster", "fast_reply", "shared_content", "lookalike",
                "co_action", "profile_change", "mass_tagging", "community_anomaly"):
        assert m["detectors"][det]["findings"] <= 1, (det, m["detectors"][det])


def test_seed_timelines_and_profile_history_are_in_the_scenarios(runs):
    scn, res = runs["background"][0]
    assert len(scn.seeds) == S.SEED_COUNT
    assert scn.history, "no earlier profile fetches"
    tagged = {i.target_user_id for i in scn.interactions if i.source_user_id in set(scn.seeds)}
    outsiders = [u for u, r in scn.roles.items() if r == "outsider" and u in tagged]
    assert len(outsiders) >= 50 and all(u in res.states for u in outsiders)


def test_background_accounts_mostly_stay_no_signals(runs):
    st = merged(runs, "background")["states"]["background"]
    total = sum(st.values())
    flagged = st.get("some_signals", 0) + st.get("many_signals", 0)
    assert flagged / total <= 0.05, st
    assert st.get("many_signals", 0) <= 1, st


def test_planted_coordination_members_get_their_rule(runs):
    st = merged(runs, "planted")["states"]
    assert st["coordination_members_with_rule"] >= 0.6 * st["coordination_members_total"], st


def test_finding_text_has_no_forbidden_word_and_no_handle(runs):
    word = re.compile("|".join(FORBIDDEN_WORDS), re.IGNORECASE)
    for key in ("planted", "background"):
        for scn, res in runs[key]:
            handles = [str(p.handle).casefold() for p in scn.profiles if p.handle and len(str(p.handle)) >= 4]
            for f in res.findings:
                text = f"{f['title']} {f['summary']}"
                assert not word.search(text), text
                # A handle as a whole word: generated handles such as "Younge" are also parts of English words.
                tokens = set(re.findall(r"[a-z0-9_]+", text.casefold()))
                assert not any(h in tokens for h in handles), text


def test_each_state_has_causes_and_profile_rules_alone_never_give_many(runs):
    from visualx.score.engine import load_rules

    families = {r.id: r.family for r in load_rules(S.RULES_PATH).rules}
    for key in ("planted", "background"):
        for scn, res in runs[key]:
            for user, state in res.states.items():
                fired = res.rules.get(user, set())
                if state in ("some_signals", "many_signals"):
                    assert fired, (scn.name, user)
                if state == "many_signals":
                    assert {families[r] for r in fired} & {"behavior", "impersonation"}, (scn.name, user, fired)
