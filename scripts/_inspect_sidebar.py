import asyncio, os, sys
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(viewport={'width':1600,'height':900}, color_scheme='dark')
        page = await ctx.new_page()
        await page.goto('http://localhost:8501/', wait_until='networkidle', timeout=30000)
        await page.wait_for_timeout(3000)

        js = """
() => {
    const sidebar = document.querySelector('section[data-testid="stSidebar"]');
    if (!sidebar) return 'no sidebar';
    function walk(el, depth) {
        depth = depth || 0;
        const out = [];
        const prefix = '  '.repeat(depth);
        const id = el.getAttribute('data-testid') || el.tagName;
        const cls = (el.className && typeof el.className === 'string') ? el.className.split(' ')[0] : '';
        out.push(prefix + id + (cls ? ' .' + cls : ''));
        if (depth < 4) {
            for (let i = 0; i < el.children.length; i++) {
                out.push.apply(out, walk(el.children[i], depth + 1));
            }
        }
        return out;
    }
    return walk(sidebar).join('\\n');
}
"""
        structure = await page.evaluate(js)
        print(structure)
        await b.close()

asyncio.run(main())
