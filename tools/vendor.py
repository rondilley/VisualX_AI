"""Fetch and verify the vendor JavaScript for the renderer (SR-04, TSC-31, AD-09).

Usage (from the project root, with the project venv):

    python tools/vendor.py fetch     # download, check npm integrity, extract, write VENDOR.lock
    python tools/vendor.py verify    # check each vendor file against VENDOR.lock (exit 1 on a difference)

Chain of checks:

1. Each package is pinned to an exact version in ``PACKAGES``.
2. ``fetch`` reads ``dist.integrity`` (sha512) for that version from the npm
   registry, downloads the tarball, and stops if the tarball hash is different.
   If a VENDOR.lock exists, the registry value must also agree with the
   integrity value in the lock (a changed upstream tarball stops the fetch).
3. Only the named files are read from the tarball (no ``extractall``, so a
   tarball cannot write outside the vendor folder). Each file gets a SHA-256
   value in VENDOR.lock.
4. ``render`` checks every file against VENDOR.lock at each render and stops on
   a difference (src/visualx/render/render.py).

All packages are MIT (GPL-3.0 compatible, AR-01). The license texts go to
``vendor/licenses/``. "notice" packages are code that the upstream bundles
contain (for example ``events`` inside sigma.min.js); only their license file
is extracted.

The only network access is to registry.npmjs.org.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REGISTRY = "https://registry.npmjs.org"
ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "src" / "visualx" / "render" / "vendor"
LOCK_NAME = "VENDOR.lock"
LOCK_VERSION = 1
TIMEOUT_S = 60
ALLOWED_LICENSES = frozenset({"MIT", "ISC", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0"})

# role "inline": the file goes into the page, in the order of this list.
# role "notice": only the license file is kept (code bundled inside an inline file).
PACKAGES: list[dict[str, Any]] = [
    {
        "name": "graphology", "version": "0.26.0", "role": "inline",
        "files": {"package/dist/graphology.umd.min.js": "graphology.umd.min.js",
                  "package/LICENSE.txt": "licenses/graphology.LICENSE.txt"},
        "global": "graphology",
    },
    {
        "name": "sigma", "version": "4.0.4", "role": "inline",
        "files": {"package/dist/sigma.min.js": "sigma.min.js",
                  "package/LICENSE.txt": "licenses/sigma.LICENSE.txt"},
        "global": "Sigma",
    },
    {
        "name": "graphology-library", "version": "0.8.0", "role": "inline",
        "files": {"package/dist/graphology-library.min.js": "graphology-library.min.js",
                  "package/LICENSE.txt": "licenses/graphology-library.LICENSE.txt"},
        "global": "graphologyLibrary",
    },
    # Bundled inside sigma.min.js and graphology.umd.min.js.
    {"name": "events", "version": "3.3.0", "role": "notice",
     "files": {"package/LICENSE": "licenses/events.LICENSE.txt"}},
    {"name": "graphology-utils", "version": "2.5.2", "role": "notice",
     "files": {"package/LICENSE.txt": "licenses/graphology-utils.LICENSE.txt"}},
    # Bundled inside graphology-library.min.js (shortest-path and louvain use them).
    {"name": "mnemonist", "version": "0.39.8", "role": "notice",
     "files": {"package/LICENSE.txt": "licenses/mnemonist.LICENSE.txt"}},
    {"name": "obliterator", "version": "2.0.4", "role": "notice",
     "files": {"package/LICENSE.txt": "licenses/obliterator.LICENSE.txt"}},
]


class VendorError(RuntimeError):
    """A vendor check failed. The caller must stop."""


def _get(url: str) -> bytes:
    if not url.startswith(REGISTRY + "/"):
        raise VendorError(f"refusing a URL outside the npm registry: {url}")
    req = urllib.request.Request(url, headers={"Accept": "application/json, application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise VendorError(f"download failed: {url}: {e}") from e


def sri_sha512(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def registry_meta(name: str, version: str) -> dict[str, Any]:
    raw = _get(f"{REGISTRY}/{name}/{version}")
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError as e:
        raise VendorError(f"{name}@{version}: registry answer is not JSON") from e
    if meta.get("version") != version:
        raise VendorError(f"{name}@{version}: registry gave version {meta.get('version')!r}")
    return meta


def check_tarball(name: str, version: str, data: bytes, integrity: str) -> None:
    if not integrity.startswith("sha512-"):
        raise VendorError(f"{name}@{version}: registry integrity is not sha512: {integrity!r}")
    got = sri_sha512(data)
    if got != integrity:
        raise VendorError(f"{name}@{version}: tarball integrity {got} does not agree with registry {integrity}")


def extract_files(name: str, data: bytes, wanted: dict[str, str]) -> dict[str, bytes]:
    """Read only the named regular files from the tarball."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf.getmembers():
            if member.name in wanted:
                if not member.isfile():
                    raise VendorError(f"{name}: {member.name} is not a regular file")
                f = tf.extractfile(member)
                if f is None:
                    raise VendorError(f"{name}: cannot read {member.name}")
                out[member.name] = f.read()
    missing = sorted(set(wanted) - set(out))
    if missing:
        raise VendorError(f"{name}: tarball has no {missing}")
    return out


