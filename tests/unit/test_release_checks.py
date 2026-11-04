"""tools/release_checks.py: the BSC-05 (d) check and the vendor license check (review 20261103, C-17, C-18)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("release_checks", ROOT / "tools" / "release_checks.py")
RC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RC)  # type: ignore[union-attr]


def _module(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_bsc05_uses_exact_paths(tmp_path):
    # C-17: "endswith" passed a file such as export_cli.py.
    _module(tmp_path, "src/visualx/export_cli.py", "rows = store.q('SELECT * FROM node_states')\n")
    _module(tmp_path, "src/visualx/cli.py", "rows = store.q('SELECT * FROM node_states')\n")
    out = RC.bsc05(tmp_path)
    assert out == ["(d) src/visualx/export_cli.py reads signal states; only score and render may"]


def test_bsc05_finds_an_indirect_read(tmp_path):
    # C-17: an export that takes the render payload reads the states without the words.
    _module(tmp_path, "src/visualx/export.py", "from .render import build_payload\n")
    out = RC.bsc05(tmp_path)
    assert out == ["(d) src/visualx/export.py imports build_payload, which give the signal states"]


def test_bsc05_has_no_finding_on_the_repository():
    assert RC.bsc05() == []


def test_vendor_license_check_needs_each_listed_file(tmp_path):
    # C-18: a glob for "*LICENSE*" matched the folder "licenses" itself.
    vend = tmp_path / "vendor"
    (vend / "licenses").mkdir(parents=True)
    lock = {"packages": [{"name": "sigma", "license": "MIT",
                          "files": [{"dest": "sigma.min.js"}, {"dest": "licenses/sigma.LICENSE.txt"}]}]}
    (vend / "VENDOR.lock").write_text(json.dumps(lock), encoding="utf-8")
    assert RC.check_vendor_licenses(vend) == ["sigma: license file licenses/sigma.LICENSE.txt is missing or empty"]
    (vend / "licenses" / "sigma.LICENSE.txt").write_text("MIT License", encoding="utf-8")
    assert RC.check_vendor_licenses(vend) == []
    assert RC.check_vendor_licenses() == []
