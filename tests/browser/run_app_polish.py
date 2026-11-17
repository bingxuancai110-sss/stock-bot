"""Each primary page, settings and both market views fit mobile and desktop."""
from pathlib import Path
from urllib.parse import urlparse,parse_qs
import os,sys,json
from playwright.sync_api import sync_playwright
from us_rotation_payload import populated_payload
ROOT=Path(os.environ['STOCKBOT_UI_ARTIFACTS']);REPO=Path(__file__).resolve().parents[2]
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium';checks=[];errors=[];posts=[]
with sync_playwright() as p:
    browser=getattr(p,engine).launch(headless=True)
    page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        u=urlparse(r.request.url);q=parse_qs(u.query)
        if r.request.method=='POST':posts.append(u.path);r.fulfill(status=403);return
        if u.path.startswith('/static/'):
            f=REPO/u.path.lstrip('/');r.fulfill(body=f.read_text(encoding='utf-8'),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
        if u.path=='/web/api/us-rotation':r.fulfill(json=populated_payload(REPO));return
        if '/api/workbench/' in u.path:r.fulfill(json={'ok':True,'rows':[{'code':'2330','name':'隔離測試公司','price':100,'change_pct':1.2,'score':80,'source':'黑馬','detail':{}}],'sources':{'黑馬':{'available':True,'date':'2026-11-04'}},'meta':{'available':True,'market_open':False},'personal':{}});return
        if '/api/' in u.path or u.path=='/web/quote-lab/app-config':r.fulfill(json={'ok':True,'market_open':False,'updates':[],'events':[]});return
        name={'/web/portfolio':'home','/web/positions':'positions','/web/market':'market' if q.get('code') else 'market-board','/web/workbench':'workbench','/web/leaderboard':'rank','/web/us-rotation':'us_rotation','/web/more':'more','/web/settings':'settings'}.get(u.path,'home')
        r.fulfill(body=(ROOT/(name+'.html')).read_text(encoding='utf-8'),content_type='text/html')
    page.route('https://fixture.test/**',route)
    def check(name,ok):
        checks.append({'check':name,'passed':bool(ok)})
        if not ok:page.screenshot(path=str(ROOT/(engine+'-app-polish-failure.png')))
        assert ok,name
    cases=[('home','/web/portfolio','#hv2'),('positions','/web/positions','.p2-holdings'),('market-board','/web/market','.market-quote-tile'),('market','/web/market?code=2330','#market-kline'),('workbench','/web/workbench','#wb-tabs button'),('rank','/web/leaderboard','#rankTabs'),('us','/web/us-rotation','.us-market-switch'),('more','/web/more','.more-item'),('settings','/web/settings','form')]
    for name,path,selector in cases:
        page.goto('https://fixture.test'+path);page.wait_for_selector(selector);page.wait_for_timeout(220)
        for width in (320,390,1280):
            page.set_viewport_size({'width':width,'height':844});page.wait_for_timeout(80)
            check(f'{name} fits viewport {width}',page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
        page.set_viewport_size({'width':390,'height':844});page.evaluate('scrollTo(0,0)');page.wait_for_timeout(100)
        check(f'{name} retains all primary destinations',page.locator('.app-bottom-nav a').count()==7)
        if name=='market-board':
            check('quote board is a framed surface',page.locator('.market-chip-grid').evaluate('(e)=>getComputedStyle(e).borderRadius==="18px"'))
            check('quote-board search and theme switch align without wasted header space',page.evaluate('()=>{const a=document.querySelector(".market-search").getBoundingClientRect(),b=document.querySelector("#market-theme-toggle").getBoundingClientRect();return Math.abs((a.top+a.bottom-b.top-b.bottom)/2)<2&&a.right<=b.left&&a.height>=44&&b.height>=44}'))
        if name=='market':
            check('chart controls meet touch height',page.locator('.market-ranges button,.market-draw-tools button').evaluate_all('(es)=>es.every(e=>e.getBoundingClientRect().height>=44)'))
            page.locator('#market-theme-toggle').click();check('market dark surface stays dark',page.locator('.market-chart-card').evaluate('(e)=>{let c=getComputedStyle(e).backgroundColor.match(/[0-9]+/g).map(Number);return c.slice(0,3).every(v=>v<80)}'))
            check('dark market period rail retains contrast',page.locator('.market-ranges').evaluate('(e)=>{let bg=getComputedStyle(e).backgroundColor.match(/[0-9]+/g).map(Number),text=getComputedStyle(e.querySelector("button:not(.active)")).color.match(/[0-9]+/g).map(Number);return bg.slice(0,3).every(v=>v<100)&&text.slice(0,3).every(v=>v>150)}'))
            page.locator('#market-theme-toggle').click()
        if name=='workbench':
            check('screener separates selected source with an underline',page.locator('#wb-tabs button.on').evaluate('(e)=>parseFloat(getComputedStyle(e).borderBottomWidth)>=3'))
            check('screener selected area and sort use dark readable controls',page.locator('#wb-asset-tabs .on,#wb-mobile-sort .on').evaluate_all('(es)=>es.length>=2&&es.every(e=>{let s=getComputedStyle(e),b=s.backgroundColor.match(/[0-9]+/g).map(Number),c=s.webkitTextFillColor.match(/[0-9]+/g).map(Number);return b[0]<100&&c[0]>200})'))
        if name=='us':
            check('US market switch fills its panel',page.locator('.us-market-switch').evaluate('(e)=>{const r=e.getBoundingClientRect(),b=Array.from(e.children).map(x=>x.getBoundingClientRect());return b.length===2&&b.every(x=>x.height>=44)&&b[0].right<=b[1].left&&b[1].right<=r.right}'))
            check('risk and confirmation colors retain semantic classes',page.locator('.us-alert-card.signal-on').count()>0 and page.locator('.us-alert-card.confirm-on').count()>0)
        if name in ('rank','us'):
            active=page.locator('#rankTabs .on' if name=='rank' else '.us-market-switch [aria-pressed=true]')
            check(f'{name} selected text contrasts with dark background',active.evaluate('(e)=>{let s=getComputedStyle(e),b=s.backgroundColor.match(/[0-9]+/g).map(Number),c=s.webkitTextFillColor.match(/[0-9]+/g).map(Number);return b[0]<100&&c[0]>200&&c[1]>200&&c[2]>200}'))
        page.screenshot(path=str(ROOT/(engine+'-app-'+name+'.png')))
    page.emulate_media(reduced_motion='reduce')
    check('reduced motion removes added control transitions',page.locator('button').first.evaluate('(e)=>getComputedStyle(e).transitionDuration==="0s"'))
    check('all-page visual review submits no writes and has no script errors',not posts and not errors)
    browser.close()
ROOT.joinpath(engine+'-app-polish-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors}))
