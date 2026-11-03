"""Real renderers and calculation callbacks; Fugle transport and user records are isolated."""
import ast, json, os, sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

REPO=Path(__file__).resolve().parents[2];ROOT=Path('/tmp/stockbot-fugle-validation')/(sys.argv[1] if len(sys.argv)>1 else 'chromium');ROOT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(REPO/'tests'));from test_fugle_accounting import actual_namespace, NOW
sys.path.insert(0,str(REPO));from quote_lab import page_markup
os.environ['STOCKBOT_UI_ARTIFACTS']=str(ROOT)
source=(REPO/'tests/browser/fixtures.py').read_text().replace('range(7)','range(10)').replace('[1355,1510,4705,26.57,1265,1060,637]','[1355,1510,4705,26.57,1265,1060,637,500,600,700]')
exec(compile(source,'<actual-ten-stock-renderer>','exec'),{'__file__':str(REPO/'tests/browser/fixtures.py')})
tree=ast.parse((REPO/'main.py').read_text());home_fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='render_daily_home_top')
live=next(n.value.value for n in ast.walk(home_fn) if isinstance(n,ast.Assign) and any(getattr(t,'id','')=='home_intraday_script' for t in n.targets) and isinstance(n.value,ast.Constant) and len(str(n.value.value))>100)
for name in ['home.html','home-fragment.html']:
 f=ROOT/name;content=f.read_text();f.write_text(content.replace('</body>',live+'</body>') if '</body>' in content else content+live)
