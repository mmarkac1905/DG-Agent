"""Visual QA — screenshot every Streamlit page using Playwright."""
import asyncio
import re
import sys
from pathlib import Path
from playwright.async_api import async_playwright

# Force stdout to handle emoji on Windows terminals (cp1252 default).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "http://localhost:8501"

PAGES = [
    {"name": "00_main", "path": "/", "wait": 3},
    {"name": "01_procurement_overview", "path": "/Procurement_Overview", "wait": 5},
    {"name": "02_vendor_scorecard", "path": "/Vendor_Scorecard", "wait": 5},
    {"name": "03_cpe_lifecycle", "path": "/CPE_Lifecycle", "wait": 5},
    {"name": "04_inventory_health", "path": "/Inventory_Health", "wait": 5},
    {"name": "05_goods_movements", "path": "/Goods_Movements", "wait": 5},
    {"name": "06_business_catalog", "path": "/Business_Glossary", "wait": 5},
    {"name": "07_data_catalog", "path": "/Data_Catalog", "wait": 5},
]


async def screenshot_all():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Very tall viewport so Streamlit's internal scroll container renders all content at once.
        context = await browser.new_context(
            viewport={"width": 1600, "height": 5200},
            color_scheme="dark",
            device_scale_factor=1,
        )
        page = await context.new_page()

        for pg in PAGES:
            url = f"{BASE_URL}{pg['path']}"
            print(f"\n--- {pg['name']} ---")
            print(f"Navigating to {url}")

            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await page.wait_for_timeout(pg["wait"] * 1000)

                error_elements = await page.query_selector_all('[data-testid="stException"]')
                if error_elements:
                    print(f"  [ERROR] found on {pg['name']}")
                    for el in error_elements:
                        text = await el.inner_text()
                        print(f"  Error text: {text[:500]}")
                else:
                    print(f"  [OK] no exceptions")

                warnings = await page.query_selector_all('[data-testid="stAlert"]')
                for w in warnings:
                    text = await w.inner_text()
                    if "No data" in text or "empty" in text.lower():
                        print(f"  [WARN] {text[:200]}")

                screenshot_path = SCREENSHOTS_DIR / f"{pg['name']}.png"
                await page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"  saved: {screenshot_path.name}")

                height = await page.evaluate("document.documentElement.scrollHeight")
                print(f"  height: {height}px")

            except Exception as e:
                print(f"  [FAIL] {e}")
                screenshot_path = SCREENSHOTS_DIR / f"{pg['name']}_error.png"
                try:
                    await page.screenshot(path=str(screenshot_path))
                except Exception:
                    pass

        # --- Business Glossary: Term Detail + S2T Specification for two terms ---
        print("\n--- 06_business_glossary (mapped + unmapped terms × both views) ---")

        async def _select_term(term_text):
            """Type into the visible selectbox of the active tab and choose a term.

            Both Term Detail and S2T Specification tabs render a selectbox with the
            same label; Streamlit keeps the inactive tab's DOM alive but hidden. We
            scope to `:visible` to pick only the one in the currently active tab.
            """
            try:
                locator = page.locator(
                    'div[data-testid="stSelectbox"]:visible:has-text("Select Business Term")'
                ).first
                await locator.click(timeout=8000)
                await page.wait_for_timeout(600)
                await page.keyboard.type(term_text, delay=30)
                await page.wait_for_timeout(700)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(2500)
            except Exception as e:
                try:
                    msg = str(e).encode("ascii", "replace").decode("ascii")
                except Exception:
                    msg = "select failed"
                print(f"    [WARN] could not select term {term_text!r}: {msg[:200]}")

        async def _click_tab(tab_name):
            try:
                # Tab name uses regex so emoji prefixes like "🔧 S2T Specification" match
                await page.get_by_role("tab", name=re.compile(re.escape(tab_name))).click(timeout=8000)
                await page.wait_for_timeout(2500)
            except Exception as e:
                try:
                    msg = str(e).encode("ascii", "replace").decode("ascii")
                except Exception:
                    msg = "click failed"
                print(f"    [WARN] could not click tab {tab_name!r}: {msg[:200]}")

        try:
            await page.goto(f"{BASE_URL}/Business_Glossary", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)

            shot_matrix = [
                ("Average Vendor Lead Time", "Term Detail",      "06_term_detail_lead_time_business"),
                ("Average Vendor Lead Time", "S2T Specification", "06_s2t_spec_lead_time"),
                ("Total Cost of Ownership",  "Term Detail",      "06_term_detail_tco_business"),
                ("Total Cost of Ownership",  "S2T Specification", "06_s2t_spec_tco_ask_claude"),
            ]
            for term_text, tab_name, shot_name in shot_matrix:
                await _click_tab(tab_name)
                await _select_term(term_text)
                errs = await page.query_selector_all('[data-testid="stException"]')
                if errs:
                    for el in errs:
                        text = await el.inner_text()
                        print(f"    [ERROR on {shot_name}] {text[:300]}")
                await page.screenshot(
                    path=str(SCREENSHOTS_DIR / f"{shot_name}.png"),
                    full_page=True,
                )
                print(f"  saved: {shot_name}.png")

            # Also snap the New Term form
            await _click_tab("New Term")
            await page.screenshot(
                path=str(SCREENSHOTS_DIR / "06_new_term_form.png"),
                full_page=True,
            )
            print("  saved: 06_new_term_form.png")

        except Exception as e:
            try:
                msg = str(e).encode("ascii", "replace").decode("ascii")
            except Exception:
                msg = "exception"
            print(f"  [FAIL] Term detail check: {msg[:300]}")

        # --- Data Lineage tabs ---
        print("\n--- 07_data_lineage_tabs ---")
        try:
            await page.goto(f"{BASE_URL}/Data_Catalog", wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(3000)

            tabs = await page.query_selector_all('[data-baseweb="tab"]')
            tab_names = ["architecture", "data_vault", "abap_catalog", "sap_dictionary"]

            for i, tab_name in enumerate(tab_names):
                if i < len(tabs):
                    await tabs[i].click()
                    await page.wait_for_timeout(2000)
                    await page.screenshot(
                        path=str(SCREENSHOTS_DIR / f"07_{tab_name}.png"),
                        full_page=True,
                    )
                    print(f"  saved: 07_{tab_name}.png")

        except Exception as e:
            print(f"  [FAIL] Lineage tab check: {e}")

        await browser.close()

    print(f"\n{'='*60}")
    print(f"Visual QA complete. Screenshots in: {SCREENSHOTS_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(screenshot_all())
