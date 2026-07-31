"""Regenerate docs/screenshot-*.png from the running app.

These are shots of the real UI driven by a real browser — only the upstream
services are mocked (tests/e2e/mock_upstream.py), so the artwork and titles are
invented and the README carries no third-party posters.

    tests/e2e/harness.sh start
    python docs/mock/shoot.py
"""
import glob
import json
import os
import pathlib
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("JELLYLOOK_URL", "http://127.0.0.1:9098")
MOCK = os.environ.get("JELLYLOOK_MOCK_URL", "http://127.0.0.1:9099")
OUT = pathlib.Path(__file__).resolve().parents[1]

# Screenshots must be reproducible, so the staggered card reveal is pinned to
# its finished state rather than caught mid-flight.
FREEZE = """
  .card { opacity: 1 !important; transform: none !important; animation: none !important; }
  .modal, .panel { animation: none !important; }
"""


def chromium_path():
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    for pattern in ("chromium-*/chrome-linux*/chrome", "chromium/chrome-linux*/chrome"):
        found = sorted(glob.glob(os.path.join(root, pattern)))
        if found:
            return found[-1]
    return None


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))


def get(path):
    with urllib.request.urlopen(BASE + path) as resp:
        return json.load(resp)


def stage():
    """Give the demo instance a scan and a couple of verdicts to show off."""
    import time
    if not get("/api/recommendations?scan=latest")["total"]:
        post("/api/scan", {"user_ids": ["u1"]})
        for _ in range(120):
            if get("/api/scan/status")["state"] in ("done", "failed"):
                break
            time.sleep(0.5)
    items = get("/api/recommendations?users=u1&per_page=50")["items"]
    if items and not get("/api/status?users=u1")["feedback"]["saved"]:
        pick = items[1]
        post("/api/feedback", {"user_ids": ["u1"], "media_type": pick["media_type"],
                               "tmdb_id": pick["tmdb_id"], "verdict": "saved",
                               "title": pick["title"], "year": pick["year"],
                               "poster_url": pick["poster_url"]})


def new_page(browser, width, height):
    ctx = browser.new_context(viewport={"width": width, "height": height},
                              device_scale_factor=2)
    page = ctx.new_page()
    for host in ("fonts.googleapis.com", "fonts.gstatic.com"):
        page.route(f"**/{host}/**", lambda route: route.abort())
    page.route("**/image.tmdb.org/**",
               lambda route: route.fulfill(status=302, headers={
                   "Location": f"{MOCK}/img/{route.request.url.rsplit('/', 1)[-1]}"}))
    return ctx, page


def ready(page, width=1280, height=900):
    page.goto(BASE)
    page.wait_for_selector("#user-chips .chip")
    page.locator(".user-row .chip", has_text="dean").locator("input").check()
    page.wait_for_selector(".card", timeout=60000)
    page.add_style_tag(content=FREEZE)
    page.wait_for_timeout(600)


def main():
    stage()
    exe = chromium_path()
    with sync_playwright() as p:
        kwargs = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if exe:
            kwargs["executable_path"] = exe
        browser = p.chromium.launch(**kwargs)

        # Desktop — the main grid
        ctx, page = new_page(browser, 1280, 980)
        ready(page)
        page.screenshot(path=OUT / "screenshot-desktop.png")

        # Desktop — filter drawer open, showing genre facets
        page.locator("#btn-filters").click()
        page.wait_for_timeout(500)
        page.screenshot(path=OUT / "screenshot-filters.png")
        page.locator("#btn-filters").click()
        page.wait_for_timeout(400)

        # Season picker
        tv = page.locator(".card", has=page.locator(".type-chip", has_text="tv")).first
        tv.locator(".seerr-btn").click()
        page.wait_for_selector("#season-overlay:not(.hidden)", timeout=15000)
        page.wait_for_timeout(400)
        page.screenshot(path=OUT / "screenshot-season-picker.png")
        page.keyboard.press("Escape")
        ctx.close()

        # Mobile
        ctx, page = new_page(browser, 390, 844)
        ready(page, 390, 844)
        page.screenshot(path=OUT / "screenshot-mobile.png")
        ctx.close()

        browser.close()
    print("written:", ", ".join(sorted(f.name for f in OUT.glob("screenshot-*.png"))))


if __name__ == "__main__":
    main()
