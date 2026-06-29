import asyncio, os, sys
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(viewport={'width':1600,'height':1200}, color_scheme='dark')
        page = await ctx.new_page()
        await page.goto('http://localhost:8501/', wait_until='networkidle', timeout=30000)
        await page.wait_for_timeout(3000)

        js = """
() => {
    function box(sel) {
        const el = document.querySelector(sel);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        const s = window.getComputedStyle(el);
        return {
            sel: sel,
            top: Math.round(r.top),
            bottom: Math.round(r.bottom),
            height: Math.round(r.height),
            paddingTop: s.paddingTop,
            paddingBottom: s.paddingBottom,
            marginTop: s.marginTop,
            marginBottom: s.marginBottom,
        };
    }
    const u = box('[data-testid="stSidebarUserContent"]');
    const n = box('[data-testid="stSidebarNav"]');
    const sep = box('.st-emotion-cache-1r9ow6z');
    const items = box('[data-testid="stSidebarNavItems"]');
    return {user: u, nav: n, sep: sep, items: items, gap: n ? (n.top - (u ? u.bottom : 0)) : null};
}
"""
        result = await page.evaluate(js)
        import json
        print(json.dumps(result, indent=2))
        await b.close()

asyncio.run(main())
