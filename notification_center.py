"""Persist short-term alerts and render saved market and personal changes."""
from datetime import timedelta
from contextlib import closing
from html import escape
import json
from urllib.parse import urlencode


CATEGORIES = {
    "short": ("短線", "/web/workbench?tab=短線"),
    "radar": ("雷達", "/web/workbench?tab=雷達"),
    "blackhorse": ("黑馬", "/web/workbench?tab=黑馬"),
    "watchlist": ("自選股", "/web/watchlist"),
}
DAILY_CATEGORIES = {
    "radar": ("radar", "breakout"),
    "blackhorse": ("blackhorse",),
    "watchlist": ("watchlist", "watchlist_position"),
}
ACTIONS = {
    "long": {"entry": "進場", "exit": "出場", "add": "加碼", "reduce": "減碼"},
    "short": {"entry": "放空", "exit": "全部回補", "add": "加空", "reduce": "部分回補"},
}
SHORT_PREFIX = "notification-center:short:"
RADAR_PREFIX = "notification-center:radar:"
BELL = ('<svg viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M18 8a6 6 0 0 0-12 0c0 7-2 7-2 9h16c0-2-2-2-2-9M10 21h4"/></svg>')


def save_short_events(connect, snapshot, today):
    day = today.isoformat()
    events = {}
    for item in snapshot.get("alerts", []):
        direction = item.get("direction", "long")
        action = ACTIONS.get(direction, {}).get(item.get("type"))
        if item.get("date") != day or not action or not item.get("observed_at"):
            continue
        provisional = bool(item.get("provisional"))
        if provisional != bool(snapshot.get("intraday")):
            continue
        key = "|".join((direction, item["code"], item["type"], str(provisional)))
        event = {field: item.get(field) for field in (
            "name", "code", "stop", "target", "type", "observed_at", "signal_quote_at", "spread")}
        event.update(direction=direction, action=action, provisional=provisional,
                     price=item.get("first_signal_price", item.get("price")))
        events[key] = event
    save_events(connect, SHORT_PREFIX, today, events)


