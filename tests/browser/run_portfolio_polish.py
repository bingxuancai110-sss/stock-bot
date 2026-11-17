"""Real holdings markup: selected state, readable pressed rows and responsive detail layout."""
from pathlib import Path
from urllib.parse import urlparse
import json, os, sys
from playwright.sync_api import sync_playwright
ROOT=Path(os.environ['STOCKBOT_UI_ARTIFACTS']);REPO=Path(__file__).resolve().parents[2]
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium';checks=[];errors=[];posts=[]
with sync_playwright() as p:
    browser=getattr(p,engine).launch(headless=True)
    page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        path=urlparse(r.request.url).path
        if r.request.method=='POST':posts.append(path);r.fulfill(status=403);return
        if path.startswith('/static/'):
            f=REPO/path.lstrip('/');r.fulfill(body=f.read_text(encoding='utf-8'),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
        if '/api/' in path or path=='/web/quote-lab/app-config':r.fulfill(json={'ok':True,'market_open':False,'updates':[],'events':[]});return
        r.fulfill(body=(ROOT/'positions.html').read_text(encoding='utf-8'),content_type='text/html')
    page.route('https://fixture.test/**',route)
    def check(name,ok):
        checks.append({'check':name,'passed':bool(ok)})
        if not ok:page.screenshot(path=str(ROOT/(engine+'-polish-failure.png')))
        assert ok,name
    page.goto('https://fixture.test/web/positions');page.wait_for_function('!!window.StockBotUI');page.wait_for_timeout(200)
    selected=page.locator('[data-p2-view][aria-selected=true]')
    check('selected holding view has readable light text',selected.evaluate('(e)=>{let c=getComputedStyle(e).webkitTextFillColor.match(/[0-9.]+/g).map(Number);return c[0]>200&&c[1]>200&&c[2]>200}'))
    for width in (320,390,1280):
        page.set_viewport_size({'width':width,'height':844})
        card=page.locator('.position-card').first;summary=card.locator('.position-more>summary')
        if not card.locator('.position-more').evaluate('(e)=>e.open'):summary.click()
        page.wait_for_timeout(250)
        check(f'expanded holdings fit viewport {width}',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
        check(f'cost values stay inside cells {width}',card.locator('.position-card-grid').evaluate('(e)=>Array.from(e.querySelectorAll("b")).every(b=>{const r=b.getBoundingClientRect(),p=b.parentElement.getBoundingClientRect();return r.left>=p.left&&r.right<=p.right&&r.bottom<=p.bottom})'))
        check(f'buy and sell actions share a usable row {width}',card.locator('.p2-card-actions').evaluate('(e)=>{const a=e.querySelector("button").getBoundingClientRect(),b=e.querySelectorAll("button")[1].getBoundingClientRect(),c=e.querySelector("a").getBoundingClientRect();return a.height>=44&&b.height>=44&&Math.abs(a.top-b.top)<1&&a.right<=b.left&&c.width>a.width*1.8&&c.top>=a.bottom}'))
    page.set_viewport_size({'width':390,'height':844})
    card=page.locator('.position-card').first;summary=card.locator('.position-more>summary')
    check('expanded stock has a persistent selection edge',card.evaluate('(e)=>getComputedStyle(e).boxShadow!=="none"'))
    before=card.locator('[data-position-price]').evaluate('(e)=>getComputedStyle(e).color')
    summary.hover();page.mouse.down();page.wait_for_timeout(180)
    check('press keeps transparent hit target and stationary row',summary.evaluate('(e)=>getComputedStyle(e).backgroundColor==="rgba(0, 0, 0, 0)"') and card.evaluate('(e)=>getComputedStyle(e).transform==="none"'))
    check('press does not recolor financial values',card.locator('[data-position-price]').evaluate('(e)=>getComputedStyle(e).color')==before)
    page.mouse.move(1,1);page.mouse.up();page.wait_for_timeout(200)
    second=page.locator('.position-card').nth(1);second.locator('.position-more>summary').click();page.wait_for_timeout(250)
    check('choosing another stock collapses the previous one',not card.locator('.position-more').evaluate('(e)=>e.open') and second.locator('.position-more').evaluate('(e)=>e.open'))
    page.evaluate("const e=document.querySelectorAll('.position-card')[1];scrollTo(0,scrollY+e.getBoundingClientRect().top-105)");page.wait_for_timeout(220)
    page.screenshot(path=str(ROOT/(engine+'-portfolio-polish.png')))
    page.emulate_media(reduced_motion='reduce');second.locator('.position-more>summary').click();second.locator('.position-more>summary').click()
    check('reduced motion removes detail animation',second.locator('.position-detail-body').evaluate('(e)=>getComputedStyle(e).animationName==="none"'))
    check('visual review has no writes or JavaScript errors',not posts and not errors)
    browser.close()
ROOT.joinpath(engine+'-polish-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors}))
