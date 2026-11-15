"""Actual shell styling and geometry checks; no production account traffic."""
import json, os, sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

REPO=Path(__file__).resolve().parents[2]
ROOT=Path(os.environ.get('STOCKBOT_UI_ARTIFACTS','/tmp/stockbot-ui-validation'))
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium'
checks=[];errors=[]
with sync_playwright() as p:
    browser=(p.webkit.launch(headless=True) if engine=='webkit' else p.chromium.launch(
        **({'executable_path':os.environ['STOCKBOT_CHROMIUM_EXECUTABLE']} if os.environ.get('STOCKBOT_CHROMIUM_EXECUTABLE') else {}),
        args=['--no-sandbox','--disable-dev-shm-usage']))
    context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        u=urlparse(r.request.url)
        if u.path.startswith('/static/'):
            f=REPO/u.path.lstrip('/')
            r.fulfill(body=f.read_text(encoding='utf-8'),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
        if u.path=='/web/quote-lab/app-config':r.fulfill(status=403,json={'ok':False});return
        if '/api/workbench/' in u.path:
            r.fulfill(json={'ok':True,'rows':[{'code':'2330','name':'隔離測試台積電','source':'黑馬','row_key':'fixture-2330','score':80,'total_score':80,'price':100,'change_pct':1.5,'industry':'測試產業','signal':'測試快照','detail':{}}],
                           'sources':{'黑馬':{'available':True,'date':'2026-11-04'}},'personal':{},'meta':{'market_open':False,'available':True}});return
        if '/api/' in u.path:r.fulfill(json={'ok':True,'updates':[],'market_open':False});return
        name={'/web/leaderboard':'rank','/web/workbench':'workbench'}.get(u.path,'positions')
        r.fulfill(body=(ROOT/(name+'.html')).read_text(encoding='utf-8'),content_type='text/html')
    page.route('https://fixture.test/**',route)
    def check(label,ok):
        checks.append({'check':label,'passed':bool(ok)})
        if not ok:page.screenshot(path=str(ROOT/(engine+'-scroll-presentation-failure.png')))
        assert ok,label
    page.goto('https://fixture.test/web/leaderboard');page.wait_for_function('!!window.StockBotUI')
    nav=page.locator('.app-bottom-nav')
    check('navigation uses an opaque surface without backdrop repaint',nav.evaluate('(e)=>{const s=getComputedStyle(e);return s.backdropFilter==="none"&&(!s.webkitBackdropFilter||s.webkitBackdropFilter==="none")&&!s.backgroundColor.endsWith(", 0.96)")}'))
    page.evaluate('document.getElementById("app-page-content").classList.add("app-page-enter")')
    check('list rows remain stationary and readable during navigation entry',page.locator('.rank-card:visible').evaluate_all('(es)=>es.length>0&&es.every(e=>{const s=getComputedStyle(e);return s.animationName==="none"&&s.transform==="none"&&s.opacity==="1"})'))
    check('active destination does not bounce',page.locator('.app-bottom-nav a.on').evaluate('(e)=>getComputedStyle(e).animationName==="none"'))
    panel=page.locator('.rank-chart-panel:visible').first
    check('history charts use actual geometry instead of estimated placeholder height',panel.evaluate('(e)=>{const s=getComputedStyle(e);return s.contentVisibility==="visible"&&s.containIntrinsicSize==="none"}'))
    initial=panel.bounding_box()['height'];doc_height=page.evaluate('document.documentElement.scrollHeight')
    panel.scroll_into_view_if_needed();page.wait_for_timeout(250)
    check('approaching a comparison section does not resize the document',abs(panel.bounding_box()['height']-initial)<1 and abs(page.evaluate('document.documentElement.scrollHeight')-doc_height)<2)
    page.evaluate('scrollTo(0,0)')
    check('programmatic navigation does not inherit a global smooth scroll',page.evaluate('scrollY')==0)
    page.clock.install()
    page.evaluate('showPageNavLoader("正在開啟持股",true)');page.clock.run_for(800)
    check('short loads use only the quiet top rail',page.locator('#page-nav-loader').evaluate('(e)=>e.classList.contains("show")') and not page.locator('#page-nav-progress-card').evaluate('(e)=>e.classList.contains("show")'))
    page.clock.run_for(800)
    card=page.locator('#page-nav-progress-card')
    check('slow-load status stays above navigation rather than covering the heading',card.is_visible() and card.bounding_box()['y']>400 and card.bounding_box()['y']+card.bounding_box()['height']<nav.bounding_box()['y'])
    check('slow-load feedback has no multicolor fill or background blur',card.evaluate('(e)=>getComputedStyle(e).backdropFilter==="none"') and page.locator('#page-nav-progress-time').evaluate('(e)=>getComputedStyle(e).backgroundImage==="none"'))
    check('elapsed timer does not repeatedly announce itself to screen readers',page.locator('#page-nav-progress-time').get_attribute('aria-hidden')=='true')
    page.screenshot(path=str(ROOT/(engine+'-quiet-navigation.png')))
    before_height=page.evaluate('document.documentElement.scrollHeight')
    page.evaluate('StockBotUI.routeStart("正在開啟持股","/web/positions")');page.clock.run_for(7100)
    check('long wait uses one visible status without shifting the document',page.locator('.ui-feedback-route[data-state=waiting]').is_visible() and not card.is_visible() and page.evaluate('document.documentElement.scrollHeight')==before_height)
    page.evaluate('StockBotUI.routeEnd(true)')
    page.evaluate('finishPageNavLoader()');page.clock.run_for(250)
    check('completed loading removes the status',not card.is_visible())
    check('shared shell has no horizontal page overflow',page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
    page.set_viewport_size({'width':1280,'height':900});page.goto('https://fixture.test/web/positions');page.wait_for_function('!!window.StockBotUI');page.wait_for_timeout(150)
    legend=page.locator('.position-allocation-legend')
    check('desktop allocation uses a compact two-column legend',legend.evaluate('(e)=>getComputedStyle(e).gridTemplateColumns.split(" ").length===2') and page.locator('.position-allocation-body').bounding_box()['height']<280)
    check('desktop holding metadata has a readable minimum size',page.locator('.position-card-primary small').first.evaluate('(e)=>parseFloat(getComputedStyle(e).fontSize)>=12'))
    plot=page.locator('.portfolio-trend-panel.panel-3m .portfolio-trend-plot')
    check('desktop short chart fits its frame and keeps its axis in view',plot.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1') and page.locator('.panel-3m .trend-chart-inner').bounding_box()['height']==360)
    page.locator('label[for="trend-period-all"]').click();page.wait_for_timeout(100)
    check('long-range chart retains horizontal panning',page.locator('.panel-all .portfolio-trend-plot').evaluate('(e)=>e.scrollWidth>e.clientWidth'))
    page.goto('https://fixture.test/web/workbench');page.wait_for_selector('.wb-row.wb-rich-row')
    row=page.locator('.wb-row.wb-rich-row').first
    before=row.evaluate('(e)=>({left:e.getBoundingClientRect().left,padding:getComputedStyle(e).paddingLeft})')
    row.hover();page.wait_for_timeout(200)
    check('hovering a desktop stock row does not move its contents',row.evaluate('(e,p)=>Math.abs(e.getBoundingClientRect().left-p.left)<1&&getComputedStyle(e).paddingLeft===p.padding',before))
    check('presentation checks cause no JavaScript errors',not errors)
    browser.close()
ROOT.joinpath(engine+'-scroll-presentation-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors},ensure_ascii=False))
