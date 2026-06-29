"""Visual QA — HEADED mode so the user can SEE the browser navigating.

This is the exact same script as visual_qa.py except headless=False.
A Chromium window will open on your screen and navigate through every page,
pausing on each for 5 seconds so you can watch it.
"""
import asyncio, os, sys
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from playwright.async_api import async_playwright

SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "http://localhost:8501"

PAGES = [
    {"name": "00_main",                  "path": "/",                      "wait": 4},
    {"name": "01_procurement_overview",  "path": "/Procurement_Overview",  "wait": 5},
    {"name": "02_vendor_scorecard",      "path": "/Vendor_Scorecard",      "wait": 5},
    {"name": "03_cpe_lifecycle",         "path": "/CPE_Lifecycle",         "wait": 5},
    {"name": "04_inventory_health",      "path": "/Inventory_Health",      "wait": 5},
    {"name": "05_goods_movements",       "path": "/Goods_Movements",       "wait": 5},
    {"name": "06_business_catalog",      "path": "/Business_Catalog",      "wait": 5},
    {"name": "07_data_catalog",          "path": "/Data_Catalog",          "wait": 5},
]


async def run():
    async with async_playwright() as p:
        # HEADED mode — you'll see a Chromium window open
        browser = await p.chromium.launch(headless=False, slow_mo=500)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 900},
            color_scheme="dark",
        )
        page = await context.new_page()

        print(f"\n{'='*60}")
        print("HEADED BROWSER QA — watch your screen for a Chromium window")
        print(f"{'='*60}\n")

        for pg in PAGES:
            url = f"{BASE_URL}{pg['path']}"
            print(f"--- {pg['name']}: {url}")
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(pg["wait"] * 1000)

                errors = await page.query_selector_all('[data-testid="stException"]')
                if errors:
                    print(f"  [ERROR] found on {pg['name']}")
                    for el in errors:
                        print(f"    {(await el.inner_text())[:300]}")
                else:
                    print(f"  [OK]")

                shot = SCREENSHOTS_DIR / f"headed_{pg['name']}.png"
                await page.screenshot(path=str(shot), full_page=True)
                print(f"  saved: {shot.name}")
            except Exception as e:
                print(f"  [FAIL] {e}")

        print("\nHolding browser open for 5 seconds so you can see it...")
        await page.wait_for_timeout(5000)
        await browser.close()


asyncio.run(run())
