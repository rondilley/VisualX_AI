"""Slice 6 unit tests: payload, template escaping, vendor check, render records (AD-09, AD-10)."""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from tests.helpers import make_env, mark_stage
from tests.unit.test_render_support import (
    BIDI_NAME,
    HOSTILE_BIO,
    HOSTILE_NAME,
    HOSTILE_URL,
    LONG_BIO,
    FINDING_ONLY_ID,
    FIRST_TIME,
    SMALL_IDS,
    fill_store,
    small_graph,
)
from visualx.delete import decode_payloads, delete_ids, find_id
from visualx.render import render as render_mod
from visualx.render import CSP, RenderError, decode_payload, encode_payload, load_vendor, run_render
from visualx.store import StaleError


@pytest.fixture
def env(tmp_path):
    cfg, store, cache, log = make_env(tmp_path)
    ids = small_graph(store)
    return cfg, store, cache, log, ids


def test_payload_round_trip():
    obj = {"a": [1, 2.5, None], "t": HOSTILE_BIO, "b": BIDI_NAME, "long": LONG_BIO}
    b64 = encode_payload(obj)
    assert re.fullmatch(r"[A-Za-z0-9+/=]+", b64)
    assert decode_payload(b64) == obj
    # delete.py must decode the same block in a page (AD-10, TSC-09).
    html = f'<script type="application/octet-stream" id="vx-payload">{b64}</script>'
    assert decode_payloads(html) == [obj]


def test_rendered_page_payload_and_content(env):
    cfg, store, _, log, ids = env
    path = run_render(cfg, store, log)
    html = path.read_text(encoding="utf-8")
    payloads = decode_payloads(html)
    assert len(payloads) == 1
    d = payloads[0]
    assert d["v"] == 1
    assert d["n"]["id"] == sorted(ids.values())
    i = d["n"]["id"].index(ids["h_url"])
    assert d["p"]["url"][i] == HOSTILE_URL
    j = d["n"]["id"].index(ids["h_long"])
    assert d["p"]["description"][j] == LONG_BIO
    # No profile images (SR-03) and no post text (DR-09) in the payload.
    assert "profile_image_url" not in d["p"]
    assert "text" not in d and "posts" not in d
    # Missing-profile node is in the payload and counted as hidden (FR-22).
    assert d["meta"]["counts"]["missing_profile"] == 1
    assert d["n"]["ps"][d["n"]["id"].index(ids["missing"])] == "missing"
    # Each fired rule has its cause (invariant 5).
    carol = d["n"]["id"].index(ids["carol"])
    assert {r[1] for r in d["r"] if r[0] == carol} == {"B01", "I01"}
    assert d["ov"][str(carol)] == "Owner note <b>checked</b>"
    # Colors and marks come from configuration.
    assert d["cfg"]["state_marks"]["many_signals"] == "!!"
    assert d["cfg"]["state_colors"]["no_signals"] == "#009E73"


def test_payload_times_activity_and_findings(env):
    cfg, store, _, log, ids = env
    d = decode_payloads(run_render(cfg, store, log).read_text(encoding="utf-8"))[0]
    order = d["n"]["id"]
    e = d["e"]
    # t0 is the earliest link time; link times are whole minutes after t0.
    assert d["meta"]["t0"] == int(datetime.fromisoformat(FIRST_TIME).timestamp())
    k = next(j for j in range(len(e["s"])) if order[e["s"][j]] == ids["alice"] and order[e["t"][j]] == ids["ego"])
    assert e["ft"][k] == 0 and e["lt"][k] == (23 * 60)
    # The repost from retweeted_by has no time: it stays None and is counted (DR-02).
    r = next(j for j in range(len(e["s"])) if order[e["s"][j]] == ids["missing"])
    assert e["ft"][r] is None and e["lt"][r] is None
    assert d["meta"]["counts"]["edges_without_time"] == 1
    # Posts and reposts seen come from the interactions table.
    a = order.index(ids["alice"])
    assert d["n"]["pp"][a] == 2 and d["n"]["rp"][a] == 0
    assert d["n"]["rp"][order.index(ids["missing"])] == 1
    # Findings: members as node indexes, other members as IDs, high severity first.
    f = d["f"]
    assert [x["sev"] for x in f] == ["high", "notable"]
    assert f[0]["title"] == HOSTILE_NAME and f[0]["sum"] == HOSTILE_BIO
    assert sorted(order[i] for i in f[0]["m"]) == sorted([ids["h_img"], ids["carol"], ids["bob"]])
    assert f[0]["mx"] == [FINDING_ONLY_ID]
    assert d["meta"]["counts"]["findings"] == 2


def test_findings_absent_table_and_unknown_severity(env):
    cfg, store, _, log, _ = env
    with store.tx() as c:
        c.execute("UPDATE findings SET severity='<b>bad</b>' WHERE finding_id='F-plain'")
    d = decode_payloads(run_render(cfg, store, log).read_text(encoding="utf-8"))[0]
    # A severity outside the enum becomes "info"; it is used as a CSS class.
    assert {x["sev"] for x in d["f"]} == {"high", "info"}
    with store.tx() as c:
        c.execute("DROP TABLE findings")
    d2 = decode_payloads(run_render(cfg, store, log).read_text(encoding="utf-8"))[0]
    assert d2["f"] == [] and d2["meta"]["counts"]["findings"] == 0


