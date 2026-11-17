"""Real trade form: committed success is a card, follow-up read never repeats POST."""
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import os, sys, json
from playwright.sync_api import sync_playwright
ROOT=Path(os.environ['STOCKBOT_UI_ARTIFACTS']);REPO=Path(__file__).resolve().parents[2]
engine=sys.argv[1] if len(sys.argv)>1 else 'chromium'
checks=[];errors=[];state={'posts':0,'reads':0,'hold':False,'fail':False}
with sync_playwright() as p:
    browser=getattr(p,engine).launch(headless=True)
    page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
    page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        u=urlparse(r.request.url);q=parse_qs(u.query)
        if u.path.startswith('/static/'):
            f=REPO/u.path.lstrip('/');r.fulfill(body=f.read_text(encoding='utf-8'),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
        if r.request.method=='POST':
            state['posts']+=1;r.fulfill(body=(ROOT/'trade-success.html').read_text(encoding='utf-8'),content_type='text/html');return
        if u.path=='/web/positions' and q.get('refresh'):
            state['reads']+=1
            if state['hold']:state['held']=r;return
            if state['fail']:r.fulfill(status=503);return
            r.fulfill(body=(ROOT/'positions-fragment.html').read_text(encoding='utf-8').replace('id="portfolio-v2"','id="portfolio-v2" data-synced="yes"'),content_type='text/html');return
        if '/api/' in u.path or u.path=='/web/quote-lab/app-config':r.fulfill(json={'ok':True,'market_open':False,'updates':[],'events':[]});return
        r.fulfill(body=(ROOT/'positions.html').read_text(encoding='utf-8'),content_type='text/html')
    page.route('https://fixture.test/**',route)
    def check(name,ok):
        checks.append({'check':name,'passed':bool(ok)})
        if not ok:page.screenshot(path=str(ROOT/(engine+'-trade-failed.png')))
        assert ok,name
    def ready():
        page.goto('https://fixture.test/web/positions');page.wait_for_function('!!window.StockBotTradeCompletion&&!!window.StockBotUI')
        page.evaluate("window.originalContent=document.getElementById('app-page-content');window.originalNav=document.querySelector('.app-bottom-nav')")
    def buy():
        page.locator('[data-p2-add]').click()
        state['submit_y']=page.evaluate('scrollY')
        for name,value in [('code','2330'),('shares','100'),('cost','100')]:page.locator('form.add [name="'+name+'"]').fill(value)
        page.locator('form.add button[type=submit]').click();page.locator('.position-confirm-submit').click()
        page.wait_for_selector('.trade-completion-dialog[open]')
    ready();page.select_option('[data-p2-sort]','pct')
    page.evaluate("document.getElementById('trend-period-1w').checked=true;document.querySelector('.position-more').open=true;scrollTo(0,700)")
    opened=page.locator('.position-more[open]').evaluate('(e)=>e.closest("[data-position-code]").dataset.positionCode')
    state['hold']=True;buy();y=state['submit_y']
    check('success card appears before follow-up read completes',page.locator('.trade-completion-dialog h2').inner_text()=='買進記帳完成' and page.locator('#portfolio-v2').count()==1)
    check('success never navigates or replaces page shell',page.evaluate('document.getElementById("app-page-content")===originalContent&&document.querySelector(".app-bottom-nav")===originalNav') and page.url=='https://fixture.test/web/positions')
    check('success sends one write',state['posts']==1)
    page.wait_for_timeout(220)
    check('success card is centered and fits mobile viewport',page.locator('.trade-completion-dialog').evaluate('(e)=>{const r=e.getBoundingClientRect();return r.left>=12&&r.right<=innerWidth-12&&Math.abs((r.top+r.bottom)/2-innerHeight/2)<3}'))
    page.locator('.trade-completion-close').click()
    check('closing card preserves viewport',abs(page.evaluate('scrollY')-y)<3)
    page.wait_for_function('true')
    for _ in range(100):
        if 'held' in state:break
        page.wait_for_timeout(10)
    state.pop('held').fulfill(body=(ROOT/'positions-fragment.html').read_text(encoding='utf-8').replace('id="portfolio-v2"','id="portfolio-v2" data-synced="yes"'),content_type='text/html');state['hold']=False
    page.wait_for_selector('#portfolio-v2[data-synced=yes]');page.wait_for_timeout(150)
    check('background synchronization preserves scroll and shell',abs(page.evaluate('scrollY')-y)<3 and page.evaluate('document.getElementById("app-page-content")===originalContent'))
    check('synchronized trade form has a fresh submission lock',page.locator('form.add').get_attribute('data-ui-submitting') is None)
    check('sorting expanded stock and chart period survive synchronization',page.locator('[data-p2-sort]').input_value()=='pct' and page.locator('.position-more[open]').evaluate('(e)=>e.closest("[data-position-code]").dataset.positionCode')==opened and page.locator('#trend-period-1w').is_checked())
    page.locator('[data-p2-add]').click();check('new transaction sheet remains usable',page.locator('#p2-trade-dialog').evaluate('(e)=>e.open'));page.locator('[data-p2-close]').click()
    ready();state['fail']=True;before=state['posts'];buy()
    page.wait_for_selector('.trade-completion-dialog button:not([hidden]):text("重新同步持股")')
    check('read failure keeps committed success explicit','記帳已完成' in page.locator('.trade-completion-state').inner_text() and '買進記帳完成' in page.locator('.trade-completion-dialog h2').inner_text())
    state['fail']=False;page.get_by_role('button',name='重新同步持股',exact=True).dblclick()
    page.wait_for_function("document.querySelector('.trade-completion-state').textContent==='持股與損益已更新'")
    check('retry only reads and never repeats trade',state['posts']==before+1)
    page.screenshot(path=str(ROOT/(engine+'-trade-success-card.png')))
    page.locator('.trade-completion-close').click()
    # The existing delegated sell confirmation uses the same completion path.
    page.evaluate("""()=>{let f=document.createElement('form');f.className='sellpanel';f.method='post';f.action='/web/positions';f.innerHTML='<input name="action" value="sell"><input name="sell_shares" value="1"><input name="sell_price" value="100"><button type="submit">測試賣出</button>';document.querySelector('.position-card').append(f)}""")
    page.get_by_role('button',name='測試賣出',exact=True).click();page.locator('.position-confirm-submit').click();page.wait_for_selector('.trade-completion-dialog[open]')
    check('sale also confirms in a card with holdings still present',page.locator('.trade-completion-dialog h2').inner_text()=='賣出記帳完成' and page.locator('.position-card').count()>0)
    page.wait_for_function("document.querySelector('.trade-completion-state').textContent==='持股與損益已更新'")
    page.locator('.trade-completion-close').click()
    ready();state['hold']=True;buy();page.locator('.trade-completion-close').click()
    for _ in range(100):
        if 'held' in state:break
        page.wait_for_timeout(10)
    page.evaluate("document.dispatchEvent(new Event('stockbot:pageleaving'));document.getElementById('app-page-content').innerHTML='<h2 id=other-route>另一頁</h2>'")
    state.pop('held').fulfill(body=(ROOT/'positions-fragment.html').read_text(encoding='utf-8'),content_type='text/html');page.wait_for_timeout(100)
    check('late synchronization cannot overwrite another page',page.locator('#other-route').count()==1 and page.locator('#portfolio-v2').count()==0)
    check('no JavaScript errors',not errors)
    browser.close()
ROOT.joinpath(engine+'-trade-completion-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'engine':engine,'passed':len(checks),'errors':errors}))
