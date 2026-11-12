"""Actual settings renderer and scripts, isolated account and intercepted requests."""
import ast
from functools import wraps
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse, parse_qs

from flask import Flask, request
from playwright.sync_api import sync_playwright

REPO=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(REPO),str(REPO/'tests')]
from test_settings_theme import actual_settings, THEMES, SOURCE
from settings_theme import create_blueprint

app=Flask(__name__)
state={'theme':'mist','mode':'success','posts':0,'reads':0}
class Store:
    def read(self,uid):return state['theme']
    def save(self,uid,theme):state['theme']=theme;return theme
def login(fn):
    @wraps(fn)
    def wrapped():return fn('isolated-fixture-user')
    return wrapped
app.register_blueprint(create_blueprint(web_login_required=login,
    valid_csrf=lambda uid:request.form.get('csrf_token')=='fixture-csrf',store_factory=Store,themes=THEMES))

theme_css=re.search(r"(?ms)^USER_UI_THEME_CSS = r'''(.*?)'''",SOURCE)[1]
base_css=ast.literal_eval(re.search(r'(?ms)^BASE_CSS = (""".*?""")',SOURCE)[1])
start=SOURCE.index("  document.addEventListener('submit',",SOURCE.index('// 一般 POST 表單'))
legacy=SOURCE[start:SOURCE.index('  // 套牢籌碼',start)].replace('{{','{').replace('}}','}')

def markup(fragment=False):
    with app.test_request_context('/web/settings'+('?fragment=1' if fragment else '')):
        result=actual_settings(theme=state['theme'])
        rendered=result.get_data(as_text=True) if hasattr(result,'get_data') else result
    if fragment:return rendered
    return ('<!doctype html><html data-ui-theme="'+state['theme']+'"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="theme-color" content="'+THEMES[state['theme']]['page']+'"><style>'+base_css+theme_css+'</style>'
        '<link rel="stylesheet" href="/static/ui-feedback.css"><link rel="stylesheet" href="/static/app-shell.css"></head>'
        '<body id="stockbot-app" data-app-view="settings"><div id="stockbot-shell">'+rendered+'</div>'
        '<script src="/static/ui-feedback.js"></script><script>function haptic(){}\n'+legacy+'</script>'
        '<script src="/static/settings-theme.js"></script></body></html>')

