"""Calibration (FR-38): a stratified sample, the owner's labels, and precision for each state, rule, and detector.

Three steps:

1. ``sample`` takes a stratified sample of the scored accounts: all or up to
   ``per_state`` accounts with ``many_signals`` and with ``some_signals``, up to
   ``per_detector`` members of the findings of each detector at its detection
   level (``DETECTION_LEVEL``, default ``notable``; accounts that are not
   already in a state stratum), and ``no_signals`` accounts at random that are
   in no such finding.
   It records each sampled account with a snapshot of its state, its rules,
   and its detectors in ``calibration_samples``. It writes a labeling sheet
   (CSV) in ``<data>/calibration/``.
2. The owner opens the sheet, looks at each account on X, and writes one
   label: ``concern`` (the account shows inauthentic, abusive, or
   impersonation behavior that the owner wants to know about), ``fine`` (a
   normal account), or ``unsure``. ``import_labels`` reads the sheet into
   ``calibration_labels``.
3. ``report`` gives, for each stratum, rule, and detector, the share of
   ``concern`` among the ``concern`` and ``fine`` labels, with a Wilson 95 %
   interval. For ``no_signals`` this share is the miss rate.

Design points:

- The sheet is blind: it has no state, rule, finding, or score. A label that
  the owner gives without the tool's opinion is a better measurement, and no
  file other than the rendered page holds a signal state of an account
  (invariant 8, BSC-05). The order of the rows is random, so the position does
  not show the stratum.
- The report holds only counts and rule and detector names, never a handle
  or a user ID.
- Text from X is hostile (SR-02). A spreadsheet runs a cell that starts with
  ``=``, ``+``, ``-``, ``@``, a tab, or a carriage return as a formula, so
  each such cell gets a leading apostrophe.
- Deletion (AD-10) removes the rows of a user from the two tables and from
  each sheet (``scrub_sheets``).
"""

from __future__ import annotations

import csv
import json
import math
import random
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LABELS: tuple[str, ...] = ("concern", "fine", "unsure")
STATE_STRATA: tuple[str, ...] = ("many_signals", "some_signals")
SHEET_COLUMNS: tuple[str, ...] = ("sample_id", "user_id", "handle", "name", "profile_url", "label", "note")
FORMULA_START: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")
SEVERITY_RANK: dict[str, int] = {"info": 0, "notable": 1, "high": 2}
# The level at which a finding counts as a detection, the same as in the benchmark
# (tests/validation/scenarios.py). Below it a finding is a fact, not a signal: for
# example most "info" bursts are popular posts.
DETECTION_LEVEL: dict[str, str] = {"lookalike": "high", "profile_outliers": "info", "one_way": "info",
                                   "tight_group": "info"}


