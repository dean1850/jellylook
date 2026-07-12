"""Screenshot the mock UI with Playwright: desktop, mobile, and season picker."""
import asyncio, pathlib
from playwright.async_api import async_playwright

MOCK = pathlib.Path("/home/claude/jellylook/mock/mock.html").resolve().as_uri()
OUT = pathlib.Path("/home/claude/jellylook/docs")
OUT.mkdir(exist_ok=True)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # Desktop — main grid
        page = await browser.new_page(viewport={"width": 1280, "height": 900},
                                      device_scale_factor=2)
        await page.goto(MOCK)
        await page.wait_for_timeout(1800)  # let webfonts settle
        await page.screenshot(path=OUT / "screenshot-desktop.png", full_page=False)

        # Desktop — season picker open
        await page.evaluate("document.getElementById('season-overlay').classList.remove('hidden')")
        await page.wait_for_timeout(300)
        await page.screenshot(path=OUT / "screenshot-season-picker.png", full_page=False)
        await page.evaluate("document.getElementById('season-overlay').classList.add('hidden')")

        # Mobile
        mobile = await browser.new_page(viewport={"width": 390, "height": 844},
                                        device_scale_factor=2)
        await mobile.goto(MOCK)
        await mobile.wait_for_timeout(1800)
        await mobile.screenshot(path=OUT / "screenshot-mobile.png", full_page=False)

        await browser.close()
        print("screenshots written:", sorted(f.name for f in OUT.glob("*.png")))

asyncio.run(main())
