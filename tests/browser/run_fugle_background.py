"""Actual JS + panel renderer; all transport is isolated from real accounts."""
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from quote_lab import page_markup

engine = sys.argv[1] if len(sys.argv) > 1 else 'chromium'
with sync_playwright() as p:
    browser = p.webkit.launch() if engine == 'webkit' else p.chromium.launch(executable_path=os.environ.get('STOCKBOT_CHROMIUM_EXECUTABLE'))
    page = browser.new_page(viewport={'width': 390, 'height': 844})
    errors, external = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script("window.WebSocket=class{constructor(){throw new Error('duplicate provider socket')}};")
    def route(request):
        path = urlparse(request.request.url).path
        if urlparse(request.request.url).hostname != 'fixture.test':
            external.append(request.request.url); request.abort(); return
        if path == '/web/quote-lab':
            request.fulfill(body='<meta charset="utf-8"><div id="app-page-content">'+page_markup()+'</div><script src="/static/fugle-live.js"></script>', content_type='text/html; charset=utf-8'); return
        if path == '/static/fugle-live.js':
            request.fulfill(body=(ROOT/'static/fugle-live.js').read_text(encoding='utf-8'), content_type='application/javascript; charset=utf-8'); return
        if path == '/web/quote-lab/app-config':
            request.fulfill(json=dict(ok=True, csrf_token='test', storage_scope='a'*64,
                                     panel_html=page_markup(), background_enabled=True)); return
        if path == '/web/quote-lab/background':
            request.fulfill(json=dict(ok=True, codes=['2308'], status='ready',
                view=dict(home=dict(portfolio=dict(ready=False), note='背景更新'),
                          positions=dict(updates=[], totals=dict(complete=False), note='背景更新')))); return
        raise AssertionError('Unexpected request: '+path)
    page.route('**/*', route)
    page.goto('https://fixture.test/web/quote-lab')
    try:
        page.get_by_text('富果行情 · 背景資料 0／1 檔', exact=True).wait_for(timeout=8000)
    except Exception:
        print('errors', errors, 'body', page.locator('body').inner_text())
        raise
    assert page.evaluate('StockBotFugle.active()') is True
    assert '伺服器背景更新已啟用' in page.locator('#ql-mode-status').text_content()
    assert page.locator('#ql-start').is_disabled()
    assert page.locator('#ql-key').is_disabled()
    assert page.locator('#ql-message').inner_text() == '背景更新'
    assert not external and not errors, (external, errors)
    browser.close()
    print('Background snapshot renders; no provider connections, page errors or editable key field.')
