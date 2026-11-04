"""Page tests for the map controls: layout, drag, playback, findings, overview, help, hover.

They run from file:// in headless Chromium and Edge with the small synthetic graph.
They prove the wiring only. They are not a frame-rate measurement (TSC-22).
"""

from __future__ import annotations

import pytest

from tests.browser.conftest import open_page, search_and_open

pytestmark = pytest.mark.browser


def idx(ids: dict[str, str], key: str) -> int:
    return sorted(ids.values()).index(ids[key])


def shown(page, sel: str = "#vx-counts") -> tuple[int, int]:
    c = page.locator(sel)
    return int(c.get_attribute("data-shown-nodes")), int(c.get_attribute("data-shown-edges"))


def graph_pos(page, i: int) -> tuple[float, float]:
    p = page.evaluate(f"window.visualxPositions.graph({i})")
    return p["x"], p["y"]


def screen_pos(page, i: int) -> tuple[float, float]:
    box = page.locator("#vx-graph").bounding_box()
    p = page.evaluate(f"window.visualxPositions.viewport({i})")
    return box["x"] + p["x"], box["y"] + p["y"]


def wait_until(page, cond, timeout_ms: int = 15_000) -> None:
    """Poll from Python. wait_for_function needs eval, and the page CSP blocks eval (SR-09)."""
    waited = 0
    while not cond():
        assert waited < timeout_ms, "condition not met in time"
        page.wait_for_timeout(50)
        waited += 50


def no_errors(rec) -> None:
    assert rec.console_errors == [] and rec.page_errors == []
    assert rec.external_requests == []


def test_layout_start_stop_and_reset(small_page):
    rec, ids = small_page
    page = rec.page
    a = idx(ids, "alice")
    start = graph_pos(page, a)
    page.click("#vx-btn-controls")
    page.fill("#vx-fa2-gravity", "2")
    page.check("#vx-fa2-linlog")
    page.click("#vx-fa2-run")
    assert page.text_content("#vx-fa2-run") == "Stop force"
    wait_until(page, lambda: int(page.get_attribute("#vx-graph", "data-layout-iterations") or 0) > 50)
    page.click("#vx-fa2-run")
    assert page.text_content("#vx-fa2-run") == "Start force"
    moved = graph_pos(page, a)
    assert moved != start
    # Stopped: the positions do not change any more.
    page.wait_for_timeout(300)
    assert graph_pos(page, a) == moved
    # Reset goes back to the layout from the analyze stage.
    page.click("#vx-layout-reset")
    page.wait_for_timeout(1200)
    back = graph_pos(page, a)
    assert abs(back[0] - start[0]) < 1e-6 and abs(back[1] - start[1]) < 1e-6
    # The grouped preset moves nodes too.
    page.select_option("#vx-layout", "state")
    page.wait_for_timeout(1200)
    assert graph_pos(page, a) != start
    assert page.get_attribute("#vx-graph", "data-layout") == "state"
    no_errors(rec)


def test_drag_moves_a_node(small_page):
    rec, ids = small_page
    page = rec.page
    d = idx(ids, "dave")
    before = graph_pos(page, d)
    x, y = screen_pos(page, d)
    page.mouse.move(x, y)
    page.mouse.down()
    for k in range(1, 11):
        page.mouse.move(x + 12 * k, y + 8 * k)
        page.wait_for_timeout(16)
    page.mouse.up()
    page.wait_for_timeout(200)
    after = graph_pos(page, d)
    assert after != before
    assert page.get_attribute("#vx-graph", "data-dragged") == str(d)
    no_errors(rec)


def test_hover_card(small_page):
    rec, ids = small_page
    page = rec.page
    a = idx(ids, "alice")
    x, y = screen_pos(page, a)
    page.mouse.move(x + 30, y + 30)
    page.mouse.move(x, y)
    page.wait_for_selector("#vx-hover:not([hidden])")
    text = page.text_content("#vx-hover")
    assert "@alice" in text and "No signals seen" in text
    assert "2 posts, 0 reposts seen" in text
    assert "1 incoming" in text and "1 outgoing links" in text
    page.mouse.move(5, 300)
    no_errors(rec)


