"""Page tests from file:// in headless Chromium and Edge: TSC-19, TSC-20, TSC-21, TSC-32.

These tests prove the page wiring with synthetic data. They are not a frame-rate
measurement (TSC-22 needs a headed browser on the owner's workstation).
"""

from __future__ import annotations

import pytest

from tests.browser.conftest import open_page, search_and_open
from tests.unit.test_render_support import (
    BIDI_NAME,
    DATA_URL,
    ENTITY_BIO,
    GOOD_URL,
    HOSTILE_BIO,
    HOSTILE_NAME,
    HOSTILE_URL,
    ZW_NAME,
)
from visualx.render import CSP

pytestmark = pytest.mark.browser


def idx(ids: dict[str, str], key: str) -> int:
    return sorted(ids.values()).index(ids[key])


# -- TSC-19 -----------------------------------------------------------------

def test_tsc19_no_console_errors_no_requests(small_page):
    rec, _ = small_page
    page = rec.page
    assert page.evaluate("location.protocol") == "file:"
    assert rec.console_errors == []
    assert rec.page_errors == []
    assert rec.external_requests == []
    assert page.get_attribute("body", "data-vx-error") is None
    # The graph drew into a WebGL canvas.
    assert page.locator("#vx-graph canvas").count() >= 1
    assert page.text_content("#vx-error") == ""


# -- TSC-20 -----------------------------------------------------------------

def test_tsc20_hostile_values_are_text(small_page):
    rec, ids = small_page
    page = rec.page

    search_and_open(page, "h_img", idx(ids, "h_img"))
    assert page.text_content("#vx-p-name") == HOSTILE_NAME
    assert page.text_content("#vx-p-bio") == HOSTILE_BIO
    # The rule cause with markup is text too.
    assert HOSTILE_NAME in page.text_content("#vx-p-rules")
    assert page.locator("img").count() == 0

    search_and_open(page, "h_bio", idx(ids, "h_bio"))
    assert page.text_content("#vx-p-bio") == HOSTILE_BIO
    assert "</script>" in page.text_content("#vx-p-bio")

    search_and_open(page, "h_url", idx(ids, "h_url"))
    assert page.locator("#vx-p-url a").count() == 0
    assert page.text_content("#vx-p-url [data-not-link]") == HOSTILE_URL

    search_and_open(page, "h_ent", idx(ids, "h_ent"))
    assert page.text_content("#vx-p-bio") == ENTITY_BIO
    assert page.locator("#vx-p-url a").count() == 0
    assert page.text_content("#vx-p-url [data-not-link]") == DATA_URL

    search_and_open(page, "h_bidi", idx(ids, "h_bidi"))
    assert page.text_content("#vx-p-name") == BIDI_NAME

    search_and_open(page, "h_zw", idx(ids, "h_zw"))
    assert page.text_content("#vx-p-name") == ZW_NAME

    search_and_open(page, "h_long", idx(ids, "h_long"))
    bio = page.text_content("#vx-p-bio")
    assert len(bio) == 100_000 and bio.endswith("-LONG-END")

    search_and_open(page, "good_url", idx(ids, "good_url"))
    link = page.locator("#vx-p-url a")
    assert link.count() == 1
    assert link.get_attribute("href") == GOOD_URL
    assert "noopener" in link.get_attribute("rel")

    # Override notes with markup are text.
    search_and_open(page, "carol", idx(ids, "carol"))
    assert page.text_content("#vx-p-override span") == "Owner note <b>checked</b>"
    # The account panel lists the findings of the account; the hostile title is text.
    assert page.text_content("#vx-p-findings").count(HOSTILE_NAME) == 1

    # Findings drawer: title, summary, evidence, and member handles are text (SR-02).
    page.click("#vx-btn-findings")
    card = page.locator(".vx-finding[data-finding='0']")
    assert card.locator(".vx-finding-title").text_content() == HOSTILE_NAME
    assert card.locator(".vx-finding-sum").text_content() == HOSTILE_BIO
    ev = card.locator(".vx-evidence").text_content()
    assert HOSTILE_BIO in ev and "<b>key</b>" in ev
    members = card.locator(".vx-member").all_text_contents()
    assert sorted(members) == ["@bob", "@carol", "@h_img"]
    assert "1 not in the map" in card.text_content()
    # The flagged list and the Overview table show the hostile name as text only.
    assert "@h_img" in page.text_content("#vx-flagged")
    page.click("#vx-view-overview")
    page.fill("#vx-ov-search", "h_img")
    assert HOSTILE_NAME in page.text_content("#vx-ov-table tbody")
    assert HOSTILE_NAME in page.text_content("#vx-ov-findings")
    page.click("#vx-view-map")

    # No script from the data ran, and the page structure is intact.
    assert rec.dialogs == []
    assert page.locator("b, i").count() == 0
    assert page.evaluate("document.scripts.length") == 5  # 3 vendor, payload, app
    assert rec.console_errors == [] and rec.page_errors == []
    assert rec.external_requests == []


# -- TSC-21 -----------------------------------------------------------------

def shown(page) -> tuple[int, int]:
    c = page.locator("#vx-counts")
    return int(c.get_attribute("data-shown-nodes")), int(c.get_attribute("data-shown-edges"))


