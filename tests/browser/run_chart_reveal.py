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
    page.evaluate("""()=>{
      const host=document.createElement('section');host.id='chart-test';host.style.cssText='margin-top:2400px;min-height:4500px';
      const original=document.querySelector('svg.portfolio-trend-svg');const svg=original.cloneNode(true);svg.id='reveal-portfolio';svg.removeAttribute('data-ui-chart-bound');svg.style.cssText='display:block;width:300px;height:260px';
      host.append(svg);const buttons=document.createElement('div');buttons.innerHTML='<button id="chart-period">1 month</button><p id="chart-readout" role="status">2026/11/16</p>';host.append(buttons);
      for(const cls of ['us-signal-svg','us-chart','ui-interactive-chart','sa-chart','portfolio-allocation-svg']){
        const other=document.createElementNS('http://www.w3.org/2000/svg','svg');other.setAttribute('class',cls);other.setAttribute('width','300');other.setAttribute('height','200');other.style.display='block';other.style.marginTop='100px';host.append(other);
      }
      const k=document.createElementNS('http://www.w3.org/2000/svg','svg');k.id='market-kline';k.setAttribute('width','300');k.setAttribute('height','200');host.append(k);
      const icon=document.createElementNS('http://www.w3.org/2000/svg','svg');icon.id='excluded-icon';icon.setAttribute('width','24');icon.setAttribute('height','24');host.append(icon);
      document.getElementById('app-page-content').append(host);StockBotUI.scan();
    }""")
    page.wait_for_timeout(220);chart=page.locator('#reveal-portfolio');before=page.evaluate('document.documentElement.scrollHeight')
    geometry=chart.evaluate('(e)=>({w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height,path:e.querySelector("path,polyline").getAttribute("d")||e.querySelector("path,polyline").getAttribute("points")})')
    def place(y):
      chart.evaluate('(e,y)=>scrollTo(0,scrollY+e.getBoundingClientRect().top-y)',y);page.wait_for_timeout(220)
      return chart.evaluate('(e)=>parseFloat(getComputedStyle(e).opacity)')
    low=place(724);middle=place(640)
    check('plot fades progressively with scroll rather than elapsed time',.4<=low<.5 and low<middle<.9)
    page.wait_for_timeout(350);check('stopping mid-scroll retains intermediate opacity',abs(chart.evaluate('(e)=>parseFloat(getComputedStyle(e).opacity)')-middle)<.02)
    check('plot controls and readout stay fully visible',all(page.locator('#'+id).evaluate('(e)=>getComputedStyle(e).opacity==="1"') for id in ['chart-period','chart-readout']))
    check('plots are never transformed or blurred',chart.evaluate('(e)=>getComputedStyle(e).transform==="none"&&getComputedStyle(e).filter==="none"'))
    check('plot dimensions and data are unchanged',chart.evaluate('(e)=>({w:e.getBoundingClientRect().width,h:e.getBoundingClientRect().height,path:e.querySelector("path,polyline").getAttribute("d")||e.querySelector("path,polyline").getAttribute("points")})')==geometry)
    chart.dispatch_event('pointerdown',{'pointerType':'touch','isPrimary':True,'button':0})
    check('touch immediately restores full clarity',chart.evaluate('(e)=>getComputedStyle(e).opacity==="1"&&!e.classList.contains("ui-section-arrive")'))
    place(724);check('touch-completed plot never fades again',chart.evaluate('(e)=>getComputedStyle(e).opacity==="1"'))
    other=page.locator('#chart-test svg.us-signal-svg');other.evaluate('(e)=>{e.setAttribute("tabindex","0");e.focus()}')
    check('keyboard focus immediately restores plot clarity',other.evaluate('(e)=>getComputedStyle(e).opacity==="1"&&!e.classList.contains("ui-section-arrive")'))
    check('all supported chart families are registered',page.locator('#chart-test svg.ui-section-arrive').count()==5)
    check('decorative svg icons are excluded',page.locator('#excluded-icon').evaluate('(e)=>!e.classList.contains("ui-section-arrive")&&getComputedStyle(e).opacity==="1"'))
    check('document height remains unchanged',page.evaluate('document.documentElement.scrollHeight')==before)
    page.emulate_media(reduced_motion='reduce');page.wait_for_timeout(50)
    check('reduced motion restores every chart',page.locator('#chart-test svg.ui-section-arrive').count()==0)
    check('no transactions or errors',not posts and not errors);browser.close()
ROOT.joinpath(engine+'-chart-reveal-results.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors}))
