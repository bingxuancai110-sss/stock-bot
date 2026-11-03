"""Exercise the real client across document reloads, storage failures and provider failures."""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from quote_lab import page_markup

engine=sys.argv[1] if len(sys.argv)>1 else 'chromium'
KEY='fixture-device-key';SCOPE='a'*64;STORE='stockbot_fugle_device_v1:'+SCOPE
state={'scope':SCOPE,'authorize':True};checks=[];errors=[];requests=[]
MOCK='''window.sockets=[];window.authOK=true;
class FakeSocket {
 constructor(url){this.readyState=0;this.sent=[];sockets.push(this);queueMicrotask(()=>{this.readyState=1;this.onopen?.({});});}
 send(raw){const m=JSON.parse(raw);this.sent.push(m);if(m.event==='auth'&&authOK)queueMicrotask(()=>this.server({event:'authenticated'}));}
 server(m){this.onmessage?.({data:JSON.stringify(m)});}
 close(){this.readyState=3;}
}window.WebSocket=FakeSocket;'''
with sync_playwright() as p:
 browser=p.webkit.launch() if engine=='webkit' else p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox'])
 context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
 context.add_init_script(MOCK)
 def route(r):
  req=r.request;u=urlparse(req.url)
  if u.hostname!='api.fugle.tw':
   assert KEY not in req.url+(req.post_data or '')+json.dumps(req.headers),'key sent to BOT'
  requests.append((u.hostname,u.path))
  if u.path=='/static/fugle-live.js':r.fulfill(body=(REPO/'static/fugle-live.js').read_text(),content_type='application/javascript');return
  if u.path=='/web/quote-lab/app-config':
   if not state['authorize']:r.fulfill(status=403,json={'ok':False});return
   r.fulfill(json={'ok':True,'csrf_token':'fixture-csrf','storage_scope':state['scope'],'panel_html':page_markup()});return
  if u.path=='/web/quote-lab/holdings':r.fulfill(json={'ok':True,'holdings':[{'code':'2308','name':'台達電'}]});return
  if u.path=='/web/quote-lab/apply':r.fulfill(json={'ok':True,'positions':{'updates':[]},'home':{}});return
  if u.hostname=='api.fugle.tw':r.fulfill(status=503,json={'error':'fixture offline'});return
  r.fulfill(body='<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="app-page-content"></div><script defer src="/static/fugle-live.js"></script></body></html>',content_type='text/html')
 context.route('https://fixture.test/**',route);context.route('https://api.fugle.tw/**',route)
 page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
 def ready(path='/web/more'):
  page.goto('https://fixture.test'+path);page.wait_for_function('!!window.StockBotFugle')
 def open_panel():
  if not page.locator('#stockbot-fugle-panel').evaluate('(e)=>e.open'):page.locator('#stockbot-fugle-panel>summary').click()
 def start(remember=False):
  open_panel();page.locator('#ql-key').fill(KEY);page.locator('#ql-remember').set_checked(remember);page.locator('#ql-start').click();page.wait_for_function('sockets.length&&sockets.at(-1).sent.some(x=>x.event==="auth")')
  if page.evaluate('authOK'):page.wait_for_selector('#ql-state[data-state=live]',state='attached')
 def stored():return page.evaluate('(k)=>JSON.parse(localStorage.getItem(k)||"null")',STORE)
 ready();start();assert stored() is None
 page.reload();page.wait_for_function('!!window.StockBotFugle');assert not page.evaluate('StockBotFugle.active()')
 checks.append('opt-out remains memory only after reload')
 # No credential may be persisted until the provider authenticates it.
 page.evaluate('authOK=false');start(True);assert stored() is None
 page.evaluate('sockets.at(-1).server({event:"authenticated"})');page.wait_for_function('(k)=>!!localStorage.getItem(k)',arg=STORE)
 assert stored()['key']==KEY and page.locator('#ql-key').input_value()==''
 checks.append('save only after successful provider authentication')
 ready('/web/portfolio');page.wait_for_function('StockBotFugle.active()&&sockets.length===1')
 assert not page.locator('#stockbot-fugle-panel').is_visible()
 assert page.evaluate('sockets[0].sent[0].data.apikey')==KEY
 checks.append('reload on Home auto-connects without showing credential')
 # Opening a fresh document in the same browser also restores the remembered key.
 page.close();page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));ready();page.wait_for_function('StockBotFugle.active()')
 checks.append('close and reopen same browser restores credentials')
 open_panel();page.locator('#ql-stop').click();assert stored()['autoStart'] is False and stored()['key']==KEY
 page.reload();page.wait_for_function('!!window.StockBotFugle');assert not page.evaluate('StockBotFugle.active()')
 open_panel();page.locator('#ql-start').click();page.wait_for_function('(k)=>JSON.parse(localStorage.getItem(k)).autoStart',arg=STORE);assert stored()['autoStart'] is True
 checks.append('manual stop persists opt-out and one click resumes saved key')
 # Transport loss and quota errors keep a valid remembered credential.
 page.evaluate('sockets.at(-1).onerror({})');assert stored()['key']==KEY
 ready();page.wait_for_selector('#ql-state[data-state=live]',state='attached')
 page.evaluate('sockets.at(-1).server({event:"error",code:1001})');assert stored()['key']==KEY
 checks.append('network interruption and subscription quota preserve saved key')
 ready();page.wait_for_selector('#ql-state[data-state=live]',state='attached');page.evaluate('sockets.at(-1).server({event:"error",code:1000})')
 assert stored() is None and not page.evaluate('StockBotFugle.active()')
 checks.append('invalid authentication clears saved key and stops retry')
 start(True);open_panel();page.locator('#ql-change-key').click()
 assert stored() is None and not page.evaluate('StockBotFugle.active()') and page.locator('#ql-key').input_value()==''
 checks.append('change key stops and removes old credential')
 start(True);open_panel();page.locator('#ql-forget').click();assert stored() is None
 ready();assert not page.evaluate('StockBotFugle.active()')
 checks.append('forget removes credential and prevents auto-connect')
 start(True);state['scope']='b'*64;ready();assert not page.evaluate('StockBotFugle.active()')
 checks.append('another account cannot restore owner credential')
 state['authorize']=False;page.reload();page.wait_for_timeout(150)
 assert page.locator('#stockbot-fugle-panel').count()==0 and not page.evaluate('!!window.StockBotFugle')
 checks.append('unauthorized session never restores credential')
 state.update(scope=SCOPE,authorize=True);ready();page.wait_for_selector('#ql-state[data-state=live]',state='attached');open_panel()
 page.evaluate("()=>{window.originalRemove=Storage.prototype.removeItem;Storage.prototype.removeItem=function(){throw new DOMException('blocked','SecurityError')};}")
 page.locator('#ql-forget').click();assert not page.evaluate('StockBotFugle.active()') and stored()['key']==KEY
 assert '無法清除' in page.locator('#ql-message').inner_text()
 page.evaluate("window.dispatchEvent(new Event('pageshow'))");assert not page.evaluate('StockBotFugle.active()')
 page.evaluate('()=>{Storage.prototype.removeItem=originalRemove;}');page.locator('#ql-forget').click();assert stored() is None
 checks.append('failed clear reports error, prevents auto-restore, and supports retry')
 # Denied browser storage degrades to a working memory-only session with a notice.
 page.evaluate("()=>{Storage.prototype.setItem=function(){throw new DOMException('blocked','SecurityError')};}")
 start(True);open_panel();assert page.evaluate('StockBotFugle.active()') and '無法保存' in page.locator('#ql-remember-status').inner_text(),(page.evaluate('StockBotFugle.active()'),page.locator('#ql-remember-status').inner_text())
 checks.append('storage denied keeps memory session and shows failure')
 # Account/global clear from another same-origin tab stops the running feed.
 page.evaluate("window.dispatchEvent(new StorageEvent('storage',{key:null}))")
 assert not page.evaluate('StockBotFugle.active()');checks.append('cross-tab clear stops live credential use')
 open_panel()
 for width in [360,390,430,1280]:
  page.set_viewport_size({'width':width,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
 checks.append('controls fit mobile and desktop widths')
 assert not errors,errors
 print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors,'bot_key_leaks':0}));browser.close()
