"""Single-owner server-side quote collector; credentials stay in server secrets.

No application import or scheduler side effects. The host supplies authenticated
holdings, private snapshot storage and a cross-process job lease.
"""
from datetime import datetime
import math
import hmac
import copy
import os
import hashlib
from contextlib import contextmanager
import threading
import time
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo('Asia/Taipei')
URL = 'https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/'


def quote_row(symbol, payload, now):
    """Retain real trade/reference timestamps; never turn trial data into a trade."""
    if not isinstance(payload, dict) or str(payload.get('symbol')) != symbol:
        return None
    if payload.get('date') != now.date().isoformat() or payload.get('type') == 'ODDLOT' or (symbol == 'IX0001' and payload.get('type') != 'INDEX'):
        return None
    trade = payload.get('lastTrade') or {}
    price = trade.get('price', payload.get('closePrice'))
    stamp = trade.get('time', payload.get('closeTime'))
    prior = payload.get('referencePrice') or payload.get('previousClose')
    if any(isinstance(value, bool) for value in (price, stamp, prior)):
        return None
    try:
        price, prior, stamp = float(price), float(prior), int(stamp)
        trade_at = datetime.fromtimestamp(stamp / 1_000_000, TZ)
        if not all(math.isfinite(value) and value > 0 for value in (price, prior, stamp)):
            return None
        if trade_at.date() != now.date() or (trade_at-now).total_seconds() > 60:
            return None
        if not 540 <= trade_at.hour*60+trade_at.minute <= 810:
            return None
        if now.hour*60+now.minute >= 810 and payload.get('isClose') is not True:
            return None
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    return dict(code=symbol, price=price, previous_close=prior, reference_price=prior,
                time=stamp, observed_at=now.timestamp()*1000,
                closed=payload.get('isClose') is True, mode='rest', type='INDEX' if symbol == 'IX0001' else 'EQUITY')


class Collector:
    """Bounded REST collector. One per configured owner, never one per page.

    At most 30 calls per rolling minute, including failures. A cycle has a
    25-second budget; the cursor advances even on failures so no ticker starves.
    The caller must persist completed snapshots and provide an exclusive lease.
    """
    def __init__(self, api_key, *, session=None, clock=time.monotonic):
        self._key = api_key
        self._session = session or requests.Session()
        self._clock = clock
        self._calls = []
        self._cooldown = 0
        self._cursor = 0
        self._lock = threading.Lock()

    def collect(self, codes, now, previous=None):
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._collect(codes, now, previous)
        finally:
            self._lock.release()

    def _collect(self, codes, now, previous):
        codes = sorted(set(str(code) for code in codes))
        # The host resolves this list from the owner's actual current positions.
        if any(not code.isalnum() or not 4 <= len(code) <= 7 for code in codes):
            raise ValueError('invalid holdings symbol')
        old = previous or {}
        quotes = {code: dict(row) for code, row in old.get('quotes', {}).items()
                  if code in codes and old.get('trading_date') == now.date().isoformat()}
        result = dict(trading_date=now.date().isoformat(), quotes=quotes,
                      checked_at=now.isoformat(), status='ready', requested=0, updated=0)
        if now.weekday() >= 5 or not 540 <= now.hour*60+now.minute <= 850:
            result['status'] = 'outside_session'
            return result
        started = self._clock()
        if started < self._cooldown:
            result['status'] = 'rate_limited'
            return result
        for offset in range(len(codes)):
            tick = self._clock()
            self._calls = [stamp for stamp in self._calls if tick-stamp < 60]
            if len(self._calls) >= 30 or tick-started >= 25:
                result['status'] = 'partial'
                break
            code = codes[self._cursor % len(codes)]
            self._cursor += 1
            self._calls.append(tick)
            result['requested'] += 1
            try:
                response = self._session.get(URL+code, headers={'X-API-KEY': self._key},
                                             timeout=(2, 3), allow_redirects=False)
                if response.status_code == 403 and code == 'IX0001':
                    result['status'] = 'partial'
                    continue
                if response.status_code in (401, 403):
                    # Stop credential/permission failures rather than hammering the API.
                    self._cooldown = tick+300
                    result['status'] = 'credential_or_permission_error'
                    break
                if response.status_code == 429:
                    try:
                        retry = max(60, float(response.headers.get('Retry-After', 60)))
                        if not math.isfinite(retry):
                            retry = 60
                    except (ValueError, TypeError):
                        retry = 60
                    self._cooldown = tick+retry
                    result['status'] = 'rate_limited'
                    break
                response.raise_for_status()
                row = quote_row(code, response.json(), now)
                if row:
                    quotes[code] = row
                    result['updated'] += 1
                else:
                    result['status'] = 'partial'
            except (requests.RequestException, ValueError, TypeError):
                # Do not log provider response bodies, headers or credentials.
                result['status'] = 'partial'
        return result


