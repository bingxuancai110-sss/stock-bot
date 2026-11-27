"""Persist short-term alerts and render saved market and personal changes."""
from datetime import timedelta
from contextlib import closing
from hashlib import sha256
from html import escape
import json
import threading
import time
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
_history_lock = threading.Lock()
_history_cache = None
BELL = ('<svg viewBox="0 0 24 24" width="21" height="21" fill="none" stroke="currentColor" '
        'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<path d="M18 8a6 6 0 0 0-12 0c0 7-2 7-2 9h16c0-2-2-2-2-9M10 21h4"/></svg>')


def notification_scope(uid):
    return sha256(f"stockbot-notices:{uid}".encode()).hexdigest()[:24]


def notification_id(day, kind, event):
    if event.get("recovered"):
        return ""
    if kind == "short":
        identity = (day, kind, event["direction"], event["code"], event["type"], event["provisional"])
    else:
        identity = (day, kind, event["title"], event.get("detail"))
    return sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:20]


def notification_ids(events):
    ids = dict.fromkeys(notification_id(*event) for event in events)
    ids.pop("", None)
    return " ".join(ids)


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


def recover_short_history(load_snapshot, memory_safe, today):
    """Read confirmed replay history from the latest saved closing snapshot."""
    global _history_cache
    if _history_cache and _history_cache[0] == today and time.monotonic() - _history_cache[1] < 120:
        return _history_cache[2], True
    if not _history_lock.acquire(blocking=False):
        return [], False
    try:
        if not memory_safe(reserve_mb=112):
            return [], False
        snap = load_snapshot()
        payload = (snap or {}).get("payload") or {}
        since = (today - timedelta(days=13)).isoformat()
        until = today.isoformat()
        events = []
        for direction, rows in (
            ("long", payload.get("all_rows", payload.get("rows", []))),
            ("short", payload.get("short_rows", [])),
        ):
            for row in rows:
                for event in row.get("signals", []):
                    day = event.get("date") or ""
                    action = ACTIONS[direction].get(event.get("type"))
                    if not action or not since <= day <= until or event.get("provisional"):
                        continue
                    record = {key: event.get(key) for key in ("price", "stop", "target", "type")}
                    record.update(code=row["code"], name=row.get("name"), direction=direction,
                                  action=action, provisional=False, recovered=True)
                    events.append((day, record))
        events.sort(key=lambda item: (item[0], item[1]["code"]), reverse=True)
        events = events[:120]
        _history_cache = (today, time.monotonic(), events)
        return events, True
    finally:
        _history_lock.release()


def merge_short_history(archived, recovered):
    def identity(day, item):
        return day, item["direction"], item["code"], item["type"], item["provisional"]
    merged = {identity(day, event): (day, event) for day, event in recovered}
    merged.update((identity(day, event), (day, event)) for day, event in archived)
    return sorted(merged.values(), key=lambda pair: (
        pair[0], pair[1].get("observed_at") or "", pair[1]["code"]), reverse=True)[:120]


def _price(value):
    return f"{float(value):,.2f}" if value is not None else "—"


def render_center(category, feeds, unavailable=(), history_ready=True, page=1):
    labels = {"all": "全部", **{key: label for key, (label, _) in CATEGORIES.items()}}
    counts = {key: min(len(feed), 120) for key, feed in feeds.items()}
    counts["all"] = sum(counts.values())
    tabs = "".join(
        '<a href="/web/notifications?kind={}" data-notice-tab="{}" data-haptic="none"{}>{}<small>{}</small></a>'.format(
            key, key, ' aria-current="page"' if key == category else "",
            label, "—" if key in unavailable else counts.get(key, 0))
        for key, label in labels.items()
    )
    events = [(day, kind, event) for kind, feed in feeds.items()
              for day, event in feed]
    events.sort(key=lambda row: (row[0], row[2].get("observed_at") or ""), reverse=True)
    panels = "".join(
        render_panel(kind, [row for row in events if kind == "all" or row[1] == kind],
                     labels, unavailable, history_ready, page if kind == category else 1,
                     visible=kind == category)
        for kind in labels
    )
    return f'''<style>{CENTER_CSS}</style><section class="notice-center" aria-label="通知台" data-notice-ids="{notification_ids(events)}">
      <div class="notice-center-head"><div><strong>最近 14 天</strong><small data-notice-count aria-live="polite">本分類 {counts.get(category, 0)} 筆</small></div>
      <div class="notice-tools"><button type="button" class="notice-mark-all" hidden>全部已讀</button>
      <a class="notice-refresh" href="/web/notifications?kind={category}&amp;refresh=1">↻ 重新整理</a></div></div>
      <nav class="notice-center-tabs" aria-label="通知分類">{tabs}</nav>
      {panels}
    </section>'''