def _safe_dest(vendor_dir: Path, dest: str) -> Path:
    p = (vendor_dir / dest).resolve()
    if vendor_dir.resolve() not in p.parents:
        raise VendorError(f"destination {dest!r} is outside the vendor folder")
    return p


def load_lock(vendor_dir: Path = VENDOR_DIR) -> dict[str, Any]:
    p = vendor_dir / LOCK_NAME
    if not p.exists():
        raise VendorError(f"{p} not found. Run 'python tools/vendor.py fetch'.")
    return json.loads(p.read_text(encoding="utf-8"))


def verify(vendor_dir: Path = VENDOR_DIR) -> list[str]:
    """Return a list of differences between the vendor files and VENDOR.lock (empty = all agree)."""
    lock = load_lock(vendor_dir)
    problems: list[str] = []
    for pkg in lock.get("packages", []):
        for f in pkg.get("files", []):
            p = vendor_dir / f["dest"]
            if not p.is_file():
                problems.append(f"{pkg['name']}: {f['dest']} is missing")
                continue
            got = sha256_hex(p.read_bytes())
            if got != f["sha256"]:
                problems.append(f"{pkg['name']}: {f['dest']} sha256 {got} does not agree with VENDOR.lock {f['sha256']}")
    return problems


def fetch(vendor_dir: Path = VENDOR_DIR, packages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    packages = PACKAGES if packages is None else packages
    old: dict[str, Any] = {}
    lock_path = vendor_dir / LOCK_NAME
    if lock_path.exists():
        old = {f"{p['name']}@{p['version']}": p for p in json.loads(lock_path.read_text(encoding="utf-8")).get("packages", [])}
    staged: list[tuple[Path, bytes]] = []
    lock_pkgs: list[dict[str, Any]] = []
    for spec in packages:
        name, version = spec["name"], spec["version"]
        meta = registry_meta(name, version)
        lic = meta.get("license")
        if lic not in ALLOWED_LICENSES:
            raise VendorError(f"{name}@{version}: license {lic!r} is not on the GPL-3.0 compatible list {sorted(ALLOWED_LICENSES)}")
        dist = meta.get("dist") or {}
        integrity, tarball = dist.get("integrity", ""), dist.get("tarball", "")
        prev = old.get(f"{name}@{version}")
        if prev is not None and prev.get("integrity") != integrity:
            raise VendorError(f"{name}@{version}: registry integrity {integrity} does not agree with VENDOR.lock {prev.get('integrity')}")
        data = _get(tarball)
        check_tarball(name, version, data, integrity)
        files = extract_files(name, data, spec["files"])
        entries = []
        for src, dest in spec["files"].items():
            staged.append((_safe_dest(vendor_dir, dest), files[src]))
            entries.append({"src": src, "dest": dest, "sha256": sha256_hex(files[src]), "bytes": len(files[src])})
        rec: dict[str, Any] = {
            "name": name, "version": version, "license": lic, "role": spec["role"],
            "tarball": tarball, "integrity": integrity, "files": entries,
        }
        if spec.get("global"):
            rec["global"] = spec["global"]
        lock_pkgs.append(rec)
        print(f"ok  {name}@{version}  {integrity}")
    for path, blob in staged:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    lock = {
        "lock_version": LOCK_VERSION,
        "note": "Written by tools/vendor.py. Do not edit. Inline order is the order of role=inline packages.",
        "bundler": None,
        "packages": lock_pkgs,
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    problems = verify(vendor_dir)
    if problems:
        raise VendorError("; ".join(problems))
    return lock


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("fetch", "verify"))
    ap.add_argument("--vendor-dir", type=Path, default=VENDOR_DIR)
    args = ap.parse_args(argv)
    try:
        if args.command == "fetch":
            fetch(args.vendor_dir)
            print(f"wrote {args.vendor_dir / LOCK_NAME}")
            return 0
        problems = verify(args.vendor_dir)
    except VendorError as e:
        print(f"VENDOR CHECK FAILED: {e}", file=sys.stderr)
        return 1
    if problems:
        for p in problems:
            print(f"VENDOR CHECK FAILED: {p}", file=sys.stderr)
        return 1
    print("vendor files agree with VENDOR.lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