engine=sys.argv[1] if len(sys.argv)>1 else 'chromium'
artifacts=Path(os.environ.get('STOCKBOT_UI_ARTIFACTS',str(REPO/'test-artifacts'/'settings-theme')))
artifacts.mkdir(parents=True,exist_ok=True)
checks=[]
with sync_playwright() as pw:
    browser=getattr(pw,engine).launch(headless=True)
    page=browser.new_page(viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
    errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
    def response_for(req):
        with app.test_client() as client:
            return client.open('/web/api/settings/theme',method=req.method,
                data=req.post_data_buffer if req.method=='POST' else None,
                content_type=req.headers.get('content-type'))
    def fulfill(route,response):
        route.fulfill(status=response.status_code,body=response.get_data(),headers=dict(response.headers))
    def intercept(route):
        path=urlparse(route.request.url).path
        if path.startswith('/static/'):
            route.fulfill(body=(REPO/path.lstrip('/')).read_text(encoding='utf-8'),content_type='text/css' if path.endswith('.css') else 'application/javascript');return
        if path=='/web/api/settings/theme':
            key='posts' if route.request.method=='POST' else 'reads';state[key]+=1
            if state['mode']=='offline':route.abort('failed');return
            if route.request.method=='POST' and state['mode']=='hold':state['pending']=route;return
            if route.request.method=='POST' and state['mode']=='html':route.fulfill(body='<html>not a save acknowledgment</html>',content_type='text/html');return
            response=response_for(route.request)
            if route.request.method=='POST' and state['mode']=='drop':route.abort('failed');return
            fulfill(route,response);return
        if path=='/web/settings':
            fragment=parse_qs(urlparse(route.request.url).query).get('fragment')==['1']
            route.fulfill(body=markup(fragment),content_type='text/html; charset=utf-8');return
        route.abort('blockedbyclient')
    page.route('**/*',intercept)
    def check(name,condition):
        checks.append({'check':name,'passed':bool(condition)})
        print(('PASS ' if condition else 'FAIL ')+name,flush=True)
        assert condition,name
    def open_page():page.goto('https://fixture.test/web/settings');page.locator('[data-settings-theme-form]').wait_for()
    def choose(theme):page.locator('.settings-theme-option').filter(has=page.locator('input[value="'+theme+'"]')).click()
    def submit():page.get_by_role('button',name='套用並儲存配色',exact=True).click()
    def wait_enabled():page.wait_for_function('!document.querySelector(".settings-theme-save").disabled')

    open_page();choose('warm');submit();wait_enabled()
    check('saves once despite legacy handler',state['posts']==1 and state['theme']=='warm')
    check('applies actual palette and feedback',page.locator('html').get_attribute('data-ui-theme')=='warm' and '已儲存' in page.locator('.ui-form-status').inner_text())
    check('saved palette changes visible canvas with existing styles',page.locator('body').evaluate('(el)=>getComputedStyle(el).backgroundColor')=='rgb(245, 243, 238)')
    check('updates browser theme color',page.locator('meta[name="theme-color"]').get_attribute('content')==THEMES['warm']['page'])
    page.reload();check('persisted on reload',page.locator('[value="warm"]').is_checked())
    state['mode']='drop';choose('stone');submit();wait_enabled()
    check('lost response reads saved state without duplicate POST',state['posts']==2 and state['reads']==1 and '已儲存' in page.locator('.ui-form-status').inner_text())
    state['mode']='offline';choose('mist');submit();wait_enabled()
    check('unknown result restores usable confirmation button',page.get_by_role('button',name='確認目前配色',exact=True).is_enabled())
    posts=state['posts'];state['mode']='success';page.get_by_role('button',name='確認目前配色',exact=True).click();wait_enabled()
    check('confirmation is GET only',state['posts']==posts and state['theme']=='stone')
    state['mode']='html';submit();wait_enabled()
    check('HTTP 200 HTML is not treated as saved','已儲存' not in page.locator('.ui-form-status').inner_text())
    state['mode']='success';page.locator('[name="csrf_token"]').evaluate('(el)=>el.value="invalid"');submit();wait_enabled()
    check('csrf rejection is clear and button usable','安全驗證' in page.locator('.ui-form-status').inner_text())
    open_page();state['mode']='hold';choose('warm');before=state['posts'];submit()
    page.wait_for_function('document.querySelector(".settings-theme-save").disabled')
    page.locator('form[data-settings-theme-form]').evaluate('(f)=>f.requestSubmit()')
    check('in-flight double submit blocked',state['posts']==before+1)
    pending=state.pop('pending');fulfill(pending,response_for(pending.request));state['mode']='success';wait_enabled()
    check('button always restored',page.get_by_role('button',name='套用並儲存配色',exact=True).is_enabled())
    for theme in ('mist','stone','warm'):
        page.evaluate('''async()=>{document.dispatchEvent(new CustomEvent('stockbot:pageleaving'));document.querySelector('#app-page-content').innerHTML=await (await fetch('/web/settings?fragment=1')).text();}''')
        choose(theme);before=state['posts'];submit();wait_enabled()
        check('fragment navigation '+theme,state['posts']==before+1 and page.locator('.app-header').count()==1)
    page.set_viewport_size({'width':1280,'height':900});check('desktop choices remain usable',page.locator('[value="stone"]').count()==1)
    page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(artifacts/(engine+'-settings.png')),full_page=True)
    check('no JavaScript errors',not errors)
    (artifacts/(engine+'-results.json')).write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
    browser.close()
print(engine+': '+str(len(checks))+' checks passed')
