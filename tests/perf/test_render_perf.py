"""TSC-23 / NFR-03: the page for 10,000 nodes and 50,000 edges is less than 10 MB.

Marked slow (run with ``pytest -m slow``). The load check opens the file in
headless Chromium only to show that the page decodes and draws the large
payload with no console error. It is not a frame-rate measurement (TSC-22).
"""

from __future__ import annotations

import time

import pytest

from tests.helpers import make_env
from tests.unit.test_render_support import synthetic
from visualx.delete import decode_payloads
from visualx.render import run_render

pytestmark = pytest.mark.slow

LIMIT_BYTES = 10 * 1024 * 1024


@pytest.fixture(scope="module")
def big_render(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("perf10k")
    cfg, store, _, log = make_env(tmp)
    synthetic(store, 10_000, 50_000)
    t0 = time.perf_counter()
    path = run_render(cfg, store, log)
    seconds = time.perf_counter() - t0
    store.close()
    return path, seconds


def test_tsc23_file_size_10k(big_render):
    path, seconds = big_render
    size = path.stat().st_size
    print(f"\nTSC-23: 10,000 nodes / 50,000 edges -> {size:,} bytes ({size / 1048576:.2f} MiB), render {seconds:.2f} s")
    assert size < LIMIT_BYTES
    d = decode_payloads(path.read_text(encoding="utf-8"))[0]
    assert len(d["n"]["id"]) == 10_000 and len(d["e"]["s"]) == 50_000


def test_10k_page_loads_headless(big_render):
    sync_api = pytest.importorskip("playwright.sync_api")
    path, _ = big_render
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        t0 = time.perf_counter()
        page.goto(path.resolve().as_uri())
        page.wait_for_selector("body[data-vx-ready='1']", timeout=120_000)
        seconds = time.perf_counter() - t0
        shown = int(page.get_attribute("#vx-counts", "data-shown-nodes"))
        browser.close()
    print(f"\n10k page ready in {seconds:.1f} s (headless, not a frame-rate number); core view shows {shown} nodes")
    assert errors == []
    assert shown <= 2000  # core view starts above core_view_max_nodes (FR-48)


@pytest.fixture(scope="module")
def render_5k(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("perf5k")
    cfg, store, _, log = make_env(tmp)
    synthetic(store, 5_000, 25_000)
    path = run_render(cfg, store, log)
    store.close()
    return path


def test_5k_controls_work_at_scale(render_5k):
    """All 5,000 nodes shown: live layout, playback, community colors, and Overview with no error.

    Headless, so it is not a frame-rate number (TSC-22). It records how long each step takes.
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(render_5k.resolve().as_uri())
        page.wait_for_selector("body[data-vx-ready='1']", timeout=120_000)
        page.click("#vx-btn-controls")
        page.uncheck("#vx-core")
        assert int(page.get_attribute("#vx-counts", "data-shown-nodes")) > 4_500
        t0 = time.perf_counter()
        page.click("#vx-fa2-run")
        page.wait_for_timeout(3000)
        page.click("#vx-fa2-run")
        iters = int(page.get_attribute("#vx-graph", "data-layout-iterations") or 0)
        t1 = time.perf_counter()
        page.select_option("#vx-color", "community")
        t2 = time.perf_counter()
        page.click("#vx-btn-playback")
        mx = page.get_attribute("#vx-pb-range", "max")
        page.fill("#vx-pb-range", str(int(mx) // 2))
        half = int(page.get_attribute("#vx-counts", "data-shown-edges"))
        page.fill("#vx-pb-range", mx)
        full = int(page.get_attribute("#vx-counts", "data-shown-edges"))
        page.click("#vx-btn-playback")
        page.click("#vx-view-overview")
        page.fill("#vx-ov-search", "a")
        page.click("#vx-view-map")
        browser.close()
    print(f"\n5k: force layout {iters} steps in {t1 - t0:.1f} s; Louvain colors {t2 - t1:.2f} s; "
          f"playback half {half} links, end {full} links")
    assert errors == []
    assert iters > 0
    assert 0 < half < full