def test_template_escaping_and_csp(env):
    cfg, store, _, log, _ = env
    html = run_render(cfg, store, log).read_text(encoding="utf-8")
    # Text from X is only inside the base64 payload (SR-05).
    for raw in (HOSTILE_NAME, "alert(2)", HOSTILE_URL, "Alice bio", "@alice", "Owner note"):
        assert raw not in html
    assert html.count('id="vx-payload"') == 1
    # The exact CSP of SR-09 is the first meta element after the charset.
    m = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', html)
    assert m is not None and m.group(1) == CSP
    assert CSP == ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'none'; "
                   "connect-src 'none'; worker-src 'none'; base-uri 'none'; form-action 'none'")
    assert html.index("Content-Security-Policy") < html.index("<script")
    # No external references (SR-03).
    assert not re.search(r"<(script|link|img|iframe)[^>]+(src|href)=", html, re.I)
    # The page script never uses innerHTML for any value (SR-02).
    app_js = (Path(render_mod.__file__).parent / "app.js").read_text(encoding="utf-8")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert bad not in app_js


def test_template_autoescape_and_payload_check():
    env = render_mod._env()
    assert env.autoescape is True
    with pytest.raises(RenderError):
        render_mod.render_page("not base64 <script>", vendor_scripts=[("x", "var a=1;")])


def test_vendor_files_agree_with_lock():
    scripts = load_vendor()
    names = [n.split("@")[0] for n, _ in scripts]
    assert names == ["graphology", "sigma", "graphology-library"]


def test_vendor_mismatch_stops_render(env, tmp_path, monkeypatch):
    cfg, store, _, log, _ = env
    bad = tmp_path / "vendor_copy"
    shutil.copytree(render_mod.VENDOR_DIR, bad)
    target = bad / "sigma.min.js"
    target.write_bytes(target.read_bytes() + b"\n;window.x=1;")
    with pytest.raises(RenderError, match="sha256"):
        load_vendor(bad)
    monkeypatch.setattr(render_mod, "VENDOR_DIR", bad)
    with pytest.raises(RenderError, match="SR-04"):
        run_render(cfg, store, log)
    assert store.q("SELECT * FROM renders") == []
    assert list(cfg.renders_dir.glob("*.html")) == []


def test_vendor_missing_file_or_lock_stops(tmp_path):
    bad = tmp_path / "v"
    shutil.copytree(render_mod.VENDOR_DIR, bad)
    (bad / "graphology.umd.min.js").unlink()
    with pytest.raises(RenderError, match="missing"):
        load_vendor(bad)
    (bad / "VENDOR.lock").unlink()
    with pytest.raises(RenderError, match="not found"):
        load_vendor(bad)


def test_render_records_and_members(env):
    cfg, store, _, log, ids = env
    path = run_render(cfg, store, log)
    assert path.parent == cfg.renders_dir
    r = store.q("SELECT * FROM renders")
    assert len(r) == 1
    assert Path(r[0]["path"]) == path.resolve()
    assert path.name == f"map_{r[0]['stage_run_id']}.html"
    members = store.q("SELECT user_id, post_id FROM render_members WHERE render_id=?", (r[0]["render_id"],))
    # Every user ID in the payload, also a finding member that is not a node (AD-10).
    assert {m["user_id"] for m in members if m["user_id"] is not None} == set(ids.values()) | {FINDING_ONLY_ID}
    # IDs from findings that are not nodes can be post IDs in the evidence, so they are
    # indexed as post IDs too (review 20261102-225212).
    posts = {m["post_id"] for m in members if m["post_id"] is not None}
    assert FINDING_ONLY_ID in posts and not posts & set(ids.values())
    sr = store.latest_stage_run("render")
    assert sr is not None and sr["stage_run_id"] == r[0]["stage_run_id"]
    assert set(__import__("json").loads(sr["inputs_json"])) == {"analyze", "score"}
    assert store.stale_downstream("render") == []
    assert any(rec["event"] == "render" for rec in log.records)


def test_render_needs_upstream(tmp_path):
    cfg, store, _, log = make_env(tmp_path)
    mark_stage(store, "analyze")
    with pytest.raises(StaleError):
        run_render(cfg, store, log)


def test_state_without_causes_stops(tmp_path):
    cfg, store, _, log = make_env(tmp_path)
    node = {"user_id": "5", "handle": "x", "name": "X", "profile_status": "ok", "is_ego": 0, "in_degree": 0,
            "out_degree": 0, "degree": 0, "weighted_degree": 0, "ego_weight": 0, "pagerank": 0,
            "betweenness": 0, "x": 0, "y": 0}
    fill_store(store, [node], [], states={"5": ("many_signals", 9.0, None)})
    with pytest.raises(RenderError, match="invariant 5"):
        run_render(cfg, store, log)


def test_bad_config_color_stops(env):
    cfg, store, _, log, _ = env
    cfg.raw["render"]["state_colors"]["no_signals"] = "red;background:url(x)"
    with pytest.raises(RenderError, match="#RRGGBB"):
        run_render(cfg, store, log)


def test_delete_finds_and_removes_render(env):
    cfg, store, cache, log, ids = env
    path = run_render(cfg, store, log)
    assert any(h.startswith("render:") for h in find_id(store, cache, ids["bob"]))
    rep = delete_ids(store, cache, cfg.root / "config", [ids["bob"]], "user")
    assert str(path.resolve()) in rep.renders_deleted
    assert not path.exists()
    assert rep.remaining == []