def test_playback_changes_visible_counts(small_page):
    rec, _ = small_page
    page = rec.page
    page.click("#vx-btn-playback")
    assert page.is_visible("#vx-playbar") and page.is_visible("#vx-clock")
    # At the end: every link with a time; frank (no links) and the no-profile account are not shown.
    assert shown(page) == (13, 13)
    rng = page.locator("#vx-pb-range")
    page.fill("#vx-pb-range", "0")
    # At the first link time: alice and the owner, one link.
    assert shown(page) == (2, 1)
    assert shown(page, "#vx-pb-counts") == (2, 1)
    assert page.text_content("#vx-clock-time") != ""
    page.fill("#vx-pb-range", rng.get_attribute("max"))
    assert shown(page) == (13, 13)
    # Play from the start: the counts grow.
    page.click("#vx-pb-rewind")
    assert shown(page) == (2, 1)
    page.select_option("#vx-pb-speed", "30")
    page.click("#vx-pb-play")
    # The whole period plays in 30 s, so every link shows before 30 s. The 4th link of the
    # small graph comes at about 69 % of the period (20.6 s measured), past a 15 s wait.
    wait_until(page, lambda: shown(page)[1] > 3, timeout_ms=32_000)
    page.click("#vx-pb-play")
    # Closing playback shows the whole graph again.
    page.click("#vx-btn-playback")
    assert shown(page) == (14, 13)
    no_errors(rec)


def test_findings_drawer_focuses_members(small_page):
    rec, ids = small_page
    page = rec.page
    page.click("#vx-btn-findings")
    assert page.is_visible("#vx-left")
    page.click(".vx-finding[data-finding='0'] .vx-finding-title")
    focus = page.get_attribute("#vx-graph", "data-focus")
    assert set(focus.split(",")) == {ids["h_img"], ids["carol"], ids["bob"]}
    assert page.is_visible("#vx-focus")
    assert "vx-active" in page.get_attribute(".vx-finding[data-finding='0']", "class")
    page.click("#vx-focus-clear")
    assert page.get_attribute("#vx-graph", "data-focus") is None
    # A member chip opens that account.
    page.click(".vx-finding[data-finding='1'] .vx-member >> text=@dave")
    assert page.text_content("#vx-p-name") == "Dave D"
    # The flagged list opens an account in one click.
    page.click(f"#vx-flagged button[data-node='{idx(ids, 'carol')}']")
    assert page.text_content("#vx-p-name") == "Carol C"
    no_errors(rec)


def test_overview_row_focuses_node(small_page):
    rec, ids = small_page
    page = rec.page
    page.click("#vx-view-overview")
    assert page.is_visible("#vx-overview") and page.is_hidden("#vx-mapview")
    assert "Accounts by state" in page.text_content("#vx-ov-states")
    # Sort by followers, largest first: carol has 3000.
    page.click("#vx-ov-table th button[data-sort='fo']")
    first = page.locator("#vx-ov-table tbody tr").first
    assert first.get_attribute("data-node") == str(idx(ids, "carol"))
    page.fill("#vx-ov-search", "bob")
    assert page.locator("#vx-ov-table tbody tr").count() == 1
    page.click("#vx-ov-table tbody tr")
    assert page.is_visible("#vx-mapview") and page.is_hidden("#vx-overview")
    assert page.text_content("#vx-p-name") == "Bob B"
    assert page.is_visible("#vx-panel")
    # No export of any kind (BSC-05, invariant 8).
    assert page.locator("a[download]").count() == 0
    assert "export" not in page.inner_text("#vx-app").lower()
    no_errors(rec)


