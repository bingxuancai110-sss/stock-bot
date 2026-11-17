"""App-wide hierarchy, anchored scrolling and immediate navigation acceptance."""
import json,os,sys
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from playwright.sync_api import sync_playwright

REPO=Path(__file__).resolve().parents[2];ROOT=Path(os.environ['STOCKBOT_UI_ARTIFACTS'])
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium'
checks=[];errors=[];state={};posts=[]
with sync_playwright() as p:
    browser=getattr(p,engine).launch(headless=True)
    ctx=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        u=urlparse(r.request.url);q=parse_qs(u.query)
        if r.request.method=='POST':posts.append(u.path);r.fulfill(status=403);return
        if u.path.startswith('/static/'):
            f=REPO/u.path.lstrip('/');r.fulfill(body=f.read_text(encoding='utf-8'),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
        if q.get('fragment') and state.get('hold'):state['held']=r;return
        if '/api/' in u.path or u.path=='/web/quote-lab/app-config':r.fulfill(json={'ok':True,'market_open':False,'updates':[],'rows':[],'meta':{'available':False,'building':False,'deferred':True}});return
        name={'/web/positions':'positions','/web/more':'more','/web/workbench':'workbench'}.get(u.path,'positions')
        suffix='-fragment' if q.get('fragment') else ''
        r.fulfill(body=(ROOT/(name+suffix+'.html')).read_text(encoding='utf-8'),content_type='text/html')
    page.route('https://fixture.test/**',route)
    def check(name,ok):
        checks.append({'check':name,'passed':bool(ok)})
        if not ok:page.screenshot(path=str(ROOT/(engine+'-experience-failure.png')))
        assert ok,name
    page.goto('https://fixture.test/web/positions');page.wait_for_function('!!window.StockBotExperience&&!!window.StockBotUI');page.wait_for_timeout(220)
    check('mobile navigation text has a readable minimum',page.locator('.app-bottom-nav a').first.evaluate('(e)=>parseFloat(getComputedStyle(e).fontSize)>=10.5'))
    card=page.locator('.position-card').first
    check('phone stock names and quotes are visibly larger',card.locator('h3').evaluate('(e)=>parseFloat(getComputedStyle(e).fontSize)>=16') and card.locator('.position-card-price b').evaluate('(e)=>parseFloat(getComputedStyle(e).fontSize)>=17'))
    check('allocation legend uses two compact columns without losing its touch targets',page.locator('.position-allocation-legend').evaluate('(e)=>getComputedStyle(e).gridTemplateColumns.split(" ").length===2') and page.locator('.allocation-item').first.evaluate('(e)=>e.getBoundingClientRect().height>=44'))
    header=page.locator('.app-header');height=header.bounding_box()['height']
    card.scroll_into_view_if_needed();page.wait_for_timeout(220)
    check('scrolling keeps a compact header visible',header.evaluate('(e)=>getComputedStyle(e).position==="sticky"&&Math.abs(e.getBoundingClientRect().top)<1') and page.locator('body').evaluate('(e)=>e.classList.contains("app-is-scrolled")'))
    check('header compaction does not alter document geometry',abs(header.bounding_box()['height']-height)<1)
    check('sticky header has an opaque surface and contained title',header.evaluate('(e)=>{const a=e.getBoundingClientRect(),b=e.querySelector("h1").getBoundingClientRect();return getComputedStyle(e).backgroundColor!=="rgba(0, 0, 0, 0)"&&b.top>=a.top&&b.bottom<=a.bottom}'))
    check('stock names and quotes stay within their phone row',card.evaluate('(e)=>{const r=e.getBoundingClientRect();return [...e.querySelectorAll("h3,.position-card-price")].every(n=>n.getBoundingClientRect().right<=r.right+1)}'))
    page.screenshot(path=str(ROOT/(engine+'-experience-holdings.png')))
    for width in [320,360,430]:
        page.set_viewport_size({'width':width,'height':844});page.wait_for_timeout(80)
        check('no horizontal page overflow at '+str(width),page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
    page.set_viewport_size({'width':390,'height':844});page.evaluate('scrollTo(0,0)');page.wait_for_timeout(180)
    check('returning to top restores the full header',not page.locator('body').evaluate('(e)=>e.classList.contains("app-is-scrolled")'))
    state['hold']=True;old_height=page.evaluate('document.documentElement.scrollHeight')
    target=page.locator('.app-bottom-nav a[href="/web/more"]');target.click();page.wait_for_timeout(180)
    check('navigation responds before the network request finishes',target.evaluate('(e)=>e.classList.contains("experience-nav-target")') and page.locator('.experience-route-status').is_visible())
    check('destination pill moves to the requested tab',page.locator('.app-nav-cursor').evaluate('(e)=>{const a=document.querySelector(".experience-nav-target").getBoundingClientRect(),b=e.getBoundingClientRect();return Math.abs(a.left-b.left)<2&&Math.abs(a.top-b.top)<2}'))
    check('pending navigation keeps current page semantics and layout',page.locator('.app-bottom-nav a[aria-current="page"]').get_attribute('href')=='/web/positions' and page.evaluate('document.documentElement.scrollHeight')==old_height)
    check('previous navigation label stays dark when the pill moves away',page.locator('.app-bottom-nav a.on:not(.experience-nav-target)').evaluate('(e)=>{const s=getComputedStyle(e),label=getComputedStyle(e.querySelector("span")),c=label.color.match(/[0-9]+/g).map(Number);return c[0]<160&&c[1]<160&&c[2]<160&&s.backgroundColor==="rgba(0, 0, 0, 0)"&&s.borderTopColor==="rgba(0, 0, 0, 0)"}'))
    check('loading destination retains readable light text',target.evaluate('(e)=>{const c=getComputedStyle(e).color.match(/[0-9]+/g).map(Number);return c[0]>200&&c[1]>200&&c[2]>200}'))
    page.screenshot(path=str(ROOT/(engine+'-experience-navigation.png')))
    page.wait_for_function('document.body.classList.contains("app-route-pending")')
    held=state.pop('held');state['hold']=False;held.fulfill(body=(ROOT/'more-fragment.html').read_text(encoding='utf-8'),content_type='text/html')
    page.wait_for_url('**/web/more');page.wait_for_timeout(220)
    check('completed navigation removes the waiting surface',page.locator('.experience-route-status').is_hidden() and page.locator('.experience-nav-target').count()==0)
    check('completed navigation commits the active destination',page.locator('.app-bottom-nav a[aria-current="page"]').get_attribute('href')=='/web/more')
    state['hold']=True;page.locator('.app-bottom-nav a[href="/web/positions"]').click();page.wait_for_timeout(180)
    held=state.pop('held');state['hold']=False;held.fulfill(status=503,body='isolated error');page.wait_for_timeout(200)
    check('failed navigation restores the selected page and hides pending feedback',page.locator('.experience-route-status').is_hidden() and page.locator('.app-bottom-nav a[aria-current="page"]').get_attribute('href')=='/web/more' and page.locator('.ui-feedback-route[data-state="error"]').is_visible())
    page.emulate_media(reduced_motion='reduce')
    check('reduced motion stops the navigation cursor transition',page.locator('.app-nav-cursor').evaluate('(e)=>getComputedStyle(e).transitionDuration.split(",").every(v=>parseFloat(v)<=.00001)'))
    check('experience code never submits a transaction',not posts)
    check('no JavaScript errors',not errors)
    browser.close()
ROOT.joinpath(engine+'-experience-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors}))
