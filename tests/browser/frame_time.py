"""TSC-22 frame-time script. The owner runs it on the workstation, in a HEADED browser.

It is not a pytest test. A number from a headless run is not valid for TSC-22
(SUCCESS_CRITERIA section 5), and the script marks such a record as not valid.

Usage, from the project root:

    .venv/Scripts/python -m tests.browser.frame_time --channel msedge --nodes 5000 --edges 25000
    .venv/Scripts/python -m tests.browser.frame_time --channel chrome --html data/renders/map_<id>.html
    .venv/Scripts/python -m tests.browser.frame_time --labels on --out background/frame_time.json

The script turns off the core view so that all nodes show, then drives a fixed
pan and zoom sequence with the mouse and records the requestAnimationFrame
intervals. It prints one JSON record with the browser version, the GPU, the
node and edge counts, the label setting, and the frame times.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

GPU_JS = """() => {
  const c = document.createElement('canvas');
  const gl = c.getContext('webgl2');
  if (!gl) { return {webgl2: false}; }
  const ext = gl.getExtension('WEBGL_debug_renderer_info');
  return {webgl2: true,
          vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
          renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)};
}"""

START_JS = """() => {
  window.__vxFrames = [];
  window.__vxRun = true;
  const tick = (t) => { window.__vxFrames.push(t); if (window.__vxRun) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}"""

STOP_JS = """() => { window.__vxRun = false; return window.__vxFrames; }"""


def synthetic_page(nodes: int, edges: int) -> Path:
    from tests.helpers import make_env
    from tests.unit.test_render_support import synthetic
    from visualx.render import run_render

    tmp = Path(tempfile.mkdtemp(prefix="vx_frame_"))
    cfg, store, _, log = make_env(tmp)
    synthetic(store, nodes, edges)
    path = run_render(cfg, store, log)
    store.close()
    return path


def drive(page, seconds: float) -> None:
    """A constant pan and zoom sequence over the graph area."""
    box = page.locator("#vx-graph").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    end = time.monotonic() + seconds
    step = 0
    while time.monotonic() < end:
        phase = step % 4
        if phase in (0, 1):
            page.mouse.move(cx, cy)
            page.mouse.wheel(0, -240 if phase == 0 else 240)
            page.wait_for_timeout(120)
        else:
            dx = 160 if phase == 2 else -160
            page.mouse.move(cx, cy)
            page.mouse.down()
            for k in range(1, 11):
                page.mouse.move(cx + dx * k / 10, cy + dx * k / 20)
                page.wait_for_timeout(16)
            page.mouse.up()
        step += 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TSC-22 headed frame-time measurement")
    ap.add_argument("--channel", default="msedge", help="msedge, chrome, or chromium (the Playwright build)")
    ap.add_argument("--html", type=Path, help="a rendered page (default: a synthetic graph)")
    ap.add_argument("--nodes", type=int, default=5000)
    ap.add_argument("--edges", type=int, default=25000)
    ap.add_argument("--labels", choices=("on", "off"), default="off")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--headless", action="store_true", help="for a script check only; the record is then not valid for TSC-22")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    path = args.html.resolve() if args.html else synthetic_page(args.nodes, args.edges)
    with sync_playwright() as pw:
        channel = None if args.channel == "chromium" else args.channel
        browser = pw.chromium.launch(channel=channel, headless=args.headless)
        page = browser.new_page(viewport={"width": 1600, "height": 900})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(path.as_uri())
        page.wait_for_selector("body[data-vx-ready='1']", timeout=180_000)
        page.click("#vx-btn-controls")  # the filters are in the Controls drawer
        if page.is_enabled("#vx-core") and page.is_checked("#vx-core"):
            page.uncheck("#vx-core")
        if (args.labels == "on") != page.is_checked("#vx-labels"):
            page.click("#vx-labels")
        page.wait_for_timeout(1000)
        shown_nodes = int(page.get_attribute("#vx-counts", "data-shown-nodes"))
        shown_edges = int(page.get_attribute("#vx-counts", "data-shown-edges"))
        gpu = page.evaluate(GPU_JS)
        page.evaluate(START_JS)
        drive(page, args.seconds)
        stamps = page.evaluate(STOP_JS)
        version = browser.version
        browser.close()

    deltas = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    deltas.sort()
    rec = {
        "criterion": "TSC-22",
        "valid_for_tsc22": not args.headless,
        "note": "requestAnimationFrame intervals during a constant pan and zoom sequence",
        "at": datetime.now(UTC).isoformat(),
        "page": str(path),
        "browser": {"channel": args.channel, "version": version, "headless": args.headless},
        "gpu": gpu,
        "graph": {"shown_nodes": shown_nodes, "shown_edges": shown_edges},
        "config": {"labels": args.labels, "edge_labels": False, "hide_edges_on_move": False},
        "frames": len(deltas),
        "mean_ms": round(statistics.fmean(deltas), 2) if deltas else None,
        "p50_ms": round(deltas[len(deltas) // 2], 2) if deltas else None,
        "p95_ms": round(deltas[int(len(deltas) * 0.95)], 2) if deltas else None,
        "target_mean_ms": 33.0,
        "console_errors": errors,
    }
    text = json.dumps(rec, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
