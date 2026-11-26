"""Isolated engine fixtures; no live stock or account activity."""
import sys,json
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from unittest.mock import patch
from playwright.sync_api import sync_playwright
repo=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(repo),str(repo/'tests')]
from test_short_side import path,controlled
from test_short_term import quote,now
import short_side as ss
import short_term as st
b,h=path();b=b[:67]
with patch.object(ss,'setup',side_effect=controlled):row=ss.analyse('2330','隔離測試空方',quote(b),h,now(b))
row['signals']=st.signal_allocations(row['signals'])
for e in row['signals']:e['allocation']['label']={'entry':'首次放空100%','add':'加空首次股數50%'}[e['type']]
row['spreads']=st.trade_spreads(row['signals'],row['price'],row['date'],direction='short')
with sync_playwright() as p:
 for engine in ('chromium','webkit'):
  browser=getattr(p,engine).launch();page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True);errors=[];directions=[];queries=[];held_count=[2];available=[True]
  page.on('pageerror',lambda e:errors.append(str(e)))
  def route(r):
   u=urlparse(r.request.url)
   if u.path.startswith('/static/'):
    f=repo/u.path.lstrip('/');r.fulfill(body=f.read_text(encoding='utf8'),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
   if '/api/workbench/short-term' in u.path:
    if not available[0]:
     r.fulfill(status=503, body='unavailable');return
    q=parse_qs(u.query);queries.append(q);d=q.get('direction',['long'])[0];directions.append(d)
    summary={key:row[key] for key in ('code','name','date','price','current_action','latest_event','provisional')}
    summary.update(position_group='held',trade=row['spreads']['open'])
    response=dict(ok=True,row=row) if q.get('code') else dict(ok=True,rows=[summary] if d=='short' else [],alerts=[],meta=dict(direction=d,signals_current=True,date=row['date'],limit=40,total=1 if d=='short' else 0,position_counts=dict(held=held_count[0],waiting=0,closed=0)))
    r.fulfill(body=json.dumps(response,ensure_ascii=False),content_type='application/json');return
   r.fulfill(body='<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/short-term.css"></head><body><div id="wb-status"></div><section id="wb-short-term"></section><script src="/static/short-term.js"></script></body></html>',content_type='text/html')
  page.route('https://fixture.test/**',route);page.goto('https://fixture.test/web/workbench');page.locator('[data-direction="short"]').click();page.locator('.st-desk-row').wait_for()
  assert directions==['long','short']
  assert page.locator('[data-direction="short"]').get_attribute('aria-pressed')=='true'
  assert '場內' in page.locator('.st-desk-row').inner_text()
  assert '+' in page.locator('.st-desk-spread').inner_text()
  assert queries[-1]['view']==['summary'] and queries[-1]['limit']==['40'] and page.locator('.st-chart').count()==0
  entry_text=page.locator('.st-desk-row td').nth(2).inner_text()
  held_count[0]=3;page.locator('.st-refresh').click()
  page.wait_for_function("document.querySelector('.st-state-tabs [data-position=held] b').textContent==='3'")
  assert page.locator('.st-desk-row td').nth(2).inner_text()==entry_text, 'metadata refresh overwrote entry price with position count'
  page.locator('.st-desk-open').click();page.locator('.st-chart svg').wait_for()
  assert queries[-1]['code']==['2330'] and page.locator('.st-momentum-bar').count()>0
  assert '▼ 放空／加空' in page.locator('.st-detail').inner_text()
  assert '未核實' in page.locator('.st-source').inner_text()
  assert '初始1R' in page.locator('.st-inspector').inner_text()
  assert page.locator('.st-desk-scroll').evaluate('e=>e.getBoundingClientRect().right<=innerWidth+1')
  available[0]=False
  page.locator('.st-refresh').click()
  page.get_by_text('暫時無法讀取，顯示上次保存的結果',exact=True).wait_for()
  assert page.locator('.st-desk-row[data-position="unknown"]').count()==1 and page.locator('.st-notice-event').count()==0
  available[0]=True
  page.locator('.st-refresh').click()
  page.wait_for_function("document.querySelector('.st-status').textContent.includes('收盤')")
  page.locator('.st-desk-filters summary').click()
  page.locator('.st-price-sort').select_option('price_desc');page.wait_for_function("document.querySelector('.st-list-summary').textContent.includes('股價由高到低')")
  assert queries[-1]['sort']==['price_desc']
  page.locator('.st-price-filter').select_option('1000plus');page.wait_for_timeout(100)
  assert queries[-1]['price_range']==['1000plus']
  assert page.locator('.st-price-tools').evaluate('e=>e.getBoundingClientRect().right<=innerWidth+1')
  page.locator('[data-direction="long"]').click();page.wait_for_function('document.querySelectorAll(".st-desk-row").length===0')
  assert directions[-1]=='long' and queries[-1]['sort']==['price_desc'] and queries[-1]['price_range']==['1000plus'] and not errors,errors
  page.reload();page.locator('.st-desk-filters summary').click()
  assert page.locator('.st-price-sort').input_value()=='price_desc' and page.locator('.st-price-filter').input_value()=='1000plus'
  print(engine+': short/long switch, labels, arrows, evidence, spread, layout and reset passed',flush=True);browser.close()