def test_tsc21_search_filters_path(small_page):
    rec, ids = small_page
    page = rec.page

    # Header facts: the node without a profile is hidden by default and counted (FR-22, FR-49).
    assert page.get_attribute("#vx-facts", "data-hidden-count") == "1"
    assert "do not share" in page.text_content("#vx-private")
    assert page.is_visible("#vx-private")
    assert "not persons" in page.text_content("#vx-limits")
    assert shown(page) == (14, 13)
    page.click("#vx-btn-controls")
    assert page.is_visible("#vx-controls")

    # Search by handle and by display name (FR-45).
    page.fill("#vx-search", "car")
    assert "@carol" in page.text_content("#vx-results")
    page.fill("#vx-search", "Bob B")
    assert page.locator("#vx-results button").count() == 1
    page.fill("#vx-search", "zzzz-no-such")
    assert "No match" in page.text_content("#vx-results")

    # State filter (FR-46): alice, dave, frank have no_signals.
    page.uncheck("#vx-state-no_signals")
    assert shown(page)[0] == 11
    page.check("#vx-state-no_signals")
    # Minimum tie weight: three shown edges have weight 2 or more.
    page.fill("#vx-minw", "2")
    assert shown(page) == (14, 3)
    page.fill("#vx-minw", "0")
    # The missing-profile toggle.
    page.check("#vx-show-missing")
    assert shown(page) == (15, 14)
    page.uncheck("#vx-show-missing")

    # Weighted shortest path (FR-47): alice -> carol -> bob is shorter than alice -> owner -> bob.
    search_and_open(page, "alice", idx(ids, "alice"))
    page.click("#vx-p-from")
    search_and_open(page, "bob", idx(ids, "bob"))
    page.click("#vx-p-to")
    page.click("#vx-tab-controls")
    page.click("#vx-path-go")
    msg = page.locator("#vx-path-msg")
    assert msg.get_attribute("data-path") == ",".join([ids["alice"], ids["carol"], ids["bob"]])
    assert page.locator("#vx-path-out li").count() == 3

    # Interaction type filter: hide quotes, the path goes through the owner (only shown edges count).
    page.uncheck("#vx-type-quote")
    assert msg.get_attribute("data-path") == ",".join([ids["alice"], ids["ego"], ids["bob"]])
    page.check("#vx-type-quote")

    # No path: frank has no edges.
    search_and_open(page, "frank", idx(ids, "frank"))
    page.click("#vx-p-from")
    page.click("#vx-tab-controls")
    page.click("#vx-path-go")
    assert msg.get_attribute("data-path") == "none"
    assert "No path" in msg.text_content()

    # Display controls work without errors.
    for mode in ("followers", "activity", "weighted_degree"):
        page.select_option("#vx-size", mode)
    for mode in ("type", "community", "state"):
        page.select_option("#vx-color", mode)
    page.check("#vx-arrows")
    page.fill("#vx-edge-opacity", "0.8")
    page.fill("#vx-node-scale", "2")
    page.fill("#vx-label-density", "3")
    page.uncheck("#vx-labels")

    # Time window chips: 7 days before the last link time keeps dave and the eight test accounts.
    page.click("#vx-windows button[data-window='10080']")
    assert shown(page) == (10, 9)
    page.click("#vx-windows button[data-window='all']")
    assert shown(page) == (14, 13)
    assert rec.console_errors == [] and rec.page_errors == []


# -- TSC-32 -----------------------------------------------------------------

INJECT = (
    '<script src="https://example.com/evil.js"></script>'
    '<script>window.__vx_v=[];document.addEventListener("securitypolicyviolation",function(e){'
    'window.__vx_v.push(e.violatedDirective+" "+e.blockedURI);});'
    'try{fetch("https://example.com/beacon").catch(function(){});}catch(e){}'
    'var im=new Image();im.src="https://example.com/i.png";</script>'
)


def test_tsc32_csp_present_and_blocks(browser, small_render, tmp_path):
    path, _ = small_render
    rec = open_page(browser, path)
    meta = rec.page.locator('meta[http-equiv="Content-Security-Policy"]')
    assert meta.get_attribute("content") == CSP
    rec.page.context.close()

    html = path.read_text(encoding="utf-8")
    head_end = html.index("</head>")
    injected = html[:head_end] + INJECT + html[head_end:]
    bad = tmp_path / "injected.html"
    bad.write_text(injected, encoding="utf-8")
    rec2 = open_page(browser, bad)
    page = rec2.page
    page.wait_for_timeout(500)
    assert rec2.routed == [], rec2.routed
    assert rec2.external_requests == [], rec2.external_requests
    assert set(rec2.csp_blocked) >= {"https://example.com/evil.js", "https://example.com/i.png"}
    csp_errors = [t for t in rec2.console_errors if "Content Security Policy" in t]
    assert len(csp_errors) >= 2, rec2.console
    violations = page.evaluate("window.__vx_v")
    assert any("connect-src" in v and "example.com" in v for v in violations), violations
    assert any("img-src" in v for v in violations), violations
    # The map still loads under the strict policy.
    assert page.get_attribute("body", "data-vx-ready") == "1"
    page.context.close()