class CalibrationError(ValueError):
    """A labeling sheet that the tool cannot import."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_cell(v: Any) -> str:
    """A CSV cell that a spreadsheet does not run as a formula."""
    s = "" if v is None else str(v)
    return "'" + s if s.startswith(FORMULA_START) else s


def sheet_dir(store: Any) -> Path:
    return Path(store.path).parent / "calibration"


# ---------------------------------------------------------------------------
# 1. Sample
# ---------------------------------------------------------------------------


def sample(store: Any, per_state: int = 20, per_detector: int = 10, no_signals: int = 30,
           seed: int | None = None) -> dict[str, Any]:
    """Take a stratified sample, record it, and write the labeling sheet. Returns the batch summary."""
    run = store.latest_stage_run("score")
    if run is None:
        raise CalibrationError("No score output. Run 'visualx score' first.")
    ego = store.ego_id
    states = {r["user_id"]: r["state"] for r in store.q("SELECT user_id, state FROM node_states")}
    rules: dict[str, list[str]] = defaultdict(list)
    for r in store.q("SELECT user_id, rule_id FROM rule_results ORDER BY rule_id"):
        rules[r["user_id"]].append(r["rule_id"])
    dets: dict[str, set[str]] = defaultdict(set)
    for r in store.q("SELECT detector, severity, members_json FROM findings"):
        if SEVERITY_RANK.get(r["severity"], 0) < SEVERITY_RANK[DETECTION_LEVEL.get(r["detector"], "notable")]:
            continue
        for u in json.loads(r["members_json"]):
            dets[u].add(r["detector"])
    profiles = {r["user_id"]: (r["handle"], r["name"]) for r in store.q("SELECT user_id, handle, name FROM profiles")}
    already = {r["user_id"] for r in store.q("SELECT user_id FROM calibration_samples")}

    def ok(u: str) -> bool:
        return u != ego and u in states and states[u] != "not_scored" and bool(profiles.get(u, (None,))[0])

    rng = random.Random(seed)
    strata: dict[str, list[str]] = {}
    for st in STATE_STRATA:
        strata[st] = sorted(u for u, s in states.items() if s == st and ok(u))
    taken = {u for st in STATE_STRATA for u in strata[st]}
    for det in sorted({d for ds in dets.values() for d in ds}):
        strata[f"finding:{det}"] = sorted(u for u, ds in dets.items() if det in ds and ok(u) and u not in taken)
    strata["no_signals"] = sorted(u for u, s in states.items() if s == "no_signals" and ok(u) and not dets.get(u))
    limits = {st: per_state for st in STATE_STRATA}
    chosen: list[tuple[str, str, int]] = []  # (user, stratum, stratum size)
    used: set[str] = set()
    for name, pool in strata.items():
        lim = limits.get(name, no_signals if name == "no_signals" else per_detector)
        # Accounts that a previous batch labeled come last: a new batch adds new labels first.
        fresh = [u for u in pool if u not in used and u not in already]
        old = [u for u in pool if u not in used and u in already]
        rng.shuffle(fresh)
        rng.shuffle(old)
        for u in (fresh + old)[:lim]:
            chosen.append((u, name, len(pool)))
            used.add(u)
    if not chosen:
        raise CalibrationError("No scored account with a profile to sample.")
    batch = "cal_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:4]
    created = _now()
    rng.shuffle(chosen)
    rows = []
    with store.tx() as c:
        for k, (u, name, size) in enumerate(chosen, 1):
            sid = f"{batch}_{k:03d}"
            c.execute("INSERT INTO calibration_samples VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (sid, batch, u, name, states[u], json.dumps(rules.get(u, [])),
                       json.dumps(sorted(dets.get(u, set()))), size, run["stage_run_id"], created))
            h, n = profiles[u]
            rows.append({"sample_id": sid, "user_id": u, "handle": h, "name": n or "",
                         "profile_url": f"https://x.com/{h}", "label": "", "note": ""})
    d = sheet_dir(store)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"labels_{batch}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: safe_cell(v) for k, v in r.items()})
    sizes: dict[str, int] = defaultdict(int)
    for _, name, _ in chosen:
        sizes[name] += 1
    return {"batch": batch, "accounts": len(chosen), "sheet": str(path), "strata": dict(sorted(sizes.items()))}


# ---------------------------------------------------------------------------
# 2. Import
# ---------------------------------------------------------------------------


def import_labels(store: Any, path: Path) -> dict[str, Any]:
    """Read a labeling sheet. A row with an empty label is skipped. A new label replaces an old one."""
    path = Path(path)
    if not path.exists():
        raise CalibrationError(f"No file {path}")
    known = {r["sample_id"]: r["user_id"] for r in store.q("SELECT sample_id, user_id FROM calibration_samples")}
    errors: list[str] = []
    good: list[tuple[str, str, str, str]] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = {"sample_id", "label"} - set(reader.fieldnames or [])
        if missing:
            raise CalibrationError(f"{path.name}: no column {sorted(missing)}")
        for n, row in enumerate(reader, 2):
            sid = (row.get("sample_id") or "").strip()
            label = (row.get("label") or "").strip().casefold()
            if not label:
                continue
            if sid not in known:
                errors.append(f"line {n}: sample_id {sid!r} is not in the store")
                continue
            if label not in LABELS:
                errors.append(f"line {n}: label {label!r} is not one of {', '.join(LABELS)}")
                continue
            note = (row.get("note") or "").strip()
            if note.startswith("'") and note[1:].startswith(FORMULA_START):
                note = note[1:]
            good.append((sid, known[sid], label, note[:500]))
    if errors:
        raise CalibrationError("The sheet has errors, no label was imported:\n  " + "\n  ".join(errors[:20]))
    at = _now()
    with store.tx() as c:
        for sid, u, label, note in good:
            c.execute("INSERT OR REPLACE INTO calibration_labels VALUES (?,?,?,?,?)", (sid, u, label, note, at))
    counts: dict[str, int] = defaultdict(int)
    for _, _, label, _ in good:
        counts[label] += 1
    return {"imported": len(good), "labels": dict(sorted(counts.items()))}


# ---------------------------------------------------------------------------
# 3. Report
# ---------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for k of n (95 % by default). None for n = 0."""
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def _cell(k: int, n: int) -> dict[str, Any]:
    ci = wilson(k, n)
    return {"concern": k, "decided": n, "share": None if n == 0 else round(k / n, 3),
            "ci95": None if ci is None else [round(ci[0], 3), round(ci[1], 3)]}


