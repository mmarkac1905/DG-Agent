"""Pixel-precise text-to-text gap measurement using Range API."""
import asyncio, os, sys, json
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(viewport={'width':1600,'height':1000}, color_scheme='dark')
        page = await ctx.new_page()
        await page.goto('http://localhost:8501/', wait_until='networkidle', timeout=30000)
        await page.wait_for_timeout(4000)

        js = """
() => {
    // Walk all text nodes and find the ones containing the texts we care about.
    function findTextNode(root, needle) {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
        let n;
        while (n = walker.nextNode()) {
            if (n.nodeValue && n.nodeValue.indexOf(needle) >= 0) return n;
        }
        return null;
    }
    function rangeRect(node) {
        if (!node) return null;
        const r = document.createRange();
        r.selectNodeContents(node);
        const rect = r.getBoundingClientRect();
        return {top: rect.top, bottom: rect.bottom, height: rect.height};
    }

    const sidebar = document.querySelector('section[data-testid="stSidebar"]');
    if (!sidebar) return {error: 'no sidebar'};

    const sourceNode = findTextNode(sidebar, 'Source:');
    const dashNode = findTextNode(sidebar, 'Dashboard');
    const goodsNode = findTextNode(sidebar, 'Goods Movements');
    const govNode = findTextNode(sidebar, 'Data Governance');

    const sourceRect = rangeRect(sourceNode);
    const dashRect = rangeRect(dashNode);
    const goodsRect = rangeRect(goodsNode);
    const govRect = rangeRect(govNode);

    return {
        source_text: sourceRect,
        dashboard_text: dashRect,
        goods_text: goodsRect,
        governance_text: govRect,
        visual_gap_brand_to_dashboard: (sourceRect && dashRect) ? Math.round(dashRect.top - sourceRect.bottom) : null,
        visual_gap_goods_to_governance: (goodsRect && govRect) ? Math.round(govRect.top - goodsRect.bottom) : null,
    };
}
"""
        result = await page.evaluate(js)
        print(json.dumps(result, indent=2))
        await b.close()

asyncio.run(main())
