"""End-to-end UI tests: real browser, real app, mocked upstreams.

Every assertion here goes through the shipping app.js and the real API. The
mock only replaces the outbound services, so a regression in the frontend, the
SQL or the scan pipeline fails these tests.

    tests/e2e/harness.sh start
    pytest tests/e2e/test_ui.py -v
"""
import glob
import json
import os
import re
import urllib.request

import pytest
from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("JELLYLOOK_URL", "http://127.0.0.1:9098")
MOCK = os.environ.get("JELLYLOOK_MOCK_URL", "http://127.0.0.1:9099")
expect.set_options(timeout=8000)


def chromium_path() -> str | None:
    """Locate a usable Chromium.

    CI images often ship a browser build that does not match the version the
    installed playwright package expects, so fall back to whatever is on disk
    rather than trying to download one.
    """
    explicit = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if explicit:
        return explicit
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    for pattern in ("chromium-*/chrome-linux/chrome", "chromium/chrome-linux/chrome"):
        found = sorted(glob.glob(os.path.join(root, pattern)))
        if found:
            return found[-1]
    return None


def api(path: str):
    with urllib.request.urlopen(BASE + path) as resp:
        return json.load(resp)


def post(url: str, payload: dict | None = None):
    body = json.dumps(payload if payload is not None else {}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def reset_state():
    """Clear every verdict so tests do not inherit each other's decisions."""
    # Auto-send is global and persists in SQLite, so a test that turns it on
    # must not be able to leak it into whatever runs next. The mock's requested
    # set is cleared for the same reason: an already-requested title is skipped
    # by design, so without this the suite would only pass on a fresh harness.
    post(BASE + "/api/settings", {"auto_request_threshold": "0"})
    post(MOCK + "/seerr/_reset")
    for user in ("u1", "u2"):
        for verdict in ("saved", "hidden"):
            for item in api(f"/api/recommendations?users={user}&verdict={verdict}")["items"]:
                body = json.dumps({
                    "user_ids": [user], "media_type": item["media_type"],
                    "tmdb_id": item["tmdb_id"], "verdict": None,
                    "title": item["title"],
                }).encode()
                req = urllib.request.Request(
                    BASE + "/api/feedback", data=body, method="POST",
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req).read()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        kwargs = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        exe = chromium_path()
        if exe:
            kwargs["executable_path"] = exe
        b = p.chromium.launch(**kwargs)
        yield b
        b.close()


def instrument(pg):
    """Wire up offline routing and collect anything that counts as a real fault.

    Webfonts are blocked rather than fetched, so the suite runs with no
    outbound network at all. Their failures are then expected and must not be
    mistaken for application errors — but a failed request to the app or the
    mock still is one, as is any uncaught JS exception.
    """
    faults: list[str] = []
    pg.on("pageerror", lambda e: faults.append(f"JS exception: {e}"))
    pg.on("console", lambda m: faults.append(f"console: {m.text}")
          if m.type == "error" and "Failed to load resource" not in m.text else None)
    pg.on("requestfailed", lambda r: faults.append(f"request failed: {r.url}")
          if r.url.startswith((BASE, MOCK)) else None)

    for host in ("fonts.googleapis.com", "fonts.gstatic.com"):
        pg.route(f"**/{host}/**", lambda route: route.abort())
    # Provider logos are the one asset app.js builds against image.tmdb.org
    # directly; point them at the mock so the page has no external dependency.
    pg.route("**/image.tmdb.org/**",
             lambda route: route.fulfill(status=302, headers={
                 "Location": f"{MOCK}/img/{route.request.url.rsplit('/', 1)[-1]}"}))
    return faults


@pytest.fixture
def page(browser):
    reset_state()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    faults = instrument(pg)
    pg.goto(BASE)
    pg.wait_for_selector("#user-chips .chip")
    yield pg
    assert not faults, f"page faults: {faults}"
    ctx.close()


def select_user(page, name="dean"):
    page.locator(".user-row .chip", has_text=name).locator("input").check()
    page.wait_for_timeout(400)


def run_scan(page):
    page.locator("#btn-scan").click()
    expect(page.locator(".card").first).to_be_visible(timeout=45000)


def ensure_results(page, user="dean"):
    select_user(page, user)
    if page.locator(".card").count() == 0:
        run_scan(page)
    expect(page.locator(".card").first).to_be_visible()


# --- basics --------------------------------------------------------------------
def test_loads_users_and_gates_scan(page):
    expect(page.locator("#user-chips .chip")).to_have_count(2)
    expect(page.locator("#btn-scan")).to_be_disabled()
    select_user(page)
    expect(page.locator("#btn-scan")).to_be_enabled()


def test_scan_produces_cards(page):
    ensure_results(page)
    assert page.locator(".card").count() > 0
    card = page.locator(".card").first
    expect(card.locator(".card-title")).to_be_visible()
    expect(card.locator(".poster-wrap img")).to_be_visible()


def test_card_shows_ratings_and_type(page):
    ensure_results(page)
    card = page.locator(".card").first
    expect(card.locator(".imdb-badge")).to_be_visible()
    expect(card.locator(".type-chip")).to_be_visible()


# --- richer cards ---------------------------------------------------------------
def test_streaming_row_credits_justwatch(page):
    ensure_results(page)
    row = page.locator(".provider-row").first
    expect(row).to_be_visible()
    expect(row.locator(".justwatch")).to_have_text("JustWatch")
    expect(row.locator(".provider-logo").first).to_be_visible()


def test_trailer_opens_youtube_link(page):
    ensure_results(page)
    card = page.locator(".card", has=page.locator(".trailer-btn")).first
    card.hover()
    card.locator(".trailer-btn").click()
    expect(page.locator("#trailer-overlay")).to_be_visible()
    href = page.locator("#trailer-link").get_attribute("href")
    assert href.startswith("https://www.youtube.com/watch?v="), href
    # Privacy: the trailer is a link, never an embedded player.
    assert page.locator("#trailer-overlay iframe").count() == 0


def test_help_panel_carries_attribution(page):
    page.locator("#btn-help").click()
    body = page.locator("#help-overlay").inner_text()
    assert "JustWatch" in body
    assert "not endorsed or certified by TMDB" in body


# --- feedback --------------------------------------------------------------------
def test_hiding_removes_card_and_counts_it(page):
    ensure_results(page)
    title = page.locator(".card").first.locator(".card-title").inner_text()
    before = page.locator(".card").count()
    page.locator(".card").first.locator(".fb-btn").nth(1).click()
    expect(page.locator(".card")).not_to_have_count(before)
    expect(page.locator("#tab-hidden")).to_contain_text("1")
    titles = [t.inner_text() for t in page.locator(".card-title").all()]
    assert title not in titles


def test_saving_marks_card_and_fills_saved_tab(page):
    ensure_results(page)
    card = page.locator(".card").first
    title = card.locator(".card-title").inner_text()
    card.locator(".fb-btn").first.click()
    expect(card.locator(".fb-btn").first).to_have_class(re.compile("is-on"))
    expect(page.locator("#tab-saved")).to_contain_text("1")

    page.locator("#tab-saved").click()
    expect(page.locator(".card").first).to_be_visible()
    assert page.locator(".card-title").first.inner_text() == title
    # Saved and Hidden are decision lists, so scan/filter controls step aside.
    expect(page.locator("#btn-filters")).to_be_hidden()


def test_unhiding_from_hidden_tab_removes_it(page):
    ensure_results(page)
    page.locator(".card").first.locator(".fb-btn").nth(1).click()
    page.wait_for_timeout(400)
    page.locator("#tab-hidden").click()
    expect(page.locator(".card")).to_have_count(1)
    page.locator(".card").first.locator(".fb-btn").nth(1).click()
    expect(page.locator(".card")).to_have_count(0)
    expect(page.locator("#status-line")).to_contain_text("Nothing hidden")


def test_feedback_is_per_user(page):
    """A hide by dean must not affect robert — the core of the per-user model."""
    ensure_results(page, "dean")
    title = page.locator(".card").first.locator(".card-title").inner_text()
    page.locator(".card").first.locator(".fb-btn").nth(1).click()
    page.wait_for_timeout(500)

    page.locator(".user-row .chip", has_text="dean").locator("input").uncheck()
    page.locator(".user-row .chip", has_text="robert").locator("input").check()
    page.wait_for_timeout(700)
    expect(page.locator("#tab-hidden")).not_to_contain_text("1")
    titles = [t.inner_text() for t in page.locator(".card-title").all()]
    assert title in titles, "robert should still be offered what dean hid"


def test_feedback_disabled_without_a_viewer(page):
    ensure_results(page)
    page.locator(".user-row .chip", has_text="dean").locator("input").uncheck()
    page.wait_for_timeout(600)
    if page.locator(".card").count():
        expect(page.locator(".card").first.locator(".fb-btn").first).to_be_disabled()


# --- auto-send to Seerr --------------------------------------------------------------
def set_auto_threshold(page, value):
    """Drive the Settings panel, so the control and the API are both exercised."""
    page.locator("#btn-settings").click()
    expect(page.locator("#settings-overlay")).to_be_visible()
    page.locator("#set-auto-threshold").select_option(value)
    page.locator("#settings-form button[type=submit]").click()
    expect(page.locator("#settings-saved")).to_contain_text("Saved")
    page.keyboard.press("Escape")
    expect(page.locator("#settings-overlay")).to_be_hidden()


def test_auto_send_requests_high_matches_and_badges_them(page):
    """Threshold on → scan → the top matches are filed and say so."""
    select_user(page)
    set_auto_threshold(page, "90")
    try:
        run_scan(page)   # always a fresh scan, so the threshold applies to it
        expect(page.locator("#status-line")).to_contain_text("Auto-sent")

        card = page.locator(".card", has=page.locator(".auto-chip")).first
        expect(card).to_be_visible()
        expect(card.locator(".seerr-btn")).to_contain_text("Auto-sent")
        expect(card.locator(".seerr-btn")).to_be_disabled()

        # Nothing below the threshold may be sent.
        for badge in page.locator(".card:has(.auto-chip) .match-badge").all():
            assert int(badge.inner_text().rstrip("%")) >= 90

        # The badge is a stored fact, not a leftover of the scan response.
        # Settle first: a reload that cancels the lazy Seerr-status calls
        # registers as a failed request and fails this test's own fixture.
        page.wait_for_load_state("networkidle")
        page.reload()
        page.wait_for_selector("#user-chips .chip")
        expect(page.locator(".auto-chip").first).to_be_visible()
        assert any(item["auto_requested"]
                   for item in api("/api/recommendations")["items"])

        page.locator("#btn-settings").click()
        assert "≥ 90%" in page.locator("#status-dl").inner_text()
        page.keyboard.press("Escape")
    finally:
        # Straight to the API: an assertion that leaves an overlay open would
        # otherwise block the cleanup click and mask the real failure.
        post(BASE + "/api/settings", {"auto_request_threshold": "0"})


def test_a_threshold_the_dropdown_does_not_list_stays_selectable(page):
    """The API takes any 50-100 value, so the panel must not blank the control
    on one it has no <option> for — the next save would write "" over it."""
    post(BASE + "/api/settings", {"auto_request_threshold": "88"})
    try:
        # Opening the panel re-reads /api/settings, so no reload is needed.
        page.locator("#btn-settings").click()
        expect(page.locator("#set-auto-threshold")).to_have_value("88")
        page.locator("#settings-form button[type=submit]").click()
        expect(page.locator("#settings-saved")).to_contain_text("Saved")
        assert api("/api/settings")["auto_request_threshold"] == "88"
    finally:
        post(BASE + "/api/settings", {"auto_request_threshold": "0"})


def test_auto_send_off_leaves_the_add_button_alone(page):
    """The default must stay hands-off: no request, no badge."""
    select_user(page)
    run_scan(page)
    assert page.locator(".auto-chip").count() == 0
    expect(page.locator("#status-line")).not_to_contain_text("Auto-sent")
    expect(page.locator(".card").first.locator(".seerr-btn")).to_be_visible()


# --- filters ----------------------------------------------------------------------
def test_filter_drawer_and_genre_chip(page):
    ensure_results(page)
    page.locator("#btn-filters").click()
    expect(page.locator("#filter-drawer")).to_be_visible()
    chip = page.locator(".genre-chip").first
    label = chip.inner_text()
    chip.click()
    page.wait_for_timeout(600)
    expect(page.locator("#filter-count")).to_be_visible()
    expect(page.locator("#filter-count")).to_have_text("1")
    genre = label.rsplit(" ", 1)[0]
    count = int(label.rsplit(" ", 1)[1])
    assert page.locator(".card").count() <= count, f"more cards than {genre} facet claimed"


def test_reset_filters_clears_badge(page):
    ensure_results(page)
    page.locator("#btn-filters").click()
    page.locator("#f-imdb").select_option("8")
    page.wait_for_timeout(500)
    expect(page.locator("#filter-count")).to_have_text("1")
    page.locator("#btn-reset-filters").click()
    page.wait_for_timeout(500)
    expect(page.locator("#filter-count")).to_be_hidden()


def test_library_and_streaming_filters(page):
    ensure_results(page)
    page.locator("#btn-filters").click()
    page.locator("#f-library").select_option("only")
    page.wait_for_timeout(600)
    for chip in page.locator(".card .lib-chip").all():
        expect(chip).to_be_visible()
    assert page.locator(".card").count() >= 1

    page.locator("#f-library").select_option("any")
    page.locator("#f-streaming").select_option("only")
    page.wait_for_timeout(600)
    cards = page.locator(".card").count()
    assert cards == page.locator(".card .provider-row").count(), \
        "every card under 'already streamable' must show providers"


def test_type_filter(page):
    ensure_results(page)
    page.locator('.seg-btn[data-filter="tv"]').click()
    page.wait_for_timeout(600)
    for chip in page.locator(".card .type-chip").all():
        assert chip.inner_text() == "tv"


# --- scan history -------------------------------------------------------------------
def test_all_time_view(page):
    ensure_results(page)
    page.locator("#scan-select").select_option("all")
    page.wait_for_timeout(700)
    expect(page.locator(".card").first).to_be_visible()
    options = page.locator("#scan-select option").all_inner_texts()
    assert options[0] == "Latest scan"
    assert options[1].startswith("All time")


def test_sort_changes_order(page):
    ensure_results(page)
    first_by_match = page.locator(".card-title").first.inner_text()
    page.locator("#sort-select").select_option("year")
    page.wait_for_timeout(600)
    expect(page.locator(".card").first).to_be_visible()
    assert page.locator("#sort-select").input_value() == "year"
    assert first_by_match  # sanity: the match-sorted view had rendered


# --- settings ------------------------------------------------------------------------
def test_settings_panel_reports_region_and_retention(page):
    select_user(page)
    page.locator("#btn-settings").click()
    body = page.locator("#settings-overlay").inner_text()
    assert "Streaming region" in body and "AU" in body
    assert "Kept for" in body
    assert "Saved / hidden" in body


def test_escape_closes_overlays(page):
    page.locator("#btn-help").click()
    expect(page.locator("#help-overlay")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#help-overlay")).to_be_hidden()


# --- responsive -----------------------------------------------------------------------
@pytest.mark.parametrize("width", [390, 768])
def test_no_horizontal_overflow(browser, width):
    reset_state()
    ctx = browser.new_context(viewport={"width": width, "height": 844})
    pg = ctx.new_page()
    faults = instrument(pg)
    pg.goto(BASE)
    pg.wait_for_selector("#user-chips .chip")
    pg.locator(".user-row .chip", has_text="dean").locator("input").check()
    pg.wait_for_timeout(600)
    expect(pg.locator(".card").first).to_be_visible(timeout=45000)
    pg.locator("#btn-filters").click()          # the widest state of the page
    pg.wait_for_timeout(400)

    assert pg.evaluate("document.documentElement.scrollWidth") <= width, (
        f"horizontal overflow at {width}px: "
        + str(pg.evaluate("""() => [...document.querySelectorAll('*')]
              .filter(el => el.getBoundingClientRect().right > %d.5)
              .slice(0, 5)
              .map(el => el.tagName + '.' + el.className)""" % width))
    )
    expect(pg.locator(".card").first.locator(".fb-btn").first).to_be_visible()

    # Card internals must fit their card too — a clipped rating row does not
    # widen the document, so the page-level check above cannot see it.
    overflowing = pg.evaluate("""() => [...document.querySelectorAll('.card')]
        .flatMap(card => [...card.querySelectorAll('.rating-row, .provider-row,
                                                    .card-actions')]
          .filter(el => el.scrollWidth > el.clientWidth + 1)
          .map(el => el.className))""".replace("\n", " "))
    assert not overflowing, f"clipped inside card at {width}px: {overflowing}"

    assert not faults, f"page faults: {faults}"
    ctx.close()


def test_reduced_motion_still_shows_cards(browser):
    """With animations off, cards must be visible rather than stuck at opacity 0."""
    reset_state()
    ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                              reduced_motion="reduce")
    pg = ctx.new_page()
    faults = instrument(pg)
    pg.goto(BASE)
    pg.wait_for_selector("#user-chips .chip")
    pg.locator(".user-row .chip", has_text="dean").locator("input").check()
    pg.wait_for_timeout(800)
    card = pg.locator(".card").first
    expect(card).to_be_visible(timeout=45000)
    assert pg.evaluate("getComputedStyle(document.querySelector('.card')).opacity") == "1"
    # The drawer's height is animated inline, so it needs its own escape hatch.
    pg.locator("#btn-filters").click()
    pg.wait_for_timeout(300)
    assert pg.locator("#filter-drawer").bounding_box()["height"] > 40
    assert not faults, f"page faults: {faults}"
    ctx.close()
