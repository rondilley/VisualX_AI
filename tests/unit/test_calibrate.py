"""Calibration (FR-38): the stratified sample, the blind sheet, the import, the report, and deletion."""

from __future__ import annotations

import csv
import json

import pytest

from tests.unit.test_rules_kit import EGO, ego_profile, interaction, profile, score
from visualx.cache import Cache
from visualx.delete import delete_ids, find_id
from visualx.score import calibrate as C

NEW = "2026-10-20T00:00:00.000Z"
WORDS = ["maple", "harbor", "quartz", "falcon", "juniper", "tundra", "copper", "violet", "saffron", "glacier",
         "orchid", "pebble", "lantern", "meadow", "cobalt", "thistle", "walnut", "ember", "fjord", "indigo",
         "magnet", "nectar", "oasis", "prairie", "quiver", "rhubarb", "sequoia", "trellis", "umber", "vortex"]


def scored(tmp_path, normal: int = 30, evil_name: str = "Ron Dilley"):
    profs = [ego_profile(),
             profile("201", handle="rondillley", name=evil_name, description="", url="", location="", created_at=NEW),
             profile("202", handle="newperson_x", name="New Person", description="", url="", location="", created_at=NEW)]
    profs += [profile(str(300 + k), handle=f"{WORDS[k]}_{WORDS[(k * 7 + 3) % 30]}", name=f"Reader {WORDS[k].title()}")
              for k in range(normal)]
    items = [interaction(src=p.user_id, text=f"Reply {k} about {WORDS[k % 30]} and {WORDS[(k + 5) % 30]} graphs")
             for k, p in enumerate(profs[1:])]
    return score(tmp_path, profiles=profs, interactions=items)


def read_sheet(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_labels(path, labels: dict[str, str]):
    rows = read_sheet(path)
    for r in rows:
        r["label"] = labels.get(r["user_id"], "")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=C.SHEET_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def test_sample_is_stratified_and_the_sheet_is_blind(tmp_path):
    s = scored(tmp_path)
    assert s.states["201"] == "many_signals" and s.states["202"] == "some_signals"
    res = C.sample(s.store, per_state=20, per_detector=10, no_signals=10, seed=1)
    assert res["strata"]["many_signals"] == 1 and res["strata"]["some_signals"] == 1
    assert res["strata"]["no_signals"] == 10
    rows = read_sheet(res["sheet"])
    assert list(rows[0]) == list(C.SHEET_COLUMNS)
    ids = [r["user_id"] for r in rows]
    assert EGO not in ids and len(set(ids)) == len(ids)
    text = open(res["sheet"], encoding="utf-8-sig").read().casefold()
    for word in ("signals", "many", "some_", "i01", "p03", "lookalike", "stratum", "finding"):
        assert word not in text, word
    snap = {r["user_id"]: r for r in s.store.q("SELECT * FROM calibration_samples")}
    assert snap["201"]["state"] == "many_signals" and "I01" in json.loads(snap["201"]["rules_json"])


def test_a_name_that_starts_like_a_formula_is_not_a_formula_in_the_sheet(tmp_path):
    s = scored(tmp_path, evil_name='=HYPERLINK("https://example.com","x")')
    res = C.sample(s.store, no_signals=5, seed=1)
    row = next(r for r in read_sheet(res["sheet"]) if r["user_id"] == "201")
    assert row["name"].startswith("'=")
    assert C.safe_cell("+1") == "'+1" and C.safe_cell("@x") == "'@x" and C.safe_cell("plain") == "plain"


def test_import_and_report_give_precision_with_intervals(tmp_path):
    s = scored(tmp_path)
    res = C.sample(s.store, no_signals=10, seed=1)
    ns = [r["user_id"] for r in read_sheet(res["sheet"]) if r["user_id"] not in ("201", "202")]
    labels = {"201": "concern", "202": "fine"}
    labels.update({u: "fine" for u in ns[:9]})
    labels[ns[9]] = "concern"
    write_labels(res["sheet"], labels)
    imp = C.import_labels(s.store, res["sheet"])
    assert imp == {"imported": 12, "labels": {"concern": 2, "fine": 10}}
    rep = C.report(s.store)
    assert rep["strata"]["many_signals"]["share"] == 1.0
    assert rep["strata"]["some_signals"]["share"] == 0.0
    assert rep["strata"]["no_signals"]["share"] == 0.1 and rep["strata"]["no_signals"]["decided"] == 10
    assert rep["rules"]["I01"]["concern"] == 1 and rep["rules"]["I01"]["decided"] == 1
    assert rep["no_signals_missed_accounts"]["estimate"] == round(0.1 * rep["strata"]["no_signals"]["stratum_size"])
    text = C.report_text(rep)
    assert "rondillley" not in text and "201" not in text.split("(")[0]
    for r in read_sheet(res["sheet"]):
        assert r["handle"] not in text


def test_import_stops_on_a_bad_label_and_imports_nothing(tmp_path):
    s = scored(tmp_path)
    res = C.sample(s.store, no_signals=5, seed=1)
    write_labels(res["sheet"], {"201": "concern", "202": "maybe"})
    with pytest.raises(C.CalibrationError, match="maybe"):
        C.import_labels(s.store, res["sheet"])
    assert s.store.q("SELECT * FROM calibration_labels") == []


def test_a_new_label_replaces_the_old_label(tmp_path):
    s = scored(tmp_path)
    res = C.sample(s.store, no_signals=5, seed=1)
    write_labels(res["sheet"], {"201": "unsure"})
    C.import_labels(s.store, res["sheet"])
    write_labels(res["sheet"], {"201": "concern"})
    C.import_labels(s.store, res["sheet"])
    assert [r["label"] for r in s.store.q("SELECT label FROM calibration_labels")] == ["concern"]


def test_a_second_batch_takes_unlabeled_accounts_first(tmp_path):
    s = scored(tmp_path, normal=30)
    a = {r["user_id"] for r in read_sheet(C.sample(s.store, no_signals=10, seed=1)["sheet"])}
    b = {r["user_id"] for r in read_sheet(C.sample(s.store, no_signals=10, seed=2)["sheet"])}
    assert len((a & b) - {"201", "202"}) == 0


def test_wilson_interval():
    lo, hi = C.wilson(0, 10)
    assert lo == 0.0 and abs(hi - 0.2775) < 0.001
    lo, hi = C.wilson(5, 10)
    assert abs(lo - 0.2366) < 0.001 and abs(hi - 0.7634) < 0.001
    assert C.wilson(0, 0) is None


def test_deletion_removes_the_user_from_the_samples_labels_and_sheets(tmp_path):
    s = scored(tmp_path)
    res = C.sample(s.store, no_signals=5, seed=1)
    write_labels(res["sheet"], {"201": "concern"})
    C.import_labels(s.store, res["sheet"])
    cache = Cache(s.store, tmp_path / "cache")
    rep = delete_ids(s.store, cache, tmp_path / "config", ["201"], "user")
    assert rep.calibration_rows_removed == 1
    assert all(r["user_id"] != "201" for r in read_sheet(res["sheet"]))
    assert s.store.q("SELECT * FROM calibration_samples WHERE user_id='201'") == []
    assert s.store.q("SELECT * FROM calibration_labels WHERE user_id='201'") == []
    assert find_id(s.store, cache, "201") == []


def test_sample_needs_a_score_output(tmp_path):
    from tests.unit.test_rules_kit import make_store

    st = make_store(tmp_path)
    with pytest.raises(C.CalibrationError, match="score"):
        C.sample(st)
