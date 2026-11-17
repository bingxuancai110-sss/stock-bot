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
watch_bars,watch_h=m['market'](70)
watch=m['st'].analyse('2308','第二檔隔離測試',m['quote'](watch_bars),watch_h,m['now'](watch_bars))
row2=watch
row['spreads']=m['st'].trade_spreads(row['signals'],row['price'],row['date'])
row2['spreads']=m['st'].trade_spreads(row2['signals'],row2['price'],row2['date'])
payload=dict(ok=True,rows=[row,row2],alerts=[],meta=dict(building=False,signals_current=True,stale=False,date=row['date'],scanned=80,
 scope=dict(universe_count=2200,markets={'上市':1100,'上櫃':1100},sectors={'電子':900,'金融':100,'傳產':1200})))
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
 check('small candidate limit and no institution gate are visible','最多 6 檔' in page.locator('.st-summary').inner_text() and '不設法人買超門檻' in page.locator('.st-summary').inner_text())
 page.clock.install();state['building']=False;page.clock.fast_forward(5100)
 page.locator('.st-card').first.wait_for()
 check('background result replaces skeleton without page reload',page.locator('.st-card').count()==2 and page.locator('.st-skeleton').count()==0)
 check('whole-market scope includes both markets and electronics','全市場初篩 2200 檔' in page.locator('.st-status').inner_text() and all(x in page.locator('.st-scope').inner_text() for x in ['上市','上櫃','電子']))
 page.locator('.st-rules summary').click()
 check('rules do not impose absolute turnover or volume minimums','成交量、成交金額不設最低門檻' in page.locator('.st-rules').inner_text())
 page.locator('.st-rules summary').click()
 check('current notification is above candidates and old entries are not announced','沒有新的進場訊號' in page.locator('.st-notices').inner_text() and page.locator('.st-notice-event').count()==0 and page.locator('.st-notices').bounding_box()['y']<page.locator('.st-cards').bounding_box()['y'])
 check('open trade spread is explicitly unrealized','未實現每股價差' in page.locator('.st-card').first.inner_text() and page.locator('.st-card').first.locator('.st-profit strong.st-up').count()==1)
 page.locator('.st-card-toggle').first.click()
 check('current chart hides old entry arrows by default',page.locator('.st-chart svg').count()==1 and page.locator('[data-marker]').count()==0 and '非新進場' in page.locator('.st-action').first.inner_text())
 page.locator('.st-history-toggle').click()
 check('old arrows require explicit history mode',page.locator('[data-marker]').count()>=2 and '歷史重播，非目前買點' in page.locator('.st-chart-context').inner_text())
 check('expanded stock quote retains neutral text and positive change stays red',page.locator('.st-quote strong').first.evaluate('(e)=>getComputedStyle(e).color===getComputedStyle(e.closest("#wb-short-term")).color') and page.locator('.st-quote .st-up').first.evaluate('(e)=>getComputedStyle(e).color==="rgb(197, 52, 59)"'))
 check('protection and profit reference are visible','支撐停損價' in page.locator('.st-inspector').inner_text() and '停利參考' in page.locator('.st-inspector').inner_text())
 check('chip data date is explicit and precedes signal date',row['chip']['date'] in page.locator('.st-chip-detail').inner_text() and row['chip']['date']<row['date'])
 marker=page.locator('[data-marker]').first;marker.click()
 check('clicking marker reveals its original reason and stop','歷史規則重播' in page.locator('.st-inspector').inner_text() and row['signals'][0]['reason'] in page.locator('.st-inspector').inner_text() and str(row['signals'][0]['stop']) in page.locator('.st-inspector').inner_text())
 page.locator('.st-current').click();page.locator('[data-range="30"]').click()
 check('return to current clears historical arrows',page.locator('[data-marker]').count()==0 and page.locator('.st-history-toggle').get_attribute('aria-pressed')=='false')
 check('period changes in place',page.locator('[data-range="30"]').get_attribute('aria-pressed')=='true')
 page.locator('[data-step="-1"]').click()
 check('earlier candle navigation enables return to latest',page.locator('[data-step="1"]').is_enabled())
 page.locator('.st-card-toggle').nth(1).click()
 check('only one stock chart is open at once',page.locator('.st-detail:visible').count()==1 and page.locator('.st-card-toggle[aria-expanded="true"]').count()==1)
 check('waiting candidate displays unmet conditions without buy markers',page.locator('.st-card[data-code="2308"] .st-checks').inner_text().find('進場型態待確認')>=0 and page.locator('.st-card[data-code="2308"] [data-marker]').count()==0 and '尚未觸發進場' in page.locator('.st-inspector').inner_text())
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
 original=payload.copy();entry_bars,entry_h=m['market'](71)
 for bar in entry_bars:
  for key in ('open','high','low','close'):bar[key]*=10
  bar['volume']=10
 entry_bars[-1]['volume']=14
 new=m['st'].analyse('2330','新進場隔離測試',m['quote'](entry_bars),[],m['now'](entry_bars))
 new.update(sector='電子',market='上櫃')
 payload.update(rows=[new],alerts=m['st'].latest_alerts([new],new['date']),meta=dict(building=False,signals_current=True,date=new['date'],scanned=80))
 page.locator('.st-refresh').click();page.locator('.st-notice-event[data-kind="entry"]').wait_for()
 check('genuine new entry appears at top with date stop and target','新進場 1 檔' in page.locator('.st-notices h3').inner_text() and new['date'] in page.locator('.st-notice-event').inner_text() and '支撐停損' in page.locator('.st-notice-event').inner_text())
 check('high-price thinly traded electronic stock can enter without institutional evidence','上櫃 電子' in page.locator('.st-date').inner_text() and '法人參考：資料待確認' in page.locator('.st-evidence').inner_text() and page.locator('.st-notice-event[data-kind="entry"]').count()==1 and 'NaN' not in page.locator('#wb-short-term').inner_text())
 payload['meta']['signals_current']=False;payload['meta']['expected_date']='2026-11-18'
 page.locator('.st-refresh').click();page.get_by_text('最新訊號尚未確認',exact=True).wait_for()
 check('stale notification never presents old entry as current',page.locator('.st-notice-event').count()==0)
 examples=[]
 for code,entry,exit in [('2330',24,36),('2308',36,24)]:
  events=[dict(id=code+'-entry',type='entry',date=row['signals'][0]['date'],price=entry),dict(id=code+'-exit',type='exit',date=row['date'],price=exit,stop=20,target=40)]
  examples.append(dict(row,code=code,active=False,signals=events,state='本次出場訊號',current_action='exit',spreads=m['st'].trade_spreads(events)))
 payload.update(rows=examples,alerts=m['st'].latest_alerts(examples,row['date']),meta=dict(building=False,signals_current=True,date=row['date'],scanned=80))
 page.locator('.st-refresh').click();page.locator('.st-card[data-code="2330"] .st-profit strong').wait_for()
 check('24 to 36 displays twelve per share in red','每股賺 NT$12.00' in page.locator('.st-card[data-code="2330"] .st-profit').inner_text() and page.locator('.st-card[data-code="2330"] .st-profit strong').evaluate(r'(e)=>{const c=getComputedStyle(e).color.match(/\d+/g).map(Number);return c[0]>c[1]&&c[0]>c[2]}'))
 check('36 to 24 displays twelve per share loss in green','每股賠 NT$12.00' in page.locator('.st-card[data-code="2308"] .st-profit').inner_text() and page.locator('.st-card[data-code="2308"] .st-profit strong').evaluate(r'(e)=>{const c=getComputedStyle(e).color.match(/\d+/g).map(Number);return c[1]>c[0]&&c[1]>c[2]}'))
 check('fresh exit notices also show per-share price spread','每股賺 NT$12.00' in page.locator('.st-notices').inner_text() and '每股賠 NT$12.00' in page.locator('.st-notices').inner_text())
 payload.clear();payload.update(original)
 check('dark notice and stock headings retain readable light text',page.locator('.st-notices h3,.st-stock h3').evaluate_all(r'(es)=>es.every(e=>Number(getComputedStyle(e).color.match(/\d+/g)[0])>180)'))
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
