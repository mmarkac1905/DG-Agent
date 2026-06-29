import asyncio, sys, os
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
from playwright.async_api import async_playwright

async def discover():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={'width':1600,'height':3200}, color_scheme='dark')
        page = await ctx.new_page()
        await page.goto('http://localhost:8501/', wait_until='networkidle', timeout=30000)
        await page.wait_for_timeout(3000)
        links = await page.eval_on_selector_all(
            '[data-testid="stSidebarNav"] a',
            '(els) => els.map(e => ({href: e.href, text: e.innerText}))'
        )
        print('Sidebar nav links:')
        for l in links:
            href = l['href']
            text = l['text']
            print(f"  text={text!r}  href={href}")
        await page.screenshot(path='screenshots/home_renamed.png', full_page=True)
        await browser.close()

asyncio.run(discover())
