"""Actual workbench and strategy engine, with clearly isolated synthetic bars."""
import json,os,sys,runpy
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

REPO=Path(__file__).resolve().parents[2];ROOT=Path(os.environ['STOCKBOT_UI_ARTIFACTS'])
sys.path.insert(0,str(REPO))
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium'
m=runpy.run_path(str(REPO/'tests/test_short_term.py'))
bars,h=m['market'](100);bars[82]['volume']=2200000
row=m['st'].analyse('2330','隔離測試股票',m['quote'](bars),h,m['now'](bars))
row2=dict(row,code='2308',name='第二檔隔離測試')
payload=dict(ok=True,rows=[row,row2],meta=dict(building=False,stale=False,date=row['date'],scanned=24))
requests=[];posts=[];errors=[];checks=[];state=dict(building=True,error=False,empty=False)
with sync_playwright() as p:
 browser=getattr(p,engine).launch(headless=True)
 page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
 page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  u=urlparse(r.request.url);requests.append(u.path)
  if r.request.method=='POST':posts.append(u.path);r.fulfill(status=403);return
  if u.path.startswith('/static/'):
   f=REPO/u.path.lstrip('/');r.fulfill(body=f.read_text(encoding='utf-8'),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
  if u.path=='/web/api/workbench/short-term':
   if state['error']:r.fulfill(status=503,json={'ok':False});return
   if state['empty']:r.fulfill(json=dict(ok=True,rows=[],meta=dict(building=False,date=row['date'],scanned=24,excluded={'過熱或波動過高':24})));return
   r.fulfill(json=dict(ok=True,rows=[],meta=dict(building=True,progress=4,pool_limit=24)) if state['building'] else payload);return
  if '/api/' in u.path or '/app-config' in u.path:
   r.fulfill(json=dict(ok=True,rows=[],meta=dict(available=True,market_open=False)));return
  r.fulfill(body=(ROOT/'workbench.html').read_text(encoding='utf-8'),content_type='text/html')
 page.route('https://fixture.test/**',route)
 def check(name,ok):
  checks.append(dict(check=name,passed=bool(ok)))
  if not ok:page.screenshot(path=str(ROOT/(engine+'-short-failure.png')))
  assert ok,name
 page.goto('https://fixture.test/web/workbench?tab=%E7%9F%AD%E7%B7%9A')
 page.locator('.st-skeleton').first.wait_for()
 check('short tab initializes directly without fetching other strategy sources','/web/api/workbench/source' not in requests)
 check('short mode hides unrelated score and sort controls',page.locator('#wb-mobile-sort').is_hidden() and page.locator('.wb-table').is_hidden())
 check('small candidate limit and no fundamental score are visible','最多 6 檔' in page.locator('.st-summary').inner_text() and '不使用基本面分數' in page.locator('.st-summary').inner_text())
 page.clock.install();state['building']=False;page.clock.fast_forward(5100)
 page.locator('.st-card').first.wait_for()
 check('background result replaces skeleton without page reload',page.locator('.st-card').count()==2 and page.locator('.st-skeleton').count()==0)
 page.locator('.st-card-toggle').first.click()
 check('card expands into actual candlesticks and markers',page.locator('.st-chart svg').count()==1 and page.locator('[data-marker]').count()>=2)
 check('expanded stock quote retains neutral text and positive change stays red',page.locator('.st-quote strong').first.evaluate('(e)=>getComputedStyle(e).color===getComputedStyle(e.closest("#wb-short-term")).color') and page.locator('.st-quote .st-up').first.evaluate('(e)=>getComputedStyle(e).color==="rgb(197, 52, 59)"'))
 check('protection and profit reference are visible','保護價' in page.locator('.st-inspector').inner_text() and '停利參考' in page.locator('.st-inspector').inner_text())
 check('chip data date is explicit and precedes signal date',row['chip']['date'] in page.locator('.st-chip-detail').inner_text() and row['chip']['date']<row['date'])
 marker=page.locator('[data-marker]').first;marker.click()
 check('clicking marker reveals its original reason and stop','歷史規則重播' in page.locator('.st-inspector').inner_text() and row['signals'][0]['reason'] in page.locator('.st-inspector').inner_text() and str(row['signals'][0]['stop']) in page.locator('.st-inspector').inner_text())
 page.locator('.st-current').click();page.locator('[data-range="30"]').click()
 check('period changes in place',page.locator('[data-range="30"]').get_attribute('aria-pressed')=='true')
 page.locator('[data-step="-1"]').click()
 check('earlier candle navigation enables return to latest',page.locator('[data-step="1"]').is_enabled())
 page.locator('.st-card-toggle').nth(1).click()
 check('only one stock chart is open at once',page.locator('.st-detail:visible').count()==1 and page.locator('.st-card-toggle[aria-expanded="true"]').count()==1)
 for width in [320,390,1024]:
  page.set_viewport_size(dict(width=width,height=844))
  check('no horizontal overflow at '+str(width),page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
  check('chart fits its card at '+str(width),page.locator('.st-chart').evaluate('(e)=>e.getBoundingClientRect().right<=e.closest(".st-card").getBoundingClientRect().right+1'))
 page.set_viewport_size(dict(width=390,height=844));page.locator('.st-card-toggle').nth(1).scroll_into_view_if_needed()
 page.locator('.st-chart').scroll_into_view_if_needed();page.wait_for_timeout(200)
 page.screenshot(path=str(ROOT/(engine+'-short-chart.png')))
 page.emulate_media(color_scheme='dark',reduced_motion='reduce')
 check('dark preference retains readable chart labels',page.locator('.st-axis').first.evaluate('(e)=>getComputedStyle(e).fill!=="rgba(0, 0, 0, 0)"'))
 check('dark chart panel uses a dark surface with light text',page.locator('.st-card').first.evaluate(r'(e)=>{const p=getComputedStyle(e).backgroundColor.match(/\d+/g).map(Number),c=getComputedStyle(e).color.match(/\d+/g).map(Number);return p[0]<60&&c[0]>180}'))
 state['empty']=True;page.locator('.st-refresh').click();page.locator('.st-excluded').wait_for()
 check('empty results explain actual exclusion reasons and retain source date','過熱或波動過高' in page.locator('.st-excluded').inner_text() and row['date'] in page.locator('.st-status').inner_text())
 state['empty']=False;page.locator('.st-refresh').click();page.locator('.st-card').first.wait_for()
 state['error']=True;page.locator('.st-refresh').click();page.get_by_text('暫時無法載入，請重試',exact=True).wait_for()
 check('refresh error retains previous candidates',page.locator('.st-card').count()==2)
 page.locator('button[data-source="雷達"]').click()
 check('switching away restores original workbench controls',page.locator('#wb-mobile-sort').is_visible() and page.locator('#wb-short-term').is_hidden())
 state['error']=False;page.locator('button[data-source="短線"]').click()
 check('returning to short mode restores cards without touching holdings',page.locator('.st-card').count()==2 and not posts)
 check('no JavaScript errors',not errors)
 browser.close()
ROOT.joinpath(engine+'-short-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(engine=engine,passed=len(checks),errors=errors)))
