"""Reclaim disposable caches before admitting a bounded market scan."""
import ctypes
import gc
import sys
import threading
import time

_lock = threading.Lock()
_last_reclaim = float('-inf')


def reclaim(namespace, now=None):
    """Never touch account, session, position or persisted snapshot data."""
    global _last_reclaim
    stamp = time.monotonic() if now is None else now
    if not _lock.acquire(blocking=False):
        return False
    try:
        if stamp - _last_reclaim < 60:
            return False
        _last_reclaim = stamp
        wall = time.time()
        # These entries are replaceable quote/history or rendered-page caches.
        for key, lock_key, keep, ttl, clock in (
            ('_realtime_cache', '_realtime_cache_lock', 128, namespace.get('REALTIME_CACHE_SECONDS', 300), wall),
            ('_SHARED_SNAPSHOT_MEMORY_CACHE', '_SHARED_SNAPSHOT_MEMORY_LOCK', 8, namespace.get('_SHARED_SNAPSHOT_MEMORY_TTL', 300), stamp),
            ('_WEB_FRAGMENT_CACHE', '_WEB_FRAGMENT_CACHE_LOCK', 16, None, stamp),
            ('_PORTFOLIO_TREND_HTML_CACHE', '_PORTFOLIO_TREND_HTML_CACHE_LOCK', 8, None, stamp),
        ):
            cache, guard = namespace.get(key), namespace.get(lock_key)
            if not isinstance(cache, dict) or guard is None:
                continue
            with guard:
                if ttl is not None:
                    for code in list(cache):
                        item = cache.get(code)
                        if isinstance(item, dict) and clock - float(item.get('at', clock)) > ttl:
                            cache.pop(code, None)
                for code in list(cache)[:-keep]:
                    cache.pop(code, None)
        cache, guard = namespace.get('_RADAR_SPARK_CACHE'), namespace.get('_RADAR_SPARK_CACHE_LOCK')
        if isinstance(cache, dict) and guard is not None:
            with guard:
                if wall - cache.get('at', 0) > namespace.get('RADAR_SPARK_CACHE_SECONDS', 15):
                    cache.update(at=0, day=None, data={})
        gc.collect()
        # Linux/glibc may retain freed HTTP/JSON buffers in its heap indefinitely.
        # malloc_trim only returns already-free pages; it does not free live data.
        if sys.platform.startswith('linux'):
            try:
                ctypes.CDLL(None).malloc_trim(0)
            except (AttributeError, OSError):
                pass
        return True
    finally:
        _lock.release()
