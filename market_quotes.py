"""Display quote provenance without treating HTTP fetch time as trade time."""
from datetime import datetime
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo('Asia/Taipei')


def quote_time(value):
    raw = str(value or '').strip()
    for fmt in ('%Y%m%d %H:%M:%S', '%Y%m%d %H:%M'):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=TW_TZ)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(raw)
        return dt.replace(tzinfo=TW_TZ) if dt.tzinfo is None else dt.astimezone(TW_TZ)
    except (ValueError, TypeError):
        return None


def prefer_quote(old, new):
    """A newly downloaded delayed bar must not roll back a more recent trade."""
    if not old:
        return True
    old_time = quote_time(old.get('quote_updated_at') or old.get('updated_at') or old.get('trade_at'))
    new_time = quote_time(new.get('quote_updated_at') or new.get('updated_at') or new.get('trade_at'))
    if old_time and new_time and old_time != new_time:
        return new_time > old_time
    old_official = 'MIS' in str(old.get('source', '')).upper()
    new_official = 'MIS' in str(new.get('source', '')).upper()
    if old_official != new_official:
        return new_official
    old_fetch = quote_time(old.get('fetched_at'))
    new_fetch = quote_time(new.get('fetched_at'))
    return old_fetch is None or (new_fetch is not None and new_fetch >= old_fetch)


def quote_freshness(quotes, now=None):
    now = now or datetime.now(TW_TZ)
    official, backup, unknown = [], [], 0
    for q in (quotes or {}).values():
        if not isinstance(q, dict) or not q.get('close'):
            continue
        source = str(q.get('source', ''))
        if 'Yahoo' in source:
            backup.append(q)
        elif 'MIS' in source.upper():
            official.append(q)
        else:
            unknown += 1
    parts = []
    if official:
        parts.append('MIS 官方報價 %s 檔' % len(official))
        ages = [max(0, int((now - dt).total_seconds())) for q in official
                if (dt := quote_time(q.get('fetched_at'))) is not None]
        if ages and max(ages) > 60 and 9 <= now.hour < 14:
            parts.append('部分官方報價已 %s 秒未重新確認' % max(ages))
    stamps = [dt for q in backup if (dt := quote_time(q.get('quote_updated_at') or q.get('updated_at'))) is not None]
    oldest = min(stamps) if stamps else None
    age = max(0, int((now - oldest).total_seconds())) if oldest else None
    if backup:
        parts.append('Yahoo 延遲備援 %s 檔' % len(backup))
        if oldest:
            parts.append('最舊備援行情 %s，約 %s 分鐘前' % (oldest.strftime('%m/%d %H:%M'), max(0, age // 60)))
        parts.append('重新抓取不代表成交時間已更新')
    if unknown:
        parts.append('%s 檔其他來源，時間請看個股明細' % unknown)
    return dict(official_count=len(official), backup_count=len(backup),
                oldest_backup_at=oldest.isoformat() if oldest else None,
                backup_age_seconds=age, poll_seconds=5,
                text='；'.join(parts) if parts else '持股報價尚待確認，完成後會自動顯示來源與時間。')
