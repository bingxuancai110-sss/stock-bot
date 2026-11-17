"""Actual workbench keeps stale rows visible and polls until fresh rows arrive."""
import json, os, sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ['STOCKBOT_UI_ARTIFACTS'])
engine = sys.argv[1] if len(sys.argv) > 1 else 'chromium'
calls = []; posts = []; errors = []; checks = []
with sync_playwright() as p:
    browser = getattr(p, engine).launch(headless=True)
    page = browser.new_page(viewport={'width':390,'height':844}, is_mobile=True, has_touch=True)
    page.on('pageerror', lambda e: errors.append(str(e)))
    def route(r):
        u = urlparse(r.request.url)
        if r.request.method == 'POST':
            posts.append(u.path); r.fulfill(status=403); return
        if u.path.startswith('/static/'):
            f = REPO / u.path.lstrip('/')
            r.fulfill(body=f.read_text(encoding='utf-8'), content_type='text/css' if f.suffix=='.css' else 'application/javascript'); return
        if u.path == '/web/api/workbench/source':
            source = parse_qs(u.query).get('source',[''])[0]
            if source == '雷達':
                calls.append(source); stale = len(calls) == 1
                r.fulfill(json={'ok':True,'rows':[{'code':'2330','name':'昨日測試' if stale else '今日測試','source':'雷達','price':100,'change_pct':1,'detail':{}}],
                    'meta':{'available':True,'date':'2026-11-16' if stale else '2026-11-17','stale':stale,'building':stale,'pool_checked':240}}); return
        if '/api/' in u.path or '/app-config' in u.path:
            r.fulfill(json={'ok':True,'rows':[],'sources':{},'meta':{'available':True,'market_open':False}}); return
        r.fulfill(body=(ROOT/'workbench.html').read_text(encoding='utf-8'), content_type='text/html')
    page.route('https://fixture.test/**', route)
    page.goto('https://fixture.test/web/workbench')
    page.locator('button[data-source="雷達"]').wait_for()
    page.clock.install()
    page.locator('button[data-source="雷達"]').click()
    page.get_by_text('2330　昨日測試', exact=True).wait_for()
    checks.append('old rows remain visible during refresh')
    assert '2026-11-16' in page.locator('#wb-note').inner_text()
    assert '背景更新中' in page.locator('#wb-note').inner_text()
    checks.append('stale date and pending state are explicit')
    page.clock.fast_forward(8100)
    page.get_by_text('2330　今日測試', exact=True).wait_for()
    assert '2026-11-17' in page.locator('#wb-note').inner_text()
    assert '舊快照' not in page.locator('#wb-note').inner_text()
    checks.append('poll replaces old rows and date with completed snapshot')
    page.clock.fast_forward(20000)
    assert len(calls) == 2
    checks.append('polling stops after completed snapshot')
    assert not posts and not errors, (posts, errors)
    checks.append('no writes or JavaScript errors')
    page.screenshot(path=str(ROOT/(engine+'-radar-refreshed.png')))
    browser.close()
print(json.dumps({'engine':engine,'passed':len(checks),'checks':checks}))