def render_panel(category, events, labels, unavailable, history_ready, page, visible):
    page_size = 30
    pages = max(1, (len(events) + page_size - 1) // page_size)
    page = min(page, pages)
    page_start = (page - 1) * page_size
    cards = []
    last_day = None
    for day, kind, event in events[page_start:page_start + page_size]:
        if day != last_day:
            cards.append(f'<h2>{escape(day)}</h2>')
            last_day = day
        link = ""
        badge = labels[kind]
        if kind == "short":
            title = f'{event.get("name") or event["code"]} {event["code"]}'
            state = ("歷史重播" if event.get("recovered") else
                     "盤中暫定" if event["provisional"] else "收盤確認")
            observed = str(event.get("observed_at") or "")
            clock = observed[11:19]
            summary = f'{state} · {clock}' if clock else state
            badge = event["action"]
            levels = "".join(f'<div><dt>{caption}</dt><dd>{_price(event.get(key))}</dd></div>'
                             for caption, key in (("訊號價", "price"), ("硬停損", "stop"), ("目標參考", "target")))
            detail = f'<dl>{levels}</dl>'
            if event.get("spread") is not None and not event["provisional"]:
                detail += f'<p>每股價差 {float(event["spread"]):+,.2f}</p>'
            quote_link = "/web/market?" + urlencode({"code": event["code"]})
            link = f'<a class="notice-chart-link" href="{escape(quote_link)}">查看 K 線 ›</a>'
        else:
            title = event["title"]
            detail = f'<p>{escape(event.get("detail") or "")}</p>'
            summary = ""
        notice_id = notification_id(day, kind, event)
        cards.append(f'<article data-kind="{kind}" data-notice-id="{notice_id}"><div class="notice-card-meta"><b>{escape(badge)}</b>'
                     f'<small>{escape(summary)}</small></div><h3>{escape(title)}</h3>{detail}{link}</article>')
    notes = []
    relevant_errors = [key for key in unavailable if category in ("all", key)]
    if relevant_errors:
        missing = "、".join(labels[key] for key in relevant_errors)
        notes.append(f'{missing}暫時讀取失敗，可重新整理。')
    if not history_ready and category in ("all", "short"):
        notes.append('短線歷史快照暫未補讀，可重新整理。')
    if not cards and not notes:
        notes.append('這個分類最近 14 天沒有紀錄。')
    status = "".join(f'<p class="notice-center-empty" role="status">{escape(note)}</p>' for note in notes)
    source_link = ""
    if category != "all":
        label, source = CATEGORIES[category]
        source_link = f'<a class="notice-chart-link" href="{source}">開啟{label} ›</a>'
    pagination = ""
    if pages > 1:
        links = []
        for caption, number in (("‹ 上一頁", page - 1), ("下一頁 ›", page + 1)):
            if 1 <= number <= pages:
                links.append(f'<a href="/web/notifications?kind={category}&amp;page={number}">{caption}</a>')
        pagination = f'<nav class="notice-pagination" aria-label="通知分頁">第 {page} / {pages} 頁 {"".join(links)}</nav>'
    hidden = "" if visible else " hidden"
    return (f'<div data-notice-panel="{category}" data-count="{len(events)}" data-page="{page}" '
            f'data-notice-ids="{notification_ids(events)}"{hidden}>'
            f'{source_link}{status}{"".join(cards)}{pagination}</div>')


HEADER_CSS = """
html body#stockbot-app #stockbot-shell > .wrap > .app-header > .app-header-actions{
  position:relative;grid-area:account;align-self:center;justify-self:end;display:flex;align-items:center;
  height:46px;border:1px solid var(--ui-line,#d3dee5);border-radius:24px;
  background:var(--ui-surface,#fff);box-sizing:border-box
}
html body#stockbot-app #stockbot-shell > .wrap > .app-header > .app-header-actions > a{
  position:static!important;inset:auto!important;transform:none!important;
  display:grid!important;place-items:center;min-width:44px;height:44px;min-height:44px!important;
  padding:0!important;margin:0!important;border:0!important;box-shadow:none!important;
  background:transparent!important;color:var(--ui-ink,#18374a)
}
html body#stockbot-app #stockbot-shell > .wrap > .app-header > .app-header-actions > a + a{
  border-radius:0;border-inline-start:1px solid var(--ui-line,#d3dee5)!important
}
.app-header-actions .app-account-link::after,.app-header-actions .app-account-link span{display:none!important}
.app-header-actions a:focus-visible{outline:2px solid currentColor;outline-offset:-3px}
.app-notice-link .app-notice-dot{position:absolute;top:8px;right:7px;width:9px;height:9px;
  border-radius:50%;background:#d02139;box-shadow:0 0 0 2px var(--ui-surface,#fff);pointer-events:none}
.app-notice-link .app-notice-dot[hidden]{display:none!important}
"""

CENTER_CSS = """
.notice-center{max-width:720px;margin:0 auto;color:var(--ui-ink,#18374a)}
.notice-center-head{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:4px 2px}
.notice-center-head p,.notice-center h2,.notice-center article p,.notice-center article small{
  color:var(--ui-muted,#556d7e);font-size:.86rem
}
.notice-center a{font-weight:750;color:inherit}
.notice-center-tabs{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:5px;
  padding:5px;margin:10px 0 18px;border-radius:15px;background:var(--ui-surface,#fff);
  border:1px solid var(--ui-line,#d3dee5)}
.notice-center-tabs a{display:grid;place-items:center;min-height:44px;text-decoration:none;border-radius:10px;font-size:.9rem}
.notice-center-tabs small,.notice-center-head small{display:block;font-size:.76rem;opacity:.8}
.notice-center-head strong{font-size:1rem}
.notice-tools{display:flex;align-items:center;gap:6px}
.notice-tools .notice-mark-all[hidden]{display:none!important}
.notice-tools .notice-mark-all,.notice-refresh{padding:9px 10px;min-height:44px;display:inline-flex;
  align-items:center;box-sizing:border-box;font-size:.86rem;border:1px solid var(--ui-line,#d3dee5);border-radius:12px;
  background:var(--ui-surface,#fff);text-decoration:none}
.notice-center-tabs a[data-notice-tab]{position:relative}
.notice-center-tabs a[data-unread=true]::before{content:"";position:absolute;top:7px;right:6px;width:6px;height:6px;
  border-radius:50%;background:#d02139}
.notice-center [data-notice-panel][hidden]{display:none!important}
html body#stockbot-app #app-page-content .notice-center-tabs a[data-notice-tab]{
  background:transparent!important;color:var(--ui-ink,#18374a)!important;
  -webkit-text-fill-color:currentColor!important;-webkit-tap-highlight-color:transparent;
  box-shadow:none!important;border-color:transparent!important;filter:none!important;
  touch-action:manipulation
}
html body#stockbot-app #app-page-content .notice-center-tabs a[data-notice-tab][aria-current=page]{
  background:var(--ui-ink,#18374a)!important;color:var(--ui-surface,#fff)!important
}
html body#stockbot-app #app-page-content .notice-center-tabs a[data-notice-tab] small{
  color:inherit!important;-webkit-text-fill-color:currentColor!important;opacity:.86
}
html body#stockbot-app #app-page-content .notice-center-tabs a[data-notice-tab]:active{
  box-shadow:inset 0 0 0 2px currentColor!important
}
.notice-center-tabs a:focus-visible{outline:2px solid currentColor;outline-offset:-3px}
.notice-center h2{margin:20px 0 8px;padding-inline:3px}
.notice-center article,.notice-center-empty{margin:0 0 8px;padding:14px 16px;border-radius:15px;
  background:var(--ui-surface,#fff);border:1px solid var(--ui-line,#d3dee5)}
.notice-center article h3{margin:3px 0;font-size:1rem;color:inherit}
.notice-card-meta{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:7px}
.notice-card-meta b{font-size:.78rem;padding:3px 8px;border-radius:7px;background:#edf2f8;color:#295874}
article[data-kind=radar] .notice-card-meta b{background:#e8f2f0;color:#246459}
article[data-kind=blackhorse] .notice-card-meta b{background:#fbf0df;color:#805018}
article[data-kind=watchlist] .notice-card-meta b{background:#f0eafa;color:#62428a}
.notice-center dl{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin:10px 0 2px}
.notice-center dl div{background:var(--ui-page,#edf1f4);padding:6px;border-radius:8px}
.notice-center dt{font-size:.72rem;color:var(--ui-muted,#556d7e)}
.notice-center dd{margin:2px 0 0;font-size:.9rem;font-weight:750;font-variant-numeric:tabular-nums}
.notice-center article p{margin:6px 0 0;line-height:1.6;overflow-wrap:anywhere}
.notice-chart-link{display:inline-flex;align-items:center;min-height:44px;margin-bottom:-7px;font-size:.86rem}
.notice-pagination{display:flex;align-items:center;gap:14px;margin:12px 0}
.notice-pagination a{display:inline-flex;align-items:center;min-height:44px}
"""
