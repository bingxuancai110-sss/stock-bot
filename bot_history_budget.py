"""Opt-in public completed-day research cache; never starts a task or network client."""
from collections import OrderedDict, Counter
from datetime import date
import json
import math
import re
from threading import Lock
import time


class Deferred(RuntimeError):
    pass


def compact(row, code, completed_day):
    """Preserve raw and adjusted close series, not live prices or account fields."""
    if not isinstance(row, dict) or row.get('code', code) != code:
        raise Deferred('missing/mismatched history')
    dates, closes, adj = (row.get(k) for k in ('close_dates', 'closes', 'adj_closes'))
    if not all(isinstance(x, (list, tuple)) for x in (dates, closes, adj)) or not 2 <= len(dates) <= 400 or len(dates) != len(closes) or len(dates) != len(adj):
        raise Deferred('incomplete history')
    if any(type(d) is not date for d in dates) or list(dates) != sorted(set(dates)) or dates[-1] != completed_day:
        raise Deferred('wrong close date or ordering')
    if any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0 for x in (*closes, *adj)):
        raise Deferred('invalid price history')
    if row.get('close_is_final') is not True or row.get('close') != closes[-1]:
        raise Deferred('unconfirmed final close')
    return json.dumps({'code': code, 'close': closes[-1], 'close_dates': [d.isoformat() for d in dates], 'closes': closes, 'adj_closes': adj, 'close_is_final': True}, separators=(',', ':'), allow_nan=False).encode()


def restore(data):
    row = json.loads(data)
    row['close_dates'] = [date.fromisoformat(d) for d in row['close_dates']]
    return row


class HistoryBudget:
    """One instance per process, only for opt-in completed-close research jobs.
    Cache byte ceiling covers serialized payloads, not Python RSS or decoded results.
    A caller must provide the authoritative close date, source revision, public
    market map, history validator and existing memory admission function.
    """
    def __init__(self, fetch, safe, verify, *, max_bytes=2*1024**2, max_items=200, ttl=900, clock=time.monotonic):
        if not 0 < max_bytes <= 8*1024**2 or not 0 < max_items <= 1000 or not 0 < ttl <= 3600:
            raise ValueError('invalid cache limits')
        self.fetch, self.safe, self.verify = fetch, safe, verify
        self.max_bytes, self.max_items, self.ttl, self.clock = max_bytes, max_items, ttl, clock
        self._lock, self._job = Lock(), Lock()
        self._cache, self._stats = OrderedDict(), Counter()
        self._bytes, self._retry = 0, 0

    def stats(self):
        with self._lock:
            return dict(self._stats, cache_bytes=self._bytes, cache_items=len(self._cache))

    def _count(self, name, n=1):
        with self._lock: self._stats[name] += n

    def _get(self, key):
        with self._lock:
            item = self._cache.pop(key, None)
            if item:
                at, data = item
                if 0 <= self.clock()-at < self.ttl:
                    self._cache[key] = item
                    self._stats['hits'] += 1
                    return data
                self._bytes -= len(data)
            self._stats['misses'] += 1
            return None

    def _put(self, key, data):
        with self._lock:
            if len(data) > self.max_bytes: return
            old = self._cache.pop(key, None)
            if old: self._bytes -= len(old[1])
            self._cache[key] = (self.clock(), data)
            self._bytes += len(data)
            while self._bytes > self.max_bytes or len(self._cache) > self.max_items:
                _, (_, removed) = self._cache.popitem(last=False)
                self._bytes -= len(removed)
                self._stats['evictions'] += 1

    def clear(self):
        """Nonblocking reclamation; don't race an active research batch."""
        if not self._job.acquire(blocking=False): return False
        try:
            with self._lock:
                self._cache.clear(); self._bytes = 0
            return True
        finally: self._job.release()

    def load(self, codes, *, span, completed_day, markets, source_revision):
        """Complete result or Deferred. Preserve successful per-symbol cache on retry.
        Never for intraday, account holdings, full OHLCV or order-book consumers.
        """
        codes = list(dict.fromkeys(codes))
        if (type(completed_day) is not date or span not in ('3mo','6mo','1y') or
            not isinstance(source_revision, str) or not 1 <= len(source_revision) <= 80 or
            len(codes) > 200 or any(not isinstance(c,str) or not re.fullmatch(r'[0-9]{4,6}[A-Z]?',c) or markets.get(c) not in ('.TW','.TWO') for c in codes)):
            raise ValueError('invalid history request')
        if not self._job.acquire(blocking=False):
            self._count('busy'); raise Deferred('another history job active')
        try:
            def key(c): return (source_revision, completed_day, span, markets[c], c)
            result = {}; missing = []; total = 0
            def add(c,data):
                nonlocal total
                total += len(data)
                if total > self.max_bytes: raise Deferred('result budget exceeded')
                result[c] = data
            def guard():
                if not self.safe(reserve_mb=160):
                    self._count('memory_deferred'); raise Deferred('memory headroom unavailable')
            guard()
            for c in codes:
                data = self._get(key(c))
                if data is None: missing.append(c)
                else: add(c,data)
            if missing and self.clock() < self._retry:
                self._count('cooldown_deferred'); raise Deferred('provider cooldown')
            for i in range(0,len(missing),4):
                guard(); batch=missing[i:i+4]
                self._count('bulk_calls'); self._count('requested_symbols',len(batch))
                try:
                    rows=self.fetch(batch,workers=1,rng=span,market_suffix={c:markets[c] for c in batch},skip_mis=True,request_timeout=5)
                    if not isinstance(rows,dict): raise Deferred('invalid provider result')
                    guard()
                    for c in batch:
                        row=rows.get(c)
                        if not self.verify(c,row,completed_day,markets[c],span): raise Deferred('unverified historical coverage')
                        data=compact(row,c,completed_day); self._put(key(c),data); add(c,data)
                except Exception as exc:
                    self._retry=self.clock()+60
                    self._count('batch_deferred')
                    raise Deferred('history batch incomplete') from exc
            guard()
            return {c:restore(result[c]) for c in codes}
        finally: self._job.release()
