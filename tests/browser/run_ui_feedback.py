from playwright.sync_api import sync_playwright
from pathlib import Path
from urllib.parse import urlparse,parse_qs
import json,sys,time,os
from us_rotation_payload import populated_payload
REPO=Path(__file__).resolve().parents[2];ROOT=Path(os.environ.get('STOCKBOT_UI_ARTIFACTS','/tmp/stockbot-ui-validation'))
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium';results=[];errors=[];requests=[]
state=dict(fail_nav=False,delay_nav=False,post='success',post_count=0,us_populated=False)
def check(name,ok,detail=None):
 results.append(dict(check=name,passed=bool(ok),detail=detail))
 ROOT.joinpath(engine+'-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
 if not ok:page.screenshot(path=str(ROOT/(engine+'-failure.png')))
 assert ok,(name,detail)
with sync_playwright() as p:
 browser=p.webkit.launch(headless=True) if engine=='webkit' else p.chromium.launch(**({'executable_path':os.environ['STOCKBOT_CHROMIUM_EXECUTABLE']} if os.environ.get('STOCKBOT_CHROMIUM_EXECUTABLE') else {}),args=['--no-sandbox','--disable-dev-shm-usage'])
 context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
 page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
 def fulfill(r):
  u=urlparse(r.request.url);query=parse_qs(u.query);requests.append((r.request.method,u.path,query))
  if u.path.startswith('/static/'):
   file=REPO/u.path.lstrip('/');r.fulfill(body=file.read_text(),content_type='text/css' if file.suffix=='.css' else 'application/javascript');return
  # Fixture accounts have no personal quote credentials. Return the real denied
  # contract instead of HTML, which otherwise causes retries during navigation.
  if u.path=='/web/quote-lab/app-config':r.fulfill(status=403,json=dict(ok=False));return
  if '/api/workbench/strategy-lab' in u.path:r.fulfill(json=dict(ok=True,active_strategy='四因子複合',strategy_library=[dict(key=k,available=True,status_label='可運作') for k in ['四因子複合','營收動能','價格動能']],strategy_data={},factors=[],reference={}));return
  if u.path=='/web/api/us-rotation':r.fulfill(json=populated_payload(REPO) if state['us_populated'] else dict(snapshot=None,tw_research={},history=[],supply_chain=[],industry_guides={},tw_companies=[],risk_profiles=[],stop_profiles=[],signal_explanations={},signal_trends={}));return
  if u.path=='/web/market/drawings':r.fulfill(json=dict(ok=True,id='fixture-drawing-'+str(state.get('drawing_count',0)+1),drawings=[]));state['drawing_count']=state.get('drawing_count',0)+1;return
  if u.path=='/web/api/positions/history':r.fulfill(json=dict(ok=True,events=[dict(date='2026-10-30',code='2330',name='台積電',type='sell',label='賣出',shares=10,price=1500,amount=15000,realized_pl=1200)]));return
  if '/api/workbench/' in u.path:r.fulfill(json=dict(ok=True,rows=[dict(code='2330',name='隔離測試台積電',source='黑馬',row_key='fixture-2330',score=80,total_score=80,price=100,change_pct=1.5,industry='測試產業',signal='測試快照',detail={})],sources={'黑馬':dict(available=True,date='2026-11-04')},personal={},meta={'market_open':False,'available':True}));return
  if '/api/' in u.path:r.fulfill(json=dict(market_open=False,updates=[],note='測試收盤快照'));return
  if u.path=='/web/position-trend':r.fulfill(body='<div>走勢載入測試</div>');return
  if r.request.method=='POST':
   state['post_count']+=1
   if state['post']=='hold':state['held_post']=r;return
   if state['post']=='error':r.fulfill(status=500,body='測試失敗');return
   if state['post']=='success':r.fulfill(body=ROOT.joinpath('trade-success.html').read_text(encoding='utf-8'),content_type='text/html');return
   html=ROOT.joinpath('positions.html').read_text()
   msg='請填入正確的代號、股數與成本價。' if state['post']=='invalid' else '已新增測試持股。'
   html=html.replace('<article class="position-card"','<div class="msg">'+msg+'</div><article class="position-card"',1)
   r.fulfill(body=html,content_type='text/html');return
  mapping={'/web/positions':'positions','/web/leaderboard':'rank','/web/settings':'settings','/web/more':'more','/shell':'loading','/web/portfolio':'home','/web/workbench':'workbench','/web/us-rotation':'us_rotation','/web/market':'market'}
  name=mapping.get(u.path,'settings');name='positions' if u.path=='/shell' and query.get('fragment') else name;suffix='-fragment' if query.get('fragment') else ''
  if query.get('fragment') and state['delay_nav']:state['held']=r;return
  if query.get('fragment') and state['fail_nav']:r.fulfill(status=503,body='測試失敗');return
  r.fulfill(body=ROOT.joinpath(name+suffix+'.html').read_text(),content_type='text/html')
 page.route('https://fixture.test/**',fulfill)
 def held_route():
  for _ in range(100):
   if 'held' in state:return state.pop('held')
   page.wait_for_timeout(10)
  raise AssertionError(('held navigation did not arrive',requests[-6:]))
 def ready(path):
  page.bring_to_front()
  page.emulate_media(reduced_motion='no-preference')
  page.goto('https://fixture.test'+path);page.wait_for_function('!!window.StockBotUI');page.wait_for_timeout(100)
 def css(loc,prop):return loc.evaluate('(e,p)=>getComputedStyle(e)[p]',prop)
 def wait_trade_preview():
  try:page.wait_for_function('document.querySelector("[data-p2-trade-total]")?.textContent==="10,005 元"')
  except Exception:
   page.screenshot(path=str(ROOT/(engine+'-trade-preview-failure.png')))
   print(page.locator('form.add').evaluate('(e)=>({inputs:[...e.querySelectorAll("input")].map(x=>({name:x.name,value:x.value})),preview:e.querySelector(".p2-trade-preview")?.innerText})'),flush=True)
   raise
 ready('/web/positions')
 check('actual holdings initialize without JS parse errors',not errors,errors)
 check('shared runtime only loaded once',page.locator('script[src*="ui-feedback.js"]').count()==1)
 check('authenticated shell retains its account settings entry',page.locator('.app-header .is-account').get_attribute('href')=='/web/settings')
 check('mobile app bar exposes all seven destinations',page.locator('.app-bottom-nav a').count()==7 and [x.inner_text().strip() for x in page.locator('.app-bottom-nav a').all()]==['今日','看盤','持股','選股','排行榜','美股輪動','更多'])
 check('all seven destinations share one bottom bar',page.locator('.bottom-inner').evaluate('(e)=>getComputedStyle(e).gridTemplateColumns.split(" ").length===7 && e.scrollHeight<90'))
 check('holdings use a readable single-column brokerage list',page.locator('.rows').evaluate('(e)=>getComputedStyle(e).display==="block"'))
 check('holdings read as a continuous brokerage list',page.locator('.position-card').first.evaluate('(e)=>getComputedStyle(e).borderRadius==="0px"&&getComputedStyle(e).boxShadow==="none"'))
 check('mobile asset summary has one full-width headline and full-width P/L rows',page.evaluate('(()=>{const t=document.querySelector("#portfolio-v2 .totals");if(!t)return false;const s=getComputedStyle(t),width=t.getBoundingClientRect().width-parseFloat(s.borderLeftWidth)-parseFloat(s.borderRightWidth)-parseFloat(s.paddingLeft)-parseFloat(s.paddingRight);return Array.from(t.children).slice(0,3).every(e=>Math.abs(e.getBoundingClientRect().width-width)<1)})()'))
 check('supporting asset metrics use a compact three-column row',page.evaluate('(()=>{const t=document.querySelector("#portfolio-v2 .totals"),w=[...t.children].slice(3).map(e=>e.getBoundingClientRect().width);return w.length===3&&w.every(x=>x>0&&x<t.getBoundingClientRect().width/2)})()'))
 check('asset summary fields never overlap',page.locator('#portfolio-v2 .totals').evaluate('(e)=>{const rs=[...e.children].map(x=>x.getBoundingClientRect());return rs.every((a,i)=>rs.every((b,j)=>i===j||Math.min(a.right,b.right)-Math.max(a.left,b.left)<1||Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)<1))}'))
 check('privacy return value contains plain text instead of HTML',page.locator('.totals [data-home-original]').evaluate_all('(es)=>es.every(e=>!e.dataset.homeOriginal.includes("<"))'))
 # Privacy must survive donut selection/reset, not only hide the asset headline.
 page.locator('[data-home-privacy-toggle]').click()
 check('privacy masks holdings and initial allocation values',page.locator('.position-card [data-home-sensitive],.position-allocation [data-home-sensitive]').evaluate_all('(es)=>es.length>10&&es.every(e=>e.textContent==="******")'))
 page.locator('[data-allocation-select]').first.click()
 check('selecting allocation cannot expose private amounts',page.locator('[data-allocation-center-value],.allocation-selection [data-home-sensitive]').evaluate_all('(es)=>es.length===2&&es.every(e=>e.textContent==="******")'))
 page.locator('[data-allocation-reset]').click()
 check('resetting allocation preserves privacy',page.locator('[data-allocation-center-value]').inner_text()=='******')
 page.locator('[data-home-privacy-toggle]').click()
 check('revealing allocation restores its latest value',page.locator('[data-allocation-center-value]').inner_text()!='******' and '元' in page.locator('[data-allocation-center-value]').inner_text())
 check('holding summary numbers stay on one line',page.locator('.position-card-primary b').evaluate_all('(es)=>es.every(e=>getComputedStyle(e).whiteSpace==="nowrap")'))
 check('seven current holding rows render',page.locator('.position-card').count()==7)
 check('unrealized P&L and weight remain in each expandable row',page.locator('.position-card-primary [data-position-net]').count()==7 and page.locator('.p2-weight-track').count()==7)
 check('phone holdings collapse to readable quote rows',page.locator('.position-card').first.evaluate('(e)=>{const p=e.querySelector(".position-card-primary"),t=e.querySelector(".position-card-top"),r=e.querySelector(".p2-list-return");return getComputedStyle(p).display==="none"&&t.getBoundingClientRect().height>=64&&t.getBoundingClientRect().height<90&&!!r&&getComputedStyle(r).display!=="none"}'))
 page.screenshot(path=str(ROOT/(engine+'-positions-top-390.png')))
 page.locator('.p2-holdings').scroll_into_view_if_needed();page.screenshot(path=str(ROOT/(engine+'-positions-list-390.png')))
 page.locator('.position-card').first.locator('.position-more > summary').click()
 check('expanded holding reveals complete figures',page.locator('.position-card').first.locator('.position-card-primary').is_visible() and page.locator('.position-card').first.locator('.p2-weight-track').is_visible())
 live_code=page.locator('.position-card').first.get_attribute('data-position-code')
 page.evaluate('(code)=>document.dispatchEvent(new CustomEvent("stockbot:fuglequotes",{detail:{positions:{market_open:false,updates:[{code,price:100,pct:-1,day_pl:-10,value:10000,net_pl:-250,net_pct:-1.5,weight:12.5}]}}}))',live_code)
 check('quote updates synchronize list and expanded return values',page.locator('.position-card').first.locator('[data-position-net-pct]').evaluate_all('(es)=>es.length===3&&es.every(e=>e.textContent==="-1.50%"&&e.classList.contains("down"))'))
 check('quote updates synchronize every displayed value and weight',page.locator('.position-card').first.locator('[data-position-value]').evaluate_all('(es)=>es.length===2&&es.every(e=>e.textContent==="10,000")') and page.locator('.position-card').first.locator('[data-position-weight]').evaluate_all('(es)=>es.length===2&&es.every(e=>e.textContent==="12.5%")'))
 check('expanded position exposes buy and sell actions',page.locator('.position-card').first.locator('[data-p2-buy]').is_visible() and page.locator('.position-card').first.locator('[data-p2-sell]').is_visible())
 page.locator('.position-card').first.locator('[data-p2-sell]').click();check('sell action opens the existing FIFO sell panel',page.locator('.position-card').first.locator('.sellbox-all').evaluate('(e)=>e.open'))
 page.wait_for_function('document.activeElement?.name === "sell_shares"')
 check('sell action focuses quantity without submitting',page.locator('.position-card').first.locator('input[name="sell_shares"]').evaluate('(e)=>document.activeElement===e') and state['post_count']==0)
 selected_code=page.locator('.position-card').first.locator('[data-p2-buy]').get_attribute('data-p2-buy')
 page.locator('.position-card').first.locator('[data-p2-buy]').click();page.wait_for_function('document.activeElement?.id === "p2-trade-shares"');check('holding buy action opens the sheet with the selected ticker',page.locator('#p2-trade-dialog').evaluate('(e)=>e.open') and page.locator('#p2-trade-code').input_value()==selected_code and page.locator('#p2-trade-shares').evaluate('(e)=>document.activeElement===e'))
 page.locator('[data-p2-close]').click();check('transaction sheet closes without posting',not page.locator('#p2-trade-dialog').evaluate('(e)=>e.open') and state['post_count']==0)
 check('portfolio performance compares to weighted index','加權指數' in page.locator('#portfolio-performance-section').inner_text() and '0050' not in page.locator('#portfolio-performance-section').inner_text())
 check('performance chart has one unambiguous interaction root',page.locator('#portfolio-trend').count()==1)
 check('portfolio return chart and allocation donut sit above holdings',page.evaluate('(()=>{const t=document.querySelector(".portfolio-trend-card"),d=document.querySelector(".position-allocation"),h=document.querySelector(".p2-holdings");return !!(t&&d&&h&&(t.compareDocumentPosition(d)&Node.DOCUMENT_POSITION_FOLLOWING)&&(d.compareDocumentPosition(h)&Node.DOCUMENT_POSITION_FOLLOWING))})()'))
 page.locator('[data-p2-sort]').select_option('name')
 check('sorting is local and remembered',page.locator('.position-card').first.get_attribute('data-sort-name')=='主動統一台股增長' and page.evaluate('localStorage.getItem("stockbot_portfolio_sort_v2")==="name"'))
 page.locator('[data-p2-view="history"]').click();check('history tab switches without navigation',page.locator('[data-p2-panel="history"]').is_visible() and page.locator('[data-p2-panel="current"]').is_hidden())
 page.locator('[data-p2-view="activity"]').click();page.wait_for_selector('.p2-event[data-type="sell"]')
 check('trade timeline loads only when opened',any(path=='/web/api/positions/history' for method,path,q in requests))
 page.locator('[data-p2-filter="buy"]').click();check('timeline filter selects buy events',page.locator('[data-p2-filter="buy"]').get_attribute('aria-pressed')=='true')
 check('timeline filter hides other event types',page.locator('.p2-event[data-type="sell"]').is_hidden())
 page.locator('[data-p2-filter="all"]').click();page.locator('[data-p2-search]').fill('2330');check('trade history searches by ticker',page.locator('.p2-event[data-type="sell"]').is_visible())
 page.locator('[data-p2-search]').fill('not-a-ticker');check('trade history search filters non-matching rows',page.locator('.p2-event[data-type="sell"]').is_hidden())
 page.locator('[data-p2-view="current"]').click()
 page.set_viewport_size({'width':1280,'height':900})
 check('desktop switches to sidebar navigation and three holding columns',css(page.locator('.top-nav'),'position')=='fixed' and css(page.locator('.app-bottom-nav'),'display')=='none' and page.locator('.rows').evaluate('(e)=>getComputedStyle(e).gridTemplateColumns.split(" ").length===3'))
 page.set_viewport_size({'width':390,'height':844})
 card=page.locator('.position-card[data-position-code="1001"]');summary=card.locator('.position-more>summary')
 summary.click();check('details respond immediately',card.locator('.position-more').get_attribute('open') is not None)
 check('shared height animation starts',page.evaluate('document.getAnimations().some(a=>a.effect&&a.effect.target.classList.contains("ui-disclosure"))'))
 summary.click();summary.click();page.wait_for_timeout(260);check('rapid toggle ends in correct state',card.locator('.position-more').get_attribute('open') is not None)
 check('chevron tracks aria-expanded',summary.get_attribute('aria-expanded')=='true')
 check('shared disclosures suppress the duplicate native marker',summary.evaluate('(e)=>getComputedStyle(e).listStyleType==="none"'))
 # Nested research disclosures must not write height during ResizeObserver delivery.
 card.evaluate('(e,markup)=>e.insertAdjacentHTML("beforeend",markup)', '<details id="nested-research-check"><summary>歷史表現</summary><details><summary>統計定義</summary><p>分母與限制</p></details></details>')
 page.wait_for_timeout(100)
 nested=page.locator('#nested-research-check');nested.locator('summary').first.click();page.wait_for_timeout(260)
 child=nested.locator('details');child.locator('summary').click();page.wait_for_timeout(300)
 check('nested disclosures both remain open',nested.evaluate('(e)=>e.open') and child.evaluate('(e)=>e.open'))
 child.locator('summary').click();page.wait_for_timeout(300)
 check('child collapses independently',nested.evaluate('(e)=>e.open') and not child.evaluate('(e)=>e.open'))
 check('nested height changes have no observer errors',not errors,errors)
 nested.evaluate('(e)=>e.remove()')
 # Chart and sorting remain local; only the opened journal tab requests its data.
 check('journal is the only added data request',sum(1 for method,path,q in requests if path=='/web/api/positions/history')==1 and not any(path not in ['/web/api/positions/quotes','/web/api/positions/history'] for method,path,q in requests if '/api/' in path))
 price=card.locator('[data-position-price]');price.scroll_into_view_if_needed();old=price.inner_text()
 price.evaluate('(e)=>e.textContent="101.25"')
 page.wait_for_function('document.querySelectorAll(".ui-number-tween").length>0')
 check('real numeric value updates before tween ends',price.inner_text()=='101.25')
 check('number overlay is aria-hidden',page.locator('.ui-number-tween').first.get_attribute('aria-hidden')=='true')
 page.wait_for_timeout(450);check('number tween cleans up',page.locator('.ui-number-tween').count()==0 and 'ui-number-source' not in (price.get_attribute('class') or ''))
 price.evaluate('(e)=>e.textContent="101.25"');page.wait_for_timeout(50)
 check('equal value does not retrigger tween',page.locator('.ui-number-tween').count()==0)
 page.emulate_media(reduced_motion='reduce');price.evaluate('(e)=>e.textContent="102.25"');page.wait_for_timeout(50)
 check('reduced motion skips numeric animation',price.inner_text()=='102.25' and page.locator('.ui-number-tween').count()==0);page.emulate_media(reduced_motion='no-preference')
 # Existing point dialogs still work, with new in-place hover/touch and keyboard tracking.
 svg=page.locator('.portfolio-trend-panel:visible svg.portfolio-trend-svg');svg.scroll_into_view_if_needed()
 plot=page.locator('.portfolio-trend-panel:visible .portfolio-trend-plot')
 check('portfolio touch allows native horizontal and vertical gestures',svg.evaluate('(e)=>getComputedStyle(e).touchAction==="auto"'))
 plot.evaluate('(e)=>e.scrollLeft=e.scrollWidth');page.wait_for_timeout(80);before_scroll=plot.evaluate('(e)=>e.scrollLeft');pb=plot.bounding_box();sx=pb['x']+pb['width']*.25;ex=pb['x']+pb['width']*.75;yy=pb['y']+min(100,pb['height']/2)
 if engine=='chromium':
  cdp=context.new_cdp_session(page);cdp.send('Input.dispatchTouchEvent',dict(type='touchStart',touchPoints=[dict(x=sx,y=yy)]))
  for i in range(1,11):cdp.send('Input.dispatchTouchEvent',dict(type='touchMove',touchPoints=[dict(x=sx+(ex-sx)*i/10,y=yy)]));page.wait_for_timeout(16)
  cdp.send('Input.dispatchTouchEvent',dict(type='touchEnd',touchPoints=[]));cdp.detach()
 else:
  page.mouse.move(sx,yy);page.mouse.down();page.mouse.move(ex,yy,steps=10);page.mouse.up()
 page.wait_for_timeout(150)
 check('native touch swipe scrolls portfolio history' if engine=='chromium' else 'mouse drag scrolls portfolio history',before_scroll>20 and plot.evaluate('(e)=>e.scrollLeft')<before_scroll-20,dict(before=before_scroll,after=plot.evaluate('(e)=>e.scrollLeft')))
 readout=page.locator('.portfolio-trend-panel:visible .ui-chart-readout');check('chart needs no tracking mode switch',readout.locator('button').count()==0)
 box=svg.bounding_box();pointer_x=page.viewport_size['width']/2;pointer_y=max(30,min(page.viewport_size['height']-100,box['y']+120));page.mouse.move(pointer_x,pointer_y);page.wait_for_timeout(70)
 svg.dispatch_event('pointerdown',dict(pointerType='touch',pointerId=7,clientX=pointer_x,clientY=pointer_y));page.wait_for_timeout(40)
 check('touch chart readout includes cached values','組合' in readout.inner_text() and '加權' in readout.inner_text())
 svg.dispatch_event('pointercancel',dict(pointerType='touch',pointerId=7));check('vertical scrolling clears touch readout',readout.locator('.ui-chart-values').is_hidden())
 page.mouse.move(pointer_x+10,pointer_y);page.wait_for_timeout(70)
 check('crosshair and data point highlight',svg.locator('.ui-chart-dot').evaluate('(e)=>getComputedStyle(e).display')!='none',dict(chart_box=box,viewport=page.viewport_size))
 svg.focus();before=readout.inner_text();page.keyboard.press('ArrowRight');check('keyboard tracks next cached day',readout.inner_text()!=before)
 check('tracking control is below the portfolio plot',readout.bounding_box()['y']>=svg.bounding_box()['y']+svg.bounding_box()['height']-2)
 check('tracking toolbar has no frame or reserved height',readout.evaluate('(e)=>{let s=getComputedStyle(e);return s.borderTopWidth==="0px"&&s.backgroundColor==="rgba(0, 0, 0, 0)"&&e.getBoundingClientRect().height===0}'))

 period=page.locator('label[for="trend-period-1y"]');period.click();page.wait_for_timeout(30)
 check('period uses existing cached panels',page.locator('.portfolio-trend-panel.panel-1y').is_visible())
 # Form confirmation + one request; backend validation 200 must not be shown as success.
 page.locator('[data-p2-add]').click();form=page.locator('form.add');check('top action opens an accessible transaction sheet',page.locator('#p2-trade-dialog').evaluate('(e)=>e.open') and form.locator('label[for="p2-trade-code"]').count()==1);form.locator('[name=code]').fill('2330');form.locator('[name=shares]').fill('100');form.locator('[name=cost]').fill('100');form.locator('[name=buy_fee]').fill('5');wait_trade_preview();check('trade preview computes gross and total without posting',form.locator('[data-p2-trade-gross]').inner_text()=='10,000 元' and form.locator('[data-p2-trade-total]').inner_text()=='10,005 元' and state['post_count']==0)
 state['post']='invalid';form.locator('button[type=submit]').click();page.wait_for_selector('.position-confirm-submit');page.locator('.position-confirm-submit').click()
 page.wait_for_selector('form.add .ui-form-status[data-state=error]')
 check('HTTP 200 validation failure is error state','請填入正確' in form.locator('.ui-form-status').inner_text())
 check('failed form retains user inputs',form.locator('[name=code]').input_value()=='2330')
 check('no success overlay on validation failure',page.locator('.position-success-overlay').count()==0)
 count=state['post_count'];state['post']='success';form.locator('button[type=submit]').click();page.locator('.position-confirm-submit').click();page.wait_for_selector('.p2-trade-result.success')
 check('success sends one write',state['post_count']==count+1)
 check('actual lightweight success response is accepted without legacy msg','已新增' in page.locator('.p2-trade-result.success').inner_text() and page.locator('form.add .ui-form-status[data-state=error]').count()==0)
 # SPA route keeps the current page visible and uses the shared top progress line.
 state['delay_nav']=True
 page.evaluate('window.stockBotSwitchPage("/web/leaderboard",true)')
 check('route does not insert a blocking skeleton card',page.locator('.ui-feedback-route').count()==0)
 check('route keeps previous page visible and interactive',page.locator('#app-page-content').evaluate('(e)=>getComputedStyle(e).opacity==="1"&&getComputedStyle(e).pointerEvents!=="none"') and page.locator('.p2-trade-result.success').is_visible(),page.locator('#app-page-content').evaluate('(e)=>({opacity:getComputedStyle(e).opacity,pointerEvents:getComputedStyle(e).pointerEvents,classes:e.className})'))
 check('navigation immediately acknowledges leaderboard selection',page.locator('.app-bottom-nav a[href="/web/leaderboard"]').evaluate('(e)=>e.classList.contains("ui-nav-pending")'))
 state['delay_nav']=False;held_route().fulfill(body=ROOT.joinpath('rank-fragment.html').read_text(),content_type='text/html')
 page.wait_for_selector('#rankTabs');page.wait_for_timeout(150)
 check('route remains free of a blocking card after success',page.locator('.ui-feedback-route').count()==0)
 check('selected destination follows the loaded page',page.locator('.app-bottom-nav a[href="/web/leaderboard"]').get_attribute('aria-current')=='page')
 check('page-specific styling follows SPA navigation',page.locator('body').get_attribute('data-app-view')=='leaderboard')
 count=len(requests);page.locator('#rankTabs [data-board=long]').click();check('period selection applies immediately',page.locator('[data-board-panel=long]').is_visible())
 check('selected period updates the comparison chart without navigation',page.locator('[data-rank-chart=long]').is_visible() and page.locator('[data-rank-chart=short]').is_hidden())
 check('real rank delta retained','↑ 2 名' in page.locator('[data-board-panel=long] .rank-move').first.inner_text())
 check('rank period visible feedback','已切換' in page.locator('.ui-rank-feedback').inner_text())
 check('rank period triggers no data fetch',not any(path.startswith('/web/') and path!='/web/quote-lab/app-config' for method,path,q in requests[count:]),requests[count:])
 rank_panel=page.locator('.rank-chart-panel:visible').first
 member_toggle=rank_panel.locator('.leaderboard-chart-toggle').first;member_line=rank_panel.locator('.leaderboard-chart-series').first
 member_toggle.click();check('ranking member name hides its own comparison line',member_toggle.get_attribute('aria-pressed')=='false' and member_line.evaluate('(e)=>getComputedStyle(e).display==="none"'))
 member_toggle.click();check('ranking member name restores its comparison line',member_toggle.get_attribute('aria-pressed')=='true' and member_line.evaluate('(e)=>getComputedStyle(e).display!=="none"'))
 # Check the settled selected color after the brief press/release feedback.
 page.wait_for_timeout(220)
 check('rank selected button has a light background',page.locator('#rankTabs [data-board=long]').evaluate('(e)=>{let c=getComputedStyle(e).backgroundColor.match(/[0-9]+/g).map(Number);return c[0]>200&&c[1]>210&&c[2]>200}'))
 history=page.locator('#historyTabs');history.scroll_into_view_if_needed();page.wait_for_timeout(80);check('history initially selected month is light',history.locator('[data-history=months]').evaluate('(e)=>{let c=getComputedStyle(e).backgroundColor.match(/[0-9]+/g).map(Number);return c[0]>200&&c[1]>210&&c[2]>200}'))
 history.locator('[data-history=seasons]').click();page.wait_for_timeout(40)
 check('history switches actual panel and aria state',page.locator('[data-history-panel=seasons]').is_visible() and history.locator('[data-history=seasons]').get_attribute('aria-pressed')=='true' and history.locator('[data-history=months]').get_attribute('aria-pressed')=='false')
 check('selected history text and WebKit fill remain dark',history.locator('[data-history=seasons]').evaluate('(e)=>getComputedStyle(e).color==="rgb(36, 78, 105)"&&getComputedStyle(e).webkitTextFillColor==="rgb(36, 78, 105)"'))
 check('selected history tab stays light after mouseup',history.locator('[data-history=seasons]').evaluate('(e)=>{let c=getComputedStyle(e).backgroundColor.match(/[0-9]+/g).map(Number);return c[0]>200&&c[1]>210&&c[2]>200}'))
 history.locator('[data-history=seasons]').hover();page.mouse.down()
 check('pressed history tab never becomes dark blue',history.locator('[data-history=seasons]').evaluate('(e)=>{let c=getComputedStyle(e).backgroundColor.match(/[0-9]+/g).map(Number);return c[0]>200&&c[1]>210&&c[2]>200}'));page.mouse.up()

 rank_svg=rank_panel.locator('svg.ui-interactive-chart');rank_svg.scroll_into_view_if_needed();rank_svg.focus();page.keyboard.press('ArrowRight')
 check('leaderboard chart tracks cached series','測試會員' in rank_panel.locator('.ui-chart-readout').inner_text())
 check('ranking tracking toolbar follows the plot',rank_panel.evaluate('(e)=>{const readout=e.querySelector(".ui-chart-readout").getBoundingClientRect(),plot=e.querySelector("svg.ui-interactive-chart").getBoundingClientRect();return readout.top>=plot.bottom-2}'))

 for width in [360,375,390,430,1280]:
  page.set_viewport_size({'width':width,'height':900});page.evaluate('scrollTo(0,0)')
  check('rank no horizontal overflow '+str(width),page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
  page.screenshot(path=str(ROOT/(engine+'-rank-'+str(width)+'.png')))
 # Preserve old content and selected nav if a route fails.
 state['fail_nav']=True;page.evaluate('window.stockBotSwitchPage("/web/positions",true)');page.wait_for_selector('.ui-feedback-route[data-state=error]');state['fail_nav']=False
 check('failed route retains current leaderboard',page.locator('#rankTabs').count()==1)
 check('failed route clears pending state',page.locator('.ui-nav-pending').count()==0 and page.locator('.ui-route-pending').count()==0)
 # Return and verify confirmation does not multiply on repeated fragments.
 page.evaluate('window.stockBotSwitchPage("/web/positions",true)');page.wait_for_selector('form.add',state='attached');page.wait_for_timeout(100)
 check('shared runtime reused after route reentry',page.locator('script[src*="ui-feedback.js"]').count()==1)
 page.locator('[data-p2-add]').click();form=page.locator('form.add');form.locator('[name=code]').fill('2330');form.locator('[name=shares]').fill('100');form.locator('[name=cost]').fill('100');form.locator('button[type=submit]').click()
 check('one confirmation dialog after route reentry',page.locator('#position-confirm-backdrop').count()==1);page.locator('.position-confirm-cancel').click()
 check('cancel confirmation returns to filled trade sheet',page.locator('#p2-trade-dialog').evaluate('(e)=>e.open') and form.locator('[name=shares]').input_value()=='100')
 page.locator('[data-p2-close]').click()
 for width in [360,390,430,1280]:
  page.set_viewport_size({'width':width,'height':900});page.evaluate('scrollTo(0,0)');check('holdings no overflow '+str(width),page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'));page.locator('.rows:has(.position-card)').screenshot(path=str(ROOT/(engine+'-holdings-cards-'+str(width)+'.png')));page.screenshot(path=str(ROOT/(engine+'-positions-'+str(width)+'.png')))
 held=page.locator('[data-position-code="00981A"] .position-held-days');check('ETF holding days stay together',held.evaluate('(e)=>e.getBoundingClientRect().height<=parseFloat(getComputedStyle(e).lineHeight)+1'))
 page.emulate_media(reduced_motion='reduce');summary=page.locator('.position-more>summary').first;summary.click();check('reduced motion details have no active height tween',not page.evaluate('document.getAnimations().some(a=>a.effect&&a.effect.target.classList.contains("ui-disclosure"))'))
 # Edge cases use a separate real shell and a controllable browser clock.
 edge=context.new_page();edge.on('pageerror',lambda e:errors.append(str(e)));edge.route('https://fixture.test/**',fulfill)
 edge.goto('https://fixture.test/web/settings');edge.wait_for_function('!!window.StockBotUI');edge.wait_for_timeout(80);edge.clock.install()
 edge.evaluate("window.StockBotUI.load(document.getElementById('load-test'),'讀取測試資料…')")
 check('shared lazy pane displays skeleton',edge.locator('#load-test .ui-skeleton').count()==3)
 edge.clock.fast_forward(20001);check('slow pane ends skeleton after deadline',edge.locator('#load-test .ui-skeleton').count()==0 and '讀取時間較長' in edge.locator('#load-test').inner_text())
 edge.evaluate("document.getElementById('load-test').textContent='已回傳資料'");edge.clock.run_for(50);check('late successful data remains usable',edge.locator('#load-test').inner_text()=='已回傳資料')
 edge.evaluate("window.StockBotUI.formState(document.getElementById('settings-form'),'loading','儲存中')")
 check('shared form loading status accessible',edge.locator('.ui-form-status').get_attribute('role')=='status')
 edge.clock.fast_forward(30001);check('unknown write result does not claim failure or success',edge.locator('.ui-form-status').get_attribute('data-state')=='waiting' and '避免重複送出' in edge.locator('.ui-form-status').inner_text())
 edge.evaluate("var f=document.getElementById('settings-form');f.addEventListener('submit',e=>e.preventDefault(),{once:true});f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))")
 edge.clock.run_for(20);check('client-cancelled submit restores original button',not edge.locator('#settings-form button').is_disabled() and edge.locator('#settings-form button').inner_text()=='儲存設定')
 state['delay_nav']=True;edge.evaluate('window.stockBotSwitchPage("/web/leaderboard",true)');held=held_route();edge.clock.fast_forward(18001)
 check('slow navigation keeps old page and offers cancel',edge.locator('.ui-feedback-route[data-state=waiting] .ui-skeleton').count()==0 and edge.locator('.ui-feedback-route button').inner_text()=='取消等待' and edge.locator('#settings-form').is_visible())
 edge.locator('.ui-feedback-route button').click();edge.clock.run_for(40);check('cancel restores old page and pending navigation',edge.locator('#settings-form').count()==1 and edge.locator('.ui-nav-pending').count()==0)
 try:held.fulfill(body=ROOT.joinpath('rank-fragment.html').read_text(),content_type='text/html')
 except Exception:pass
 # Two quick navigation requests: stale completion cannot replace the newer page.
 edge.evaluate('window.stockBotSwitchPage("/web/leaderboard",true)');old=held_route()
 edge.evaluate('window.stockBotSwitchPage("/web/positions",true)');new=held_route();state['delay_nav']=False
 new.fulfill(body=ROOT.joinpath('positions-fragment.html').read_text(),content_type='text/html');edge.clock.run_for(80);edge.wait_for_selector('form.add',state='attached')
 try:old.fulfill(body=ROOT.joinpath('rank-fragment.html').read_text(),content_type='text/html')
 except Exception:pass
 edge.clock.run_for(80);check('latest route wins rapid navigation',edge.locator('form.add').count()==1 and edge.locator('#rankTabs').count()==0 and edge.locator('.ui-nav-pending').count()==0)
 edge.screenshot(path=str(ROOT/(engine+'-latest-route.png')))
 # Many simultaneous quote changes stay bounded and release their overlays.
 edge.evaluate("document.getElementById('app-page-content').innerHTML='<div id=stress>'+Array.from({length:50},(_,i)=>'<span data-ui-number style=display:block>100.00</span>').join('')+'</div>'")
 edge.clock.run_for(50);edge.evaluate("document.querySelectorAll('[data-ui-number]').forEach((e,i)=>e.textContent=(101+i).toFixed(2))");edge.clock.run_for(30)
 check('numeric animations capped on mobile',edge.locator('.ui-number-tween').count()<=12)
 edge.clock.run_for(500);check('all numeric overlays released after batch update',edge.locator('.ui-number-tween').count()==0 and edge.locator('.ui-number-source').count()==0)
 edge.evaluate("document.querySelector('[data-ui-number]').textContent='—'");edge.clock.run_for(40);check('missing value is not animated as zero',edge.locator('.ui-number-tween').count()==0)
 edge.evaluate("window.uiMutations=0;new MutationObserver(m=>window.uiMutations+=m.length).observe(document.getElementById('app-page-content'),{subtree:true,childList:true})")
 edge.clock.run_for(300);check('shared observer has no idle mutation loop',edge.evaluate('window.uiMutations')==0)
 edge.close()
 # The initial server shell shares skeletons and stops them on real failures.
 shell=context.new_page();shell.on('pageerror',lambda e:errors.append(str(e)));shell.route('https://fixture.test/**',fulfill);state['delay_nav']=True
 shell.goto('https://fixture.test/shell',wait_until='domcontentloaded');shell.wait_for_function('!!window.StockBotUI');initial=held_route()
 check('initial page displays shared skeleton',shell.locator('#loading .ui-skeleton').count()==3 and shell.locator('#loading').is_visible())
 check('initial entry uses the requested full-screen launch animation',shell.locator('#loading').evaluate('(e)=>{const r=e.getBoundingClientRect();return getComputedStyle(e).position==="fixed"&&r.width>=innerWidth-1&&r.height>=innerHeight-1}'))
 check('launch animation does not claim completed processing stages',shell.locator('.market-loader-steps').is_hidden() and shell.locator('.market-loader-chart').is_visible())
 state['delay_nav']=False;initial.fulfill(body=ROOT.joinpath('positions-fragment.html').read_text(),content_type='text/html');shell.wait_for_selector('form.add',state='attached');shell.wait_for_timeout(100)
 check('initial skeleton releases as soon as real content arrives',not shell.locator('#loading').is_visible() and shell.locator('.position-card').count()==7)
 shell.close();state['fail_nav']=True
 shell=context.new_page();shell.on('pageerror',lambda e:errors.append(str(e)));shell.route('https://fixture.test/**',fulfill);shell.goto('https://fixture.test/shell');shell.wait_for_selector('#loading[data-ui-load-state=error]');state['fail_nav']=False
 check('failed initial load has no indefinite skeleton',not shell.locator('.ui-shell-skeleton').is_visible())
 check('failed initial load offers explicit retry',shell.locator('.market-loader-retry').is_visible())
 shell.close()
 # Slow writes remain single-submit; a late completion cannot replace a newer route.
 post=context.new_page();post.on('pageerror',lambda e:errors.append(str(e)));post.route('https://fixture.test/**',fulfill);post.goto('https://fixture.test/web/positions');post.wait_for_function('!!window.StockBotUI');post.clock.install();post.locator('[data-p2-add]').click()
 post.clock.run_for(200);form=post.locator('form.add');form.locator('[name=code]').fill('2330');form.locator('[name=shares]').fill('100');form.locator('[name=cost]').fill('100');state['post']='error'
 form.locator('button[type=submit]').click();post.locator('.position-confirm-submit').click();post.wait_for_selector('form.add .ui-form-status[data-state=error]')
 check('HTTP failure restores form without success indication',form.locator('[name=code]').input_value()=='2330' and post.locator('.position-success-overlay').count()==0)
 state['post']='hold';before_post=state['post_count'];form.locator('button[type=submit]').click();post.locator('.position-confirm-submit').click()
 for _ in range(100):
  if 'held_post' in state:break
  post.wait_for_timeout(10)
 pending_post=state.pop('held_post');post.clock.fast_forward(30001)
 check('slow write shows unconfirmed status and exit',form.locator('.ui-form-status[data-state=waiting]').count()==1 and post.locator('.position-submit-progress-card button').is_visible())
 form.evaluate('(e)=>e.requestSubmit()');post.clock.run_for(40);check('slow write cannot be double submitted',state['post_count']==before_post+1 and post.locator('#position-confirm-backdrop').count()==0)
 post.locator('.position-submit-progress-card button').click();post.evaluate('window.stockBotSwitchPage("/web/leaderboard",true)');post.clock.run_for(100);post.wait_for_selector('#rankTabs')
 pending_post.fulfill(body=ROOT.joinpath('positions.html').read_text().replace('<article class="position-card"','<div class="msg">已新增測試持股。</div><article class="position-card"',1),content_type='text/html');post.clock.run_for(100)
 check('late write completion preserves newer route',post.locator('#rankTabs').count()==1 and post.locator('form.add').count()==0)
 check('late write completion provides visible outcome',post.locator('.ui-global-status').is_visible())
 post.close();state['post']='success'
 # Existing homepage custom expansions and privacy participate in the shared layer.
 page.set_viewport_size({'width':390,'height':844});ready('/web/portfolio')
 check('homepage asset anchor uses a flat color',page.locator('#hv2>.first').evaluate('(e)=>getComputedStyle(e).backgroundImage==="none"&&getComputedStyle(e).boxShadow==="none"'))
 check('phone home keeps four reachable quick actions in one row',page.locator('.hv2-feature-launcher').evaluate('(e)=>getComputedStyle(e).gridTemplateColumns.split(" ").length===4&&[...e.querySelectorAll("a")].length===4&&[...e.querySelectorAll("a")].every(x=>x.getBoundingClientRect().height>=44)'))
 check('phone impact highlights read as continuous rows',page.locator('.home-impact-card').evaluate_all('(es)=>es.length===2&&es.every(e=>getComputedStyle(e).display==="grid"&&getComputedStyle(e).borderRadius==="0px"&&getComputedStyle(e).boxShadow==="none")'))
 group=page.locator('.hv2-g-up');group.locator('[data-hv2-more]').click()
 check('homepage custom more state retained',group.locator('[data-home-chart-row]:visible').count()==4 and group.locator('[data-hv2-more]').get_attribute('aria-expanded')=='true')
 row=group.locator('[data-home-chart-row]').first;row.click();check('homepage calculation details still open',row.get_attribute('aria-expanded')=='true')
 page.locator('[data-home-impact-mode=stock]').click();check('homepage period uses its actual cached percentage','%' in row.locator('strong').inner_text())
 page.locator('[data-hv2-sort=name]').click();check('homepage sorting feedback remains selected',page.locator('[data-hv2-sort=name]').get_attribute('aria-pressed')=='true')
 sensitive=page.locator('[data-home-sensitive]').first;sensitive.scroll_into_view_if_needed();page.wait_for_timeout(120);sensitive.evaluate('(e)=>{e.dataset.homeOriginal="71,000";e.textContent="71,000";}');page.wait_for_function('document.querySelectorAll(".ui-number-tween").length>0')
 page.locator('[data-home-privacy-toggle]').click();check('privacy immediately clears financial animation overlay',page.locator('.ui-number-tween').count()==0 and sensitive.inner_text()!='71,000')
 for width in [360,390,430]:
  page.set_viewport_size({'width':width,'height':900});check('homepage no overflow '+str(width),page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
 ready('/web/more')
 check('more tools keep continuous rows with accessible touch areas',page.locator('.more-item').evaluate_all('(es)=>es.length>=10&&es.every(e=>getComputedStyle(e).borderRadius==="0px"&&e.getBoundingClientRect().height>=44)'))
 check('more preserves trading, comparison, appearance and install entries',all(page.locator('.more-item[href="'+path+'"]').count()==1 for path in ['/web/trades','/web/compare','/web/settings#appearance','/web/install']))
 for width in [360,390,430]:
  page.set_viewport_size({'width':width,'height':900});check('more tools stay within phone viewport '+str(width),page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
 ready('/web/us-rotation');page.wait_for_function('document.querySelector("#us-status")?.textContent==="尚未有完成快照"')
 check('US rotation empty-data state stays explicit','等待第一筆可信資料' in page.locator('#us-summary').inner_text() and '尚無美股產業快照' in page.locator('#us-heat').inner_text())
 check('US rotation sections use the continuous app layout',page.locator('#us-radar .us-section').first.evaluate('(e)=>getComputedStyle(e).borderRadius==="0px"&&getComputedStyle(e).boxShadow==="none"'))
 page.locator('[data-period="5"]').click();check('US rotation period control updates locally',page.locator('[data-period="5"]').get_attribute('aria-pressed')=='true' and page.locator('[data-period="20"]').get_attribute('aria-pressed')=='false')
 advanced=page.locator('#us-radar .us-advanced').first;advanced.locator('summary').click();check('US rotation advanced analysis opens in place',advanced.evaluate('(e)=>e.open'))
 for width in [360,390,430]:
  page.set_viewport_size({'width':width,'height':900});check('US rotation no overflow '+str(width),page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
 state['us_populated']=True;page.set_viewport_size({'width':390,'height':844});ready('/web/us-rotation');page.wait_for_selector('#us-heat [data-industry]')
 check('rotation date and data source stay visible','2026-11-04' in page.locator('#us-asof').inner_text() and '隔離測試日線' in page.locator('#us-status').inner_text())
 check('risk observation precedes the industry overview',page.evaluate('!!(document.querySelector("#us-risk-section").compareDocumentPosition(document.querySelector("#us-summary"))&Node.DOCUMENT_POSITION_FOLLOWING)'))
 check('phone industry heat uses compact continuous rows',page.locator('#us-heat').evaluate('(e)=>getComputedStyle(e).display==="block"') and page.locator('#us-heat .us-heat-card').first.evaluate('(e)=>getComputedStyle(e).display==="grid"&&getComputedStyle(e).borderRadius==="0px"'))
 check('risk and stop signals are clearly labeled',page.locator('[data-signal="us"] .us-alert-badge').inner_text()=='已達警戒' and page.locator('[data-signal="us_stop"] .us-alert-badge').inner_text()=='已突破')
 check('unopened risk charts do not render SVGs',page.locator('.us-signal-svg').count()==0)
 page.screenshot(path=str(ROOT/(engine+'-us-rotation-populated-390.png')))
 signal=page.locator('[data-signal="us"]');signal.locator('.us-signal-trend-panel>summary').click();signal.locator('.us-signal-svg').wait_for(state='visible')
 check('risk chart renders only after its disclosure opens',signal.locator('.us-signal-svg').count()==1 and page.locator('[data-signal="us_stop"] .us-signal-svg').count()==0)
 readout=signal.locator('.us-trend-readout');latest=readout.inner_text();signal.locator('.us-signal-svg').focus();page.keyboard.press('ArrowLeft');check('risk chart keyboard tracking changes the inspected date',readout.inner_text()!=latest)
 signal.locator('[data-trend-period="30"]').click();check('risk chart period selection stays local',signal.locator('[data-trend-period="30"]').get_attribute('aria-pressed')=='true' and '30 個交易日' in signal.locator('.us-trend-caption').inner_text())
 signal.locator(':scope>details.us-research:not(.us-signal-trend-panel)>summary').click();signal.locator('.us-stat-limit-copy>summary').click();check('historical statistics preserve their verification warning','歷史研究數據待核驗' in signal.inner_text() and '原始研究來源' in signal.inner_text())
 page.locator('[data-signal-market-select="taiwan"]').click();check('market switch hides US signals and shows Taiwan signals',signal.is_hidden() and page.locator('[data-signal="taiwan"]').is_visible() and page.locator('[data-signal="taiwan_stop"]').is_visible())
 check('Taiwan risk and unconfirmed breakout remain distinct',page.locator('[data-signal="taiwan"] .us-alert-badge').inner_text()=='已達警戒' and page.locator('[data-signal="taiwan_stop"] .us-alert-badge').inner_text()=='尚未突破')
 api_count=sum(path=='/web/api/us-rotation' for method,path,q in requests)
 page.locator('[data-period="5"]').click();check('industry period displays the selected cached return',page.locator('[data-industry="technology"] .us-heat-value').inner_text()=='+2.00%')
 page.locator('[data-industry="industrials"]').click();check('industry row selection synchronizes the Taiwan sector picker',page.locator('#us-tw-sector').input_value()=='industrials' and page.locator('[data-industry="industrials"]').get_attribute('aria-pressed')=='true')
 page.locator('#us-tw-sector').select_option('technology');page.wait_for_function('document.activeElement?.id==="us-tw-companies-heading"');check('Taiwan sector navigation focuses the actual company section',page.locator('[data-research-code="2330"]').is_visible())
 company=page.locator('[data-research-code="2330"]');company.locator('details>summary').click();check('company research retains dates, evidence and existing market link','2026-10-07' in company.inner_text() and company.locator('a[href*="stock-analysis?code=2330"]').count()==1)
 check('rotation selection and chart inspection never refetch the snapshot',sum(path=='/web/api/us-rotation' for method,path,q in requests)==api_count)
 for width in [360,390,430]:
  page.set_viewport_size({'width':width,'height':900});check('populated rotation stays within phone viewport '+str(width),page.evaluate('document.documentElement.scrollWidth<=innerWidth+1&&innerWidth<=visualViewport.width+1'))
 ready('/web/market');page.wait_for_function('Number(document.querySelector("#market-kline")?.dataset.visibleCount)>0')
 check('market K-line draws actual renderer candles and MACD',page.locator('#market-kline .market-candle').count()>0 and page.locator('#market-kline .market-macd').count()==1)
 check('market offers real fullscreen and drawing controls',page.locator('#market-fullscreen-open').is_visible() and page.locator('[data-draw-mode=horizontal]').is_visible() and page.locator('[data-draw-mode=trendline]').is_visible())
 visible_before=int(page.locator('#market-kline').get_attribute('data-visible-count'));page.locator('[data-chart-zoom=in]').click();page.wait_for_function('(n)=>Number(document.querySelector("#market-kline")?.dataset.visibleCount)<n',arg=visible_before)
 check('market zoom updates the visible candle window',int(page.locator('#market-kline').get_attribute('data-visible-count'))<visible_before)
 page.locator('#market-fullscreen-open').click();page.wait_for_function('document.querySelector(".market-chart-card")?.classList.contains("is-fullscreen")')
 check('fullscreen expands the chart and keeps drawing tools available',page.locator('.market-chart-card.is-fullscreen').is_visible() and page.locator('[data-draw-mode=horizontal]').is_visible())
 page.locator('[data-draw-mode=horizontal]').click();box=page.locator('#market-kline').bounding_box();page.mouse.click(box['x']+box['width']*.55,box['y']+box['height']*.3)
 page.wait_for_function('document.querySelector("#market-drawing-status")?.textContent==="標線已同步保存"')
 check('fullscreen horizontal drawing persists through the drawings API',page.locator('#market-user-drawings line[data-id]').count()==1 and state.get('drawing_count')==1)
 page.locator('[data-draw-mode=trendline]').click();box=page.locator('#market-kline').bounding_box();page.mouse.move(box['x']+box['width']*.35,box['y']+box['height']*.28);page.mouse.down();page.mouse.move(box['x']+box['width']*.48,box['y']+box['height']*.42,steps=6);page.mouse.up()
 page.wait_for_function('document.querySelector("#market-drawing-status")?.textContent==="標線已同步保存"&&document.querySelectorAll("#market-user-drawings line[data-id]").length===2')
 check('fullscreen trendline drag saves a two-point drawing',state.get('drawing_count')==2)
 page.locator('#market-fullscreen-close').click();check('market exits fullscreen without a route reload',not page.locator('.market-chart-card').evaluate('(e)=>e.classList.contains("is-fullscreen")'))
 ready('/web/workbench')
 check('closed screener drawer does not expand the phone layout viewport',page.evaluate('({width})=>innerWidth<=width+1&&document.documentElement.scrollWidth<=width+1',dict(width=page.viewport_size['width'])))
 check('screener bottom navigation stays in the visible phone viewport',page.locator('.app-bottom-nav').evaluate('(e)=>{const r=e.getBoundingClientRect();return r.right<=document.documentElement.clientWidth+1&&r.bottom<=visualViewport.height+1}'))
 page.wait_for_selector('#wb-rows .wb-row')
 check('phone screener quote row keeps a compact three-column summary',page.locator('#wb-rows .wb-rich-row').first.evaluate('(e)=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return s.gridTemplateColumns.split(" ").length===3&&r.height>=64&&r.height<=120&&getComputedStyle(e.querySelector(".wb-fact-line")).display==="none"}'))
 check('phone screener keeps price and score visible',page.locator('#wb-rows .wb-row').first.locator('.wb-price-block').is_visible() and page.locator('#wb-rows .wb-row').first.locator('.wb-score-block').is_visible())
 page.locator('#wb-rows .wb-row').first.click()
 page.wait_for_function('getComputedStyle(document.querySelector("#wb-drawer")).visibility==="visible"')
 check('opened screener drawer is visible inside the phone viewport',page.locator('#wb-drawer').evaluate('(e)=>getComputedStyle(e).visibility==="visible"&&e.getBoundingClientRect().right<=document.documentElement.clientWidth+1'))
 check('screener row opens its actual detail renderer','2330' in page.locator('#wb-detail').inner_text() and '正在整理詳細資料' not in page.locator('#wb-detail').inner_text())
 page.locator('#wb-close').click()
 check('screener close restores the list and hides the drawer',page.locator('#wb-drawer').get_attribute('aria-hidden')=='true' and not page.locator('#wb-drawer').evaluate('(e)=>e.classList.contains("open")') and page.locator('#wb-mask').is_hidden())
 check('screener assets use a three-part mobile selector',page.locator('#wb-asset-tabs').evaluate('(e)=>getComputedStyle(e).display==="grid"&&getComputedStyle(e).gridTemplateColumns.split(" ").length===3'))
 check('screener search spans above its compact action row',page.evaluate('(()=>{const w=document.querySelector("#stockbot-workbench .wb-tools"),l=w?.querySelector("label"),b=w?.querySelector("button"),lr=l?.getBoundingClientRect(),br=b?.getBoundingClientRect();return !!(w&&l&&b&&getComputedStyle(w).gridTemplateColumns.split(" ").length===2&&lr&&br&&lr.y<br.y&&parseFloat(getComputedStyle(b).minHeight)>=40)})()'))
 page.locator('#wb-filter').click()
 check('screener filter opens as wrapping touch controls',page.locator('#wb-filter-panel').evaluate('(e)=>!e.hidden&&getComputedStyle(e).display==="flex"&&getComputedStyle(e).flexWrap==="wrap"'))
 check('screener filter buttons meet mobile touch height',page.locator('#wb-filter-panel button').evaluate_all('(es)=>es.length>0&&es.every(e=>parseFloat(getComputedStyle(e).minHeight)>=36)'))
 page.locator('[data-asset=lab]').click();page.wait_for_selector('.wb-lab-strategy.active')
 check('strategy lab still initializes after shared upgrades','經典四項選股' in page.locator('.wb-lab-strategy.active').inner_text())
 page.locator('.wb-lab-strategy').nth(1).click();check('strategy selection still works',page.locator('.wb-lab-strategy.active').get_attribute('data-lab-strategy')=='營收動能')
 check('strategy text stays readable with shared feedback',page.locator('.wb-lab-strategy.active strong').evaluate('(e)=>getComputedStyle(e).webkitTextFillColor') not in ['rgb(255, 255, 255)','rgba(0, 0, 0, 0)'])
 page.screenshot(path=str(ROOT/(engine+'-workbench.png')))
 check('all actual renderer scripts free of errors',not errors,errors)
 browser.close()
ROOT.joinpath(engine+'-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2));print(json.dumps({'engine':engine,'passed':len(results),'errors':errors,'requests':len(requests)},ensure_ascii=False))
