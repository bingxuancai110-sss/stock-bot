"""Opt-in V3 market adapter. Importing does not start workers or access accounts."""
from copy import deepcopy
from datetime import datetime, timedelta
import hmac
import math
import re
from threading import Lock
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Taipei')
SYMBOL = re.compile(r'(?:[0-9]{4,6}[A-Z]?|IX0001)\Z')


class V3Market:
    def __init__(self, collector, universe, *, now):
        codes = frozenset(universe)
        if not 1 <= len(codes) <= 20 or any(not isinstance(c, str) or not SYMBOL.fullmatch(c) for c in codes):
            raise ValueError('invalid V3 universe')
        self.codes, self.now = codes, now
        self._collector, self._snapshot = collector, {}
        self._refresh_lock, self._lock = Lock(), Lock()

    def refresh(self):
        """Explicit scheduler entry only; HTTP readers never call the provider."""
        if not self._refresh_lock.acquire(blocking=False):
            return 'busy'
        try:
            now = self.now().astimezone(TZ)
            if now.weekday() >= 5 or not 540 <= now.hour*60+now.minute <= 850:
                return 'outside_session'
            result = self._collector.collect(self.codes, now, deepcopy(self._snapshot))
            if result is None:
                return 'busy'
            with self._lock:
                self._snapshot = deepcopy(result)
            return result.get('status', 'unknown')
        finally:
            self._refresh_lock.release()

    def read(self, symbols):
        now = self.now().astimezone(TZ)
        day, minute = now.date().isoformat(), now.hour*60+now.minute
        intraday = now.weekday() < 5 and 540 <= minute < 810
        with self._lock:
            snap = deepcopy(self._snapshot)
        rows = snap.get('quotes', {}) if snap.get('trading_date') == day else {}
        if not isinstance(rows, dict):
            rows = {}
        quotes, missing = {}, []
        for code in symbols:
            row = rows.get(code)
            try:
                if (not isinstance(row, dict) or row.get('code') != code or row.get('is_trial') or
                        row.get('type') == 'ODDLOT' or (code == 'IX0001' and row.get('type') != 'INDEX')):
                    raise ValueError()
                vals = [row[k] for k in ('price', 'previous_close', 'time', 'observed_at')]
                if any(isinstance(v, bool) for v in vals):
                    raise ValueError()
                price, prev, stamp, observed = map(float, vals)
                if not all(math.isfinite(v) and v > 0 for v in (price, prev, stamp, observed)):
                    raise ValueError()
                trade = datetime.fromtimestamp(stamp/1e6, TZ)
                seen = datetime.fromtimestamp(observed/1000, TZ)
                age, seen_age = (now-trade).total_seconds(), (now-seen).total_seconds()
                if (trade.date() != now.date() or seen.date() != now.date() or
                        not 540 <= trade.hour*60+trade.minute <= 810 or age < -5 or seen_age < -5 or
                        seen < trade-timedelta(seconds=5)):
                    raise ValueError()
                if intraday and age <= 180 and seen_age <= 180 and row.get('closed') is not True:
                    status = 'fresh_intraday'
                elif now.weekday() < 5 and minute >= 810 and row.get('closed') is True:
                    status = 'final_close'
                else:
                    raise ValueError()
                pct = (price/prev-1)*100
                if not math.isfinite(pct):
                    raise ValueError()
                quotes[code] = dict(price=price, previous_close=prev, change_pct=pct,
                                    trade_at=trade.isoformat(), observed_at=seen.isoformat(),
                                    status=status, source='Fugle V3 snapshot')
            except (ValueError, TypeError, KeyError, OverflowError, OSError):
                missing.append(code)
        return dict(as_of=now.isoformat(), complete=not missing, quotes=quotes, missing=missing,
                    execution_authorized=False), 200 if quotes else 503


def build_fugle_market(api_key, universe, *, now):
    """Use existing Collector; caller must provision isolated quota before live use."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError('missing provider credential')
    from fugle_background import Collector
    return V3Market(Collector(api_key), universe, now=now)


def register_v3_market_api(app, market, *, token):
    """Explicit Flask registration; no main.py change or automatic deployment."""
    from flask import request, jsonify
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError('V3 token must be at least 32 characters')
    @app.get('/api/v3/market/quotes')
    def v3_market_quotes():
        auth = request.headers.get('Authorization', '')
        if not hmac.compare_digest(auth.encode(), ('Bearer '+token).encode()):
            payload, status = {'error': 'unauthorized'}, 401
        else:
            raw = request.args.get('symbols', '')
            codes = list(dict.fromkeys(c.strip().upper() for c in raw.split(','))) if len(raw) <= 160 else []
            if not 1 <= len(codes) <= 20 or any(not SYMBOL.fullmatch(c) for c in codes):
                payload, status = {'error': 'invalid_symbols'}, 400
            elif not set(codes) <= market.codes:
                payload, status = {'error': 'outside_v3_universe'}, 403
            else:
                payload, status = market.read(codes)
        response = jsonify(payload)
        response.headers['Cache-Control'] = 'no-store'
        return response, status
