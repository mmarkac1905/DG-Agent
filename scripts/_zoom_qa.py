"""Zoom-in screenshots at 1800x1200 for detail inspection."""
import asyncio, os, sys
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from playwright.async_api import async_playwright

OUT = Path(__file__).resolve().parent.parent / "screenshots"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={'width': 1800, 'height': 1200},
            color_scheme='dark',
        )
        page = await ctx.new_page()

        pages = [
            ('home_zoom', '/'),
            ('01_zoom_top', '/Procurement_Overview'),
            ('02_zoom_top', '/Vendor_Scorecard'),
            ('03_zoom_top', '/CPE_Lifecycle'),
            ('04_zoom_top', '/Inventory_Health'),
            ('05_zoom_top', '/Goods_Movements'),
        ]
        for name, path in pages:
            await page.goto(f'http://localhost:8501{path}', wait_until='networkidle', timeout=30000)
            await page.wait_for_timeout(4000)
            out = OUT / f'{name}.png'
            await page.screenshot(path=str(out), full_page=False)
            print(f'saved {name}')
        await browser.close()

asyncio.run(main())
