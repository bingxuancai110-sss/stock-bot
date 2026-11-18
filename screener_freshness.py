"""Separate institution date, quote date and calculation time."""
from datetime import date, datetime, timedelta, timezone

POLICY = 1


def date_text(value):
    if isinstance(value, (datetime,date)):
        return value.date().isoformat() if isinstance(value,datetime) else value.isoformat()
    text=str(value or '')[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def quote_date(quote):
    observed=quote.get('market_observed_at')
    if observed:
        try:
            return datetime.fromtimestamp(float(observed),timezone(timedelta(hours=8))).date().isoformat()
        except (ValueError,TypeError,OverflowError,OSError):
            pass
    days=quote.get('close_dates') or []
    return date_text(days[-1]) if days else None


def blackhorse_due(snapshot, now, in_market, calendar, institution_date=None):
    if not snapshot:
        return True,None
    try:
        computed=datetime.fromisoformat(str(snapshot.get('computed_at')).replace('Z','+00:00'))
        if computed.tzinfo is None:computed=computed.replace(tzinfo=timezone.utc)
        current=now if now.tzinfo else now.replace(tzinfo=timezone(timedelta(hours=8)))
        age=(current-computed).total_seconds()
    except (ValueError,TypeError):
        return True,None
    meta=snapshot.get('source_meta') or {}
    expected=now.date()
    if now.hour<9:expected-=timedelta(days=1)
    for _ in range(20):
        if calendar(expected):break
        expected-=timedelta(days=1)
    old_quote=date_text(meta.get('quote_date'))
    old_inst=date_text(snapshot.get('source_date'))
    latest_inst=date_text(institution_date)
    due=(age<0 or meta.get('blackhorse_policy')!=POLICY or
         not old_quote or old_quote!=expected.isoformat() or
         bool(latest_inst and old_inst!=latest_inst) or
         bool((in_market or (now.hour>=14 and calendar(now.date()))) and age>=1800))
    return bool(due),max(0,age)
