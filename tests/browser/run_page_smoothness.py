"""Isolated actual-renderer checks; all fixture.test traffic is intercepted."""
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import json, os, sys
from playwright.sync_api import sync_playwright

REPO=Path(__file__).resolve().parents[2]
ROOT=Path(os.environ.get('STOCKBOT_UI_ARTIFACTS','/tmp/stockbot-ui-validation'))
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium'
results=[];errors=[];state={};requests=[]

with sync_playwright() as p:
    browser=(p.webkit.launch(headless=True) if engine=='webkit' else p.chromium.launch(
        **({'executable_path':os.environ['STOCKBOT_CHROMIUM_EXECUTABLE']} if os.environ.get('STOCKBOT_CHROMIUM_EXECUTABLE') else {}),
        args=['--no-sandbox','--disable-dev-shm-usage']))
    context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
    def check(label,ok):
        results.append({'check':label,'passed':bool(ok)})
        if not ok:page.screenshot(path=str(ROOT/(engine+'-smoothness-failure.png')))
        assert ok,label
    def fixture(route):
        u=urlparse(route.request.url);q=parse_qs(u.query);requests.append(u.path)
        if u.path.startswith('/static/'):
            file=REPO/u.path.lstrip('/')
            route.fulfill(body=file.read_text(encoding='utf-8'),content_type='text/css' if file.suffix=='.css' else 'application/javascript');return
        if state.get('hold') and u.path=='/web/positions' and q.get('fragment'):
            state['held']=route;return
        if u.path=='/web/quote-lab/app-config':route.fulfill(status=403,json={'ok':False});return
        if '/api/' in u.path:route.fulfill(json={'ok':True,'market_open':False,'updates':[]});return
        name={'/web/positions':'positions','/web/leaderboard':'rank','/web/more':'more'}.get(u.path,'home')
        suffix='-fragment' if q.get('fragment') else ''
        route.fulfill(body=(ROOT/(name+suffix+'.html')).read_text(encoding='utf-8'),content_type='text/html')
    page.route('https://fixture.test/**',fixture)
    def ready(path='/web/positions'):
        page.goto('https://fixture.test'+path)
        page.wait_for_function('!!window.StockBotUI');page.wait_for_timeout(150)
    ready()
    page.evaluate("""()=>{
      const scope=document.getElementById('app-page-content'),section=document.createElement('section');
      section.id='viewport-test';section.dataset.uiReveal='1';section.style.marginTop='3600px';
      section.innerHTML='<h2>延後互動初始化</h2>';
      const svg=scope.querySelector('svg.portfolio-trend-svg').cloneNode(true);
      delete svg.dataset.uiChartBound;section.append(svg);scope.append(section);
      window.nearCount=0;StockBotUI.whenNear(section,()=>window.nearCount++);StockBotUI.scan();
    }""")
    page.wait_for_timeout(150)
    check('offscreen section stays readable before its observer fires',page.locator('#viewport-test').evaluate('(e)=>getComputedStyle(e).opacity==="1"&&getComputedStyle(e).visibility==="visible"'))
    check('far chart binding and deferred job wait for approach',page.locator('#viewport-test svg').get_attribute('data-ui-chart-bound') is None and page.evaluate('nearCount')==0)
    before=len(requests)
    page.locator('#viewport-test').scroll_into_view_if_needed()
    page.wait_for_function('nearCount===1&&document.querySelector("#viewport-test svg").dataset.uiChartBound==="1"')
    check('approach initializes the existing chart without a new request',len(requests)==before)
    page.wait_for_timeout(250)
    page.evaluate('scrollTo(0,0)');page.locator('#viewport-test').scroll_into_view_if_needed();page.wait_for_timeout(100)
    check('observer callback and arrival animation do not repeat',page.evaluate('nearCount')==1 and not page.locator('#viewport-test').evaluate('(e)=>e.classList.contains("ui-section-arrive")'))
    # An old-page queued callback must never run after a successful route change.
    page.evaluate("""()=>{const e=document.createElement('section');e.id='cancel-near';e.style.marginTop='5000px';e.textContent='離頁工作';document.getElementById('app-page-content').append(e);window.cancelCount=0;StockBotUI.whenNear(e,()=>cancelCount++);} """)
    page.locator('.app-bottom-nav a[href="/web/leaderboard"]').click();page.wait_for_url('**/web/leaderboard');page.wait_for_timeout(150)
    check('queued offscreen work is discarded on departure',page.evaluate('cancelCount')==0)

    ready()
    page.evaluate("""()=>{const r=document.getElementById('trend-period-1w');r.checked=true;r.dispatchEvent(new Event('change',{bubbles:true}));document.getElementById('trend-benchmark').checked=false;scrollTo({top:1000,behavior:'instant'});} """)
    page.wait_for_timeout(100)
    saved=page.evaluate('scrollY')
    page.locator('.app-bottom-nav a[href="/web/leaderboard"]').click();page.wait_for_url('**/web/leaderboard')
    page.locator('#rankTabs button[data-board="long"]').click()
    state['hold']=True
    page.go_back()
    for _ in range(60):
        if 'held' in state:break
        page.wait_for_timeout(25)
    check('back navigation waits on the actual delayed fragment','held' in state)
    page.wait_for_timeout(600)
    check('old page stays usable while the new fragment is pending',page.locator('#rankTabs').count()==1)
    state.pop('held').fulfill(body=(ROOT/'positions-fragment.html').read_text(encoding='utf-8'),content_type='text/html');state['hold']=False
    page.wait_for_selector('.position-card');page.wait_for_timeout(150)
    actual=page.evaluate('scrollY')
    if abs(actual-saved)>=3:print({'saved_scroll':saved,'restored_scroll':actual},flush=True)
    check('back restores scroll after loading rather than an 80ms timer',abs(actual-saved)<3)
    check('chart period and benchmark visibility return together',page.locator('#trend-period-1w').is_checked() and not page.locator('#trend-benchmark').is_checked())
    page.locator('.app-bottom-nav a[href="/web/leaderboard"]').click();page.wait_for_url('**/web/leaderboard');page.wait_for_timeout(150)
    check('revisiting leaderboard restores its selected comparison',page.locator('#rankTabs button[data-board="long"]').get_attribute('aria-pressed')=='true')

    page.locator('.app-bottom-nav a[href="/web/portfolio"]').click();page.wait_for_selector('#hv2');page.wait_for_timeout(100)
    first_home_url=page.url
    page.evaluate("scrollTo({top:500,behavior:'instant'})");home_scroll=page.evaluate('scrollY')
    page.locator('.app-bottom-nav a[href="/web/leaderboard"]').click();page.wait_for_selector('#rankTabs')
    page.locator('.app-bottom-nav a[href="/web/portfolio"]').click();page.wait_for_selector('#hv2');page.wait_for_timeout(100)
    check('home timestamp changes without resetting its remembered position',page.url!=first_home_url and abs(page.evaluate('scrollY')-home_scroll)<3)

    ready()
    chart=page.locator('svg.portfolio-trend-svg:visible').first
    chart.scroll_into_view_if_needed();page.wait_for_timeout(150)
    label=chart.get_attribute('aria-label')
    page.evaluate('StockBotUI.cleanup();StockBotUI.scan()');page.wait_for_timeout(150)
    check('chart reinitialization does not duplicate guides or accessibility labels',chart.locator('.ui-chart-crosshair').count()==1 and chart.get_attribute('aria-label')==label)
    page.emulate_media(reduced_motion='reduce')
    page.evaluate("""()=>{const e=document.createElement('section');e.id='reduced-section';e.dataset.uiReveal='1';e.style.marginTop='4000px';e.textContent='減少動態效果';document.getElementById('app-page-content').append(e);StockBotUI.scan();}""")
    page.wait_for_timeout(80);page.locator('#reduced-section').scroll_into_view_if_needed()
    check('reduced motion leaves arriving content static and readable',page.locator('#reduced-section').evaluate('(e)=>getComputedStyle(e).animationName==="none"&&getComputedStyle(e).opacity==="1"'))
    page.evaluate("""()=>{window.IntersectionObserver=undefined;const e=document.createElement('div');document.getElementById('app-page-content').append(e);window.fallbackCount=0;StockBotUI.whenNear(e,()=>fallbackCount++);} """)
    check('older browsers initialize directly without IntersectionObserver',page.evaluate('fallbackCount')==1)
    check('all actual-renderer interactions have no runtime errors',not errors)
    browser.close()

ROOT.joinpath(engine+'-smoothness-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(results),'errors':errors},ensure_ascii=False))
