"""Exercise the real cached-quote path without importing main or contacting markets."""
import ast
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import lru_cache
import json
import io
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock


def _probe():
    source = (Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8')
    # Parse just the production functions, not the unrelated multi-megabyte
    # inline HTML templates, and never import main's startup/background jobs.
    lock = re.search(r'^_realtime_cache_lock = .+$', source, re.M).group(0)
    writer = source[source.index('def _store_realtime_cache('):source.index('\n_TWSE_MIS_CACHE =')]
    quote_fn = source[source.index('def get_realtime_stock('):source.index('\ndef get_realtime_stocks_bulk(')]
    nodes = ast.parse(lock + '\n' + writer + '\n' + quote_fn).body
    today = date(2026, 11, 4)
    network = Mock(side_effect=AssertionError('cache hit must not download history'))
    ns = dict(threading=threading, time=time, STOCK_NAME_MAP={},
              _realtime_cache={}, REALTIME_CACHE_MAX_ITEMS=3, REALTIME_CACHE_SECONDS=300,
              _realtime_cache_period=lambda: ('20261104', 'close'),
              taiwan_today=lambda: today, requests=SimpleNamespace(get=network))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<real-quote-cache>', 'exec'), ns)
    key = '2308:3mo:20261104:close'
    original = dict(close=1270, pct=0, closes=[1200, 1270],
                    close_dates=[date(2026, 11, 3), today], ma20=1300,
                    support=1180, source='historical')
    ns['_store_realtime_cache'](key, original)
    # The report fetched a quote already. Its factor calculation fetches the same
    # ticker through bulk quotes, supplying an official price to this cache hit.
    quote = dict(close=1220, previous_close=1270, pct=-3.937,
                 close_date='20261104', close_time='13:30:00', close_is_final=True,
                 source='TWSE MIS', limit_up=1395, limit_down=1145, limit_known=True)
    refreshed = ns['get_realtime_stock']('2308', official_quote=quote)
    again = ns['get_realtime_stock']('2308')
    line_names = ['_line_job_key', '_line_async_claim', '_line_async_release',
                  '_line_async_active_feature', '_line_async_query']
    for name in line_names:
        start = source.index('def ' + name + '(')
        end = source.find('\ndef ', start + 1)
        fn = ast.parse(source[start:end]).body[0]
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<real-line-worker>', 'exec'), ns)
    replies = []
    pushes = Mock(side_effect=AssertionError('successful Reply must not use Push'))
    ns.update(_LINE_ASYNC_JOB_LOCK=threading.Lock(), _LINE_ASYNC_JOBS={},
              _LINE_ASYNC_JOB_TTL=300, _LINE_HEAVY_QUERY_SLOT=threading.Lock(),
              _line_ux_config=lambda feature: dict(name=feature, emoji=''),
              start_loading_animation=lambda *args: False,
              build_quick_reply=lambda: None, TextSendMessage=SimpleNamespace,
              line_bot_api=SimpleNamespace(reply_message=lambda token, messages: replies.append((token, messages))),
              _line_async_normalize_result=lambda value: [SimpleNamespace(text=str(value))],
              _push_line_with_retry=pushes)
    for feature in ['quote', 'holdings']:
        assert ns['_line_async_query']('test-user', feature,
            lambda: ns['get_realtime_stock']('2308', official_quote=quote)['close'], feature)
        deadline = time.monotonic() + 2
        while ns['_LINE_HEAVY_QUERY_SLOT'].locked() and time.monotonic() < deadline:
            time.sleep(.01)
        assert not ns['_LINE_HEAVY_QUERY_SLOT'].locked(), 'completed report kept the global slot'
        assert not ns['_LINE_ASYNC_JOBS'], 'completed report kept its user claim'
    pushes.assert_not_called()
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda i: ns['_store_realtime_cache'](str(i), {'close': i + 1}), range(40)))
    network.assert_not_called()
    return dict(close=refreshed['close'], last_close=refreshed['closes'][-1],
                original_close=original['close'], original_last=original['closes'][-1],
                ma20=refreshed['ma20'], source=refreshed['source'],
                close_date=refreshed['close_date'], final=refreshed['close_is_final'],
                limit_down=refreshed['limit_down'], cached_again=again['close'],
                size=len(ns['_realtime_cache']),
                replies=[(token, messages[0].text) for token, messages in replies])


@lru_cache(maxsize=1)
def _result():
    # A primitive-lock regression hangs forever; isolate it so pytest and the
    # following tests cannot inherit a locked quote cache or orphan worker.
    run = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--probe'],
                         stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, timeout=8, check=True)
    return json.loads(run.stdout)


def test_official_price_can_refresh_an_existing_quote_without_deadlocking():
    data = _result()
    assert data['close'] == data['last_close'] == data['cached_again'] == 1220
    assert data['source'] == 'TWSE MIS' and data['close_date'] == '20261104'
    assert data['final'] is True and data['limit_down'] == 1145


def test_refresh_preserves_technical_fields_and_does_not_mutate_original_history():
    data = _result()
    assert data['ma20'] == 1300
    assert data['original_close'] == data['original_last'] == 1270


def test_parallel_cache_writes_keep_the_memory_bound_and_release_the_lock():
    assert _result()['size'] == 3


def test_line_quote_then_holdings_can_reply_and_release_both_job_locks():
    assert _result()['replies'] == [['quote', '1220.0'], ['holdings', '1220.0']]


if __name__ == '__main__' and '--probe' in sys.argv:
    with redirect_stdout(io.StringIO()):
        result = _probe()
    print(json.dumps(result))
