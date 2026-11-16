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
    page.evaluate('''()=>{
      const section=document.createElement('section');section.id='reveal-fixture';section.dataset.uiReveal='1';section.style.cssText='margin-top:2200px;padding:20px;min-height:400px';
      section.innerHTML='<h2 id="reveal-heading">滑入後的文字</h2><span id="reveal-quote" data-position-price>123.45</span><button id="reveal-buy">買進</button><svg id="reveal-chart" width="240" height="100"><path d="M0 70 L240 20" stroke="blue"/></svg><button type="button" id="reveal-stock-row"><span class="wb-row-main" id="reveal-row-text">公司名稱與說明</span><b data-position-price>140.00</b></button>';
      document.getElementById('app-page-content').append(section);window.revealStarts=[];
      document.addEventListener('animationstart',e=>{if(e.animationName==='app-text-arrive')window.revealStarts.push(e.target.id)});StockBotUI.scan();
    }''')
    heading=page.locator('#reveal-heading');section=page.locator('#reveal-fixture');page.wait_for_timeout(80)
    check('offscreen text remains visible without a waiting opacity state',heading.evaluate('(e)=>getComputedStyle(e).opacity==="1"&&!e.classList.contains("ui-section-arrive")'))
    doc_height=page.evaluate('document.documentElement.scrollHeight')
    heading.evaluate('(e)=>e.scrollIntoView({block:"center"})')
    page.wait_for_function('window.revealStarts.includes("reveal-heading")')
    check('heading starts a first-appearance reveal','reveal-heading' in page.evaluate('revealStarts'))
    check('animation uses the approved duration and small translation',heading.evaluate('''e=>{const a=e.getAnimations().find(a=>a.animationName==='app-text-arrive');return !!a&&a.effect.getTiming().duration===200&&a.effect.getKeyframes()[0].transform==='translateY(5px)'}'''))
    check('financial quote stays fully readable',page.locator('#reveal-quote').evaluate('(e)=>getComputedStyle(e).opacity==="1"&&getComputedStyle(e).transform==="none"'))
    check('buy button remains enabled and unanimated',page.locator('#reveal-buy').is_enabled() and page.locator('#reveal-buy').evaluate('(e)=>getComputedStyle(e).opacity==="1"&&getComputedStyle(e).transform==="none"'))
    check('chart container stays stationary',section.evaluate('(e)=>getComputedStyle(e).transform==="none"&&!e.classList.contains("ui-section-arrive")') and page.locator('#reveal-chart').evaluate('(e)=>getComputedStyle(e).transform==="none"'))
    page.locator('#reveal-row-text').evaluate('(e)=>e.scrollIntoView({block:"center"})');page.wait_for_function('revealStarts.includes("reveal-row-text")')
    check('screener text reveals without moving its button or quote',page.locator('#reveal-stock-row').evaluate('(e)=>getComputedStyle(e).transform==="none"&&!e.classList.contains("ui-section-arrive")&&getComputedStyle(e.querySelector("[data-position-price]")).opacity==="1"'))
    page.wait_for_timeout(260)
    check('reveal cleans up its class and restores normal text',heading.evaluate('(e)=>!e.classList.contains("ui-section-arrive")&&getComputedStyle(e).opacity==="1"&&getComputedStyle(e).transform==="none"'))
    check('reveal does not change document height',page.evaluate('document.documentElement.scrollHeight')==doc_height)
    page.evaluate('scrollTo(0,0)');page.wait_for_timeout(80);heading.evaluate('(e)=>e.scrollIntoView({block:"center"})');page.wait_for_timeout(260)
    check('scrolling back does not replay the heading',page.evaluate('revealStarts.filter(id=>id==="reveal-heading").length')==1)
    heading.evaluate('(e)=>e.textContent="更新後的標題"');page.wait_for_timeout(80)
    check('text refresh does not replay the reveal',page.evaluate('revealStarts.filter(id=>id==="reveal-heading").length')==1)
    page.emulate_media(reduced_motion='reduce')
    page.evaluate('''()=>{const h=document.createElement('h2');h.id='reduced-heading';h.textContent='減少動態效果';h.style.marginTop='1600px';document.getElementById('app-page-content').append(h);StockBotUI.scan()}''');page.wait_for_timeout(50)
    page.locator('#reduced-heading').evaluate('(e)=>e.scrollIntoView({block:"center"})');page.wait_for_timeout(80)
    check('reduced motion shows text immediately with no reveal',page.locator('#reduced-heading').evaluate('(e)=>getComputedStyle(e).opacity==="1"&&!e.classList.contains("ui-section-arrive")') and 'reduced-heading' not in page.evaluate('revealStarts'))
    nojs=browser.new_context(java_script_enabled=False,viewport={'width':390,'height':844});np=nojs.new_page();np.route('https://fixture.test/**',route);np.goto('https://fixture.test/web/positions')
    check('headings stay readable when JavaScript is unavailable',np.locator('h2').first.evaluate('(e)=>getComputedStyle(e).opacity==="1"'))
    nojs.close();check('reveal tests never submit transactions',not posts);check('no JavaScript errors',not errors)
    browser.close()
ROOT.joinpath(engine+'-text-reveal-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors}))
