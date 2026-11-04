"""Release checks: TSC-24 secret scan, TSC-25 license check, BSC-05 policy checklist.

Run: .venv/Scripts/python tools/release_checks.py [--data DIR]
Exit code 0 when all checks pass. Output holds file paths and rule names only,
never the matched secret text.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --- TSC-24 ---------------------------------------------------------------
SECRET_PATTERNS = {
    "bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}=*"),
    "oauth access/refresh token field": re.compile(r'"(access_token|refresh_token)"\s*:\s*"[^"]{8,}"'),
    "X/Twitter app token (AAAA...)": re.compile(r"\bAAAAAAAAAAAAAAAAAAAAA[A-Za-z0-9%]{30,}"),
    "OpenAI key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "xAI key": re.compile(r"\bxai-[A-Za-z0-9]{20,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    "Anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"),
}
SKIP_DIRS = {".venv", "node_modules", ".git", "__pycache__", ".pytest_cache", ".claude"}
TEXT_SUFFIX = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".jsonl", ".html", ".js", ".j2", ".cfg", ".ini", ".lock", ""}


def _iter_files(base: Path):
    for p in base.rglob("*"):
        if any(part in SKIP_DIRS for part in p.relative_to(base).parts):
            continue
        if p.is_file():
            yield p


def scan_secrets(paths: list[Path]) -> list[str]:
    hits = []
    for base in paths:
        if not base.exists():
            continue
        for p in _iter_files(base) if base.is_dir() else [base]:
            if p.name.endswith(".key.txt") or p.name == "log_hmac.key":
                continue  # key files are covered by .gitignore and never read here
            try:
                if p.suffix == ".gz":
                    text = gzip.open(p, "rt", encoding="utf-8", errors="replace").read()
                elif p.suffix in TEXT_SUFFIX:
                    text = p.read_text(encoding="utf-8", errors="replace")
                else:
                    continue
            except OSError as e:
                hits.append(f"{p}: cannot read ({e.__class__.__name__})")
                continue
            for name, pat in SECRET_PATTERNS.items():
                if pat.search(text):
                    hits.append(f"{p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}: {name}")
    return hits


def gitignore_covers_keys() -> list[str]:
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    need = ["*.key.txt", "data/", "*.sqlite", "map_*.html", "config/overrides.yaml", "config/allowlist.yaml"]
    return [f".gitignore does not cover {n}" for n in need if n not in gi]


# --- TSC-25 ---------------------------------------------------------------
OK_LICENSE = re.compile(
    r"\b(MIT|BSD|Apache|ISC|PSF|Python Software Foundation|LGPL|GPL-?2\.0-or-later|GPLv2\+|GPL-?3|GPLv3|"
    r"GNU General Public License v2 or later|GNU General Public License v3|GNU Lesser|MPL-?2\.0|"
    r"Mozilla Public License 2\.0|Unicode|Zlib|Unlicense|0BSD|CC0)\b",
    re.IGNORECASE,
)
BAD_LICENSE = re.compile(r"\b(GPL-?2\.0-only|GPLv2 only|CC-BY-NC|Non-?Commercial|SSPL|BUSL|Commons Clause)\b", re.IGNORECASE)


# Packages whose metadata is too generic for the pattern, with the checked license.
KNOWN = {
    # Metadata says only "GPL"; the wheel ships the GPL v2 text. python-igraph and the
    # igraph C core state "GPL-2.0-or-later" upstream, which GPL-3.0 can use (AR-01).
    "igraph": "GPL-2.0-or-later (upstream statement; wheel ships the GPLv2 text)",
}


def _dist_license(d: metadata.Distribution) -> str:
    md = d.metadata
    parts = [md.get("License-Expression") or "", md.get("License") or ""]
    parts += [c for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    return " | ".join(p for p in parts if p)[:300]


def check_python_licenses() -> tuple[list[str], list[str]]:
    bad, report = [], []
    for d in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        name = d.metadata["Name"]
        lic = _dist_license(d)
        report.append(f"{name} {d.version}: {lic or 'NOT DECLARED'}")
        if name and name.lower() == "visualx":
            continue
        if name and name.lower() in KNOWN:
            report[-1] += f" -> checked: {KNOWN[name.lower()]}"
            continue
        if BAD_LICENSE.search(lic) or not OK_LICENSE.search(lic):
            bad.append(f"{name} {d.version}: license not known to be GPL-3.0 compatible ({lic or 'not declared'})")
    return bad, report


def check_vendor_licenses() -> list[str]:
    vend = ROOT / "src" / "visualx" / "render" / "vendor"
    if not vend.exists():
        return ["vendor folder missing"]
    lock = vend / "VENDOR.lock"
    if not lock.exists():
        return ["VENDOR.lock missing"]
    text = lock.read_text(encoding="utf-8")
    bad = []
    if BAD_LICENSE.search(text):
        bad.append("VENDOR.lock names a license that is not compatible")
    if "@cosmograph" in text:
        bad.append("@cosmograph package is forbidden (CC-BY-NC-4.0)")
    if not list(vend.glob("*LICENSE*")):
        bad.append("vendor license texts missing")
    return bad


# --- BSC-05 ---------------------------------------------------------------
SCRAPE_LIBS = re.compile(r"^\s*(import|from)\s+(selenium|playwright|pyppeteer|bs4|scrapy|snscrape|twscrape|tweepy|ntscraper|requests_html)\b", re.M)


def bsc05() -> list[str]:
    out = []
    src = ROOT / "src"
    for p in src.rglob("*.py"):
        t = p.read_text(encoding="utf-8")
        if SCRAPE_LIBS.search(t):
            out.append(f"(a) {p.relative_to(ROOT)} imports a scraping or browser-automation library")
        if re.search(r"(switch|rotate)_?(token|app|key)", t, re.I):
            out.append(f"(b) {p.relative_to(ROOT)} looks like token or app rotation")
        rel = p.relative_to(ROOT).as_posix()
        if re.search(r"\b(node_states|rule_results|findings)\b", t) and not any(
            rel.endswith(ok) for ok in ("store.py", "delete.py", "cli.py", "score/engine.py", "score/patterns.py",
                                        "score/network.py", "score/calibrate.py", "render/render.py",
                                        "snapshot.py")
        ):
            out.append(f"(d) {rel} reads signal states; only score and render may")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    failures: list[str] = []
    s = scan_secrets([ROOT / "src", ROOT / "tests", ROOT / "config", ROOT / "docs", ROOT / "tools",
                      ROOT / "README.md", ROOT / "CLAUDE.md", Path(a.data),
                      # Review outputs can hold a provider error message with a key in a URL
                      # (2026-11-02: a Gemini key in a 402 error). Scan them too.
                      ROOT / ".ai-reviews"])
    failures += [f"TSC-24 {h}" for h in s]
    failures += [f"TSC-24 {h}" for h in gitignore_covers_keys()]
    bad, report = check_python_licenses()
    failures += [f"TSC-25 {b}" for b in bad]
    failures += [f"TSC-25 {b}" for b in check_vendor_licenses()]
    failures += [f"BSC-05 {b}" for b in bsc05()]
    if a.verbose:
        print("\n".join(report))
    if failures:
        print("FAILED:\n  " + "\n  ".join(failures))
        return 1
    print("TSC-24 secret scan: 0 matches. TSC-25 licenses: all compatible. BSC-05 (a)(b)(d): no finding.")
    print("BSC-05 (c) is the TR-06 test in tests/unit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
