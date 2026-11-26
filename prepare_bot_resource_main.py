"""Generate an opt-in integration copy. Never imports main or overwrites source."""
from pathlib import Path
import argparse


def patch_main(source):
    newline='\r\n' if '\r\n' in source else '\n'
    if '_BOT_RESOURCE_TEST_MODE = ' in source:raise ValueError('already patched')
    def replace(a,b):
        nonlocal source
        a=a.replace('\n',newline);b=b.replace('\n',newline)
        if source.count(a)!=1:raise ValueError('integration anchor mismatch: '+a[:100])
        source=source.replace(a,b,1)
    replace('from concurrent.futures import ThreadPoolExecutor\n','''from concurrent.futures import ThreadPoolExecutor

# Opt-in resource integration; disabled for all existing deployments by default.
_BOT_RESOURCE_TEST_MODE = os.environ.get("BOT_RESOURCE_TEST_MODE") == "1"
_BOT_RESOURCE_HISTORY_TEST = _BOT_RESOURCE_TEST_MODE and os.environ.get("BOT_RESOURCE_HISTORY_TEST") == "1"
_BOT_RESOURCE_PUBLIC = frozenset(("stock_info_map", "monthly_revenue", "valuation", "chips_superman"))
_BOT_RESOURCE_HOST = None
_BOT_RESOURCE_HOST_LOCK = threading.Lock()
if _BOT_RESOURCE_TEST_MODE:
    from bot_resource_host import MeteredRequests
    requests = MeteredRequests(requests)


def _get_bot_resource_host():
    global _BOT_RESOURCE_HOST
    if not _BOT_RESOURCE_TEST_MODE:
        return None
    with _BOT_RESOURCE_HOST_LOCK:
        if _BOT_RESOURCE_HOST is None:
            from bot_resource_host import ResourceHost
            _BOT_RESOURCE_HOST = ResourceHost(get_db_connection, release_db_connection,
                _background_work_memory_safe, get_realtime_stocks_bulk)
        return _BOT_RESOURCE_HOST
''')
    replace('''def _save_shared_data_snapshot(snapshot_key, payload, data_date=None,
                               source_meta=None):
    """把可追溯的非即時資料保存到 Supabase；失敗不可阻塞原本流程。"""''','''def _save_shared_data_snapshot(snapshot_key, payload, data_date=None,
                               source_meta=None, *, resource_replay=False):
    """把可追溯的非即時資料保存到 Supabase；失敗不可阻塞原本流程。"""
    if globals().get("_BOT_RESOURCE_TEST_MODE", False) and snapshot_key in _BOT_RESOURCE_PUBLIC and isinstance(payload, dict) and payload:
        try:
            _get_bot_resource_host().writer.save(snapshot_key, payload, data_date, source_meta, replay=resource_replay)
            with _SHARED_SNAPSHOT_MEMORY_LOCK:
                _SHARED_SNAPSHOT_MEMORY_CACHE.pop(str(snapshot_key), None)
            return True
        except Exception as exc:
            print("共享快照測試寫入暫緩：" + type(exc).__name__, flush=True)
            return False
    if resource_replay:
        return False''')
    replace('''    """讀取 Supabase 共享快照；先查短 TTL 記憶體快取，過期才回資料庫。"""
    key = str(snapshot_key)''','''    """讀取 Supabase 共享快照；先查短 TTL 記憶體快取，過期才回資料庫。"""
    key = str(snapshot_key)
    if globals().get("_BOT_RESOURCE_TEST_MODE", False) and key in _BOT_RESOURCE_PUBLIC:
        try:
            return _get_bot_resource_host().snapshots.load(key, max_age=(
                _SHARED_SNAPSHOT_MAX_AGE.get(key, 86400) if max_age_seconds is None else float(max_age_seconds)))
        except Exception as exc:
            print("共享快照測試讀取暫緩：" + type(exc).__name__, flush=True)
            return None''')
    replace('''    """一次 DB round-trip 讀取多個共享快照，並預熱既有單筆讀取會使用的記憶體快取。"""
    keys = list(dict.fromkeys(str(k).strip() for k in (snapshot_keys or []) if str(k).strip()))
    if not keys:
        return {}''','''    """一次 DB round-trip 讀取多個共享快照，並預熱既有單筆讀取會使用的記憶體快取。"""
    keys = list(dict.fromkeys(str(k).strip() for k in (snapshot_keys or []) if str(k).strip()))
    if not keys:
        return {}
    if globals().get("_BOT_RESOURCE_TEST_MODE", False) and all(k in _BOT_RESOURCE_PUBLIC for k in keys):
        return {k: _load_shared_data_snapshot(k) for k in keys}''')
    replace('''def _review_price_map(codes, span):
    """Abort optional research between bounded batches; never publish partial statistics."""
    price_map = {}
    codes = list(dict.fromkeys(codes))''','''def _review_price_map(codes, span):
    """Abort optional research between bounded batches; never publish partial statistics."""
    price_map = {}
    codes = list(dict.fromkeys(codes))
    if (codes and globals().get("_BOT_RESOURCE_HISTORY_TEST", False)
            and _taiwan_post_close() and is_twse_trading_day(taiwan_today())):
        raw_markets = get_market_map()
        markets = {c: {"上市": ".TW", "listed": ".TW", "twse": ".TW", ".tw": ".TW",
                       "上櫃": ".TWO", "otc": ".TWO", "tpex": ".TWO", ".two": ".TWO"}.get(
                   str(raw_markets.get(c, "")).strip().lower()) for c in codes}
        return _get_bot_resource_host().review(codes, span, day=taiwan_today(), markets=markets)''')
    replace('''            result = fn()
            _job_mark_done(name, result, time.time() - t0)''','''            if globals().get("_BOT_RESOURCE_TEST_MODE", False) and name in ("預熱快取", "短線盤中更新檢查"):
                result = _get_bot_resource_host().optional_job(name, fn)
            else:
                result = fn()
            _job_mark_done(name, result, time.time() - t0)''')
    return source


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',default='main.py');parser.add_argument('--output',required=True)
    a=parser.parse_args();src=Path(a.source);out=Path(a.output)
    if src.resolve()==out.resolve() or out.exists():raise ValueError('output must be a new separate file')
    with src.open(encoding='utf-8',newline='') as f:source=f.read()
    result=patch_main(source);compile(result,str(out),'exec')
    with out.open('x',encoding='utf-8',newline='') as f:f.write(result)

if __name__=='__main__':main()
