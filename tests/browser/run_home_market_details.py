"""Homepage disclosure preserves live metadata across updates and page lifecycle."""
from pathlib import Path
from urllib.parse import urlparse
import os, json, sys
from playwright.sync_api import sync_playwright

ROOT = Path(os.environ['STOCKBOT_UI_ARTIFACTS'])
REPO = Path(__file__).resolve().parents[2]
engine = sys.argv[1] if len(sys.argv) > 1 else 'chromium'
checks, errors, posts = [], [], []
with sync_playwright() as p:
    browser = getattr(p, engine).launch(headless=True)
    page = browser.new_page(viewport={'width': 390, 'height': 844})
    page.on('pageerror', lambda e: errors.append(str(e)))
    def route(r):
        path = urlparse(r.request.url).path
        if r.request.method == 'POST':
            posts.append(path); r.fulfill(status=403); return
        if path.startswith('/static/'):
            f = REPO / path.lstrip('/')
            r.fulfill(body=f.read_text(encoding='utf-8'), content_type='text/css' if f.suffix == '.css' else 'application/javascript'); return
        if '/api/' in path or path == '/web/quote-lab/app-config':
            r.fulfill(json={'ok': True, 'market_open': False, 'updates': []}); return
        r.fulfill(body=(ROOT / 'home.html').read_text(encoding='utf-8'), content_type='text/html')
    page.route('https://fixture.test/**', route)
    def check(name, ok):
        checks.append({'check': name, 'passed': bool(ok)}); assert ok, name
    page.goto('https://fixture.test/web/portfolio')
    page.wait_for_selector('.home-market-details')
    details = page.locator('.home-market-details')
    check('one collapsed disclosure retains all original metadata', details.count() == 1 and details.get_attribute('open') is None and details.locator('.hv2-src').count() == 1 and details.locator('[data-home-quote-quality]').count() == 1)
    check('pending prices stay visibly identified', '待確認' in page.locator('.home-market-status').inner_text())
    check('summary has a usable touch target', details.locator('summary').bounding_box()['height'] >= 44)
    details.locator('summary').click()
    check('source details can be opened', details.locator('.hv2-src').is_visible())
    page.evaluate("""()=>{
      window.originalQuality=document.querySelector('[data-home-quote-quality]');
      originalQuality.textContent='持股行情已更新 2026-11-16 13:30:00';
      document.querySelector('[data-home-live-title]').textContent='收盤行情';
    }""")
    page.wait_for_function("document.querySelector('.home-market-status').textContent==='收盤行情'")
    check('live updates refresh status and timestamp', page.locator('.home-market-stamp').inner_text() == '13:30 更新' and 'is-pending' not in (details.get_attribute('class') or ''))
    page.evaluate("""()=>{document.dispatchEvent(new Event('stockbot:pageleaving'));document.dispatchEvent(new Event('stockbot:page-ready'));window.dispatchEvent(new Event('pagehide'));window.dispatchEvent(new Event('pageshow'));}""")
    check('lifecycle reuses disclosure and exact provider node', details.count() == 1 and details.get_attribute('open') is not None and page.evaluate("document.querySelector('[data-home-quote-quality]')===originalQuality"))
    page.evaluate("originalQuality.textContent='行情暫缺，等待更新'")
    page.wait_for_function("document.querySelector('.home-market-details').classList.contains('is-pending')")
    check('observer reconnects after returning', '待確認' in page.locator('.home-market-status').inner_text())
    page.emulate_media(reduced_motion='reduce')
    page.reload();page.wait_for_selector('.home-market-details')
    check('reduced motion retains disclosure', page.locator('.home-market-details').count() == 1)
    for width in (320, 390, 768, 1280):
        page.set_viewport_size({'width': width, 'height': 844})
        check(f'homepage fits viewport {width}', page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
    if os.environ.get('STOCKBOT_RANK_LEGACY_CSS'):
        page.add_style_tag(path=os.environ['STOCKBOT_RANK_LEGACY_CSS'])
    page.evaluate("""()=>{
      const legacy=document.createElement('style');legacy.textContent='body #app-page-content .rank-rows .rank-meta-item{flex-direction:column!important;min-height:54px!important} body #app-page-content .rank-rows .rank-meta-item>em{width:100%!important}';document.head.append(legacy);
      const rows=document.createElement('section'); rows.className='rank-rows';
      rows.innerHTML='<article class="rank-card"><div class="rank-row-main"><div class="rank-primary-line"><span class="rank-number">#1</span><span class="name">測試長姓名｜投資組合管理人</span></div><span class="rank-return">+123.45%</span><span class="rank-movement">↑ 2 名</span></div><div class="rank-meta"><span class="rank-meta-item"><em>本季</em><b class="rank-meta-value">+12.3%</b></span><span class="rank-meta-item rank-meta-excess"><em>超額報酬</em><b class="rank-meta-value">贏 13.7% （大盤 -11.4%）</b></span><span class="rank-meta-item"><em>持股</em><b class="rank-meta-value">9 檔（含 ETF 2 檔）</b></span><span class="rank-meta-item"><em>有效樣本</em><b class="rank-meta-value">28 天</b></span></div></article>';
      document.getElementById('app-page-content').replaceChildren(rows);
    }""")
    for width in (320, 390, 1280):
        page.set_viewport_size({'width': width, 'height': 844})
        check(f'long ranking labels remain inside card {width}', page.locator('.rank-card').evaluate('(card)=>Array.from(card.querySelectorAll(".name,.rank-return,.rank-meta-value")).every(e=>{const r=e.getBoundingClientRect(),c=card.getBoundingClientRect();return r.right<=c.right&&r.left>=c.left})'))
    page.set_viewport_size({'width': 390, 'height': 844})
    check('legacy column style cannot expand ranking rows', page.locator('.rank-meta-item').first.evaluate('(e)=>getComputedStyle(e).flexDirection==="row"'))
    check('legacy full width labels cannot squeeze returns vertically', page.locator('.rank-meta-item').first.evaluate('(e)=>e.querySelector("b").getBoundingClientRect().width>35&&e.getBoundingClientRect().height<45'))
    page.screenshot(path=str(ROOT / (engine+'-rank-layout.png')))
    if posts or errors: print({'posts': posts, 'errors': errors}, flush=True)
    check('no writes or JavaScript errors', not posts and not errors)
    browser.close()
ROOT.joinpath(engine+'-home-details-results.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'engine': engine, 'passed': len(checks), 'errors': errors}))
