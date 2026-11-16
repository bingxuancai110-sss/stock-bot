"""First-appearance text reveal, without hidden data or moving chart containers."""
from pathlib import Path
from urllib.parse import urlparse
import os,json,sys
from playwright.sync_api import sync_playwright
ROOT=Path(os.environ['STOCKBOT_UI_ARTIFACTS']);REPO=Path(__file__).resolve().parents[2]
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium';checks=[];errors=[];posts=[]
with sync_playwright() as p:
    browser=getattr(p,engine).launch(headless=True)
    ctx=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        path=urlparse(r.request.url).path
        if r.request.method=='POST':posts.append(path);r.fulfill(status=403);return
        if path.startswith('/static/'):
            f=REPO/path.lstrip('/');r.fulfill(body=f.read_text(encoding='utf-8'),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
        if '/api/' in path or path=='/web/quote-lab/app-config':r.fulfill(json={'ok':True,'market_open':False,'updates':[]});return
        r.fulfill(body=(ROOT/'positions.html').read_text(encoding='utf-8'),content_type='text/html')
    page.route('https://fixture.test/**',route)
    def check(name,ok):
        checks.append({'check':name,'passed':bool(ok)});assert ok,name
    page.goto('https://fixture.test/web/positions');page.wait_for_function('!!window.StockBotUI');page.wait_for_timeout(250)
    check('first viewport has no hidden headline',page.locator('h2').first.evaluate('(e)=>getComputedStyle(e).opacity==="1"&&getComputedStyle(e).visibility==="visible"'))
    page.evaluate("""()=>{
      const section=document.createElement('section');section.id='reveal-fixture';section.dataset.uiReveal='1';section.style.cssText='margin-top:2200px;padding:20px;min-height:2200px';
      section.innerHTML='<h2 id="reveal-heading">Scroll text</h2><span id="reveal-quote" data-position-price>123.45</span><button id="reveal-buy">Buy</button><svg id="reveal-chart" width="240" height="100"></svg><div aria-live="polite"><button type="button" id="reveal-stock-row"><span class="wb-row-main" id="reveal-row-text">Company</span><b data-position-price>140.00</b></button></div><div aria-live="assertive"><h2 id="reveal-alert">Alert</h2></div><div role="status"><h2 id="reveal-status">Status</h2></div>';
      document.getElementById('app-page-content').append(section);StockBotUI.scan();
    }""")
    heading=page.locator('#reveal-heading');section=page.locator('#reveal-fixture');page.wait_for_timeout(220)
    doc_height=page.evaluate('document.documentElement.scrollHeight')
    def place(y):
        heading.evaluate('(e,y)=>scrollTo(0,scrollY+e.getBoundingClientRect().top-y)',y);page.wait_for_timeout(220)
        return heading.evaluate('(e)=>parseFloat(getComputedStyle(e).opacity)')
    faint=place(724);middle=place(640);clear=place(530)
    check('text starts faint at the bottom of the readable viewport',.05<faint<.2)
    check('opacity increases gradually with scroll position',faint<middle<.9 and clear==1)
    check('completed text is unfiltered and stationary',heading.evaluate('(e)=>!e.classList.contains("ui-section-arrive")&&getComputedStyle(e).transform==="none"&&getComputedStyle(e).filter==="none"'))
    check('financial quote stays fully readable',page.locator('#reveal-quote').evaluate('(e)=>getComputedStyle(e).opacity==="1"&&getComputedStyle(e).transform==="none"'))
    check('buy button remains enabled and unanimated',page.locator('#reveal-buy').is_enabled() and page.locator('#reveal-buy').evaluate('(e)=>getComputedStyle(e).opacity==="1"&&getComputedStyle(e).transform==="none"'))
    check('chart container stays stationary',section.evaluate('(e)=>getComputedStyle(e).transform==="none"&&!e.classList.contains("ui-section-arrive")'))
    check('screener text in polite regions is included',page.locator('#reveal-row-text').evaluate('(e)=>e.classList.contains("ui-section-arrive")'))
    check('assertive alerts and status stay immediate',all(page.locator('#'+id).evaluate('(e)=>getComputedStyle(e).opacity==="1"&&!e.classList.contains("ui-section-arrive")') for id in ['reveal-alert','reveal-status']))
    check('reveal does not change document height',page.evaluate('document.documentElement.scrollHeight')==doc_height)
    place(724)
    check('scrolling back does not fade completed text again',heading.evaluate('(e)=>getComputedStyle(e).opacity==="1"&&!e.classList.contains("ui-section-arrive")'))
    heading.evaluate('(e)=>e.textContent="Updated heading"');page.wait_for_timeout(220)
    check('text refresh does not replay the reveal',heading.evaluate('(e)=>getComputedStyle(e).opacity==="1"&&!e.classList.contains("ui-section-arrive")'))
    page.emulate_media(reduced_motion='reduce');page.wait_for_timeout(50)
    check('reduced motion clears all pending fades',page.locator('.ui-section-arrive').count()==0)
    check('reduced motion restores screener text',page.locator('#reveal-row-text').evaluate('(e)=>getComputedStyle(e).opacity==="1"'))
    nojs=browser.new_context(java_script_enabled=False,viewport={'width':390,'height':844});np=nojs.new_page();np.route('https://fixture.test/**',route);np.goto('https://fixture.test/web/positions')
    check('headings stay readable when JavaScript is unavailable',np.locator('h2').first.evaluate('(e)=>getComputedStyle(e).opacity==="1"'))
    nojs.close();check('reveal tests never submit transactions',not posts);check('no JavaScript errors',not errors)
    browser.close()
ROOT.joinpath(engine+'-text-reveal-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors}))
