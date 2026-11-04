"""TSC-31: vendor tarball integrity and VENDOR.lock hashes (tools/vendor.py). Offline tests."""

from __future__ import annotations

import io
import json
import shutil
import tarfile

import pytest

from tools import vendor
from visualx.render import render as render_mod


def _tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _fake_registry(monkeypatch, tgz: bytes, integrity: str, license_: str = "MIT"):
    meta = {"version": "1.0.0", "license": license_,
            "dist": {"integrity": integrity, "tarball": f"{vendor.REGISTRY}/demo/-/demo-1.0.0.tgz"}}

    def fake_get(url: str) -> bytes:
        if url.endswith(".tgz"):
            return tgz
        return json.dumps(meta).encode()

    monkeypatch.setattr(vendor, "_get", fake_get)


SPEC = [{"name": "demo", "version": "1.0.0", "role": "inline",
         "files": {"package/dist/demo.min.js": "demo.min.js", "package/LICENSE": "licenses/demo.LICENSE.txt"}}]
FILES = {"package/dist/demo.min.js": b"var demo=1;", "package/LICENSE": b"MIT License", "package/other.js": b"x"}


def test_fetch_writes_lock_and_files(tmp_path, monkeypatch):
    tgz = _tarball(FILES)
    _fake_registry(monkeypatch, tgz, vendor.sri_sha512(tgz))
    lock = vendor.fetch(tmp_path, SPEC)
    assert (tmp_path / "demo.min.js").read_bytes() == b"var demo=1;"
    assert not (tmp_path / "other.js").exists()
    entry = lock["packages"][0]
    assert entry["integrity"] == vendor.sri_sha512(tgz)
    assert {f["dest"]: f["sha256"] for f in entry["files"]}["demo.min.js"] == vendor.sha256_hex(b"var demo=1;")
    assert vendor.verify(tmp_path) == []


def test_fetch_stops_on_integrity_difference(tmp_path, monkeypatch):
    tgz = _tarball(FILES)
    _fake_registry(monkeypatch, tgz, vendor.sri_sha512(b"some other tarball"))
    with pytest.raises(vendor.VendorError, match="does not agree with registry"):
        vendor.fetch(tmp_path, SPEC)
    assert not (tmp_path / "VENDOR.lock").exists()
    assert not (tmp_path / "demo.min.js").exists()


def test_fetch_stops_when_registry_changes_a_locked_version(tmp_path, monkeypatch):
    tgz = _tarball(FILES)
    _fake_registry(monkeypatch, tgz, vendor.sri_sha512(tgz))
    vendor.fetch(tmp_path, SPEC)
    tgz2 = _tarball({**FILES, "package/dist/demo.min.js": b"var demo=2;"})
    _fake_registry(monkeypatch, tgz2, vendor.sri_sha512(tgz2))
    with pytest.raises(vendor.VendorError, match="VENDOR.lock"):
        vendor.fetch(tmp_path, SPEC)


def test_fetch_refuses_incompatible_license(tmp_path, monkeypatch):
    tgz = _tarball(FILES)
    _fake_registry(monkeypatch, tgz, vendor.sri_sha512(tgz), license_="CC-BY-NC-4.0")
    with pytest.raises(vendor.VendorError, match="license"):
        vendor.fetch(tmp_path, SPEC)


def test_extract_needs_every_named_file():
    with pytest.raises(vendor.VendorError, match="has no"):
        vendor.extract_files("demo", _tarball({"package/a.js": b"1"}), {"package/b.js": "b.js"})


def test_destination_cannot_leave_vendor_folder(tmp_path):
    with pytest.raises(vendor.VendorError):
        vendor._safe_dest(tmp_path, "../outside.js")


def test_only_registry_urls():
    with pytest.raises(vendor.VendorError):
        vendor._get("https://example.com/x.tgz")


def test_real_vendor_files_agree_with_lock():
    assert vendor.verify(render_mod.VENDOR_DIR) == []
    lock = vendor.load_lock(render_mod.VENDOR_DIR)
    pins = {p["name"]: p["version"] for p in lock["packages"]}
    assert pins["sigma"].startswith("4.0.")
    assert all(p["license"] in vendor.ALLOWED_LICENSES for p in lock["packages"])
    assert all(p["integrity"].startswith("sha512-") for p in lock["packages"])
    # Every package keeps its license text (AR-01).
    for p in lock["packages"]:
        assert any(f["dest"].startswith("licenses/") for f in p["files"])


def test_verify_finds_a_changed_file(tmp_path):
    copy = tmp_path / "v"
    shutil.copytree(render_mod.VENDOR_DIR, copy)
    (copy / "graphology-library.min.js").write_bytes(b"changed")
    problems = vendor.verify(copy)
    assert len(problems) == 1 and "graphology-library.min.js" in problems[0]
    assert vendor.main(["verify", "--vendor-dir", str(copy)]) == 1
