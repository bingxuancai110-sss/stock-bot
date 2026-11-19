from pathlib import Path
from playwright.sync_api import sync_playwright
repo=Path(__file__).resolve().parents[2]
with sync_playwright() as p:
 for engine in ('chromium','webkit'):
  browser=getattr(p,engine).launch();page=browser.new_page(viewport={'width':390,'height':844});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.set_content('<html><body id="stockbot-app"><main id="app-page-content">Original data</main><a data-app-nav="1" href="https://fixture.test/web/positions">Holdings</a></body></html>')
  page.add_style_tag(path=str(repo/'static/ui-feedback.css'));page.add_script_tag(path=str(repo/'static/ui-feedback.js'));page.add_script_tag(path=str(repo/'static/app-experience.js'))
  page.evaluate("StockBotUI.routeStart('Opening holdings','/web/positions')")
  page.wait_for_timeout(100);assert page.locator('.ui-route-preview').count()==0
  page.wait_for_timeout(250);assert page.locator('.ui-route-preview,.experience-route-status').count()==0
  assert page.locator('.ui-nav-pending').count()==1
  assert page.locator('#app-page-content').evaluate('e=>Number(getComputedStyle(e).opacity)')>=.87
  assert page.locator('#app-page-content').inner_text()=='Original data'
  page.evaluate("StockBotUI.routeEnd(true)");page.wait_for_timeout(35)
  assert page.locator('.ui-route-preview').count()==0
  assert page.locator('#app-page-content').evaluate("e=>getComputedStyle(e).animationName")=='sb-content-arrive'
  page.wait_for_timeout(300);assert page.locator('.ui-route-arrive').count()==0
  page.evaluate("StockBotUI.routeStart('Fast','/web/positions');StockBotUI.routeEnd(true)");page.wait_for_timeout(350);assert page.locator('.ui-route-preview').count()==0
  page.evaluate("StockBotUI.routeStart('Slow','/web/positions')");page.wait_for_timeout(350);page.evaluate("StockBotUI.routeEnd(false,'Failed',()=>{})")
  assert page.locator('.ui-route-preview').count()==0 and page.locator('[data-state=error]').count()==1
  page.emulate_media(reduced_motion='reduce');page.evaluate("StockBotUI.routeStart('Reduced','/web/positions')");page.wait_for_timeout(350)
  assert page.locator('.ui-route-preview,.experience-route-status').count()==0
  assert page.locator('#app-page-content').evaluate('e=>getComputedStyle(e).opacity')=='1'
  page.evaluate('StockBotUI.routeEnd(true)');assert not errors,errors
  print(engine+': 10 route transition checks passed',flush=True);browser.close()
