"""Reproduce a Friday cached valuation arriving after a newer Monday view."""
import ast, json, os, sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from quote_lab import page_markup
from test_fugle_accounting import actual_namespace,quotes,NOW
ns=actual_namespace();current=ns['_apply_fugle_browser_quotes']('owner',quotes())
old=json.loads(json.dumps(current));old['view_as_of']='2026-11-06 21:53:36'
old['home'].update(fetched_at='2026-11-06 21:53:36',market={'pct':-4.51,'updated_at':'2026-11-06 13:30:00','source':'old'})
old['home']['portfolio'].update(pct=-7.18,relative=-2.68)
old['positions']['totals'].update(value=147311,net_pct=-26.73)
partial=json.loads(json.dumps(current));partial.update(view_as_of='2026-11-09 11:05:00')
partial['home'].update(fetched_at='2026-11-09 11:05:00',market=old['home']['market'])
partial['home']['portfolio'].update(pct=None,relative=None,ready=False)
partial['positions']['totals'].update(complete=False,value=None,net_pct=None);partial['positions']['updates']=[]
new=json.loads(json.dumps(current));new['view_as_of']='2026-11-09 11:05:01'
new['home'].update(fetched_at='2026-11-09 11:05:01',market={'pct':6.1,'updated_at':'2026-11-09 11:05:01','source':'Fugle server'})
new['home']['portfolio'].update(pct=8,relative=1.9)
new['positions']['totals'].update(value=160000,net_pct=-20.5)
new['positions']['updates'][0]['weight']=65
late=json.loads(json.dumps(old));late['view_as_of']='2026-11-09 10:50:00';late['home']['fetched_at']='2026-11-09 10:50:00'
state={'view':partial,'calls':0}
tree=ast.parse((ROOT/'main.py').read_text())
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='render_daily_home_top')
script=next(n.value.value for n in ast.walk(fn) if isinstance(n,ast.Assign) and any(getattr(t,'id','')=='home_intraday_script' for t in n.targets) and isinstance(n.value,ast.Constant) and len(str(n.value.value))>100)
HTML='''<meta charset="utf-8"><div id="app-page-content"><div id="hv2">
<section data-home-privacy-card><b class="hv2-px" data-home-sensitive>158,533</b><div class="hv2-head-r"><b>-21.24%</b></div></section>
<b data-home-market-pct>+6.03%</b><b data-home-portfolio-pct>+7.62%</b><b data-home-relative>+1.59%</b>
<p data-home-market-freshness>即時 TAIEX · 更新 2026-11-09 11:04:00</p><p data-home-quote-quality>Current data</p>
<article data-position-code="2308"><div class="position-card-weight"><b>50%</b><i></i></div></article><p data-home-live-title>Current data</p><p data-home-live-note></p>
</div></div>'''+script+'<script defer src="/static/fugle-live.js"></script>'
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium'
with sync_playwright() as p:
 browser=p.webkit.launch() if engine=='webkit' else p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
 page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 page.clock.install(time=datetime(2026,11,9,11,5,tzinfo=ZoneInfo('Asia/Taipei')))
 page.add_init_script('localStorage.setItem("stockbot_fugle_view_v1:"+"a".repeat(64),'+json.dumps(json.dumps({'version':1,'savedAt':datetime(2026,11,6,21,53,tzinfo=ZoneInfo('Asia/Taipei')).timestamp()*1000,'data':old}))+');')
 def route(r):
  path=urlparse(r.request.url).path
  if path=='/static/fugle-live.js':r.fulfill(body=(ROOT/'static/fugle-live.js').read_text(),content_type='application/javascript; charset=utf-8');return
  if path=='/web/quote-lab/app-config':r.fulfill(json={'ok':True,'csrf_token':'test','storage_scope':'a'*64,'panel_html':page_markup(),'background_enabled':True});return
  if path=='/web/quote-lab/background':state['calls']+=1;r.fulfill(json={'ok':True,'view':state['view'],'codes':['2308','00981A'],'status':'partial'});return
  if path=='/web/api/portfolio/intraday':r.fulfill(json={'market_open':False});return
  if path=='/web/portfolio':r.fulfill(body=HTML,content_type='text/html; charset=utf-8');return
  raise AssertionError(path)
 page.route('https://fixture.test/**',route);page.goto('https://fixture.test/web/portfolio');page.wait_for_function('!!window.StockBotFugle')
 page.wait_for_timeout(150)
 def value():return page.locator('.hv2-px').inner_text()
 assert value()=='158,533' and page.locator('[data-home-portfolio-pct]').inner_text()=='+7.62%'
 assert page.locator('[data-home-market-pct]').inner_text()=='+6.03%'
 assert '2026-11-06' not in page.locator('[data-home-quote-quality]').inner_text()
 state['view']=new;page.evaluate('StockBotFugle.request()');page.wait_for_timeout(150)
 assert value()=='160,000' and '8.00%' in page.locator('[data-home-portfolio-pct]').inner_text()
 assert page.locator('[data-position-code="2308"] .position-card-weight>b').inner_text()=='65.0%'
 state['view']=late;page.evaluate('StockBotFugle.request()');page.wait_for_timeout(150)
 assert value()=='160,000' and '8.00%' in page.locator('[data-home-portfolio-pct]').inner_text()
 assert page.locator('[data-position-code="2308"] .position-card-weight>b').inner_text()=='65.0%'
 state['view']=partial;page.evaluate('StockBotFugle.request()');page.wait_for_timeout(150)
 assert value()=='160,000' and '6.10%' in page.locator('[data-home-market-pct]').inner_text()
 # A late independent legacy response must also fail to roll back the home renderer.
 page.evaluate('(home)=>document.dispatchEvent(new CustomEvent("stockbot:fuglequotes",{detail:{home}}))',old['home'])
 assert value()=='160,000' and '8.00%' in page.locator('[data-home-portfolio-pct]').inner_text()
 assert not errors,errors
 print(json.dumps({'engine':engine,'passed':5,'errors':errors}));browser.close()
