"""Playwright fixtures for the page tests (TSC-19 to TSC-21, TSC-32).

Each test runs in headless Chromium (the Playwright build) and in headless
Microsoft Edge (channel "msedge") when Edge is installed. The page loads from
file://. Every request that is not file:// is recorded and aborted, so a
defect cannot reach the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import make_env
from tests.unit.test_render_support import small_graph

playwright_sync = pytest.importorskip("playwright.sync_api")

BROWSERS = ["chromium", "msedge"]


@dataclass
class PageRecord:
    page: Any
    url: str
    console: list[tuple[str, str]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    routed: list[str] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def console_errors(self) -> list[str]:
        return [t for k, t in self.console if k == "error"]

    @property
    def csp_blocked(self) -> list[str]:
        """Requests that the browser stopped with the CSP before they left the page."""
        return [u for u, f in self.failures.items() if "csp" in f.lower()]

    @property
    def external_requests(self) -> list[str]:
        """Requests that left the page: any routed request, and any request not blocked by the CSP.

        Chromium fires a "request" event for a load that the CSP blocks, then a
        "requestfailed" event with the reason "csp". Such a load never reaches the
        network layer (the route handler does not see it).
        """
        blocked = set(self.csp_blocked)
        return [u for u in self.requests if u != self.url and u not in blocked] + list(self.routed)


@pytest.fixture(scope="session")
def pw():
    with playwright_sync.sync_playwright() as p:
        yield p


@pytest.fixture(scope="session", params=BROWSERS)
def browser(request, pw):
    channel = None if request.param == "chromium" else request.param
    try:
        b = pw.chromium.launch(channel=channel, headless=True)
    except Exception as e:  # browser not installed on this workstation
        pytest.skip(f"{request.param} is not available: {e}")
    yield b
    b.close()


@pytest.fixture(scope="session")
def small_render(tmp_path_factory) -> tuple[Path, dict[str, str]]:
    tmp = tmp_path_factory.mktemp("render_small")
    cfg, store, _, log = make_env(tmp)
    ids = small_graph(store)
    from visualx.render import run_render

    path = run_render(cfg, store, log)
    store.close()
    return path, ids


@pytest.fixture(scope="session")
def groups_render(tmp_path_factory) -> tuple[Path, Path]:
    """A map from the full build, analyze, and score stages, with Leiden groups and topics,
    and the --redact render of the same store."""
    from tests.unit.test_v02 import pipeline
    from visualx.render import run_render

    tmp = tmp_path_factory.mktemp("render_groups")
    cfg, store, _, log = pipeline(tmp)
    path = run_render(cfg, store, log)
    red = run_render(cfg, store, log, redact=True)
    store.close()
    return path, red


@pytest.fixture(scope="session")
def changes_render(tmp_path_factory) -> Path:
    """Two renders with changes between them (snapshot.py). Returns the second map."""
    from tests.unit.test_snapshot import second_run
    from tests.unit.test_v02 import pipeline
    from visualx.render import run_render

    tmp = tmp_path_factory.mktemp("render_changes")
    cfg, store, _, log = pipeline(tmp)
    run_render(cfg, store, log)
    second_run(store)
    path = run_render(cfg, store, log)
    store.close()
    return path


def open_page(browser, path: Path, *, wait_ready: bool = True) -> PageRecord:
    ctx = browser.new_context()
    page = ctx.new_page()
    rec = PageRecord(page=page, url=path.resolve().as_uri())

    def on_route(route):
        rec.routed.append(route.request.url)
        route.abort()

    ctx.route("http://*/**", on_route)
    ctx.route("https://*/**", on_route)
    page.on("console", lambda m: rec.console.append((m.type, m.text)))
    page.on("pageerror", lambda e: rec.page_errors.append(str(e)))
    page.on("request", lambda r: rec.requests.append(r.url))
    page.on("requestfailed", lambda r: rec.failures.__setitem__(r.url, str(r.failure)))

    def on_dialog(d):
        rec.dialogs.append(d.message)
        d.dismiss()

    page.on("dialog", on_dialog)
    page.goto(rec.url)
    if wait_ready:
        page.wait_for_selector("body[data-vx-ready='1']", timeout=20_000)
    return rec


@pytest.fixture
def small_page(browser, small_render):
    path, ids = small_render
    rec = open_page(browser, path)
    yield rec, ids
    rec.page.context.close()


def search_and_open(page, query: str, user_id_index: int | None = None) -> None:
    """Type in the search box and click the first result (or the result for a node index)."""
    page.fill("#vx-search", "")
    page.fill("#vx-search", query)
    sel = "#vx-results button[data-node]" if user_id_index is None else f"#vx-results button[data-node='{user_id_index}']"
    page.locator(sel).first.click()
    page.wait_for_selector("#vx-p-name")
