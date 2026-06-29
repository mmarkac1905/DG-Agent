import asyncio, os, sys, json
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
    const out = {};
    // Branding end vs Dashboard header start
    const u = document.querySelector('[data-testid="stSidebarUserContent"]');
    const nav = document.querySelector('[data-testid="stSidebarNav"]');
    if (u && nav) {
        out.branding_to_dashboard = Math.round(nav.getBoundingClientRect().top - u.getBoundingClientRect().bottom);
    }

    // Inspect nav section headers — they may be h3 or div with class containing "SectionHeader"
    const navItems = document.querySelector('[data-testid="stSidebarNavItems"]');
    if (navItems) {
        const children = Array.from(navItems.children);
        out.nav_children = children.map(c => ({
            tag: c.tagName,
            testid: c.getAttribute('data-testid') || '',
            cls: (typeof c.className === 'string') ? c.className.split(' ')[0] : '',
            text: (c.innerText || '').slice(0, 50),
            top: Math.round(c.getBoundingClientRect().top),
            bottom: Math.round(c.getBoundingClientRect().bottom),
            height: Math.round(c.getBoundingClientRect().height),
        }));

        // Compute consecutive gaps
        out.gaps = [];
        for (let i = 0; i < children.length - 1; i++) {
            const a = children[i].getBoundingClientRect();
            const c = children[i+1].getBoundingClientRect();
            out.gaps.push({
                from: (children[i].innerText || '').slice(0,30),
                to: (children[i+1].innerText || '').slice(0,30),
                gap: Math.round(c.top - a.bottom),
            });
        }
    }
    return out;
}
"""
        result = await page.evaluate(js)
        print(json.dumps(result, indent=2))
        await b.close()

asyncio.run(main())
