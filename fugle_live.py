"""Ephemeral, owner-scoped browser quotes; no provider keys or quote snapshots are stored."""
from datetime import datetime
import math
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Taipei')


def normalize_quotes(raw, allowed, now, max_observation_age=120):
    if not isinstance(raw, list) or len(raw) > 60:
        raise ValueError('行情格式不正確')
    result = {}
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError('行情格式不正確')
        code = str(row.get('code', '')).strip().upper()
        if code not in allowed or code in result:
            raise ValueError('行情代號不在目前持股中或重複')
        if row.get('is_trial') or row.get('type') == 'ODDLOT' or any(isinstance(row.get(k),bool) for k in ('price','previous_close','reference_price','time','observed_at')):
            continue
        try:
            price, previous = float(row['price']), float(row.get('reference_price', row['previous_close']))
            micros, observed = float(row['time']), float(row['observed_at'])
            if not all(math.isfinite(x) for x in (price, previous, micros, observed)) or price <= 0 or previous <= 0:
                continue
            trade = datetime.fromtimestamp(micros / 1_000_000, TZ)
            seen = datetime.fromtimestamp(observed / 1000, TZ)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        if (trade.date() != now.date() or not 540 <= trade.hour*60+trade.minute <= 810 or
                (trade-now).total_seconds() > 60 or not -5 <= (now-seen).total_seconds() <= max_observation_age):
            continue
        if now.hour*60+now.minute >= 810 and row.get('closed') is not True:
            continue
        if not math.isfinite((price-previous)/previous*100):
            continue
        mode = '成交串流' if row.get('mode') == 'stream' else '定時查詢'
        result[code] = dict(close=price, previous_close=previous, pct=(price-previous)/previous*100,
                            close_date=trade.strftime('%Y%m%d'), close_time=trade.strftime('%H:%M:%S'),
                            updated_at=trade.strftime('%Y-%m-%d %H:%M:%S'), fetched_at=seen.strftime('%Y-%m-%d %H:%M:%S'),
                            has_live_trade_price=True, close_is_final=row.get('closed') is True,
                            source='富果'+mode+'（本機直連）')
        # Retain provider price limits; percentages alone cannot prove a limit hit.
        try:
            upper, lower = float(row.get('limit_up')), float(row.get('limit_down'))
            if (not isinstance(row.get('limit_up'), bool) and
                    not isinstance(row.get('limit_down'), bool) and
                    math.isfinite(upper) and math.isfinite(lower) and 0 < lower < upper):
                result[code].update(limit_up=upper, limit_down=lower, limit_known=True)
        except (TypeError, ValueError):
            pass
    return result


def normalize_market(raw, now, max_observation_age=120):
    """Only the actual TAIEX index may supply the portfolio benchmark."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get('code') != 'IX0001' or raw.get('type') != 'INDEX':
        raise ValueError('大盤指數格式不正確')
    quote = normalize_quotes([raw], {'IX0001'}, now, max_observation_age=max_observation_age).get('IX0001')
    if quote:
        quote['source'] = '富果加權指數（本機直連）'
    return quote
