"""Real shared scripts, isolated renderer inputs; never sends production writes."""
import json, os, sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

REPO=Path(__file__).resolve().parents[2]
ROOT=Path(os.environ['STOCKBOT_UI_ARTIFACTS'])
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium'
checks=[];errors=[];posts=[]
with sync_playwright() as p:
    browser=getattr(p,engine).launch(headless=True)
    context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        path=urlparse(r.request.url).path
        if r.request.method=='POST':posts.append(path);r.fulfill(status=403);return
        if path.startswith('/static/'):
            f=REPO/path.lstrip('/');r.fulfill(body=f.read_text(encoding='utf-8'),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
        if '/api/' in path or path=='/web/quote-lab/app-config':r.fulfill(json={'ok':True,'market_open':False,'updates':[]});return
        r.fulfill(body=(ROOT/'positions.html').read_text(encoding='utf-8'),content_type='text/html')
    page.route('https://fixture.test/**',route)
    def check(name,value):
        checks.append({'check':name,'passed':bool(value)})
        if not value:page.screenshot(path=str(ROOT/(engine+'-interaction-failure.png')))
        assert value,name
    page.goto('https://fixture.test/web/positions');page.wait_for_function('!!window.StockBotUI')
    card=page.locator('.position-card[data-position-code="1001"]');summary=card.locator('.position-more>summary')
    summary.scroll_into_view_if_needed();page.wait_for_timeout(300)
    box=summary.bounding_box();x=box['x']+box['width']/2;y=box['y']+15
    def pointer(kind,pid=9,dx=0,dy=0):
        summary.dispatch_event(kind,{'pointerType':'touch','pointerId':pid,'isPrimary':True,'clientX':x+dx,'clientY':y+dy,'button':0})
    before=card.evaluate('(e)=>({w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height,price:getComputedStyle(e.querySelector("[data-position-price]")).color})')
    pointer('pointerdown');check('press gives immediate whole-row feedback','ui-pressed' in card.get_attribute('class'))
    page.wait_for_timeout(240);check('held contact keeps its feedback until release','ui-pressed' in card.get_attribute('class'))
    check('feedback leaves name and quote unobscured',summary.evaluate('(e)=>getComputedStyle(e).backgroundColor==="rgba(0, 0, 0, 0)"') and card.locator('h3').is_visible())
    check('feedback does not scale a quote row or recolor its price',card.evaluate('(e,p)=>getComputedStyle(e).transform==="none"&&Math.abs(e.getBoundingClientRect().width-p.w)<.5&&Math.abs(e.getBoundingClientRect().height-p.h)<.5&&getComputedStyle(e.querySelector("[data-position-price]")).color===p.price',before))
    pointer('pointermove',dx=2,dy=24);check('swiping cancels the pressed surface immediately','ui-pressed' not in card.get_attribute('class'))
    pointer('pointerup');check('swipe does not open holding details',not card.locator('.position-more').evaluate('(e)=>e.open'))
    pointer('pointerdown');pointer('pointercancel');check('browser gesture cancellation clears feedback','ui-pressed' not in card.get_attribute('class'))
    pointer('pointerdown');pointer('pointerup');page.wait_for_timeout(25);pointer('pointerdown',pid=10);page.wait_for_timeout(130)
    check('old release timer cannot erase a later press','ui-pressed' in card.get_attribute('class'))
    pointer('pointercancel',pid=10)
    pointer('pointerdown');page.evaluate('window.scrollBy(0,30)');page.wait_for_timeout(40)
    check('native page scrolling clears any active press','ui-pressed' not in card.get_attribute('class'));pointer('pointerup')
    summary.click();page.wait_for_timeout(260)
    check('a normal tap still opens holding details',card.locator('.position-more').evaluate('(e)=>e.open'))
    price=card.locator('[data-position-price]');price.scroll_into_view_if_needed();page.wait_for_timeout(250)
    page.evaluate('''()=>{const root=document.getElementById('app-page-content');const original=root.querySelectorAll;window.fluidQueries=[];root.querySelectorAll=function(selector){window.fluidQueries.push(selector);return original.call(this,selector)}}''')
    price.evaluate('(e)=>e.textContent="101.25"');page.wait_for_timeout(60)
    check('quote update renders its real value immediately',price.inner_text()=='101.25')
    # The existing privacy masker still checks its sensitive values. Shared UI
    # feedback must not additionally walk every control/disclosure/chart.
    check('quote text updates do not rescan all controls and disclosures',page.evaluate('window.fluidQueries.every(s=>s==="[data-home-sensitive]"||s==="[data-home-privacy-card]")'))
    page.wait_for_timeout(450);check('number overlays clean up completely',page.locator('.ui-number-tween,.ui-number-source').count()==0)
    card.evaluate('''e=>e.insertAdjacentHTML('beforeend','<details id="fluid-detail"><summary>互動測試</summary><p>資料一</p></details>')''');page.wait_for_timeout(80)
    detail=page.locator('#fluid-detail');check('newly inserted controls are still initialized','ui-control' in detail.locator('summary').get_attribute('class'))
    detail.locator('summary').click();page.wait_for_timeout(280);detail.locator('p').evaluate('(e)=>e.textContent="資料更新後不重播整張卡片的高度動畫"');page.wait_for_timeout(40)
    check('async content update does not restart a height animation',not detail.evaluate('(e)=>document.getAnimations().some(a=>a.effect&&a.effect.target===e)'))
    page.emulate_media(reduced_motion='reduce');detail.locator('summary').click()
    check('reduced motion retains the disclosure result without animation',not detail.evaluate('(e)=>e.open||document.getAnimations().some(a=>a.effect&&a.effect.target===e)'))
    pointer('pointerdown');check('reduced motion disables pressed surface transitions',card.evaluate('(e)=>getComputedStyle(e).transitionDuration.split(",").every(t=>parseFloat(t)===0)'));pointer('pointercancel')
    check('feedback tests never submit transactions',not posts)
    check('no JavaScript errors',not errors)
    browser.close()
ROOT.joinpath(engine+'-interaction-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors}))
