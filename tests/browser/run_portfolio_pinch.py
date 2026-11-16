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
    svg=page.locator('.portfolio-trend-panel:visible svg.portfolio-trend-svg');svg.scroll_into_view_if_needed();page.wait_for_timeout(100)
    plot=page.locator('.portfolio-trend-panel:visible .portfolio-trend-plot');inner=page.locator('.portfolio-trend-panel:visible .trend-chart-inner')
    baseline=inner.evaluate('(e)=>e.getBoundingClientRect().width');height=svg.evaluate('(e)=>e.getBoundingClientRect().height');data=svg.get_attribute('viewBox');path=svg.locator('.port-line').get_attribute('d');page_height=page.evaluate('document.documentElement.scrollHeight')
    check('one-finger panning remains enabled without native page pinch',svg.evaluate('(e)=>getComputedStyle(e).touchAction==="pan-x pan-y"'))
    check('reset control is initially hidden',page.locator('.portfolio-trend-panel:visible .ui-portfolio-zoom-reset').is_hidden())
    box=plot.bounding_box();mid=box['x']+box['width']/2;y=max(100,min(650,box['y']+100));anchor=(plot.evaluate('(e)=>e.scrollLeft')+box['width']/2)/baseline
    if engine=='chromium':cdp=ctx.new_cdp_session(page)
    def pinch(start,end):
      if engine=='chromium':
        def pts(d):return [dict(id=1,x=mid-d/2,y=y),dict(id=2,x=mid+d/2,y=y)]
        cdp.send('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=pts(start)))
        for i in range(1,11):
          cdp.send('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=pts(start+(end-start)*i/10)));page.wait_for_timeout(18)
        cdp.send('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]))
      else:
        plot.evaluate("""(e,p)=>{function send(type,d){const touches=type==='touchend'?[]:[{identifier:1,target:e,clientX:p.mid-d/2,clientY:p.y},{identifier:2,target:e,clientX:p.mid+d/2,clientY:p.y}];const ev=new Event(type,{bubbles:true,cancelable:true});Object.defineProperty(ev,'touches',{value:touches});e.dispatchEvent(ev)}send('touchstart',p.start);send('touchmove',p.end);send('touchend',0)}""",dict(mid=mid,y=y,start=start,end=end))
      page.wait_for_timeout(100)
    pinch(90,210);zoomed=inner.evaluate('(e)=>e.getBoundingClientRect().width')
    check('two-finger spread zooms the timeline',zoomed>baseline*1.8)
    after_anchor=(plot.evaluate('(e)=>e.scrollLeft')+box['width']/2)/zoomed
    check('zoom remains anchored beneath the finger midpoint',abs(after_anchor-anchor)<.02)
    check('plot zoom does not zoom or widen the page',page.evaluate('visualViewport.scale===1&&document.documentElement.scrollWidth<=innerWidth+1'))
    check('pinch preserves return scale, path data and chart height',svg.get_attribute('viewBox')==data and svg.locator('.port-line').get_attribute('d')==path and svg.evaluate('(e)=>e.getBoundingClientRect().height')==height)
    check('reset control is available after zoom',page.locator('.portfolio-trend-panel:visible .ui-portfolio-zoom-reset').is_visible())
    pinch(210,95);smaller=inner.evaluate('(e)=>e.getBoundingClientRect().width')
    check('two-finger contraction zooms back out',smaller<zoomed*.7 and smaller>=plot.evaluate('(e)=>e.clientWidth'))
    svg.focus();page.keyboard.press('+');page.wait_for_timeout(100)
    check('keyboard zoom alternative works',inner.evaluate('(e)=>e.getBoundingClientRect().width')>smaller+10)
    page.keyboard.press('0');page.wait_for_timeout(100)
    check('reset restores original responsive width and hides its control',abs(inner.evaluate('(e)=>e.getBoundingClientRect().width')-baseline)<1 and page.locator('.portfolio-trend-panel:visible .ui-portfolio-zoom-reset').is_hidden())
    # Cancelling a partial pinch must not leave tracking blocked.
    plot.evaluate("""e=>{for(const type of ['touchstart','touchcancel']){const ev=new Event(type,{bubbles:true,cancelable:true});Object.defineProperty(ev,'touches',{value:type==='touchstart'?[{clientX:120,clientY:300},{clientX:220,clientY:300}]:[]});e.dispatchEvent(ev)}}""")
    svg.dispatch_event('pointerdown',dict(pointerType='touch',pointerId=21,clientX=mid,clientY=y));page.wait_for_timeout(70)
    check('cancelled pinch releases one-finger date tracking',not page.locator('.portfolio-trend-panel:visible .ui-chart-values').is_hidden())
    if engine=='chromium':
      box=plot.bounding_box();xx=box['x']+box['width']/2;yy=max(180,min(650,box['y']+180));old_scroll=page.evaluate('scrollY')
      cdp.send('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(id=1,x=xx,y=yy)]))
      for i in range(1,11):
        cdp.send('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(id=1,x=xx,y=yy-i*12)]));page.wait_for_timeout(18)
      cdp.send('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]));page.wait_for_timeout(100)
      check('one finger still scrolls the page vertically',page.evaluate('scrollY')>old_scroll+40)
      cdp.detach()
    # Periods retain independent widths, including the long scrollable history.
    page.locator('label[for=trend-period-1y]').click();page.wait_for_timeout(80)
    check('other periods keep their original width rules',page.locator('.portfolio-trend-panel.panel-1y .trend-chart-inner').evaluate('(e)=>e.style.getPropertyPriority("width")!=="important"'))
    page.locator('label[for=trend-period-3m]').click();page.wait_for_timeout(80)
    check('returning to the reset period keeps its default width',abs(inner.evaluate('(e)=>e.getBoundingClientRect().width')-baseline)<1)
    cleaned=page.evaluate('()=>{StockBotUI.cleanup();return document.querySelectorAll(".ui-portfolio-zoom-reset").length===0&&document.querySelector(".portfolio-trend-panel.panel-3m .trend-chart-inner").style.getPropertyPriority("width")!=="important"}')
    check('cleanup removes zoom controls and restores inline styles',cleaned)
    check('no JavaScript errors or transaction writes',not errors and not posts);browser.close()
ROOT.joinpath(engine+'-pinch-results.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors}))
