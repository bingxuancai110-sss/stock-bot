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
  browser=getattr(p,engine).launch();page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True);errors=[];directions=[];queries=[];held_count=[2]
  page.on('pageerror',lambda e:errors.append(str(e)))
  def route(r):
   u=urlparse(r.request.url)
   if u.path.startswith('/static/'):
    f=repo/u.path.lstrip('/');r.fulfill(body=f.read_text(encoding='utf8'),content_type='text/css' if f.suffix=='.css' else 'application/javascript');return
   if '/api/workbench/short-term' in u.path:
    q=parse_qs(u.query);queries.append(q);d=q.get('direction',['long'])[0];directions.append(d)
    r.fulfill(body=json.dumps(dict(ok=True,rows=[row] if d=='short' else [],alerts=[],meta=dict(direction=d,signals_current=True,date=row['date'],total=1 if d=='short' else 0,position_counts=dict(held=held_count[0],waiting=0,closed=0))),ensure_ascii=False),content_type='application/json');return
   r.fulfill(body='<html><head><meta charset="utf-8"><link rel="stylesheet" href="/static/short-term.css"></head><body><div id="wb-status"></div><section id="wb-short-term"></section><script src="/static/short-term.js"></script></body></html>',content_type='text/html')
  page.route('https://fixture.test/**',route);page.goto('https://fixture.test/web/workbench');page.locator('[data-direction="short"]').click();page.locator('.st-card').wait_for()
  assert directions==['long','short']
  assert page.locator('[data-direction="short"]').get_attribute('aria-pressed')=='true'
  assert '加空' in page.locator('.st-card').inner_text()
  assert '每股賺' in page.locator('.st-card').inner_text(), (row['spreads'],page.locator('.st-card').inner_text(),errors)
  entry_text=page.locator('.st-levels b').first.inner_text()
  held_count[0]=3;page.locator('.st-refresh').click()
  page.wait_for_function("document.querySelector('.st-state-tabs [data-position=held] b').textContent==='3'")
  assert page.locator('.st-levels b').first.inner_text()==entry_text, 'metadata refresh overwrote entry price with position count'
  page.locator('.st-expand').click();page.locator('.st-chart svg').wait_for()
  assert '▼ 放空／加空' in page.locator('.st-detail').inner_text()
  assert '未核實' in page.locator('.st-source').inner_text()
  assert '初始1R' in page.locator('.st-inspector').inner_text()
  assert page.locator('.st-card').evaluate('e=>e.getBoundingClientRect().right<=innerWidth+1')
  page.locator('.st-price-sort').select_option('price_desc');page.wait_for_function("document.querySelector('.st-list-summary').textContent.includes('股價由高到低')")
  assert queries[-1]['sort']==['price_desc']
  page.locator('.st-price-filter').select_option('1000plus');page.wait_for_timeout(100)
  assert queries[-1]['price_range']==['1000plus']
  assert page.locator('.st-price-tools').evaluate('e=>e.getBoundingClientRect().right<=innerWidth+1')
  page.locator('[data-direction="long"]').click();page.wait_for_function('document.querySelectorAll(".st-card").length===0')
  assert directions[-1]=='long' and queries[-1]['sort']==['price_desc'] and queries[-1]['price_range']==['1000plus'] and not errors,errors
  page.reload();page.locator('.st-price-sort').wait_for();assert page.locator('.st-price-sort').input_value()=='price_desc' and page.locator('.st-price-filter').input_value()=='1000plus'
  print(engine+': short/long switch, labels, arrows, evidence, spread, layout and reset passed',flush=True);browser.close()