def report(store: Any, batch: str | None = None) -> dict[str, Any]:
    """Precision for each stratum, rule, and detector from the labeled samples (newest label of each sample)."""
    sql = ("SELECT s.sample_id, s.batch_id, s.stratum, s.state, s.rules_json, s.detectors_json, s.stratum_size, "
           "l.label FROM calibration_samples s LEFT JOIN calibration_labels l ON l.sample_id = s.sample_id")
    args: tuple[Any, ...] = ()
    if batch:
        sql += " WHERE s.batch_id = ?"
        args = (batch,)
    rows = [dict(r) for r in store.q(sql, args)]
    if not rows:
        raise CalibrationError("No calibration sample. Run 'visualx calibrate sample' first.")
    strata: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_rule: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_det: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    size: dict[str, int] = {}
    for r in rows:
        st = r["stratum"]
        size[st] = max(size.get(st, 0), int(r["stratum_size"]))
        lab = r["label"] or "unlabeled"
        strata[st][lab] += 1
        if lab not in ("concern", "fine"):
            continue
        hit = 1 if lab == "concern" else 0
        for rid in json.loads(r["rules_json"]):
            by_rule[rid][0] += hit
            by_rule[rid][1] += 1
        for det in json.loads(r["detectors_json"]):
            by_det[det][0] += hit
            by_det[det][1] += 1
    out_strata = {}
    for st, c in sorted(strata.items()):
        k, n = c.get("concern", 0), c.get("concern", 0) + c.get("fine", 0)
        out_strata[st] = {**_cell(k, n), "sampled": sum(c.values()), "unsure": c.get("unsure", 0),
                          "unlabeled": c.get("unlabeled", 0), "stratum_size": size[st]}
    ns = out_strata.get("no_signals")
    missed = None
    if ns and ns["decided"]:
        ci = ns["ci95"]
        missed = {"estimate": round(ns["share"] * ns["stratum_size"]),
                  "ci95": [round(ci[0] * ns["stratum_size"]), round(ci[1] * ns["stratum_size"])]}
    return {
        "batches": sorted({r["batch_id"] for r in rows}),
        "strata": out_strata,
        "rules": {k: _cell(*v) for k, v in sorted(by_rule.items())},
        "detectors": {k: _cell(*v) for k, v in sorted(by_det.items())},
        "no_signals_missed_accounts": missed,
    }


def _pct(c: dict[str, Any]) -> str:
    if c["share"] is None:
        return "no decided label"
    lo, hi = c["ci95"]
    return f"{round(100 * c['share'])} % concern of {c['decided']} (95 % interval {round(100 * lo)} to {round(100 * hi)} %)"


def report_text(rep: dict[str, Any]) -> str:
    lines = [f"Calibration report ({', '.join(rep['batches'])})", "", "Strata (share of 'concern' among 'concern' and 'fine'):"]
    for st, c in rep["strata"].items():
        lines.append(f"  {st:28s} {_pct(c)}; sampled {c['sampled']} of {c['stratum_size']}, "
                     f"unsure {c['unsure']}, unlabeled {c['unlabeled']}")
    m = rep["no_signals_missed_accounts"]
    if m:
        lines.append(f"  Estimated 'no_signals' accounts that are a concern: {m['estimate']} "
                     f"(95 % interval {m['ci95'][0]} to {m['ci95'][1]})")
    for key, title in (("rules", "Rules"), ("detectors", "Detectors")):
        lines.append("")
        lines.append(f"{title} (labeled accounts where it fired):")
        if not rep[key]:
            lines.append("  none")
        for name, c in rep[key].items():
            lines.append(f"  {name:28s} {_pct(c)}")
    lines.append("")
    lines.append("A small count gives a wide interval. Label more batches to make the intervals narrower.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deletion (AD-10)
# ---------------------------------------------------------------------------


def scrub_sheets(store: Any, user_id: str) -> int:
    """Remove the rows of ``user_id`` from each labeling sheet. Returns the number of rows removed."""
    d = sheet_dir(store)
    removed = 0
    for path in sorted(d.glob("labels_*.csv")) if d.exists() else []:
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or list(SHEET_COLUMNS)
            rows = list(reader)
        keep = [r for r in rows if (r.get("user_id") or "").lstrip("'") != user_id]
        if len(keep) == len(rows):
            continue
        removed += len(rows) - len(keep)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(keep)
    return removed


def sheets_with(store: Any, user_id: str) -> list[str]:
    """Labeling sheets that still hold ``user_id`` (TSC-09)."""
    d = sheet_dir(store)
    out = []
    for path in sorted(d.glob("labels_*.csv")) if d.exists() else []:
        with path.open(encoding="utf-8-sig", newline="") as f:
            if any((r.get("user_id") or "").lstrip("'") == user_id for r in csv.DictReader(f)):
                out.append(f"calibration:{path.name}")
    return out