class BackgroundService:
    """Owner-scoped snapshots with a lease supplied by the existing database.

    The scheduler calls tick without an HTTP request. Readers never fetch quotes.
    Snapshot persistence must not include the API key or provider headers.
    """
    def __init__(self, owner, collector, *, holdings, load, save, lease):
        self._owner = str(owner)
        self._collector = collector
        self._holdings, self._load, self._save, self._lease = holdings, load, save, lease
        self._snapshot = None
        self._snapshot_lock = threading.Lock()

    def enabled_for(self, uid):
        return hmac.compare_digest(str(uid).encode(), self._owner.encode())

    def read(self, uid):
        if not self.enabled_for(uid):
            raise PermissionError('private quote snapshot')
        with self._snapshot_lock:
            snapshot = self._snapshot
        # A different worker may own the collector lease; always read persisted state.
        snapshot = self._load(self._owner) or snapshot or {}
        return copy.deepcopy(snapshot)

    def tick(self, now):
        # Skip before accessing DB or acquiring a lease outside the update window.
        if now.weekday() >= 5 or not 540 <= now.hour*60+now.minute <= 850:
            return 'outside_session'
        with self._lease() as claimed:
            if not claimed:
                return 'already_running'
            previous = self._load(self._owner) or {}
            checked = previous.get('checked_at')
            if checked:
                try:
                    age = (now-datetime.fromisoformat(checked)).total_seconds()
                    if 0 <= age < 55:
                        with self._snapshot_lock:
                            self._snapshot = copy.deepcopy(previous)
                        return 'already_updated'
                except (ValueError, TypeError):
                    pass
            codes = self._holdings(self._owner)
            if (codes and now.hour*60+now.minute >= 810 and
                    previous.get('trading_date') == now.date().isoformat() and
                    all((previous.get('quotes', {}).get(code) or {}).get('closed') is True
                        for code in codes)):
                with self._snapshot_lock:
                    self._snapshot = copy.deepcopy(previous)
                return 'final_close_saved'
            result = self._collector.collect(codes, now, previous)
            if result is None:
                return 'already_running'
            # Save first: a failed write must not masquerade as a persisted snapshot.
            self._save(self._owner, result)
            with self._snapshot_lock:
                self._snapshot = copy.deepcopy(result)
            return result['status']


def resolve_owner(get_connection, release_connection):
    """Reuse the existing exact UID/email owner allowlist; never guess an account."""
    owner = os.environ.get('FUGLE_TEST_USER_ID', '').strip()
    if owner:
        return owner
    email = os.environ.get('FUGLE_TEST_USER_EMAIL', '').strip().lower()
    if not email:
        return ''
    connection = None
    try:
        connection = get_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT DISTINCT user_id FROM user_auth_identities WHERE provider='google' AND LOWER(email)=%s LIMIT 2", (email,))
            rows = cursor.fetchall()
        return str(rows[0][0]) if len(rows) == 1 and rows[0][0] else ''
    except Exception:
        if connection is not None:
            try: connection.rollback()
            except Exception: pass
        return ''
    finally:
        if connection is not None:
            release_connection(connection)


def start_server_service(*, get_positions, merge_positions, get_connection,
                         release_connection, load_snapshot, save_snapshot, now):
    """Opt-in bootstrap. No key migration, API calls or thread when unconfigured.

    Configure the key using the host's protected environment-variable settings,
    not source code, browser storage inspection, logs or an unauthenticated URL.
    """
    key = os.environ.get('FUGLE_BACKGROUND_API_KEY', '').strip()
    if not key:
        print('Fugle background disabled: missing FUGLE_BACKGROUND_API_KEY', flush=True)
        return None
    owner = resolve_owner(get_connection, release_connection)
    if not owner:
        print('Fugle background disabled: owner configuration unresolved', flush=True)
        return None

    def snapshot_key(uid):
        return 'fugle_private_quotes:'+hashlib.sha256(str(uid).encode()).hexdigest()

    def load(uid):
        # Read authoritative state for the lease/cadence check; a worker-local
        # shared-snapshot cache can be five minutes behind another instance.
        connection = get_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT payload FROM shared_data_snapshots WHERE snapshot_key=%s',
                               (snapshot_key(uid),))
                row = cursor.fetchone()
                return row[0] if row and isinstance(row[0], dict) else {}
        finally:
            release_connection(connection)

    def save(uid, data):
        saved = save_snapshot(snapshot_key(uid), data, data_date=now().date(),
                              source_meta={'source': 'Fugle private background REST'})
        if not saved:
            raise RuntimeError('private quote snapshot persistence failed')

    def holdings(uid):
        codes = [str(row['code']).strip() for row in merge_positions(get_positions(uid))
                 if int(row.get('shares') or 0) > 0]
        return codes+['IX0001'] if codes else []

    @contextmanager
    def lease():
        connection = get_connection()
        claimed = False
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', (735104221,))
                claimed = bool(cursor.fetchone()[0])
            yield claimed
        finally:
            try:
                if claimed:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_advisory_unlock(%s)', (735104221,))
            finally:
                release_connection(connection)

    service = BackgroundService(owner, Collector(key), holdings=holdings,
                                load=load, save=save, lease=lease)
    def run():
        stop = threading.Event()
        # One worker only. Fetches do not overlap or enqueue during slow cycles.
        while True:
            try:
                status = service.tick(now())
                if status != 'outside_session':
                    print('Fugle background cycle status='+status, flush=True)
            except Exception as exc:
                print('Fugle background cycle failed: '+type(exc).__name__, flush=True)
            if stop.wait(60):
                break
    print('Fugle background collector enabled', flush=True)
    threading.Thread(target=run, name='fugle-private-quotes', daemon=True).start()
    return service
