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
        r.fulfill(body=(ROOT/'rank.html').read_text(encoding='utf-8'),content_type='text/html')
    page.route('https://fixture.test/**',route)
    def check(name,ok):
        checks.append({'check':name,'passed':bool(ok)});assert ok,name
    page.goto('https://fixture.test/web/leaderboard');page.wait_for_function('!!window.StockBotUI');page.wait_for_timeout(200)
    svg=page.locator('.rank-chart-panel:visible svg.ui-interactive-chart').first;svg.scroll_into_view_if_needed();page.wait_for_timeout(120)
    svg.evaluate("""svg=>{const path=svg.querySelector('[data-ui-points]');const n=4000,w=svg.viewBox.baseVal.width;path.dataset.uiPoints=JSON.stringify(Array.from({length:n},(_,i)=>({x:i*w/(n-1),y:50,date:'fixture-'+i,name:'Performance fixture',value:i/100})));window.performanceFixturePath=path;const parse=JSON.parse;window.chartParseCount=0;JSON.parse=function(text,...args){if(text===path.dataset.uiPoints)chartParseCount++;return parse.call(this,text,...args)}}""")
    page.wait_for_timeout(60);box=svg.bounding_box();last=''
    for i in range(30):
      svg.dispatch_event('pointermove',dict(pointerType='mouse',clientX=box['x']+box['width']*(.1+.8*i/29),clientY=box['y']+60));page.wait_for_timeout(22)
    parses=page.evaluate('chartParseCount');readout=page.locator('.rank-chart-panel:visible .ui-chart-values').first
    check('tracking still selects the correct late-series date','fixture-35' in readout.inner_text() or 'fixture-36' in readout.inner_text())
    if '--baseline' not in sys.argv:check('tracking reuses parsed series across animation frames',parses<=2)
    page.evaluate("performanceFixturePath.dataset.uiPoints=JSON.stringify([{x:0,y:50,date:'updated-date',name:'Updated fixture',value:7}])");page.wait_for_timeout(40)
    svg.dispatch_event('pointermove',dict(pointerType='mouse',clientX=box['x']+1,clientY=box['y']+60));page.wait_for_timeout(50)
    check('changed source data invalidates the tracking cache','updated-date' in readout.inner_text())
    page.evaluate("performanceFixturePath.style.display='none'");page.wait_for_timeout(40)
    svg.dispatch_event('pointermove',dict(pointerType='mouse',clientX=box['x']+1,clientY=box['y']+60));page.wait_for_timeout(50)
    check('hidden series disappear from tracking','Updated fixture' not in readout.inner_text())
    # Repeated readiness events must not replay an already visible hero.
    page.evaluate("""()=>{const e=document.createElement('section');e.className='rank-situation';e.textContent='Fixture hero';document.getElementById('app-page-content').prepend(e);const animate=e.animate.bind(e);window.heroAnimations=0;e.animate=(...args)=>{heroAnimations++;return animate(...args)};for(let i=0;i<6;i++)document.dispatchEvent(new Event('stockbot:pagecontentready'))}""");page.wait_for_timeout(60)
    animations=page.evaluate('heroAnimations')
    if '--baseline' not in sys.argv:check('repeated readiness events do not stack hero animations',animations<=1)
    check('no transaction writes or script errors',not posts and not errors)
    browser.close()
ROOT.joinpath(engine+'-interaction-performance'+('-baseline' if '--baseline' in sys.argv else '')+'.json').write_text(json.dumps({'checks':checks,'series_parses_for_30_moves':parses,'repeated_hero_animations':animations},indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'series_parses_for_30_moves':parses,'repeated_hero_animations':animations,'errors':errors}))