def save_events(connect, prefix, today, events):
    if not events:
        return
    with closing(connect()) as conn, conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO shared_data_snapshots (snapshot_key, data_date, payload)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (snapshot_key) DO UPDATE SET
                computed_at = NOW(),
                payload = EXCLUDED.payload || shared_data_snapshots.payload
            WHERE shared_data_snapshots.payload IS DISTINCT FROM
                  (EXCLUDED.payload || shared_data_snapshots.payload)
        """, (prefix + today.isoformat(), today, json.dumps(events, ensure_ascii=False)))


def read_events(cur, category, uid, today):
    since = today - timedelta(days=13)
    if category == "short":
        keys = [SHORT_PREFIX + (since + timedelta(days=i)).isoformat() for i in range(14)]
        cur.execute("""
            SELECT s.data_date, event.value FROM shared_data_snapshots s
            CROSS JOIN LATERAL jsonb_each(s.payload) AS event
            WHERE s.snapshot_key = ANY(%s)
            ORDER BY s.data_date DESC, event.value->>'observed_at' DESC, event.key
            LIMIT 120
        """, (keys,))
        return [(str(day), event) for day, event in cur.fetchall()]
    if category == "radar":
        keys = [RADAR_PREFIX + (since + timedelta(days=i)).isoformat() for i in range(14)]
        cur.execute("""
            SELECT event_day, title, detail FROM (
                SELECT snapshot_date AS event_day, NULL::text AS observed_at, title, detail, id::text AS event_id
                FROM premarket_events
                WHERE snapshot_date BETWEEN %s AND %s AND category = ANY(%s) AND user_id IS NULL
                UNION ALL
                SELECT s.data_date, event.value->>'observed_at', event.value->>'title',
                       event.value->>'detail', event.key
                FROM shared_data_snapshots s CROSS JOIN LATERAL jsonb_each(s.payload) AS event
                WHERE s.snapshot_key = ANY(%s)
            ) feed
            ORDER BY event_day DESC, observed_at DESC NULLS LAST, event_id DESC LIMIT 120
        """, (since, today, list(DAILY_CATEGORIES[category]), keys))
    else:
        cur.execute("""
        SELECT snapshot_date, title, detail FROM premarket_events
        WHERE snapshot_date BETWEEN %s AND %s AND category = ANY(%s)
          AND user_id IS NOT DISTINCT FROM %s
        ORDER BY snapshot_date DESC, id DESC LIMIT 120
        """, (since, today, list(DAILY_CATEGORIES[category]), str(uid) if category == "watchlist" else None))
    return [(str(day), {"title": title, "detail": detail}) for day, title, detail in cur.fetchall()]


def _price(value):
    return f"{float(value):,.2f}" if value is not None else "—"


def render_center(category, events, error=False):
    tabs = "".join(
        '<a href="/web/notifications?kind={}"{}>{}</a>'.format(
            key, ' aria-current="page"' if key == category else '', label)
        for key, (label, _) in CATEGORIES.items()
    )
    cards = []
    last_day = None
    label, source = CATEGORIES[category]
    for day, event in events:
        if day != last_day:
            cards.append(f'<h2>{escape(day)}</h2>')
            last_day = day
        link = ""
        if category == "short":
            title = f'{event.get("name") or event["code"]} {event["code"]}｜{event["action"]}'
            state = "盤中暫定紀錄" if event["provisional"] else "收盤確認"
            observed = str(event.get("observed_at") or "")
            clock = observed[11:19]
            summary = f'{state} · {clock}' if clock else state
            detail = f'訊號價 {_price(event["price"])} · 硬停損 {_price(event["stop"])} · 目標參考 {_price(event["target"])}'
            if event.get("spread") is not None and not event["provisional"]:
                detail += f' · 每股價差 {float(event["spread"]):+,.2f}'
            tag = f'<small>{escape(summary)}</small>'
            quote_link = "/web/market?" + urlencode({"code": event["code"]})
            link = f'<a class="notice-chart-link" href="{escape(quote_link)}">查看 K 線 ›</a>'
        else:
            title, detail = event["title"], event["detail"] or ""
            tag = ""
        cards.append(f'<article>{tag}<h3>{escape(title)}</h3><p>{escape(detail)}</p>{link}</article>')
    if error:
        cards.append('<p class="notice-center-empty" role="status">通知暫時無法讀取，請稍後重新整理。</p>')
    elif not cards:
        cards.append(f'<p class="notice-center-empty">最近 14 天沒有已保存的{label}通知。</p>')
    unit = "策略訊號紀錄" if category == "short" else "已保存的每日變化"
    if category == "radar":
        unit = "盤中與每日變化"
    return f'''<style>{CENTER_CSS}</style><section class="notice-center" aria-label="通知台">
      <div class="notice-center-head"><p>最近 14 天 · {unit}</p><a href="{source}">開啟{label} ›</a></div>
      <nav class="notice-center-tabs" aria-label="通知分類">{tabs}</nav>
      {''.join(cards)}
    </section>'''


HEADER_CSS = """
.app-header.has-notice-center > :is(h1,.eyebrow,.dateline){margin-inline:58px!important}
.app-header.has-notice-center > .app-account-link,
.app-header.has-notice-center > .app-notice-link{
  position:absolute!important;top:50%!important;transform:translateY(-50%)!important;
  display:grid!important;place-items:center;width:44px;height:44px;min-height:44px!important;
  padding:0!important;border-radius:50%;box-sizing:border-box;z-index:4
}
.app-header.has-notice-center > .app-account-link{left:4px!important;right:auto!important}
.app-header.has-notice-center > .app-account-link::after,
.app-header.has-notice-center > .app-account-link span{display:none!important}
.app-header.has-notice-center > .app-notice-link{
  right:4px;color:var(--ui-ink,#18374a);background:var(--ui-surface,#fff);
  border:1px solid var(--ui-line,#d3dee5);
}
.app-notice-link:focus-visible{outline:2px solid currentColor;outline-offset:3px}
"""

CENTER_CSS = """
.notice-center{max-width:720px;margin:0 auto;color:var(--ui-ink,#18374a)}
.notice-center-head{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:4px 2px}
.notice-center-head p,.notice-center h2,.notice-center article p,.notice-center article small{
  color:var(--ui-muted,#556d7e);font-size:.86rem
}
.notice-center a{font-weight:750;color:inherit}
.notice-center-tabs{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px;
  padding:5px;margin:10px 0 18px;border-radius:15px;background:var(--ui-surface,#fff);
  border:1px solid var(--ui-line,#d3dee5)}
.notice-center-tabs a{display:grid;place-items:center;min-height:44px;text-decoration:none;border-radius:10px;font-size:.9rem}
.notice-center-tabs a[aria-current=page]{background:var(--ui-ink,#18374a);color:var(--ui-surface,#fff)}
.notice-center h2{margin:20px 0 8px;padding-inline:3px}
.notice-center article,.notice-center-empty{margin:0 0 8px;padding:14px 16px;border-radius:15px;
  background:var(--ui-surface,#fff);border:1px solid var(--ui-line,#d3dee5)}
.notice-center article h3{margin:3px 0;font-size:1rem;color:inherit}
.notice-center article p{margin:6px 0 0;line-height:1.6;overflow-wrap:anywhere}
.notice-chart-link{display:inline-flex;align-items:center;min-height:44px;margin-bottom:-7px;font-size:.86rem}
"""
