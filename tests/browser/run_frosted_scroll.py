"""Bounded glass surfaces and native scrolling, using isolated renderer inputs."""
from pathlib import Path
from urllib.parse import urlparse
import json,os,sys
from playwright.sync_api import sync_playwright
REPO=Path(__file__).resolve().parents[2];ROOT=Path(os.environ['STOCKBOT_UI_ARTIFACTS'])
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium';checks=[];errors=[]
with sync_playwright() as p:
    browser=getattr(p,engine).launch(headless=True)
    ctx=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        path=urlparse(r.request.url).path
        if path.startswith('/static/'):
            f=REPO/path.lstrip('/');r.fulfill(body=f.read_text(encoding='utf-8'),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
        if '/api/' in path or path=='/web/quote-lab/app-config':r.fulfill(json={'ok':True,'market_open':False,'updates':[]});return
        r.fulfill(body=(ROOT/'positions.html').read_text(encoding='utf-8'),content_type='text/html')
    page.route('https://fixture.test/**',route)
    def check(name,ok):
        checks.append({'check':name,'passed':bool(ok)})
        if not ok:page.screenshot(path=str(ROOT/(engine+'-glass-failure.png')))
        assert ok,name
    page.goto('https://fixture.test/web/positions');page.wait_for_function('!!window.StockBotExperience');page.wait_for_timeout(150)
    header=page.locator('.app-header');nav=page.locator('.app-bottom-nav')
    check('top fog is hidden at the beginning',header.evaluate('(e)=>getComputedStyle(e,"::after").opacity==="0"'))
    before=page.evaluate('document.documentElement.scrollHeight')
    if engine=='webkit':page.locator('.p2-holdings').scroll_into_view_if_needed()
    else:page.mouse.wheel(0,600)
    page.wait_for_timeout(350)
    check('native scrolling still moves the page',page.evaluate('scrollY>100'))
    check('header has a frosted backdrop while scrolled',header.evaluate('(e)=>(getComputedStyle(e).backdropFilter||getComputedStyle(e).webkitBackdropFilter).includes("blur(18px)")'))
    check('navigation has a bounded frosted backdrop',nav.evaluate('(e)=>(getComputedStyle(e).backdropFilter||getComputedStyle(e).webkitBackdropFilter).includes("blur(22px)")&&e.getBoundingClientRect().height<100'))
    check('both edge fades are visible in the middle',header.evaluate('(e)=>getComputedStyle(e,"::after").opacity==="1"') and nav.evaluate('(e)=>getComputedStyle(e,"::before").opacity==="1"'))
    check('edge masks blend into the page',header.evaluate('(e)=>{const s=getComputedStyle(e,"::after");return (s.maskImage||s.webkitMaskImage).includes("linear-gradient")&&s.backgroundImage.includes("linear-gradient")}'))
    check('fog surfaces do not intercept taps',header.evaluate('(e)=>getComputedStyle(e,"::after").pointerEvents==="none"') and nav.evaluate('(e)=>getComputedStyle(e,"::before").pointerEvents==="none"'))
    check('glass does not change page geometry',page.evaluate('document.documentElement.scrollHeight')==before)
    check('blur is not applied to the data document',page.locator('#app-page-content').evaluate('(e)=>getComputedStyle(e).filter==="none"&&(getComputedStyle(e).backdropFilter||"none")==="none"'))
    check('quote text has no blur filter',page.locator('[data-position-price]').first.evaluate('(e)=>getComputedStyle(e).filter==="none"'))
    page.screenshot(path=str(ROOT/(engine+'-frosted-scroll.png')))
    page.evaluate('scrollTo(0,document.documentElement.scrollHeight)');page.wait_for_timeout(300)
    check('lower fog fades away at the page end',nav.evaluate('(e)=>getComputedStyle(e,"::before").opacity==="0"'))
    page.evaluate('scrollTo(0,0)');page.wait_for_timeout(300)
    check('upper fog fades away after returning to top',header.evaluate('(e)=>getComputedStyle(e,"::after").opacity==="0"'))
    check('no horizontal overflow',page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
    check('no JavaScript errors',not errors)
    browser.close()
ROOT.joinpath(engine+'-glass-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors}))