def test_help_opens_and_closes(small_page):
    rec, _ = small_page
    page = rec.page
    assert page.is_hidden("#vx-help")
    page.click("#vx-btn-help")
    assert page.is_visible("#vx-help")
    assert "not a person" in page.text_content("#vx-help")
    page.keyboard.press("Escape")
    assert page.is_hidden("#vx-help")
    page.click("#vx-btn-theme")
    assert page.get_attribute("body", "data-theme") == "light"
    page.click("#vx-btn-theme")
    no_errors(rec)


def test_search_suggestions_and_keyboard(small_page):
    rec, _ = small_page
    page = rec.page
    page.keyboard.press("/")
    page.keyboard.type("car")
    assert page.is_visible("#vx-results")
    page.keyboard.press("Enter")
    page.wait_for_selector("#vx-p-name")
    assert page.text_content("#vx-p-name") == "Carol C"
    search_and_open(page, "Bob B")
    assert page.text_content("#vx-p-name") == "Bob B"
    no_errors(rec)


def test_community_color_leaves_out_the_owner(browser, groups_render):
    """The communities come from Leiden without the owner (FR-26). With the owner in the
    graph, one group holds almost all accounts (live data 2026-11-03)."""
    rec = open_page(browser, groups_render[0])
    page = rec.page
    page.click("#vx-btn-controls")
    page.select_option("#vx-color", "community")
    sizes = page.get_attribute("#vx-graph", "data-communities")
    assert sizes == "7,6"  # account 30 links into both cliques and joins one of them
    assert "exploratory" in page.text_content("#vx-comm-note")
    page.select_option("#vx-color", "topic")
    assert "Topic: security" in page.text_content("#vx-legend")
    page.select_option("#vx-color", "state")
    no_errors(rec)
    rec.page.context.close()


def test_community_topic_and_hub_filters(browser, groups_render):
    rec = open_page(browser, groups_render[0])
    page = rec.page
    page.click("#vx-btn-controls")
    assert shown(page)[0] == 14
    page.select_option("#vx-f-community", "0")
    assert shown(page)[0] == 8  # the group of 7 and the owner, who always shows
    page.select_option("#vx-f-community", "all")
    page.select_option("#vx-f-topic", "security")
    n_topic = shown(page)[0]
    assert 3 <= n_topic < 14
    page.select_option("#vx-f-topic", "all")
    page.check("#vx-f-hubs")
    assert 1 < shown(page)[0] < 14
    assert "bridges" in page.text_content("#vx-f-hubs-note")
    page.uncheck("#vx-f-hubs")
    assert shown(page)[0] == 14
    search_and_open(page, "acct30")
    text = page.text_content("#vx-p-groups")
    assert "bridge" in text and "Leiden runs agree" in text
    no_errors(rec)
    rec.page.context.close()


def test_changes_since_the_last_snapshot(browser, changes_render):
    rec = open_page(browser, changes_render)
    page = rec.page
    page.click("#vx-view-overview")
    card = page.text_content("#vx-ov-changes")
    assert "Changes since" in card and "New accounts" in card
    page.click("#vx-btn-controls")
    page.select_option("#vx-f-change", "new")
    assert shown(page)[0] == 2  # the new account and the owner, who always shows
    page.select_option("#vx-f-change", "state")
    assert shown(page)[0] == 2
    page.select_option("#vx-f-change", "all")
    search_and_open(page, "acct20")
    text = page.text_content("#vx-p-changes")
    assert "Some signals" in text and "B10" in text
    assert page.locator(".vx-finding[data-new='1']").count() == 1
    no_errors(rec)
    rec.page.context.close()


def test_redacted_page_opens_and_shows_no_handle(browser, groups_render):
    rec = open_page(browser, groups_render[1])
    page = rec.page
    assert page.get_attribute("body", "data-redacted") == "1"
    assert "Redacted" in page.text_content("#vx-private")
    body = page.text_content("body")
    assert "acct10" not in body and "Name 10" not in body
    no_errors(rec)
    rec.page.context.close()