CODES=['1001','1002','1003','00981A','1005','1006','1007','1008','1009','1010']
MOCK='''window.sockets=[];window.providerCalls=[];window.maxActive=0;window.activeSubs=new Map();window.nextChannel=1;window.fixtureHidden=false;window.quoteStatus=200;
Object.defineProperty(document,'hidden',{get:()=>window.fixtureHidden});
class FakeSocket{
 constructor(url){this.url=url;this.readyState=0;this.sent=[];sockets.push(this);queueMicrotask(()=>{this.readyState=1;this.onopen?.({});});this.beat=setInterval(()=>this.server({event:'heartbeat'}),15000);}
 send(raw){const m=JSON.parse(raw);this.sent.push(m);if(m.event==='auth')queueMicrotask(()=>this.server({event:'authenticated'}));if(m.event==='subscribe'){const rows=m.data.symbols.map(symbol=>{const id=String(nextChannel++);activeSubs.set(id,symbol);return {channel:'trades',id,symbol};});maxActive=Math.max(maxActive,activeSubs.size);queueMicrotask(()=>{this.server({event:'subscribed',data:rows});rows.forEach(x=>this.server({event:'snapshot',channel:'trades',data:{symbol:x.symbol,price:101,time:Date.now()*1000,serial:1}}));});}if(m.event==='unsubscribe'){const rows=m.data.ids.map(id=>({id,channel:'trades',symbol:activeSubs.get(id)}));rows.forEach(x=>activeSubs.delete(x.id));queueMicrotask(()=>this.server({event:'unsubscribed',data:rows}));}}
 server(m){this.onmessage?.({data:JSON.stringify(m)});}
 close(){this.readyState=3;clearInterval(this.beat);activeSubs.clear();}
}
window.WebSocket=FakeSocket;window.emit=m=>sockets.at(-1).server(m);
'''
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium';checks=[];errors=[];posts=[];external=[];provider_status={'status':200};ns=actual_namespace()
ns['get_positions']=lambda uid:[dict(code=c,shares=1063 if c=='00981A' else 100,cost=90,lots=[]) for c in CODES]
with sync_playwright() as p:
 browser=p.webkit.launch() if engine=='webkit' else p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
 page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
 page.clock.install(time=NOW);page.add_init_script(MOCK);page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  u=urlparse(r.request.url);q=parse_qs(u.query)
  if u.path.startswith('/static/'):
   f=REPO/u.path.lstrip('/');r.fulfill(body=f.read_text(),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
  if u.path=='/web/quote-lab/app-config':r.fulfill(json={'ok':True,'csrf_token':'fixture-csrf','panel_html':page_markup()});return
  if u.path=='/web/quote-lab/holdings':r.fulfill(json={'ok':True,'holdings':[{'code':c,'name':'測試公司 '+c} for c in CODES]});return
  if u.path=='/web/quote-lab/apply':
   body=parse_qs(r.request.post_data or '');assert set(body)=={'quotes','csrf_token'} and body['csrf_token']==['fixture-csrf'];posts.append(body)
   raw=json.loads(body['quotes'][0]);latest=max((x['observed_at'] for x in raw),default=int(NOW.timestamp()*1000))
   ns['taiwan_now']=lambda:datetime.fromtimestamp(latest/1000,ZoneInfo('Asia/Taipei'))
   try:r.fulfill(json=ns['_apply_fugle_browser_quotes']('fixture-owner',raw))
   except Exception as e:raise AssertionError('Actual accounting failed: '+str(e))
   return
  if u.hostname=='api.fugle.tw':
   external.append(r.request.headers);code=u.path.split('/')[-1]
   if provider_status['status']==429:r.fulfill(status=429,headers={'Retry-After':'60'},json={'error':'quota'});return
   # Provider clock is supplied through the browser request interception clock.
   r.fulfill(json={'symbol':code,'date':'2026-11-03','type':'EQUITY','previousClose':110,'referencePrice':100,'lastTrade':{'price':101,'time':int(NOW.timestamp()*1e6),'serial':1}});return
  if '/api/' in u.path:r.fulfill(json={'ok':True,'market_open':True,'updates':[],'portfolio':{'ready':False},'note':'fixture normal source'});return
  name='home' if u.path=='/web/portfolio' else 'positions'
  suffix='-fragment' if q.get('fragment') else ''
  r.fulfill(body=(ROOT/(name+suffix+'.html')).read_text(),content_type='text/html')
 page.route('https://fixture.test/**',route);page.route('https://api.fugle.tw/**',route)
 page.goto('https://fixture.test/web/positions');page.wait_for_selector('#stockbot-fugle-panel')
 page.locator('#stockbot-fugle-panel>summary').click();page.locator('#ql-key').fill('fixture-provider-key');page.locator('#ql-start').click()
 # Drive the same event loop and real HTTP callbacks without waiting a trading minute.
 for i in range(30):
  page.clock.run_for(1500);page.wait_for_timeout(20)
  if posts and len(json.loads(posts[-1]['quotes'][0]))==10:break
 assert posts and len(json.loads(posts[-1]['quotes'][0]))==10,'ten holdings not initialized'
 page.wait_for_timeout(100)
 assert page.locator('[data-position-code="1001"] [data-position-price]').inner_text()=='101.00'
 assert page.locator('[data-position-code="1001"] [data-position-value]').inner_text()=='10,100'
 assert '+1.00%' in page.locator('[data-position-code="1001"] [data-position-pct]').inner_text()
 assert page.locator('[data-fugle-total="value"]').inner_text()=='198,263'
 assert page.evaluate('maxActive')<=5 and page.evaluate('sockets.length')==1
 assert all(h.get('x-api-key')=='fixture-provider-key' for h in external)
 assert all('fixture-provider-key' not in json.dumps(body) for body in posts)
 checks+=['ten quotes initialize prices/values/fees','five stream subscriptions','key only goes to provider']
 # A trial cannot replace a real trade or leak into the calculation.
 page.evaluate("emit({event:'data',channel:'trades',data:{symbol:'1001',price:99999,time:Date.now()*1000,isTrial:true}})")
 page.clock.run_for(2500);page.wait_for_timeout(100)
 assert page.locator('[data-position-code="1001"] [data-position-price]').inner_text()=='101.00';checks.append('trial ignored')
 # Use actual fragment navigation: the connection must survive the old page DOM being replaced.
 page.locator('a[data-app-nav][href="/web/portfolio"]').last.click();page.wait_for_selector('#hv2')
 page.clock.run_for(6000);page.wait_for_timeout(100)
 assert page.evaluate('sockets.length')==1 and page.locator('#stockbot-fugle-panel').count()==1
 assert '富果' in page.locator('[data-home-quote-quality]').inner_text()
 assert page.locator('[data-home-portfolio-pct]').inner_text()!='資料不足';checks+=['same connection across pages','home calculation uses Fugle']
 # On return the new page handlers use the existing feed, not a second WebSocket.
 page.locator('a[data-app-nav][href="/web/positions"]').last.click();page.wait_for_selector('[data-position-code="1001"]')
 page.evaluate("emit({event:'data',channel:'trades',data:{symbol:'1001',price:120,time:Date.now()*1000,serial:2}})")
 page.clock.run_for(7000);page.wait_for_timeout(100)
 assert page.locator('[data-position-code="1001"] [data-position-price]').inner_text()=='120.00'
 assert page.evaluate('sockets.length')==1;checks.append('new stream trade updates returned page')
 for width in [360,390,430,1280]:
  page.set_viewport_size({'width':width,'height':900});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1');checks.append('width '+str(width))
 page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(ROOT/(engine+'-formal.png')),full_page=True)
 # Background pauses transport, and foreground resumes using the same in-memory key.
 page.evaluate("fixtureHidden=true;document.dispatchEvent(new Event('visibilitychange'))")
 assert page.evaluate('sockets.at(-1).readyState')==3
 page.evaluate("fixtureHidden=false;document.dispatchEvent(new Event('visibilitychange'))")
 page.clock.run_for(7000);page.wait_for_timeout(100)
 assert page.evaluate('sockets.length')==2 and page.evaluate("sockets.at(-1).sent.find(x=>x.event==='auth').data.apikey")=='fixture-provider-key'
 checks.append('background pauses and resumes in-memory connection')
 provider_status['status']=429;count=len(external)
 page.clock.run_for(30000);page.wait_for_timeout(100)
 assert len(external)-count<=1,'429 retried before cooldown';checks.append('429 respects cooldown')
 assert all('fixture-provider-key' not in json.dumps(body) for body in posts)
 page.locator('#stockbot-fugle-panel>summary').click() if not page.locator('#stockbot-fugle-panel').evaluate('(e)=>e.open') else None
 page.locator('#ql-stop').click();assert page.locator('#ql-key').input_value()=='' and page.evaluate('StockBotFugle.active()') is False;checks.append('stop clears credentials and feed')
 assert not errors,errors
 print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors,'provider_requests':len(external),'calculation_posts':len(posts)}));browser.close()
