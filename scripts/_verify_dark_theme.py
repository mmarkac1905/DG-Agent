"""Verify dark theme is forced — open browser with LIGHT color scheme preference."""
import asyncio, os, sys
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
from playwright.async_api import async_playwright

OUT = Path(__file__).resolve().parent.parent / "screenshots"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # IMPORTANT: Request LIGHT mode — if dashboard is still dark, config.toml is working
        ctx = await browser.new_context(
            viewport={'width': 1600, 'height': 900},
            color_scheme='light',
        )
        page = await ctx.new_page()
        await page.goto('http://localhost:8501/', wait_until='networkidle', timeout=30000)
        await page.wait_for_timeout(4000)

        # Sample the background pixel in the main content area
        bg = await page.evaluate("""
            () => {
                const app = document.querySelector('.stApp');
                return window.getComputedStyle(app).backgroundColor;
            }
        """)
        print(f'App background color (browser in LIGHT mode): {bg}')
        print('Expected: rgb(10, 14, 23) or similar dark color (from config.toml backgroundColor=#0a0e17)')

        await page.screenshot(path=str(OUT / 'verify_dark_forced.png'), full_page=False)
        print(f'saved: verify_dark_forced.png')

        await browser.close()

asyncio.run(main())
