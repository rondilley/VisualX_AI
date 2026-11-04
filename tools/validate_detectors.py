"""Measure the pattern detectors on the labeled benchmark (tests/validation/scenarios.py).

Usage (from the repository root):

    .venv/Scripts/python tools/validate_detectors.py            # full run, writes the report
    .venv/Scripts/python tools/validate_detectors.py --quick    # fewer runs, no report file
    .venv/Scripts/python tools/validate_detectors.py --json out.json

Four run sets:

- tune: planted campaigns in their tune form, seeds 1 to 8. The thresholds
  were set on these runs.
- tune_background: no planted campaign, seeds 201 to 208. Also used to set
  thresholds.
- holdout: planted campaigns in their holdout form (other sizes, timings,
  and look-alike methods), seeds 531 to 538. These seeds were not run before
  the fourth measurement (the minimum of behavior points for many_signals).
- holdout_background: no planted campaign, seeds 631 to 638, not run before
  the fourth measurement.

Earlier held-out sets were used up during development (the report says how):
holdout seeds 101 to 103, 301 to 308, 501 to 508, 511 to 518, and 521 to 528,
background seeds 401 to 408, 601 to 608, 611 to 618, and 621 to 628. The sets
501 to 508 and 601 to 608 were the first measurement, 511 to 518 and 611 to
618 the second (the network detectors), 521 to 528 and 621 to 628 the third
(the B09 and B10 overlap group). Seeds 611 to 618 showed the B09 and B10
weakness, and 621 to 628 showed the profile and B08 weakness.

In a background run, each detection-level finding is a false positive
(except for the info detectors, which report facts; see the report).

Contact counts cycle through 300, 700, 1200, and 2000. The tool writes only
synthetic data to temporary folders. It makes no network call.

The report file has a part that a person writes and a measured part between
the markers ``<!-- measured:start -->`` and ``<!-- measured:end -->``. The
tool replaces only the measured part.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tests.validation import scenarios as S  # noqa: E402

SIZES = (300, 700, 1200, 2000)
START_MARK = "<!-- measured:start -->"
END_MARK = "<!-- measured:end -->"


KINDS = ("tune", "holdout", "tune_background", "holdout_background")
BASE_SEED = {"tune": 1, "holdout": 531, "tune_background": 201, "holdout_background": 631}


def run_one(job: tuple[str, int, int]) -> dict[str, Any]:
    kind, seed, n = job
    camps = () if kind.endswith("background") else S.DETECTORS
    scn = S.build_scenario(seed, n, camps, holdout=(kind == "holdout"))
    with tempfile.TemporaryDirectory() as d:
        res = S.run_scenario(scn, Path(d))
    ev = S.evaluate(scn, res)
    ev["kind"], ev["seed"], ev["n"], ev["seconds"] = kind, seed, n, round(res.seconds, 2)
    ev["accounts"] = len(res.states)
    return ev


def jobs(quick: bool) -> list[tuple[str, int, int]]:
    k = 3 if quick else 8
    out: list[tuple[str, int, int]] = []
    for kind in KINDS:
        base = BASE_SEED[kind]
        for i in range(k):
            out.append((kind, base + i, SIZES[i % len(SIZES)]))
    return out


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100.0 * x:.0f} %"


def table(groups: dict[str, list[dict[str, Any]]]) -> list[str]:
    tune, hold = S.merge(groups["tune"]), S.merge(groups["holdout"])
    tbg, hbg = S.merge(groups["tune_background"]), S.merge(groups["holdout_background"])
    ntb, nhb = max(1, len(groups["tune_background"])), max(1, len(groups["holdout_background"]))
    lines = [
        "| Detector | Level | Recall (tune) | Precision (tune) | Recall (holdout) | Precision (holdout) "
        "| Hard negatives flagged (tune, holdout) | Background findings in each run (tune, holdout) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for det in S.DETECTORS:
        t, h = tune["detectors"][det], hold["detectors"][det]
        lines.append(
            f"| `{det}` | {S.DETECT_LEVEL[det]} | {pct(S.recall(t))} ({t['tp']}/{t['pos']}) | {pct(S.precision(t))} "
            f"| {pct(S.recall(h))} ({h['tp']}/{h['pos']}) | {pct(S.precision(h))} "
            f"| {t['fp_neg']}, {h['fp_neg']} "
            f"| {tbg['detectors'][det]['findings'] / ntb:.1f}, {hbg['detectors'][det]['findings'] / nhb:.1f} |"
        )
    return lines


def details(groups: dict[str, list[dict[str, Any]]]) -> list[str]:
    out: list[str] = []
    for kind in KINDS:
        m = S.merge(groups[kind])
        out.append(f"**`{kind}`** ({len(groups[kind])} runs):")
        out.append("")
        for det in S.DETECTORS:
            d = m["detectors"][det]
            bits = []
            if d["missed"]:
                bits.append("Not found: " + ", ".join(f"`{v}` x{d['missed'].count(v)}" for v in sorted(set(d["missed"]))))
            if d["neg_hits"]:
                bits.append("Hard negatives flagged: " + ", ".join(f"`{v}`" for v in sorted(set(d["neg_hits"]))))
            if d["fp_bg"]:
                roles = d["fp_bg_roles"]
                bits.append(f"Background accounts flagged: {d['fp_bg']} ("
                            + ", ".join(f"`{r}` {roles.count(r)}" for r in sorted(set(roles))) + ")")
            if d["findings_all"] != d["findings"]:
                bits.append(f"Info findings: {d['findings_all'] - d['findings']}")
            if bits:
                out.append(f"- `{det}`:")
                out.extend(f"    - {x}." for x in bits)
        out.append("")
    return out


def states(groups: dict[str, list[dict[str, Any]]]) -> list[str]:
    out = ["| Run set | Background accounts | `no_signals` | `some_signals` | `many_signals` | Some or many |",
           "|---|---|---|---|---|---|"]
    for kind in KINDS:
        st = S.merge(groups[kind])["states"]["background"]
        total = sum(st.values())
        some, many = st.get("some_signals", 0), st.get("many_signals", 0)
        out.append(f"| `{kind}` | {total} | {st.get('no_signals', 0)} | {some} | {many} | "
                   f"{100.0 * (some + many) / max(1, total):.1f} % |")
    out.append("")
    for kind in ("tune", "holdout"):
        st = S.merge(groups[kind])["states"]
        cm = st["coordination_members"]
        out.append(f"- `{kind}`: {st['coordination_members_with_rule']} of {st['coordination_members_total']} "
                   f"planted coordination members have their rule (B02, B09 to B12). States: "
                   + ", ".join(f"`{k}` {v}" for k, v in sorted(cm.items())) + ".")
    for kind in ("tune_background", "holdout_background"):
        bgr = S.merge(groups[kind])["states"]["background_flagged_rules"]
        out.append(f"- Rules on the background accounts with `some_signals` or `many_signals` (`{kind}`): "
                   + ("`" + ", ".join(f"{k} {v}" for k, v in bgr.items()) + "`" if bgr else "none") + ".")
    return out


def report(groups: dict[str, list[dict[str, Any]]], secs: float) -> str:
    runs = [e for g in groups.values() for e in g]
    sizes = sorted({e["n"] for e in runs})
    slow = max(runs, key=lambda e: e["seconds"])
    lines = [
        START_MARK,
        "",
        f"Source: `tools/validate_detectors.py`, {len(runs)} runs, contact counts {', '.join(map(str, sizes))}. "
        f"The longest `score` stage: {slow['seconds']} s ({slow['accounts']} accounts). Total time: {secs:.0f} s.",
        "",
        "### Detection",
        "",
        *table(groups),
        "",
        "### Accounts not found and incorrect flags",
        "",
        *details(groups),
        "### Account states",
        "",
        *states(groups),
        "",
        END_MARK,
    ]
    return "\n".join(lines)


def write_report(path: Path, measured: str) -> None:
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if START_MARK in text and END_MARK in text:
            head = text.split(START_MARK, 1)[0]
            tail = text.split(END_MARK, 1)[1]
            path.write_text(head + measured + tail, encoding="utf-8")
            return
    path.write_text("# Detector validation\n\n## Measured results\n\n" + measured + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--quick", action="store_true", help="3 runs for each set; do not write the report")
    ap.add_argument("--report", default=str(ROOT / "background" / "2026-11-03_detector_validation.md"))
    ap.add_argument("--json", default=None, help="also write the raw evaluation to this file")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    t0 = time.perf_counter()
    js = jobs(args.quick)
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(run_one, js))
    groups: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    for r in results:
        groups[r["kind"]].append(r)
    secs = time.perf_counter() - t0
    measured = report(groups, secs)
    print(measured)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1, default=list), encoding="utf-8")
    if not args.quick:
        write_report(Path(args.report), measured)
        print(f"\nWrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
