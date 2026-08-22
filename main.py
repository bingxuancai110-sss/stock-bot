import os
import re
import html
import base64
import ssl
import socket
import time
import threading
import requests
from requests.adapters import HTTPAdapter
import psycopg2
from psycopg2 import pool
import psycopg2.extensions
from psycopg2.extras import execute_values
from urllib.parse import quote, urlparse
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (MessageEvent, TextMessage, TextSendMessage,
                            FlexSendMessage, FollowEvent,
                            QuickReply, QuickReplyButton, MessageAction)
from datetime import datetime, timedelta, timezone, date
from concurrent.futures import ThreadPoolExecutor

TW_TZ = timezone(timedelta(hours=8))


def taiwan_now():
    return datetime.now(TW_TZ)


def taiwan_today():
    return taiwan_now().date()


def next_taiwan_trading_day(source_date):
    """由資料日取得下一個平日顯示日；週五會跳到下週一。"""
    d = source_date + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def as_taiwan_datetime(value):
    """將 PostgreSQL 回傳的 aware 或舊版 naive 時間統一成台灣時區。"""
    if not value:
        return None
    if getattr(value, "tzinfo", None):
        return value.astimezone(TW_TZ)
    # 舊欄位是 TIMESTAMP WITHOUT TIME ZONE；migration 以前的值按既有 UTC 資料處理。
    return value.replace(tzinfo=timezone.utc).astimezone(TW_TZ)
import random
import math
import json
from collections import defaultdict

# ===== 每日盤前變化偵測（內嵌版） =====
CHANGE_LEVEL = {"S": 4, "A": 3, "B": 2, "C": 1}
LEVEL_LABEL = {"S": "重大變化", "A": "明顯變化", "B": "一般變化", "C": "無明顯變化"}


def configure_daily_change_detector(**dependencies):
    """把既有單檔 bot 的函式注入本模組，避免複製資料抓取與評分邏輯。"""
    globals().update(dependencies)


def _require_dependencies():
    required = ("get_db_connection", "release_db_connection", "compute_screener_rows",
                "fetch_taiex_summary", "fetch_quotes_bulk", "fetch_stock_news",
                "get_user_watchlist", "compute_watchlist_scores", "get_notify_users",
                "get_all_watchlist_user_ids", "stock_display_name")
    missing = [name for name in required if name not in globals()]
    if missing:
        raise RuntimeError("每日盤前變化偵測尚未注入既有 bot 函式：" + ", ".join(missing))



def init_premarket_change_tables():
    _require_dependencies()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS premarket_snapshots (
            snapshot_date DATE PRIMARY KEY,
            previous_trade_date DATE,
            blackhorse JSONB NOT NULL DEFAULT '[]'::jsonb,
            radar JSONB NOT NULL DEFAULT '[]'::jsonb,
            market JSONB NOT NULL DEFAULT '{}'::jsonb,
            news JSONB NOT NULL DEFAULT '[]'::jsonb,
            institutional JSONB NOT NULL DEFAULT '{}'::jsonb,
            briefing_date DATE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS premarket_events (
            id BIGSERIAL PRIMARY KEY,
            snapshot_date DATE NOT NULL,
            user_id TEXT,
            severity CHAR(1) NOT NULL CHECK (severity IN ('S','A','B','C')),
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            detail TEXT NOT NULL,
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            event_key TEXT NOT NULL,
            briefing_date DATE,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(snapshot_date, user_id, event_key)
        )
        """)
        cur.execute("ALTER TABLE premarket_snapshots ADD COLUMN IF NOT EXISTS institutional JSONB NOT NULL DEFAULT '{}'::jsonb")
        cur.execute("ALTER TABLE premarket_snapshots ADD COLUMN IF NOT EXISTS briefing_date DATE")
        cur.execute("ALTER TABLE premarket_events ADD COLUMN IF NOT EXISTS briefing_date DATE")
        # 舊資料以平日規則補上顯示日；新資料由程式明確寫入下一交易日。
        cur.execute("""
            UPDATE premarket_snapshots
            SET briefing_date = CASE EXTRACT(ISODOW FROM snapshot_date)
                WHEN 5 THEN snapshot_date + 3
                WHEN 6 THEN snapshot_date + 2
                ELSE snapshot_date + 1
            END
            WHERE briefing_date IS NULL
        """)
        cur.execute("""
            UPDATE premarket_events
            SET briefing_date = CASE EXTRACT(ISODOW FROM snapshot_date)
                WHEN 5 THEN snapshot_date + 3
                WHEN 6 THEN snapshot_date + 2
                ELSE snapshot_date + 1
            END
            WHERE briefing_date IS NULL
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_premarket_events_date ON premarket_events(snapshot_date, severity)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_premarket_events_briefing ON premarket_events(briefing_date, severity)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_premarket_snapshots_briefing ON premarket_snapshots(briefing_date)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def _jsonable(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _trade_date(rows):
    dates = []
    for row in rows or []:
        for key in ("trade_date", "date", "data_date"):
            if row.get(key):
                try:
                    dates.append(date.fromisoformat(str(row[key])[:10]))
                except ValueError:
                    pass
    return max(dates) if dates else None


def _normalise_pick_rows(rows, mode):
    result = []
    for rank, row in enumerate(rows or [], 1):
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        result.append({
            "code": code,
            "name": row.get("name") or stock_display_name(code, fallback=code),
            "rank": int(row.get("rank") or rank),
            "score": row.get("score"),
            "breakout": row.get("breakout") or "",
            "vol_ratio": row.get("vol_ratio"),
            "streak": row.get("streak"),
            "pct": row.get("pct"),
            "mode": mode,
        })
    return result


def _get_previous_snapshot(snapshot_date):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT snapshot_date, previous_trade_date, blackhorse, radar, market, news, institutional
            FROM premarket_snapshots
            WHERE snapshot_date < %s ORDER BY snapshot_date DESC LIMIT 1
        """, (snapshot_date,))
        row = cur.fetchone()
        if not row:
            return None
        return {"snapshot_date": row[0], "previous_trade_date": row[1],
                "blackhorse": row[2] or [], "radar": row[3] or [],
                "market": row[4] or {}, "news": row[5] or [],
                "institutional": row[6] or {}}
    finally:
        release_db_connection(conn)


def _save_snapshot(snapshot):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO premarket_snapshots
                (snapshot_date, previous_trade_date, blackhorse, radar, market, news, institutional, briefing_date)
            VALUES (%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s)
            ON CONFLICT (snapshot_date) DO UPDATE SET
                previous_trade_date=EXCLUDED.previous_trade_date,
                blackhorse=EXCLUDED.blackhorse, radar=EXCLUDED.radar,
                market=EXCLUDED.market, news=EXCLUDED.news,
                institutional=EXCLUDED.institutional,
                briefing_date=EXCLUDED.briefing_date, created_at=NOW()
        """, (snapshot["snapshot_date"], snapshot.get("previous_trade_date"),
              json.dumps(_jsonable(snapshot["blackhorse"]), ensure_ascii=False),
              json.dumps(_jsonable(snapshot["radar"]), ensure_ascii=False),
              json.dumps(_jsonable(snapshot["market"]), ensure_ascii=False),
              json.dumps(_jsonable(snapshot["news"]), ensure_ascii=False),
              json.dumps(_jsonable(snapshot.get("institutional", {})), ensure_ascii=False),
              snapshot.get("briefing_date")))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def _save_events(snapshot_date, user_id, events, briefing_date=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM premarket_events WHERE snapshot_date=%s AND user_id IS NOT DISTINCT FROM %s",
                    (snapshot_date, user_id))
        for event in events:
            cur.execute("""
                INSERT INTO premarket_events
                  (snapshot_date,user_id,severity,category,title,detail,evidence,event_key,briefing_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                ON CONFLICT (snapshot_date,user_id,event_key) DO UPDATE SET
                  severity=EXCLUDED.severity, category=EXCLUDED.category,
                  title=EXCLUDED.title, detail=EXCLUDED.detail,
                  evidence=EXCLUDED.evidence, briefing_date=EXCLUDED.briefing_date
            """, (snapshot_date, user_id, event["severity"], event["category"],
                  event["title"], event["detail"],
                  json.dumps(_jsonable(event.get("evidence", {})), ensure_ascii=False),
                  event["event_key"], briefing_date))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def _event(level, category, title, detail, key, evidence=None):
    return {"severity": level, "category": category, "title": title,
            "detail": detail, "event_key": key, "evidence": evidence or {}}


def _pick_events(today, yesterday):
    old = {x["code"]: x for x in yesterday or []}
    new = {x["code"]: x for x in today or []}
    events = []
    old_top = (yesterday or [])[:5]
    new_top = (today or [])[:5]
    old1 = old_top[0] if old_top else None
    new1 = new_top[0] if new_top else None
    if old1 and new1 and old1["code"] != new1["code"]:
        events.append(_event("S", "blackhorse", f"黑馬 #1 換人：{new1['name']}",
            f"{old1['name']} 今日由 #1 變為 #{old1.get('rank','?')}，{new1['name']} 上升至 #1。",
            "blackhorse_no1_changed", {"old": old1, "new": new1}))
    for code in sorted(set(new) & set(old)):
        delta_rank = (old[code].get("rank") or 0) - (new[code].get("rank") or 0)
        old_score, new_score = old[code].get("score"), new[code].get("score")
        if old_score is not None and new_score is not None:
            delta_score = new_score - old_score
            if abs(delta_score) >= 15:
                events.append(_event("S", "blackhorse", f"{new[code]['name']} 黑馬分數 {old_score} → {new_score}",
                    "黑馬分數單日變化達 15 分以上。", f"blackhorse_score_{code}", {"code": code, "old": old_score, "new": new_score}))
            elif abs(delta_score) >= 8:
                events.append(_event("A", "blackhorse", f"{new[code]['name']} 黑馬分數 {old_score} → {new_score}",
                    "黑馬分數出現明顯變化。", f"blackhorse_score_{code}", {"code": code, "old": old_score, "new": new_score}))
            elif abs(delta_score) >= 3:
                events.append(_event("B", "blackhorse", f"{new[code]['name']} 黑馬分數 {old_score} → {new_score}",
                    "黑馬分數相較前一交易日有一般變化。", f"blackhorse_score_{code}", {"code": code, "old": old_score, "new": new_score}))
        if abs(delta_rank) >= 1:
            level = "A" if abs(delta_rank) >= 3 else "B"
            events.append(_event(level, "blackhorse", f"{new[code]['name']} 黑馬排名上升 {delta_rank} 名" if delta_rank > 0 else f"{new[code]['name']} 黑馬排名下降 {abs(delta_rank)} 名",
                "排名相較前一交易日有變化。", f"blackhorse_rank_{code}", {"code": code, "old": old[code].get("rank"), "new": new[code].get("rank")}))
    entered = sorted(set(new) - set(old), key=lambda c: new[c].get("rank") or 999)
    exited = sorted(set(old) - set(new), key=lambda c: old[c].get("rank") or 999)
    if entered:
        level = "S" if len(entered) >= 3 else "A"
        names = "、".join(new[c]["name"] for c in entered[:3])
        events.append(_event(level, "blackhorse", f"今日新增 {len(entered)} 檔黑馬", names, "blackhorse_entered", {"codes": entered}))
    if exited:
        level = "S" if len(exited) >= 3 else "A"
        names = "、".join(old[c]["name"] for c in exited[:3])
        events.append(_event(level, "blackhorse", f"今日掉出 {len(exited)} 檔黑馬", names, "blackhorse_exited", {"codes": exited}))
    return events


def _radar_events(today, yesterday):
    old = {x["code"]: x for x in yesterday or []}
    new = {x["code"]: x for x in today or []}
    events = []
    entered = sorted(set(new) - set(old))
    if entered:
        names = "、".join(new[c]["name"] for c in entered[:4])
        events.append(_event("A" if len(entered) < 3 else "S", "radar", f"雷達新增 {len(entered)} 檔訊號", names, "radar_entered", {"codes": entered}))
    for code in sorted(set(new) & set(old)):
        before, after = old[code].get("breakout"), new[code].get("breakout")
        if before != after and after:
            events.append(_event("S" if "跌破" in after else "A", "breakout", f"{new[code]['name']} 出現{after}",
                "今日雷達型態狀態與前一交易日不同。", f"breakout_{code}", {"code": code, "old": before, "new": after}))
    return events


def _institutional_events(current, previous):
    events = []
    old = previous or {}
    for code in sorted(set(current) & set(old)):
        before, after = old[code], current[code]
        old_dir = (before.get("total_net_lots") or 0) > 0
        new_dir = (after.get("total_net_lots") or 0) > 0
        if (before.get("total_net_lots") or 0) != 0 and (after.get("total_net_lots") or 0) != 0 and old_dir != new_dir:
            direction = "買超轉賣超" if old_dir else "賣超轉買超"
            events.append(_event("S", "institutional", f"{after.get('name', code)} 法人方向：{direction}",
                "三大法人合計方向相較前一交易日反轉。", f"institutional_direction_{code}", {"code": code, "old": before, "new": after}))
        old_streak = before.get("streak")
        new_streak = after.get("streak")
        if old_streak is not None and new_streak is not None and abs(new_streak - old_streak) >= 1:
            side = "買超" if (after.get("total_net_lots") or 0) > 0 else "賣超"
            events.append(_event("A", "institutional", f"{after.get('name', code)} 法人連續{side} {old_streak} → {new_streak} 日",
                "連續買超／賣超天數相較前一交易日有變化。", f"institutional_streak_{code}", {"code": code, "old": old_streak, "new": new_streak}))
    return events


def _market_events(market, old_market):
    events = []
    for label, key in (("台股大盤", "taiex_pct"), ("道瓊", "^DJI_pct"), ("那斯達克", "^IXIC_pct"), ("S&P 500", "^GSPC_pct"), ("費城半導體", "^SOX_pct")):
        cur, old = market.get(key), (old_market or {}).get(key)
        if cur is None or old is None:
            continue
        delta = cur - old
        if abs(delta) >= 2:
            events.append(_event("S", "market", f"{label} 變化 {old:+.2f}% → {cur:+.2f}%", "大盤或美股主要指數出現重大日間變化。", f"market_{key}", {"old": old, "new": cur}))
        elif abs(delta) >= 1:
            events.append(_event("A", "market", f"{label} 變化 {old:+.2f}% → {cur:+.2f}%", "指數方向相較前一交易日明顯改變。", f"market_{key}", {"old": old, "new": cur}))
        elif abs(delta) >= 0.5:
            events.append(_event("B", "market", f"{label} 變化 {old:+.2f}% → {cur:+.2f}%", "指數相較前一交易日有一般變化。", f"market_{key}", {"old": old, "new": cur}))
    return events


def _news_events(news, old_news):
    events = []
    count_delta = len(news or []) - len(old_news or [])
    if count_delta >= 3:
        events.append(_event("A", "news", f"相關新聞增加 {count_delta} 則", "僅統計現有新聞來源回傳的標題數量。", "news_count", {"old": len(old_news or []), "new": len(news or [])}))
    important_words = ("台積電", "聯準會", "CPI", "非農", "關稅", "制裁", "併購", "財報", "法說", "停牌", "重大", "警示")
    matched = [n.get("title", "") for n in news or [] if any(w.lower() in n.get("title", "").lower() for w in important_words)]
    if matched:
        events.append(_event("S", "news", "出現重大相關新聞標題", "以下僅列現有來源回傳的標題，不自行生成解讀。", "news_important", {"titles": matched[:5]}))
    elif count_delta:
        events.append(_event("B", "news", f"相關新聞數量 {len(old_news or [])} → {len(news or [])}", "新聞數量有變化，但未命中規則式重大關鍵字。", "news_count_minor", {"old": len(old_news or []), "new": len(news or [])}))
    return events


def _current_market():
    market = {}
    taiex = fetch_taiex_summary() or {}
    for key in ("pct", "change_pct", "percent"):
        if taiex.get(key) is not None:
            market["taiex_pct"] = float(taiex[key]); break
    symbols = ["^DJI", "^IXIC", "^GSPC", "^SOX"]
    quotes = fetch_quotes_bulk(symbols) or {}
    for symbol in symbols:
        q = quotes.get(symbol)
        if isinstance(q, dict):
            pct = q.get("pct")
        elif isinstance(q, (tuple, list)) and len(q) >= 2:
            # fetch_quotes_bulk() 的既有格式是 (close, pct, diff)，
            # 與部分其他報價 helper 的 dict 格式不同；盤前偵測要兼容兩者。
            pct = q[1]
        else:
            pct = None
        if pct is not None:
            market[f"{symbol}_pct"] = float(pct)
    return market


def collect_daily_snapshot(snapshot_date=None):
    _require_dependencies()
    snapshot_date = snapshot_date or taiwan_today()
    blackhorse_rows, _, _ = compute_screener_rows("blackhorse")
    radar_rows, _, _ = compute_screener_rows("radar")
    blackhorse = _normalise_pick_rows(sorted(blackhorse_rows or [], key=lambda x: x.get("score") if x.get("score") is not None else -1, reverse=True), "blackhorse")[:20]
    radar = _normalise_pick_rows(radar_rows, "radar")[:50]
    market = _current_market()
    news = fetch_stock_news("台股 OR 台積電 OR 聯準會", max_items=20, within_hours=36) or []
    institutional = {}
    if "fetch_institutional_data" in globals():
        raw_inst = fetch_institutional_data() or {}
        relevant = {x["code"] for x in blackhorse + radar}
        for code in relevant:
            if code in raw_inst:
                item = dict(raw_inst[code])
                if "get_consecutive_days" in globals():
                    item["streak"] = max(get_consecutive_days(code, "buy"), get_consecutive_days(code, "sell"))
                institutional[code] = item
    previous = _get_previous_snapshot(snapshot_date)
    prev_date = previous["snapshot_date"] if previous else None
    snapshot = {"snapshot_date": snapshot_date, "briefing_date": next_taiwan_trading_day(snapshot_date),
                "previous_trade_date": prev_date, "blackhorse": blackhorse, "radar": radar, "market": market,
                "news": news, "institutional": institutional}
    _save_snapshot(snapshot)
    return snapshot, previous


def build_global_events(snapshot, previous):
    if not previous:
        return []
    events = []
    events += _pick_events(snapshot["blackhorse"], previous.get("blackhorse"))
    events += _radar_events(snapshot["radar"], previous.get("radar"))
    events += _market_events(snapshot["market"], previous.get("market"))
    events += _institutional_events(snapshot.get("institutional", {}), previous.get("institutional", {}))
    events += _news_events(snapshot["news"], previous.get("news"))
    return events


def _watchlist_events(user_id, snapshot_date, previous_date):
    _require_dependencies()
    codes = get_user_watchlist(user_id) or []
    if not codes or not previous_date:
        return []
    current = compute_watchlist_scores(codes) or {}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
          SELECT code,total,position,close FROM watchlist_scores
          WHERE user_id=%s AND snapshot_date=%s
        """, (str(user_id), previous_date))
        old = {r[0]: {"total": r[1], "position": r[2], "close": r[3]} for r in cur.fetchall()}
    finally:
        release_db_connection(conn)
    events = []
    for code, row in current.items():
        if code not in old:
            continue
        before, after = old[code], row
        delta = (after.get("total") or 0) - (before.get("total") or 0)
        name = row.get("stock", {}).get("name") or stock_display_name(code, fallback=code)
        if abs(delta) >= 10:
            events.append(_event("S", "watchlist", f"你的{name}分數 {before['total']} → {after['total']}", "自選股綜合分數出現重大變化。", f"watch_score_{code}", {"code": code, "old": before, "new": after}))
        elif abs(delta) >= 5:
            events.append(_event("A", "watchlist", f"你的{name}分數 {before['total']} → {after['total']}", "自選股綜合分數出現明顯變化。", f"watch_score_{code}", {"code": code, "old": before, "new": after}))
        # 支撐／壓力狀態以既有即時報價函式的真實旗標判斷；缺資料就不產生事件。
        price = (row.get("stock") or {}).get("close")
        old_price = before.get("close")
        if price is not None and old_price is not None:
            old_pos, new_pos = before.get("position"), after.get("position")
            if old_pos is not None and new_pos is not None and old_pos != new_pos:
                events.append(_event("A", "watchlist_position", f"你的{name}支撐／壓力狀態變化", "位階分數相較前一交易日不同。", f"watch_position_{code}", {"code": code, "old": old_pos, "new": new_pos}))
    return events


def _sort_events(events):
    return sorted(events, key=lambda e: (-CHANGE_LEVEL[e["severity"]], e["category"], e["event_key"]))


def run_daily_change_detection(snapshot_date=None):
    snapshot, previous = collect_daily_snapshot(snapshot_date)
    events = build_global_events(snapshot, previous)
    snapshot_date = snapshot["snapshot_date"]
    briefing_date = snapshot.get("briefing_date") or next_taiwan_trading_day(snapshot_date)
    previous_date = snapshot.get("previous_trade_date")
    user_ids = set(get_notify_users() or [])
    if "get_all_watchlist_user_ids" in globals():
        user_ids.update(get_all_watchlist_user_ids() or [])
    for uid in user_ids:
        try:
            user_events = events + _watchlist_events(uid, snapshot_date, previous_date)
            user_events = _sort_events(user_events)
            _save_events(snapshot_date, uid, user_events, briefing_date)
        except Exception as exc:
            print(f"❌ 盤前變化偵測：使用者 {uid} 失敗：{exc}")
    _save_events(snapshot_date, None, _sort_events(events), briefing_date)
    return (f"盤前變化偵測完成：{len(events)} 個全市場事件、"
            f"資料日 {snapshot_date}、顯示日 {briefing_date}")


def _premarket_display_date(display_date=None):
    """取得盤前資料的顯示日；週末優先找下一批，找不到就回退最近交易日批次。"""
    requested = display_date or taiwan_today()
    if requested.weekday() < 5:
        return requested
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # 週末若已建立下週盤前批次，優先使用其 briefing_date。
        cur.execute("""
            SELECT MIN(briefing_date)
            FROM premarket_snapshots
            WHERE briefing_date >= %s
        """, (requested,))
        row = cur.fetchone()
        if not row or not row[0]:
            cur.execute("""
                SELECT MIN(briefing_date)
                FROM premarket_events
                WHERE briefing_date >= %s
            """, (requested,))
            row = cur.fetchone()
        if row and row[0]:
            return row[0]

        # 若 briefing_date 尚未補齊或下一批尚未建立，改用最近一筆
        # 已存在的交易日資料；不能因為週末查不到下一批就退回空快照。
        cur.execute("""
            SELECT briefing_date
            FROM premarket_snapshots
            WHERE snapshot_date <= %s
            ORDER BY snapshot_date DESC
            LIMIT 1
        """, (requested,))
        row = cur.fetchone()
        if not row or not row[0]:
            cur.execute("""
                SELECT briefing_date
                FROM premarket_events
                WHERE snapshot_date <= %s
                ORDER BY snapshot_date DESC
                LIMIT 1
            """, (requested,))
            row = cur.fetchone()
        return row[0] if row and row[0] else requested
    finally:
        release_db_connection(conn)


def _premarket_source_date(display_date):
    """由盤前顯示日找到收盤後產生這批事件的資料日。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT snapshot_date FROM premarket_snapshots
            WHERE briefing_date=%s ORDER BY snapshot_date DESC LIMIT 1
        """, (display_date,))
        row = cur.fetchone()
        if not row:
            cur.execute("""
                SELECT snapshot_date FROM premarket_events
                WHERE briefing_date=%s ORDER BY snapshot_date DESC LIMIT 1
            """, (display_date,))
            row = cur.fetchone()
        # 舊部署或手動補資料時，保留同日查詢作為相容 fallback。
        return row[0] if row else display_date
    finally:
        release_db_connection(conn)


def get_today_change_events(user_id=None, snapshot_date=None, limit=None):
    display_date = _premarket_display_date(snapshot_date or taiwan_today())
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        sql = """
          SELECT severity,category,title,detail,evidence,event_key
          FROM premarket_events
          WHERE briefing_date=%s AND user_id IS NOT DISTINCT FROM %s
          ORDER BY CASE severity WHEN 'S' THEN 4 WHEN 'A' THEN 3 WHEN 'B' THEN 2 ELSE 1 END DESC, id ASC
        """
        params = [display_date, user_id]
        if limit:
            sql += " LIMIT %s"; params.append(limit)
        cur.execute(sql, params)
        return [{"severity": r[0], "category": r[1], "title": r[2], "detail": r[3], "evidence": r[4], "event_key": r[5]} for r in cur.fetchall()]
    finally:
        release_db_connection(conn)


def _change_event_identity(event):
    """事件去重鍵；優先使用寫入時的 event_key，避免全域與個人列重複。"""
    if not isinstance(event, dict):
        return ""
    return str(event.get("event_key") or (
        f"{event.get('category', '')}:{event.get('title', '')}"
    )).strip()


def merge_change_events(user_id=None, snapshot_date=None, limit=None):
    """合併全域市場事件與使用者事件，再依優先級排序。

    目前資料庫為了讓每位使用者都能取得同一批市場事件，個人列有時已包含
    全域事件；這裡仍明確合併並以 event_key 去重，兼容舊資料與未來改成只存
    個人事件的寫入方式。全域事件先放入，保留市場事件的原始內容。
    """
    global_events = get_today_change_events(None, snapshot_date)
    scoped_events = get_today_change_events(user_id, snapshot_date) if user_id else []
    merged, seen = [], set()
    for event in (global_events or []) + (scoped_events or []):
        key = _change_event_identity(event)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(dict(event))
    merged = _sort_events(merged)
    return merged[:limit] if limit else merged


def build_today_attention_push(user_id):
    events = merge_change_events(user_id, limit=3)
    lines = ["🔥 今日值得注意"]
    if not events:
        return "😴 今日市場訊號偏少"
    for idx, event in enumerate(events, 1):
        lines.append(f"{['①','②','③'][idx-1]} {event['title']}")
    return "\n".join(lines)


def get_today_change_snapshot(snapshot_date=None):
    requested = snapshot_date or taiwan_today()
    display_date = _premarket_display_date(requested)
    source_date = _premarket_source_date(display_date)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT snapshot_date, briefing_date, previous_trade_date,
                   blackhorse, radar, market, news, institutional
            FROM premarket_snapshots WHERE snapshot_date=%s
        """, (source_date,))
        row = cur.fetchone()
        # 舊資料可能只有 snapshot_date、沒有 briefing_date；週末仍應顯示
        # 最近一筆不超過今天的真實快照，而不是把 None 變成空字典。
        if not row and requested.weekday() >= 5:
            cur.execute("""
                SELECT snapshot_date, briefing_date, previous_trade_date,
                       blackhorse, radar, market, news, institutional
                FROM premarket_snapshots
                WHERE snapshot_date <= %s
                ORDER BY snapshot_date DESC
                LIMIT 1
            """, (requested,))
            row = cur.fetchone()
        if not row:
            return None
        return {"snapshot_date": row[0].isoformat(),
                "source_date": row[0].isoformat(),
                "briefing_date": row[1].isoformat() if row[1] else display_date.isoformat(),
                "previous_trade_date": row[2].isoformat() if row[2] else None,
                "blackhorse": row[3] or [], "radar": row[4] or [],
                "market": row[5] or {}, "news": row[6] or [],
                "institutional": row[7] or {}}
    finally:
        release_db_connection(conn)


def get_today_signal_state(user_id=None, snapshot_date=None):
    """首頁用的真實資料狀態；不把尚未更新誤報成市場安靜。"""
    snapshot_date = _premarket_display_date(snapshot_date or taiwan_today())
    snapshot = get_today_change_snapshot(snapshot_date)
    events = merge_change_events(user_id, snapshot_date)
    if not snapshot:
        return {"kind": "not_updated", "title": "今日盤前資料尚未更新",
                "detail": "等待每日資料快照與變化偵測完成；目前不顯示推測訊號。", "events": events}
    if not snapshot.get("previous_trade_date"):
        return {"kind": "baseline", "title": "已建立今日基準快照",
                "detail": "從下一個有效交易日開始顯示排名、分數與法人方向變化。", "events": events}
    if not events:
        return {"kind": "quiet", "title": "今日市場訊號偏少",
                "detail": "已完成前一交易日比較，目前沒有達到事件規則的變化。", "events": events}
    return {"kind": "events", "title": "今日有新的市場變化",
            "detail": f"已偵測 {len(events)} 個變化，首頁顯示優先級最高的 3 個。", "events": events}


def get_today_event_timeline(user_id=None, snapshot_date=None, snapshot=None):
    """把今日與前一有效交易日事件比對成回訪時間線，不產生不存在的事件。"""
    snapshot_date = _premarket_display_date(snapshot_date or taiwan_today())
    snapshot = (get_today_change_snapshot(snapshot_date)
                if snapshot is None else snapshot)
    if not snapshot or not snapshot.get("previous_trade_date"):
        return {"new": [], "ongoing": [], "resolved": [],
                "previous_date": None, "current": [], "previous": []}

    current = merge_change_events(user_id, snapshot_date)
    # 目前顯示日的事件來自 source_date；上一批事件的顯示日就是目前 source_date。
    previous_display_date = date.fromisoformat(snapshot["source_date"])
    previous = merge_change_events(user_id, previous_display_date)

    def key(event):
        return event.get("event_key") or f"{event.get('category','')}:{event.get('title','')}"

    old_keys = {key(event) for event in previous}
    current_keys = {key(event) for event in current}
    return {
        "new": [event for event in current if key(event) not in old_keys],
        "ongoing": [event for event in current if key(event) in old_keys],
        "resolved": [event for event in previous if key(event) not in current_keys],
        "previous_date": snapshot["previous_trade_date"],
        "current": current,
        "previous": previous,
    }


# 今日首頁的 fast fragment 與完整 fragment 會在數秒內連續到達；
# 將同一位使用者、同一顯示日的快照與事件時間線短暫共用，
# 避免兩次 fragment 重複查詢資料庫。這不是行情快取，也不改資料日期。
_DAILY_HOME_CONTEXT_TTL = 60
_daily_home_context_cache = {}
_daily_home_context_lock = threading.Lock()


def _get_daily_home_context(user_id, display_date):
    display_date = _premarket_display_date(display_date)
    key = (str(user_id).strip(), display_date.isoformat())
    now = time.time()
    with _daily_home_context_lock:
        cached = _daily_home_context_cache.get(key)
        if cached and now - cached.get("at", 0) < _DAILY_HOME_CONTEXT_TTL:
            return cached["value"]

    snapshot = get_today_change_snapshot(display_date)
    timeline = get_today_event_timeline(user_id, display_date, snapshot=snapshot)
    value = {"snapshot": snapshot, "timeline": timeline}
    with _daily_home_context_lock:
        _daily_home_context_cache[key] = {"at": now, "value": value}
        if len(_daily_home_context_cache) > 500:
            cutoff = now - _DAILY_HOME_CONTEXT_TTL * 2
            for cache_key, entry in list(_daily_home_context_cache.items()):
                if entry.get("at", 0) < cutoff:
                    _daily_home_context_cache.pop(cache_key, None)
    return value


def _daily_signal_state(snapshot, timeline):
    events = (timeline.get("new", []) + timeline.get("ongoing", []))
    if not snapshot:
        return {"kind": "not_updated", "title": "今日盤前資料尚未更新",
                "detail": "等待每日資料快照與變化偵測完成；目前不顯示推測訊號。"}
    if not snapshot.get("previous_trade_date"):
        return {"kind": "baseline", "title": "已建立今日基準快照",
                "detail": "從下一個有效交易日開始顯示排名、分數與法人方向變化。"}
    if not events:
        return {"kind": "quiet", "title": "今日市場訊號偏少",
                "detail": "已完成前一交易日比較，目前沒有達到事件規則的變化。"}
    return {"kind": "events", "title": "今日有新的市場變化",
            "detail": f"已偵測 {len(events)} 個變化，首頁顯示優先級最高的 3 個。"}


def build_today_change_web_data(user_id):
    """網頁顯示最近可用盤前日的完整快照與事件；LINE 只取前三項摘要。"""
    requested_date = taiwan_today()
    display_date = _premarket_display_date(requested_date)
    global_events = get_today_change_events(None, display_date)
    user_events = get_today_change_events(user_id, display_date)
    merged_events = merge_change_events(user_id, display_date)
    state = get_today_signal_state(user_id, display_date)
    current_snapshot = get_today_change_snapshot(display_date)
    return {"date": display_date.isoformat(),
            "requested_date": requested_date.isoformat(),
            "is_weekend": requested_date.weekday() >= 5,
            "levels": LEVEL_LABEL, "snapshot": current_snapshot,
            "events": merged_events,
            "global_events": global_events, "all_events": user_events,
            "state": state}


# 在既有 bot 完成所有函式定義後呼叫 configure_daily_change_detector(...)
# 再呼叫 init_premarket_change_tables()；不要在 import 區塊直接初始化，
# 因為這個專案的資料庫 helper 定義在檔案前半段、其餘資料函式定義在後半段。
# 新增排程端點：run_in_background("盤前變化偵測", run_daily_change_detection)
# 盤前 LINE 由 build_morning_push_message() 提供短摘要與今日網頁入口。
# 完整網頁則在既有盤前頁 route 呼叫 build_today_change_web_data(user_id)。

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

# --- 繁體中文名稱對照表（僅作為個股直接查詢時的備用名稱） ---
STOCK_NAME_MAP = {
    "2330": "台積電", "2454": "聯發科", "3661": "世芯-KY", "6669": "緯穎",
    "3037": "欣興", "2382": "廣達", "3231": "緯創", "4931": "新日興",
    "3081": "聯亞", "6442": "光聖", "3529": "力旺", "3443": "創意",
    "6173": "信昌電", "1503": "士電"
}

# --- 1. 喚醒專用根路由 ---
@app.route("/", methods=["GET"])
def home():
    return "Bot is alive and awake!", 200

# --- Supabase 連線池（程式啟動時建立一次，取代每次呼叫都開新連線） ---
_db_url = os.environ.get("DATABASE_URL")
_url = urlparse(_db_url)
_ipv4_addr = socket.gethostbyname(_url.hostname)

# 一定要用 ThreadedConnectionPool 而不是 SimpleConnectionPool。
# gunicorn 開了多執行緒之後，SimpleConnectionPool 可能把同一條連線
# 同時交給兩個執行緒，兩邊交錯寫入同一個 SSL 串流，就會出現
# 「SSL error: decryption failed or bad record mac」——
# 錯誤訊息看起來像憑證問題，實際上是併發問題，很容易查錯方向。
# 單執行緒時永遠不會發生，所以加上 --threads 之前都相安無事。
connection_pool = psycopg2.pool.ThreadedConnectionPool(
    1, 20,          # 上限要 ≥ gunicorn 的 threads 數，否則執行緒會卡在等連線
    database=_url.path[1:],
    user=_url.username,
    password=_url.password,
    host=_ipv4_addr,
    port=_url.port,
    sslmode='require',
    options='-c timezone=Asia/Taipei',
    connect_timeout=10,
    # 連線被 Supabase pooler 中途切斷時，下次借出才會發現而丟出例外。
    # 開啟 keepalive 讓閒置連線不被靜默斷開。
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=3,
)


def get_db_connection():
    """
    借一條連線。若借到的是已經壞掉的連線（例如被伺服器中途切斷），
    直接丟棄再借一條——把壞連線放回池子只會讓下一個人也踩到。
    """
    for _attempt in range(3):
        conn = connection_pool.getconn()
        try:
            if conn.closed:
                connection_pool.putconn(conn, close=True)
                continue
            return conn
        except Exception as e:
            print(f"⚠️ 取得連線異常，丟棄重試: {e}")
            try:
                connection_pool.putconn(conn, close=True)
            except Exception:
                pass
    return connection_pool.getconn()


def release_db_connection(conn):
    """
    歸還連線。交易若停在異常狀態，必須先 rollback 再放回，
    否則下一個借到這條連線的人會直接收到
    「current transaction is aborted」而摸不著頭緒。
    """
    if not conn:
        return
    try:
        # 連線處於錯誤或交易中的狀態時先清乾淨
        if conn.closed:
            connection_pool.putconn(conn, close=True)
            return
        if conn.get_transaction_status() not in (
                psycopg2.extensions.TRANSACTION_STATUS_IDLE,):
            conn.rollback()
        connection_pool.putconn(conn)
    except Exception as e:
        print(f"⚠️ 歸還連線失敗，關閉該連線: {e}")
        try:
            connection_pool.putconn(conn, close=True)
        except Exception:
            pass

def init_db():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watchlists (
                user_id TEXT,
                code TEXT,
                PRIMARY KEY (user_id, code)
            )
        ''')
        # 自選股分類（長線／短線／觀察）。用 ALTER 而非寫在 CREATE 裡，
        # 因為 CREATE TABLE IF NOT EXISTS 對既有的表不會補欄位，
        # 舊使用者的自選清單早就建好了，只靠 CREATE 這個欄位永遠不會出現。
        cursor.execute('''
            ALTER TABLE watchlists ADD COLUMN IF NOT EXISTS tag TEXT
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                notify BOOLEAN DEFAULT FALSE
            )
        ''')
        cursor.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS notify BOOLEAN DEFAULT FALSE
        ''')
        # 存 LINE 顯示名稱，這樣在 Supabase 後台才認得出誰是誰，
        # 要開通推播直接把那一列的 notify 改成 true 即可
        cursor.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT
        ''')
        cursor.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMP
        ''')
        # 是否申請過推播，讓管理者一眼看出誰在等開通
        cursor.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS requested BOOLEAN DEFAULT FALSE
        ''')
        cursor.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_feature TEXT
        ''')
        cursor.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS activity_count INTEGER DEFAULT 0
        ''')
        # 使用者活動紀錄：只記錄功能與時間，不記錄持股內容。
        # 未來若要搬到 Web 管理後台，可直接沿用此表查詢。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS activity_log (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                feature TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'open',
                source TEXT NOT NULL DEFAULT 'line',
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                occurred_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_activity_log_user_time
            ON activity_log(user_id, occurred_at DESC)
        ''')
        # LINE 可能因網路或回應逾時重送同一 webhook；用資料庫唯一鍵跨 worker 去重。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS line_event_dedup (
                event_id TEXT PRIMARY KEY,
                received_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_line_event_dedup_received
            ON line_event_dedup(received_at DESC)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_activity_log_feature_time
            ON activity_log(feature, occurred_at DESC)
        ''')
        # ── 網頁版：持股、問卷設定、登入權杖 ──
        # 持股與自選股分開存：自選股是「在看的」，持股是「真的買了的」，
        # 只有後者才有股數與成本，組合分析也只該算後者。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                code TEXT NOT NULL,
                shares INTEGER NOT NULL,
                cost REAL NOT NULL,
                bought_on DATE,
                note TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_positions_user ON positions (user_id)
        ''')
        # 問卷與門檻設定。前四題必填，其餘可略過，所以全部允許 NULL。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_profile (
                user_id TEXT PRIMARY KEY,
                age_band TEXT,
                horizon TEXT,
                asset_share TEXT,
                income_type TEXT,
                drawdown_experience TEXT,
                loss_alert_pct INTEGER,
                position_alert_pct INTEGER,
                check_frequency TEXT,
                holding_period TEXT,
                other_assets TEXT,
                fee_discount REAL,
                min_fee INTEGER,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        # 既有資料表不會因 CREATE TABLE IF NOT EXISTS 而新增欄位，
        # 之後補的欄位一律用 ALTER TABLE，否則舊使用者存不進去。
        for _col, _type in [
            ("fee_discount", "REAL"),
            ("min_fee", "INTEGER"),
            ("loss_alert_pct", "INTEGER"),
            ("position_alert_pct", "INTEGER"),
            ("check_frequency", "TEXT"),
            ("holding_period", "TEXT"),
            ("other_assets", "TEXT"),
            ("drawdown_experience", "TEXT"),
        ]:
            cursor.execute(
                f"ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS {_col} {_type}")

        # 網頁登入權杖：LINE 傳「網頁」時產生，點連結即登入，
        # 這樣不必另外做 LINE Login 也不必讓使用者記帳號密碼。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS web_sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP NOT NULL
            )
        ''')
        # 一次性登入驗證碼。
        # LINE 內建瀏覽器的 cookie 跟外部瀏覽器不互通，點連結登入的方式
        # 一換瀏覽器就失效；讓使用者拿一組短碼自己輸入，就能在任何裝置
        # 任何瀏覽器登入，不必再回 LINE 重拿連結。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS web_codes (
                code TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN DEFAULT FALSE
            )
        ''')
        # 每日三大法人買賣超歷史（用來算「連續買超天數」等長線指標）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS inst_history (
                code TEXT,
                trade_date DATE,
                name TEXT,
                foreign_net_lots INTEGER,
                trust_net_lots INTEGER,
                dealer_net_lots INTEGER,
                total_net_lots INTEGER,
                PRIMARY KEY (code, trade_date)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_inst_history_code_date
            ON inst_history (code, trade_date DESC)
        ''')
        # 上市公司基本資料（產業別）：抓一次即可，用來做「產業趨勢」分析
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_info (
                code TEXT PRIMARY KEY,
                name TEXT,
                industry TEXT
            )
        ''')
        cursor.execute(
            "ALTER TABLE stock_info ADD COLUMN IF NOT EXISTS market TEXT")
        # 每月營收快照：TWSE 只提供最新一期，歷史必須自己每月累積
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS revenue_history (
                code TEXT,
                period TEXT,
                yoy_pct REAL,
                cum_yoy_pct REAL,
                mom_pct REAL,
                month_revenue REAL,
                PRIMARY KEY (code, period)
            )
        ''')
        # 已實現損益：賣出當下記一筆快照（成本、賣價、損益），
        # 跟目前持股分開存──賣掉之後那筆持股就從 positions 消失了，
        # 沒有這張表就永遠算不出「這一季賺賠多少」「勝率」這類長期指標。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS realized_trades (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                code TEXT NOT NULL,
                shares INTEGER NOT NULL,
                buy_cost REAL NOT NULL,
                sell_price REAL NOT NULL,
                realized_pl REAL,
                realized_pct REAL,
                bought_on DATE,
                sold_on DATE DEFAULT CURRENT_DATE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_realized_trades_user
            ON realized_trades (user_id, sold_on DESC)
        ''')
        # 賣出時實際付出的手續費與證交稅。使用者自己填才準——
        # 各券商折扣、最低收費、當沖減半都不一樣，程式算的只能是估計值。
        for _col, _type in [("fee", "REAL"), ("tax", "REAL")]:
            cursor.execute(
                f"ALTER TABLE realized_trades ADD COLUMN IF NOT EXISTS {_col} {_type}")
        # 每日組合快照：市值與大盤指數各存一筆，用來畫「我的組合 vs 大盤」走勢圖。
        # 沒有這張表就沒有歷史可畫，所以越早開始存越好，畫圖是之後的事。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                snapshot_date DATE NOT NULL,
                total_value REAL,
                total_cost REAL,
                taiex_close REAL,
                UNIQUE(user_id, snapshot_date)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_user
            ON portfolio_snapshots (user_id, snapshot_date)
        ''')
        # 自選股每日評分快照。分數本身每次查都算得出來，但「昨天幾分」算不出來——
        # 沒有這張表就永遠只能顯示靜態分數，看不出誰在變強、誰在轉弱，
        # 而變化往往比絕對分數更有訊號價值。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watchlist_scores (
                user_id TEXT,
                code TEXT,
                snapshot_date DATE,
                total INTEGER,
                chip INTEGER,
                position INTEGER,
                revenue INTEGER,
                valuation INTEGER,
                close REAL,
                PRIMARY KEY (user_id, code, snapshot_date)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_watchlist_scores_lookup
            ON watchlist_scores (user_id, code, snapshot_date DESC)
        ''')
        # 產業動能排名的歷史。get_industry_momentum() 每次都算得出當期排名，
        # 但「這個族群是正在變強還是變弱」需要跟過去比，那要自己累積。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS industry_momentum_history (
                industry TEXT,
                snapshot_date DATE,
                p75 REAL,
                median REAL,
                rank INTEGER,
                count INTEGER,
                PRIMARY KEY (industry, snapshot_date)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_industry_momentum_date
            ON industry_momentum_history (snapshot_date DESC)
        ''')
        # 選股成效追蹤：每天把黑馬／雷達選出來的名單存起來，
        # 之後回頭算它們的報酬率。
        # 這是唯一能回答「這套評分到底有沒有用」的方式——
        # 沒有這張表，選股邏輯永遠只是一套說得通但沒被驗證過的規則。
        # 存的是「當下的推薦價」，之後用現價比對即可算出報酬。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pick_history (
                mode TEXT,
                code TEXT,
                pick_date DATE,
                rank INTEGER,
                score INTEGER,
                name TEXT,
                industry TEXT,
                price REAL,
                PRIMARY KEY (mode, code, pick_date)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_pick_history_date
            ON pick_history (pick_date DESC, mode)
        ''')
        # 排行榜成員。預設不參加，要自己填暱稱加入——
        # 排行榜會把報酬率給別人看，那跟「持股只用於你自己的分析」是兩件事，
        # 必須明確 opt-in 才不會違背當初給使用者的承諾。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS leaderboard_members (
                user_id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL,
                joined_on DATE DEFAULT CURRENT_DATE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        # 是否公開持股內容。獨立於「參加排行榜」之外，預設關閉——
        # 早期加入的人是在「不會顯示持股內容」的承諾下同意的，
        # 不能因為後來加了功能就把他們的持股攤開來。
        cursor.execute(
            "ALTER TABLE leaderboard_members "
            "ADD COLUMN IF NOT EXISTS show_holdings BOOLEAN DEFAULT FALSE")
        # 排行榜每日名次快照：用來顯示昨日到今日的上升／下降與連續天數。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS leaderboard_rank_snapshots (
                board TEXT NOT NULL,
                snapshot_date DATE NOT NULL,
                user_id TEXT NOT NULL,
                rank INTEGER NOT NULL,
                return_pct REAL,
                PRIMARY KEY (board, snapshot_date, user_id)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_leaderboard_rank_history
            ON leaderboard_rank_snapshots (board, user_id, snapshot_date DESC)
        ''')
        # 背景工作狀態。
        # 原本只存在記憶體，但那有兩個問題：gunicorn 開多個 worker 時，
        # 工作在 A 執行、查詢卻可能連到 B，看到的是空的；服務一重啟也全沒了。
        # 存資料庫就沒有這些問題，而且每個工作只佔一列，成本極低。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS job_runs (
                name TEXT PRIMARY KEY,
                running BOOLEAN DEFAULT FALSE,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                seconds REAL,
                result TEXT,
                run_id TEXT,
                progress_stage TEXT DEFAULT 'industry',
                progress_index INTEGER DEFAULT 0,
                progress_total INTEGER
            )
        ''')
        for _col, _type in [
            ("run_id", "TEXT"),
            ("progress_stage", "TEXT DEFAULT 'industry'"),
            ("progress_index", "INTEGER DEFAULT 0"),
            ("progress_total", "INTEGER"),
        ]:
            cursor.execute(
                f"ALTER TABLE job_runs ADD COLUMN IF NOT EXISTS {_col} {_type}")

        # 早期欄位是 TIMESTAMP WITHOUT TIME ZONE；當時 Render／Supabase
        # 可能以 UTC 寫入，導致管理名單看到的時間少 8 小時。只在欄位仍是
        # 無時區型別時執行一次 migration，避免每次啟動重複轉換。
        timestamp_targets = {
            "users": ("last_seen",),
            "activity_log": ("occurred_at",),
            "job_runs": ("started_at", "finished_at"),
        }
        cursor.execute("""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
              AND column_name = ANY(%s)
        """, (list(timestamp_targets),
              [column for columns in timestamp_targets.values() for column in columns]))
        for _table, _column, _data_type in cursor.fetchall():
            if _data_type == "timestamp without time zone":
                cursor.execute(
                    f'''ALTER TABLE "{_table}" ALTER COLUMN "{_column}"
                        TYPE TIMESTAMPTZ
                        USING "{_column}" AT TIME ZONE 'UTC' ''')
        conn.commit()
        cursor.close()
    except Exception as e:
        conn.rollback()
        print(f"❌ 初始化資料庫錯誤: {e}")
    finally:
        release_db_connection(conn)

init_db()

def add_user_to_db(user_id):
    """
    記錄使用者。順便抓 LINE 顯示名稱存起來——沒有名字的話，
    後台看到的只有一串亂碼 user_id，無法判斷要開通誰的推播。
    抓名字失敗不影響主要流程。
    """
    uid = str(user_id).strip()
    name = None
    try:
        name = line_bot_api.get_profile(uid).display_name
    except Exception as e:
        print(f"⚠️ 取得使用者名稱失敗 {uid}: {e}")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO users (user_id, display_name, last_seen)
            VALUES (%s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                display_name = COALESCE(EXCLUDED.display_name, users.display_name),
                last_seen = NOW()
            """,
            (uid, name)
        )
        conn.commit()
        cursor.close()
    except Exception as e:
        conn.rollback()
        print(f"❌ 新增使用者錯誤: {e}")
    finally:
        release_db_connection(conn)

FEATURE_LABELS = {
    "leaderboard": "排行榜",
    "blackhorse": "黑馬",
    "radar": "雷達",
    "portfolio": "組合分析",
    "web_entry": "網頁",
    "trades": "紀錄",
    "compare": "比較",
    "positions": "自選",
    "news": "新聞",
    "premarket": "盤前",
    "debrief": "解盤",
    "chips": "籌碼超人",
    "quote": "個股查詢",
    "screener": "選股",
    "settings": "設定",
    "more": "更多",
    "admin": "管理",
}


def infer_line_feature(text):
    raw = (text or "").strip()
    exact = {
        "盤前": "premarket", "解盤": "debrief", "黑馬": "blackhorse",
        "雷達": "radar", "籌碼": "chips", "籌碼超人": "chips",
        "自選": "positions", "新聞": "news", "網頁": "web_entry",
        "網頁版": "web_entry", "排行榜": "leaderboard", "紀錄": "trades",
        "比較": "compare", "管理": "admin", "使用者名單": "admin",
        "今日活躍": "admin", "沉睡使用者": "admin", "功能統計": "admin",
        "流失": "admin", "可能流失": "admin",
    }
    if raw in exact:
        return exact[raw]
    if raw.startswith(("加 ", "加")) or raw.startswith(("刪 ", "刪")) or raw.startswith("分類"):
        return "positions"
    if normalize_code(raw):
        return "quote"
    return None


def record_activity(user_id, feature, action="open", source="line", metadata=None):
    """記錄功能使用，不寫入持股內容；失敗時不影響原本功能。"""
    uid = str(user_id).strip()
    if not uid or not feature:
        return
    meta = metadata if isinstance(metadata, dict) else {}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO activity_log
               (user_id, feature, action, source, metadata, occurred_at)
               VALUES (%s, %s, %s, %s, %s::jsonb, NOW())""",
            (uid, str(feature), str(action), str(source), json.dumps(meta, ensure_ascii=False)))
        cur.execute(
            """UPDATE users
               SET last_seen = NOW(), last_feature = %s,
                   activity_count = COALESCE(activity_count, 0) + 1
               WHERE user_id = %s""",
            (str(feature), uid))
        conn.commit()
        cur.close()
    except Exception as exc:
        conn.rollback()
        print(f"⚠️ 活動紀錄失敗 {uid}/{feature}: {exc}")
    finally:
        release_db_connection(conn)


def _activity_feature_label(feature):
    return FEATURE_LABELS.get(str(feature), str(feature))


def _admin_user_rows(status="all", limit=10, offset=0):
    """回傳管理名單摘要，不讀取或顯示個別持股內容。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        where = ""
        if status == "today":
            where = "WHERE u.last_seen >= CURRENT_DATE"
        elif status == "dormant":
            where = "WHERE u.last_seen < NOW() - INTERVAL '3 days'"
        elif status == "dormant7":
            where = "WHERE u.last_seen < NOW() - INTERVAL '7 days'"
        cur.execute(f"""
            SELECT u.user_id, COALESCE(u.display_name, '(未知)'), u.last_seen,
                   COALESCE(u.last_feature, ''), COALESCE(u.notify, FALSE),
                   COALESCE(u.requested, FALSE), COALESCE(u.activity_count, 0)
            FROM users u {where}
            ORDER BY u.last_seen DESC NULLS LAST, u.user_id
            LIMIT %s OFFSET %s
        """, (int(limit), int(offset)))
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception as exc:
        print(f"❌ 管理名單查詢失敗：{exc}")
        return []
    finally:
        release_db_connection(conn)


def _admin_recent_features(user_id, days=30, limit=3):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT feature, MAX(occurred_at) AS last_used
            FROM activity_log
            WHERE user_id = %s AND occurred_at >= NOW() - (%s * INTERVAL '1 day')
            GROUP BY feature ORDER BY last_used DESC LIMIT %s
        """, (str(user_id).strip(), int(days), int(limit)))
        rows = cur.fetchall()
        cur.close()
        return [_activity_feature_label(row[0]) for row in rows]
    except Exception as exc:
        print(f"❌ 最近功能查詢失敗：{exc}")
        return []
    finally:
        release_db_connection(conn)


def _admin_recent_features_map(user_ids, days=30, limit=3):
    """一次讀取多位使用者最近功能，避免使用者名單產生 N+1 查詢。"""
    ids = [str(uid).strip() for uid in (user_ids or []) if str(uid).strip()]
    result = {uid: [] for uid in ids}
    if not ids:
        return result
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, feature, last_used
            FROM (
                SELECT user_id, feature, MAX(occurred_at) AS last_used,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id ORDER BY MAX(occurred_at) DESC
                       ) AS rn
                FROM activity_log
                WHERE user_id = ANY(%s)
                  AND occurred_at >= NOW() - (%s * INTERVAL '1 day')
                GROUP BY user_id, feature
            ) recent
            WHERE rn <= %s
            ORDER BY user_id, last_used DESC
        """, (ids, int(days), int(limit)))
        for user_id, feature, _last_used in cur.fetchall():
            result.setdefault(str(user_id).strip(), []).append(
                _activity_feature_label(feature))
        cur.close()
        return result
    except Exception as exc:
        print(f"❌ 批次最近功能查詢失敗：{exc}")
        return result
    finally:
        release_db_connection(conn)


def _admin_format_time(value):
    local = as_taiwan_datetime(value)
    if not local:
        return "尚未使用"
    now = taiwan_now()
    if local.date() == now.date():
        return f"今天 {local.strftime('%H:%M')}"
    return local.strftime('%m/%d %H:%M')


def _admin_status(value):
    when = as_taiwan_datetime(value)
    if not when:
        return "⚪ 尚未使用"
    days = (taiwan_now() - when).total_seconds() / 86400
    if days < 1:
        return "🟢 今日使用"
    if days < 3:
        return "🟡 1–2天未使用"
    if days < 7:
        return "🟡 3–6天未使用"
    return "🔴 7天以上未使用"


def build_admin_dashboard_report():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE last_seen >= CURRENT_DATE")
        today = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE last_seen >= CURRENT_DATE - INTERVAL '1 day' AND last_seen < CURRENT_DATE")
        yesterday = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE last_seen < NOW() - INTERVAL '3 days' AND last_seen >= NOW() - INTERVAL '7 days'")
        dormant3 = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE last_seen < NOW() - INTERVAL '7 days'")
        dormant7 = cur.fetchone()[0]
        cur.execute("""
            SELECT feature, COUNT(DISTINCT user_id) AS people, COUNT(*) AS uses
            FROM activity_log WHERE occurred_at >= NOW() - INTERVAL '7 days'
            GROUP BY feature ORDER BY people DESC, uses DESC
        """)
        features = cur.fetchall()
        cur.close()
    except Exception as exc:
        print(f"❌ 管理中心統計失敗：{exc}")
        return "❌ 管理中心暫時無法讀取，請查看 Render Logs。"
    finally:
        release_db_connection(conn)

    lines = ["📊 台股 BOT｜管理中心", "", f"👥 使用者總數　{total} 人",
             f"🟢 今日活躍　　{today} 人", f"📅 昨日活躍　　{yesterday} 人",
             f"🟡 3–6天未使用　{dormant3} 人", f"🔴 7天以上未使用　{dormant7} 人",
             "", "━━━━━━━━━━━━", "", "📱 近 7 天功能使用"]
    feature_map = {feature: (people, uses) for feature, people, uses in features}
    for feature in (
        "premarket", "debrief", "web_entry", "portfolio",
        "leaderboard", "blackhorse", "radar", "trades", "compare"
    ):
        people, uses = feature_map.get(feature, (0, 0))
        lines.append(f"{_activity_feature_label(feature):<6}　{people} 人／{uses} 次")
    lines += ["", "━━━━━━━━━━━━", "", "資料更新：" + taiwan_now().strftime('%m/%d %H:%M')]
    return "\n".join(lines)


def _admin_system_data_status_report():
    """使用者名單上方的資料新鮮度摘要；只讀真實資料，不自行推測事件。"""
    today = taiwan_today()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT MAX(trade_date), COUNT(DISTINCT trade_date)
            FROM inst_history
        """)
        inst_date, inst_days = cur.fetchone()

        cur.execute("""
            SELECT MAX(snapshot_date)
            FROM premarket_snapshots
        """)
        premarket_date = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*)
            FROM premarket_events
            WHERE briefing_date = %s
        """, (today,))
        event_count = cur.fetchone()[0] or 0

        cur.execute("""
            SELECT snapshot_date, COUNT(DISTINCT board)
            FROM leaderboard_rank_snapshots
            GROUP BY snapshot_date
            ORDER BY snapshot_date DESC
            LIMIT 1
        """)
        rank_row = cur.fetchone()
        rank_date, rank_boards = rank_row if rank_row else (None, 0)

        cur.execute("""
            SELECT running, started_at, finished_at, result,
                   progress_stage, progress_index, progress_total
            FROM job_runs
            WHERE name = '每日快照'
        """)
        job_row = cur.fetchone()
        cur.close()
    except Exception as exc:
        print(f"❌ 管理資料狀態查詢失敗：{exc}")
        return "📡 系統資料狀態\n⚠️ 暫時無法讀取，請查看 Render Logs。"
    finally:
        release_db_connection(conn)

    def fmt_date(value):
        return value.strftime("%Y/%m/%d") if value else "尚無資料"

    def premarket_status(value):
        if not value:
            return "⚪ 尚無快照"
        return "✅ 今日已更新" if value == today else "ℹ️ 最近資料"

    def rank_status(value):
        if not value:
            return "⚪ 尚無快照"
        return "✅ 今日已更新" if value == today else "ℹ️ 最近交易日"

    inst_status = "✅ 有資料" if inst_date else "⚪ 尚無資料"

    if not job_row:
        job_text = "⚪ 尚無執行紀錄"
    else:
        running, started_at, finished_at, result, stage, index, total = job_row
        if running:
            progress = f"{stage or '處理中'} {index or 0}"
            if total is not None:
                progress += f"/{total}"
            job_text = f"⏳ 執行中（{progress}）"
        elif result and str(result).startswith("失敗："):
            job_text = f"❌ 失敗：{str(result)[3:][:32]}"
        elif finished_at:
            when = _admin_format_time(finished_at)
            job_text = f"✅ 最後完成：{when}"
        else:
            job_text = "⚪ 尚未完成"

    return "\n".join([
        "📡 系統資料狀態",
        f"法人資料　最新 {fmt_date(inst_date)}（{inst_days or 0} 個交易日）　{inst_status}",
        f"盤前快照　最新 {fmt_date(premarket_date)}　{premarket_status(premarket_date)}",
        f"今日事件　{event_count} 個（以今日盤前顯示日為準）",
        f"排行榜快照　最新 {fmt_date(rank_date)}（{rank_boards or 0} 榜）　{rank_status(rank_date)}",
        f"每日快照　{job_text}",
    ])


def build_admin_user_list_report(status="all", limit=10, offset=0):
    rows = _admin_user_rows(status=status, limit=limit, offset=offset)
    labels = {"all": "使用者名單", "today": "今日活躍", "dormant": "沉睡使用者", "dormant7": "7天以上未使用"}
    lines = [f"👥 {labels.get(status, '使用者名單')}", "─" * 14]
    if status == "all":
        lines += ["", _admin_system_data_status_report(), "", "─" * 14]
    if not rows:
        lines.append("目前沒有符合條件的使用者。")
        return "\n".join(lines)
    recent_features = _admin_recent_features_map([row[0] for row in rows])
    for i, (uid, name, last_seen, last_feature, notify, requested, count) in enumerate(rows, offset + 1):
        features = recent_features.get(str(uid).strip(), [])
        masked = f"{uid[:4]}••••{uid[-4:]}" if len(uid) > 8 else uid
        push_state = ("🔔 盤前推播：開啟" if notify else
                      "📮 盤前推播：申請中，待管理者開通" if requested else
                      "🔕 盤前推播：關閉")
        lines += [f"{i}. {name}", f"   {_admin_status(last_seen)}",
                  f"   最後使用：{_admin_format_time(last_seen)}",
                  f"   最近使用：{'／'.join(features) if features else _activity_feature_label(last_feature) if last_feature else '—'}",
                  f"   {push_state}", f"   LINE：{masked}"]
    if status == "all":
        lines += ["", "─" * 14,
                  "推播管理：輸入「開通 編號」或「停用 編號」",
                  "例如：開通 3　／　停用 3"]
    return "\n".join(lines)


def build_admin_churn_report():
    rows = _admin_user_rows(status="dormant", limit=50, offset=0)
    rows = [row for row in rows if row[2]]
    if not rows:
        return "⚠️ 可能流失使用者\n\n目前沒有 3 天以上未使用的使用者。"
    groups = {"3–6天未使用": [], "7天以上未使用": []}
    now = taiwan_now()
    for row in rows:
        when = as_taiwan_datetime(row[2])
        if not when:
            continue
        days = (now - when).total_seconds() / 86400
        group = "7天以上未使用" if days >= 7 else "3–6天未使用"
        groups[group].append(row)
    lines = ["⚠️ 可能流失使用者", "", f"目前 3 天以上未回來：{len(rows)} 人"]
    for label, group in groups.items():
        if not group:
            continue
        lines += ["", ("🔴 " if label.startswith("7") else "🟡 ") + label]
        for row in group[:10]:
            features = _admin_recent_features(row[0], days=30, limit=2)
            lines.append(f"・{row[1]}　最後使用：{_admin_format_time(row[2])}　最近：{'／'.join(features) if features else '—'}")
    lines += ["", "📌 此名單代表長時間未使用，不代表確定流失。"]
    return "\n".join(lines)


def is_admin(user_id):
    """
    管理者由環境變數 ADMIN_USER_ID 指定，多位管理者用逗號分隔，例如：
    ADMIN_USER_ID=Uaaa...,Ubbb...
    未設定時沒有人是管理者，管理指令對所有人都無效。
    """
    raw = os.environ.get("ADMIN_USER_ID", "")
    admins = {a.strip() for a in raw.split(",") if a.strip()}
    return str(user_id).strip() in admins if admins else False


def set_push_flags(user_id, notify=None, requested=None):
    """在同一個 transaction 更新主動推播與申請狀態。"""
    fields, values = [], []
    if notify is not None:
        fields.append("notify = %s")
        values.append(bool(notify))
    if requested is not None:
        fields.append("requested = %s")
        values.append(bool(requested))
    if not fields:
        return False
    values.append(str(user_id).strip())
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE user_id = %s",
            values)
        changed = cursor.rowcount == 1
        conn.commit()
        cursor.close()
        return changed
    except Exception as e:
        conn.rollback()
        print(f"❌ 更新推播狀態錯誤: {e}")
        return False
    finally:
        release_db_connection(conn)


def set_requested(user_id, flag=True):
    return set_push_flags(user_id, requested=flag)


def list_users():
    """
    回傳所有使用者，順序與管理者看到的名單一致，
    這樣「開通 N／停用 N」的編號才不會對錯人。
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, COALESCE(display_name, '(未知)'),
                   COALESCE(notify, FALSE), COALESCE(requested, FALSE)
            FROM users ORDER BY last_seen DESC NULLS LAST, user_id
        """)
        rows = cursor.fetchall()
        cursor.close()
        return rows
    except Exception as e:
        print(f"❌ 讀取使用者名單錯誤: {e}")
        return []
    finally:
        release_db_connection(conn)


def build_usage_stats_report():
    """
    使用狀況統計。

    刻意只給聚合數字，不顯示任何個別使用者的持股明細——
    使用者輸入持股時預設「作者不會看我買了什麼」，那是個人財務資料。
    要判斷「這個工具有沒有人在用」，聚合數字已經足夠；
    看個別持股不會讓判斷更準，只會踩到不該踩的線。

    「最多人持有」列的是被幾個人持有，不是誰持有——
    3 人以上才顯示，避免只有一個人持有時等於指名道姓。
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        stats = {}

        cur.execute("SELECT COUNT(*) FROM users")
        stats["users"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE last_seen >= NOW() - INTERVAL '7 days'")
        stats["active7"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE last_seen >= NOW() - INTERVAL '30 days'")
        stats["active30"] = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT user_id), COUNT(*) FROM positions")
        stats["pos_users"], stats["pos_rows"] = cur.fetchone()
        cur.execute("SELECT COUNT(DISTINCT user_id), COUNT(*) FROM watchlists")
        stats["wl_users"], stats["wl_rows"] = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM user_profile")
        stats["profiles"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT user_id), COUNT(*) FROM realized_trades")
        stats["tr_users"], stats["tr_rows"] = cur.fetchone()

        # 熱門標的：只算「多少人持有」，不記錄是誰、也不看金額
        cur.execute("""
            SELECT code, COUNT(DISTINCT user_id) AS n FROM positions
            GROUP BY code HAVING COUNT(DISTINCT user_id) >= 3
            ORDER BY n DESC, code LIMIT 8
        """)
        stats["hot_pos"] = cur.fetchall()
        cur.execute("""
            SELECT code, COUNT(DISTINCT user_id) AS n FROM watchlists
            GROUP BY code HAVING COUNT(DISTINCT user_id) >= 3
            ORDER BY n DESC, code LIMIT 8
        """)
        stats["hot_wl"] = cur.fetchall()
        cur.close()
    except Exception as e:
        print(f"❌ 讀取使用統計失敗: {e}")
        return f"❌ 統計查詢失敗，請看 Render Logs。"
    finally:
        release_db_connection(conn)

    inst = fetch_institutional_data() or {}
    lines = ["📈 使用狀況統計", "─" * 14]
    lines.append(f"👥 使用者　{stats['users']} 人")
    lines.append(f"　近 7 天活躍　{stats['active7']} 人")
    lines.append(f"　近 30 天活躍　{stats['active30']} 人")

    lines.append("")
    lines.append("📦 功能使用")
    avg_pos = (stats["pos_rows"] / stats["pos_users"]) if stats["pos_users"] else 0
    avg_wl = (stats["wl_rows"] / stats["wl_users"]) if stats["wl_users"] else 0
    lines.append(f"　建立持股　{stats['pos_users']} 人"
                 + (f"（平均 {avg_pos:.1f} 筆）" if stats["pos_users"] else ""))
    lines.append(f"　自選清單　{stats['wl_users']} 人"
                 + (f"（平均 {avg_wl:.1f} 檔）" if stats["wl_users"] else ""))
    lines.append(f"　填過問卷　{stats['profiles']} 人")
    lines.append(f"　有賣出紀錄　{stats['tr_users']} 人（共 {stats['tr_rows']} 筆）")

    # 轉換率：註冊了但沒建持股的比例，是判斷「卡在哪一步」的關鍵
    if stats["users"]:
        rate = stats["pos_users"] / stats["users"] * 100
        lines.append(f"　→ {rate:.0f}% 的使用者建立過持股")

    if stats["hot_pos"]:
        lines.append("")
        lines.append("🔥 最多人持有（3 人以上才顯示）")
        for code, n in stats["hot_pos"]:
            lines.append(f"　{stock_display_name(code, inst)} {code}　{n} 人")
    if stats["hot_wl"]:
        lines.append("")
        lines.append("👀 最多人自選（3 人以上才顯示）")
        for code, n in stats["hot_wl"]:
            lines.append(f"　{stock_display_name(code, inst)} {code}　{n} 人")

    lines += [
        "",
        "─" * 14,
        "※ 只顯示聚合數字。個別使用者的持股內容、",
        "　 股數與金額不會出現在這裡，也不應該去查。",
    ]
    return "\n".join(lines)


def build_user_list_report():
    rows = list_users()
    if not rows:
        return "目前沒有任何使用者紀錄。"

    on = sum(1 for r in rows if r[2])
    lines = [f"👥 使用者名單（共 {len(rows)} 人，已開通 {on} 人）", "─" * 14]
    for i, (uid, name, notify, requested) in enumerate(rows, start=1):
        mark = "🔔" if notify else ("📮" if requested else "　")
        lines.append(f"{i:>2}. {mark} {name}")
    lines += [
        "─" * 14,
        "🔔 已開通　📮 申請中",
        "",
        "開通：輸入「開通 3」",
        "停用：輸入「停用 3」",
    ]
    # 額度上限直接算給管理者看，不要讓他自己去記「大概九個人」
    if on > PUSH_MAX_USERS:
        lines.append(f"⚠️ 已開通 {on} 人，超過上限 {PUSH_MAX_USERS} 人")
        lines.append(f"　推播時只有前 {PUSH_MAX_USERS} 位會收到，請停用部分名額")
    else:
        lines.append(f"（免費方案每月 {PUSH_MONTHLY_QUOTA} 則，"
                     f"每人每交易日 1 則 → 上限 {PUSH_MAX_USERS} 人，"
                     f"還可開通 {PUSH_MAX_USERS - on} 人）")

    # 資料完整性：缺交易日不會報錯，統計照樣算得出來也看起來合理，
    # 只有跟券商對帳才會發現。放在這裡是因為管理者本來就會看名單，
    # 不必特地去翻 cron 紀錄才知道資料有沒有漏。
    n_days, missing, newest = check_data_integrity(30)
    lines += ["", "─" * 14, "📊 法人資料完整性（近30天）"]
    if not n_days:
        lines.append("⚠️ 完全沒有資料，請跑 /cron/fetch-t86")
    else:
        lines.append(f"　已存 {n_days} 個交易日，最新 {newest}")
        if missing:
            shown = "、".join(d.strftime("%m/%d") for d in missing[:6])
            more = f" 等 {len(missing)} 天" if len(missing) > 6 else ""
            lines.append(f"　⚠️ 疑似缺 {shown}{more}")
            lines.append("　（可能是國定假日；若否，用 /backfill 回補）")
        else:
            lines.append("　✅ 區間內無缺漏")

    return "\n".join(lines)


def set_notify(user_id, flag: bool):
    return set_push_flags(user_id, notify=flag)

def get_notify_users():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE notify = TRUE")
        ids = [row[0] for row in cursor.fetchall()]
        cursor.close()
        return ids
    except Exception as e:
        print(f"❌ 讀取通知名單錯誤: {e}")
        return []
    finally:
        release_db_connection(conn)

# ── 自選股分類 ──
# 只有三種，不開放自由輸入：LINE 是純文字介面，自由標籤很容易打錯字，
# 最後變成「長期」「長線」「長綫」三個實際上同一件事的分類。
WATCHLIST_TAGS = ["長線", "短線", "觀察"]
TAG_ALIASES = {
    "長線": "長線", "長期": "長線", "長": "長線", "存股": "長線",
    "短線": "短線", "短期": "短線", "短": "短線", "波段": "短線",
    "觀察": "觀察", "看": "觀察", "追蹤": "觀察", "觀": "觀察",
}
TAG_ICONS = {"長線": "🌱", "短線": "⚡", "觀察": "👀"}


def normalize_tag(raw):
    """把使用者輸入的分類字樣轉成標準值；認不出來就回 None（歸為未分類）。"""
    t = str(raw or "").strip()
    return TAG_ALIASES.get(t)


def add_watchlist_db(user_id, code, tag=None):
    """
    新增或更新自選股。已存在時只更新分類——
    使用者重打一次「加 2330 長線」的意思顯然是要改分類，不是要被告知重複。
    tag 為 None 時保留原本的分類，不會把既有分類洗掉。
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO watchlists (user_id, code, tag) VALUES (%s, %s, %s)
            ON CONFLICT (user_id, code) DO UPDATE SET
                tag = COALESCE(EXCLUDED.tag, watchlists.tag)
            """,
            (str(user_id).strip(), str(code).strip(), tag)
        )
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        print(f"❌ 寫入自選股錯誤: {e}")
        return False
    finally:
        release_db_connection(conn)


def set_watchlist_tag(user_id, code, tag):
    """只改分類，不新增。回傳是否真的有這檔可改。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE watchlists SET tag = %s WHERE user_id = %s AND code = %s",
            (tag, str(user_id).strip(), str(code).strip())
        )
        changed = cursor.rowcount
        conn.commit()
        cursor.close()
        return changed > 0
    except Exception as e:
        conn.rollback()
        print(f"❌ 更新自選股分類錯誤: {e}")
        return False
    finally:
        release_db_connection(conn)


def get_watchlist_tags(user_id):
    """回傳 {code: tag}，未分類的 tag 為 None。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code, tag FROM watchlists WHERE user_id = %s",
                       (str(user_id).strip(),))
        rows = cursor.fetchall()
        cursor.close()
        return {r[0]: r[1] for r in rows}
    except Exception as e:
        print(f"❌ 讀取自選股分類錯誤: {e}")
        return {}
    finally:
        release_db_connection(conn)


def remove_watchlist_db(user_id, code):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM watchlists WHERE user_id = %s AND code = %s",
            (str(user_id).strip(), str(code).strip())
        )
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        print(f"❌ 刪除自選股錯誤: {e}")
        return False
    finally:
        release_db_connection(conn)

def get_user_watchlist(user_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code FROM watchlists WHERE user_id = %s", (str(user_id).strip(),))
        codes = [row[0] for row in cursor.fetchall()]
        cursor.close()
        return codes
    except Exception as e:
        print(f"❌ 讀取自選股錯誤: {e}")
        return []
    finally:
        release_db_connection(conn)

# ============================================================
# 網頁版：登入權杖、持股、問卷設定
# ============================================================
import secrets
import hmac
import hashlib
from functools import wraps
from flask import make_response, redirect, url_for

WEB_SESSION_DAYS = 30  # 權杖有效天數


def _web_csrf_secret():
    """CSRF 簽章金鑰；正式環境建議設定 WEB_CSRF_SECRET。"""
    return (os.environ.get("WEB_CSRF_SECRET")
            or os.environ.get("CRON_SECRET")
            or "change-this-web-csrf-secret")


def current_web_csrf_token():
    """由登入 token 派生每個登入 session 的 CSRF token，不把新欄位寫進資料庫。"""
    token = request.args.get("t") or request.cookies.get("stockbot_token")
    if not token:
        return ""
    return hmac.new(_web_csrf_secret().encode("utf-8"),
                    str(token).encode("utf-8"), hashlib.sha256).hexdigest()


def csrf_hidden_input():
    token = current_web_csrf_token()
    return (f'<input type="hidden" name="csrf_token" value="{html.escape(token, quote=True)}">'
            if token else "")


def inject_csrf_inputs(markup):
    """替所有已登入的 POST 表單加 hidden CSRF 欄位；登入碼表單沒有 session 時不注入。"""
    if not markup or not current_web_csrf_token():
        return markup
    hidden = csrf_hidden_input()
    pattern = r'(<form\b[^>]*\bmethod=["\']post["\'][^>]*>)'
    return re.sub(pattern, lambda m: m.group(1) + hidden, markup,
                  flags=re.IGNORECASE)


def valid_web_csrf():
    expected = current_web_csrf_token()
    supplied = request.form.get("csrf_token", "")
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def safe_html_text(value):
    return html.escape(str(value or ""), quote=True)


def create_web_token(user_id):
    """產生一次性登入連結用的權杖。舊權杖不刪除，讓使用者可以多裝置同時登入。"""
    token = secrets.token_urlsafe(24)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO web_sessions (token, user_id, expires_at)
            VALUES (%s, %s, NOW() + INTERVAL '%s days')
            """,
            (token, str(user_id).strip(), WEB_SESSION_DAYS),
        )
        conn.commit()
        cursor.close()
        return token
    except Exception as e:
        conn.rollback()
        print(f"❌ 建立網頁權杖失敗: {e}")
        return None
    finally:
        release_db_connection(conn)


def resolve_web_token(token):
    """驗證權杖並回傳 user_id；過期或不存在回傳 None。"""
    if not token:
        return None
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM web_sessions WHERE token = %s AND expires_at > NOW()",
            (token,),
        )
        row = cursor.fetchone()
        cursor.close()
        return row[0] if row else None
    except Exception as e:
        print(f"❌ 驗證網頁權杖失敗: {e}")
        return None
    finally:
        release_db_connection(conn)


def current_web_user():
    """從網址參數或 cookie 取得目前登入者。網址參數優先，方便換裝置。"""
    return resolve_web_token(request.args.get("t") or request.cookies.get("stockbot_token"))


def preserve_web_token(markup):
    """LINE WebView 若不保留 cookie，仍讓同一個有效 token 跟著站內導覽走。"""
    token = request.args.get("t") or request.cookies.get("stockbot_token")
    if not token or not markup:
        return markup
    encoded = quote(str(token), safe="")

    def add_token(match):
        prefix, href, closing = match.groups()
        if not href.startswith("/web") or "t=" in href:
            return match.group(0)
        fragment = ""
        if "#" in href:
            href, frag = href.split("#", 1)
            fragment = "#" + frag
        separator = "&" if "?" in href else "?"
        return f"{prefix}{href}{separator}t={encoded}{fragment}{closing}"

    return re.sub(r'((?:href|action)=["\'])(/web[^"\']*)(["\'])',
                  add_token, markup)


# ── 一次性登入驗證碼 ──
WEB_CODE_MINUTES = 30   # 跟網頁連結一起給，可能過一陣子才想到要換瀏覽器開


def create_web_code(user_id):
    """
    產生 6 位數登入碼。同一使用者先前未使用的碼一律作廢，
    避免使用者連續要了三次卻不知道該用哪一組。
    極小機率撞號時重試幾次即可。
    """
    uid = str(user_id).strip()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE web_codes SET used = TRUE WHERE user_id = %s AND used = FALSE",
            (uid,))
        for _ in range(5):
            code = f"{secrets.randbelow(1000000):06d}"
            try:
                cursor.execute(
                    """
                    INSERT INTO web_codes (code, user_id, expires_at)
                    VALUES (%s, %s, NOW() + INTERVAL '%s minutes')
                    """,
                    (code, uid, WEB_CODE_MINUTES),
                )
                conn.commit()
                cursor.close()
                return code
            except Exception:
                conn.rollback()   # 多半是主鍵重複，換一組再試
                cursor = conn.cursor()
        cursor.close()
        return None
    except Exception as e:
        conn.rollback()
        print(f"❌ 建立登入碼失敗: {e}")
        return None
    finally:
        release_db_connection(conn)


def redeem_web_code(code):
    """
    驗證登入碼並換成正式權杖。用過即作廢——
    驗證碼只有六位數，允許重複使用等於把帳號長期暴露在猜號之下。
    回傳 (token, user_id)，失敗回傳 (None, None)。
    """
    code = re.sub(r"\D", "", str(code or ""))   # 容忍使用者貼上時夾帶空格或符號
    if len(code) != 6:
        return None, None

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # 用 UPDATE ... RETURNING 一步完成「檢查並標記已使用」，
        # 兩個請求同時送同一組碼時只有一個會拿到結果。
        cursor.execute(
            """
            UPDATE web_codes SET used = TRUE
            WHERE code = %s AND used = FALSE AND expires_at > NOW()
            RETURNING user_id
            """,
            (code,),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
    except Exception as e:
        conn.rollback()
        print(f"❌ 驗證登入碼失敗: {e}")
        return None, None
    finally:
        release_db_connection(conn)

    if not row:
        return None, None
    uid = row[0]
    return create_web_token(uid), uid


def web_login_required(view):
    """未登入就導向說明頁，不直接報錯——使用者可能只是連結過期。"""
    @wraps(view)
    def wrapper(*args, **kwargs):
        uid = current_web_user()
        if not uid:
            # fragment 不能回完整 HTML 登入頁；否則 loading shell 會把整頁
            # 登入畫面塞進既有 content，造成 LINE WebView 看似突然跳頁。
            if request.args.get("fragment") == "1":
                return make_response("AUTH_EXPIRED", 401,
                                     {"X-StockBot-Auth": "expired"})
            return render_page("需要登入", NEED_LOGIN_HTML), 401
        # fragment 請求只是同一頁的載入片段，不重複計算成一次使用。
        if request.args.get("fragment") != "1":
            path = request.path
            feature = {
                "/web/portfolio": "portfolio",
                "/web/positions": "positions",
                "/web/leaderboard": "leaderboard",
                "/web/trades": "trades",
                "/web/compare": "compare",
                "/web/more": "more",
                "/web/settings": "settings",
            }.get(path)
            if path == "/web/screener":
                mode = request.args.get("mode", "blackhorse")
                feature = "radar" if mode == "radar" else "blackhorse" if mode == "blackhorse" else "screener"
            if feature:
                record_activity(uid, feature, action="open", source="web")
        return view(uid, *args, **kwargs)
    return wrapper


# ── 持股 CRUD ──
def get_positions(user_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, code, shares, cost, bought_on, note
            FROM positions WHERE user_id = %s ORDER BY id
            """,
            (str(user_id).strip(),),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [
            {"id": r[0], "code": r[1], "shares": r[2], "cost": r[3],
             "bought_on": r[4], "note": r[5]}
            for r in rows
        ]
    except Exception as e:
        print(f"❌ 讀取持股失敗: {e}")
        return []
    finally:
        release_db_connection(conn)


def merge_positions(positions):
    """
    同一檔股票的多筆進場合併成一列顯示，成本用加權平均。
    資料庫仍保留每一筆原始紀錄——之後要分析「是否在虧損時加碼」
    需要知道每次進場的時間與價位，合併掉就永遠算不出來了。
    回傳每組另附 lots（原始明細），供展開檢視與刪除。
    """
    grouped = {}
    for p in positions:
        g = grouped.setdefault(p["code"], {
            "code": p["code"], "shares": 0, "cost_total": 0.0, "lots": []})
        g["shares"] += p["shares"]
        g["cost_total"] += p["cost"] * p["shares"]
        g["lots"].append(p)

    merged = []
    for g in grouped.values():
        dates = [l["bought_on"] for l in g["lots"] if l["bought_on"]]
        merged.append({
            "code": g["code"],
            "shares": g["shares"],
            "cost": g["cost_total"] / g["shares"] if g["shares"] else 0.0,
            "bought_on": min(dates) if dates else None,   # 以最早一筆算持有天數
            "lots": sorted(g["lots"], key=lambda x: (x["bought_on"] or date.min)),
        })
    return merged


def add_position(user_id, code, shares, cost, bought_on=None, note=None):
    try:
        shares = int(shares)
        cost = float(cost)
    except (TypeError, ValueError):
        return False
    if shares <= 0 or cost <= 0 or not math.isfinite(cost):
        return False
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO positions (user_id, code, shares, cost, bought_on, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (str(user_id).strip(), str(code).strip(), int(shares),
             float(cost), bought_on or None, note or None),
        )
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        print(f"❌ 新增持股失敗: {e}")
        return False
    finally:
        release_db_connection(conn)


def delete_position(user_id, pos_id):
    """一定要同時比對 user_id，否則有人改網址上的 id 就能刪別人的持股。"""
    try:
        pos_id = int(pos_id)
    except (TypeError, ValueError):
        return False
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM positions WHERE id = %s AND user_id = %s",
                       (int(pos_id), str(user_id).strip()))
        deleted = cursor.rowcount
        conn.commit()
        cursor.close()
        return deleted > 0
    except Exception as e:
        conn.rollback()
        print(f"❌ 刪除持股失敗: {e}")
        return False
    finally:
        release_db_connection(conn)


def sell_position(user_id, pos_id, sell_shares,
                  sell_price=None, fee=None, tax=None):
    """原子化賣出：持股更新與已實現損益必須同時成功或同時回滾。

    賣價、手續費與證交稅由使用者提供時以實際對帳單為準；留空賣價才
    使用即時市價，留空費用／稅才使用既有估算公式。資料庫交易內會以
    FOR UPDATE 鎖定持股，避免同一筆持股被並行賣出兩次。
    """
    try:
        pos_id = int(pos_id)
        sell_shares = int(sell_shares)
    except (TypeError, ValueError):
        return False, "賣出資料格式不正確", None
    if pos_id <= 0:
        return False, "找不到這筆持股", None
    if sell_shares <= 0:
        return False, "賣出股數必須大於 0", None

    def parse_nonnegative(value, label):
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label}必須是有效數字")
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError(f"{label}必須是有效的非負數字")
        return parsed

    try:
        sell_price = parse_nonnegative(sell_price, "賣出價格")
        fee = parse_nonnegative(fee, "手續費")
        tax = parse_nonnegative(tax, "證交稅")
    except ValueError as exc:
        return False, str(exc), None
    if sell_price is not None and sell_price <= 0:
        return False, "賣出價格必須大於 0", None

    uid = str(user_id).strip()
    # 先只讀取代號再抓市價；真正扣股與寫入紀錄時會重新 FOR UPDATE 驗證。
    code = None
    if sell_price is None:
        lookup_conn = get_db_connection()
        try:
            lookup_cur = lookup_conn.cursor()
            lookup_cur.execute(
                "SELECT code FROM positions WHERE id = %s AND user_id = %s",
                (pos_id, uid))
            lookup_row = lookup_cur.fetchone()
            lookup_cur.close()
            lookup_conn.rollback()
            if not lookup_row:
                return False, "找不到這筆持股", None
            code = str(lookup_row[0]).strip()
        except Exception as exc:
            lookup_conn.rollback()
            print(f"❌ 讀取賣出股票失敗: {exc}")
            return False, "系統錯誤，請稍後再試", None
        finally:
            release_db_connection(lookup_conn)

        try:
            price_data = get_realtime_stock(code)
            sell_price = price_data.get("close") if isinstance(price_data, dict) else None
            sell_price = float(sell_price) if sell_price is not None else None
        except Exception as exc:
            print(f"⚠️ 取得賣出市價失敗 {code}: {exc}")
            sell_price = None
        if sell_price is None or not math.isfinite(sell_price) or sell_price <= 0:
            return False, "目前查不到有效賣價，持股未變更；請稍後再試或手動輸入成交價", None

    if not math.isfinite(float(sell_price)) or float(sell_price) <= 0:
        return False, "賣出價格必須是有效且大於 0 的數字", None
    sell_price = float(sell_price)

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT code, shares, cost, bought_on FROM positions "
            "WHERE id = %s AND user_id = %s FOR UPDATE",
            (pos_id, uid))
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return False, "找不到這筆持股", None

        code, current_shares, lot_cost, bought_on = row
        try:
            current_shares = int(current_shares)
            lot_cost = float(lot_cost)
        except (TypeError, ValueError):
            conn.rollback()
            return False, "持股資料格式錯誤，未執行賣出", None
        if current_shares <= 0 or not math.isfinite(lot_cost) or lot_cost <= 0:
            conn.rollback()
            return False, "持股成本或股數資料異常，未執行賣出", None
        if sell_shares > current_shares:
            conn.rollback()
            return False, f"賣出股數不能超過持有股數（{current_shares:,} 股）", None

        gross = sell_shares * sell_price
        if fee is None:
            fee = float(broker_fee(gross))
        if tax is None:
            tax = float(gross * (TAX_RATE_ETF if is_etf(code) else TAX_RATE_STOCK))
        if (not math.isfinite(fee) or fee < 0 or
                not math.isfinite(tax) or tax < 0):
            conn.rollback()
            return False, "手續費或證交稅計算結果異常，未執行賣出", None

        cost_total = sell_shares * lot_cost
        realized_pl = (gross - fee - tax) - cost_total
        realized_pct = (realized_pl / cost_total * 100) if cost_total else None
        if not math.isfinite(realized_pl) or (realized_pct is not None and not math.isfinite(realized_pct)):
            conn.rollback()
            return False, "損益計算結果異常，未執行賣出", None

        if sell_shares == current_shares:
            cursor.execute(
                "DELETE FROM positions WHERE id = %s AND user_id = %s",
                (pos_id, uid))
        else:
            cursor.execute(
                "UPDATE positions SET shares = shares - %s "
                "WHERE id = %s AND user_id = %s AND shares >= %s",
                (sell_shares, pos_id, uid, sell_shares))
        if cursor.rowcount != 1:
            conn.rollback()
            return False, "持股在處理期間已變更，請重新整理後再試", None

        # 與 positions 更新共用同一個 cursor／transaction，不能只成功一半。
        cursor.execute(
            """
            INSERT INTO realized_trades
                (user_id, code, shares, buy_cost, sell_price,
                 realized_pl, realized_pct, bought_on, sold_on, fee, tax)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (uid, str(code).strip(), sell_shares, lot_cost, sell_price,
             realized_pl, realized_pct, bought_on or None, taiwan_today(), fee, tax),
        )
        conn.commit()
        cursor.close()
        return True, None, {
            "code": str(code).strip(), "shares": sell_shares,
            "sell_price": sell_price, "cost": lot_cost,
            "pl": realized_pl, "pct": realized_pct,
            "fee": fee, "tax": tax,
            "held_days": ((taiwan_today() - bought_on).days if bought_on else None),
        }
    except Exception as exc:
        conn.rollback()
        print(f"❌ 賣出持股與已實現損益交易失敗: {exc}")
        return False, "賣出未完成，持股與已實現損益均未變更", None
    finally:
        release_db_connection(conn)


def record_realized_trade(user_id, code, shares, buy_cost, sell_price,
                          realized_pl, realized_pct, bought_on, fee=None, tax=None):
    """記一筆已實現損益。賣出時連市價都查不到就不會呼叫這裡。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO realized_trades
                (user_id, code, shares, buy_cost, sell_price,
                 realized_pl, realized_pct, bought_on, sold_on, fee, tax)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_DATE, %s, %s)
            """,
            (str(user_id).strip(), str(code).strip(), int(shares), float(buy_cost),
             float(sell_price), realized_pl, realized_pct, bought_on or None,
             fee, tax),
        )
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        print(f"❌ 記錄已實現損益失敗: {e}")
        return False
    finally:
        release_db_connection(conn)


def get_realized_trades(user_id, limit=100, code=None, month=None):
    """
    回傳已實現損益紀錄，依賣出日期新到舊排序。
    code：只看某一檔；month：只看某個月（格式 YYYY-MM）。
    兩者都用條件式組裝 SQL，不用「%s IS NULL OR …」——
    psycopg2 無法推斷那種寫法的參數型別，會直接丟型別錯誤，
    而錯誤被 except 吃掉後只回空清單，表面上看起來像「沒有紀錄」。
    """
    where, params = ["user_id = %s"], [str(user_id).strip()]
    if code:
        where.append("code = %s")
        params.append(str(code).strip())
    if month:
        where.append("to_char(sold_on, 'YYYY-MM') = %s")
        params.append(str(month).strip())
    params.append(limit)

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT code, shares, buy_cost, sell_price, realized_pl,
                   realized_pct, bought_on, sold_on, fee, tax
            FROM realized_trades WHERE {' AND '.join(where)}
            ORDER BY sold_on DESC, id DESC LIMIT %s
            """,
            tuple(params),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [
            {"code": r[0], "shares": r[1], "buy_cost": r[2], "sell_price": r[3],
             "realized_pl": r[4], "realized_pct": r[5],
             "bought_on": r[6], "sold_on": r[7],
             "fee": r[8], "tax": r[9]}
            for r in rows
        ]
    except Exception as e:
        print(f"❌ 讀取已實現損益失敗: {e}")
        return []
    finally:
        release_db_connection(conn)


def get_trade_filters(user_id):
    """回傳這位使用者交易紀錄裡出現過的月份與股票代號，用來產生篩選選項。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT to_char(sold_on, 'YYYY-MM') AS m
            FROM realized_trades WHERE user_id = %s AND sold_on IS NOT NULL
            ORDER BY m DESC
            """,
            (str(user_id).strip(),))
        months = [r[0] for r in cursor.fetchall()]
        cursor.execute(
            "SELECT DISTINCT code FROM realized_trades WHERE user_id = %s ORDER BY code",
            (str(user_id).strip(),))
        codes = [r[0] for r in cursor.fetchall()]
        cursor.close()
        return months, codes
    except Exception as e:
        print(f"❌ 讀取交易篩選選項失敗: {e}")
        return [], []
    finally:
        release_db_connection(conn)


def summarize_trades(trades):
    """
    交易紀錄的統計摘要。只算有損益數字的那些。

    盈虧比（平均獲利 ÷ 平均虧損）比勝率更值得看：
    勝率七成但每次小賺、輸一次全吐回去，長期仍是虧的。
    兩個數字要一起看才知道這套做法能不能持續。
    """
    priced = [t for t in trades if t["realized_pl"] is not None]
    if not priced:
        return None

    wins = [t for t in priced if t["realized_pl"] > 0]
    losses = [t for t in priced if t["realized_pl"] < 0]
    total_pl = sum(t["realized_pl"] for t in priced)
    avg_win = (sum(t["realized_pl"] for t in wins) / len(wins)) if wins else 0
    avg_loss = (abs(sum(t["realized_pl"] for t in losses)) / len(losses)) if losses else 0
    hold = [(t["sold_on"] - t["bought_on"]).days
            for t in priced if t["bought_on"] and t["sold_on"]]
    costs = sum((t.get("fee") or 0) + (t.get("tax") or 0) for t in priced)

    return {
        "count": len(priced),
        "total_pl": total_pl,
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(priced) * 100,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "payoff": (avg_win / avg_loss) if avg_loss else None,
        "best": max(priced, key=lambda t: t["realized_pl"]),
        "worst": min(priced, key=lambda t: t["realized_pl"]),
        "avg_hold": (sum(hold) / len(hold)) if hold else None,
        "costs": costs,
    }


def save_portfolio_snapshot(user_id, total_value, total_cost, taiex_close):
    """
    存一天的組合市值快照。同一天重複寫入會覆蓋（保持最新收盤數字），
    這樣即使 cron 意外跑了兩次也不會產生重複的一天。
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO portfolio_snapshots
                (user_id, snapshot_date, total_value, total_cost, taiex_close)
            VALUES (%s, CURRENT_DATE, %s, %s, %s)
            ON CONFLICT (user_id, snapshot_date) DO UPDATE SET
                total_value = EXCLUDED.total_value,
                total_cost = EXCLUDED.total_cost,
                taiex_close = EXCLUDED.taiex_close
            """,
            (str(user_id).strip(), total_value, total_cost, taiex_close),
        )
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        print(f"❌ 寫入組合快照失敗: {e}")
        return False
    finally:
        release_db_connection(conn)


# ── 排行榜 ──
def get_leaderboard_member(user_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT nickname, joined_on, COALESCE(show_holdings, FALSE) "
                    "FROM leaderboard_members WHERE user_id = %s",
                    (str(user_id).strip(),))
        r = cur.fetchone()
        cur.close()
        return ({"nickname": r[0], "joined_on": r[1], "show_holdings": r[2]}
                if r else None)
    except Exception as e:
        print(f"❌ 讀取排行榜成員失敗: {e}")
        return None
    finally:
        release_db_connection(conn)


def join_leaderboard(user_id, nickname, show_holdings=False):
    """
    加入排行榜。重新加入時「不」重設起算日——
    否則賠錢的人可以退出再加入把負報酬洗掉，排行榜就沒有意義了。
    """
    nick = str(nickname or "").strip()[:12]
    if not nick:
        return False, "請輸入暱稱"
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO leaderboard_members (user_id, nickname, show_holdings)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                nickname = EXCLUDED.nickname,
                show_holdings = EXCLUDED.show_holdings
            """,
            (str(user_id).strip(), nick, bool(show_holdings)))
        conn.commit()
        cur.close()
        clear_leaderboard_cache()
        return True, None
    except Exception as e:
        conn.rollback()
        print(f"❌ 加入排行榜失敗: {e}")
        return False, "加入失敗，請稍後再試"
    finally:
        release_db_connection(conn)


def leave_leaderboard(user_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM leaderboard_members WHERE user_id = %s",
                    (str(user_id).strip(),))
        conn.commit()
        cur.close()
        clear_leaderboard_cache()
        return True
    except Exception as e:
        conn.rollback()
        print(f"❌ 退出排行榜失敗: {e}")
        return False
    finally:
        release_db_connection(conn)


def get_realized_by_date(user_id, days=180):
    """
    取每日的賣出成本基礎與賣出金額，供 TWR 計算資金流用。
    回傳 {日期: (賣掉部位的原始成本, 實際賣得金額)}
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT sold_on,
                   SUM(shares * buy_cost),
                   SUM(shares * sell_price)
            FROM realized_trades
            WHERE user_id = %s AND sold_on >= CURRENT_DATE - %s
            GROUP BY sold_on
            """,
            (str(user_id).strip(), days))
        rows = cur.fetchall()
        cur.close()
        return {r[0]: (float(r[1] or 0), float(r[2] or 0)) for r in rows}
    except Exception as e:
        print(f"❌ 讀取賣出紀錄失敗: {e}")
        return {}
    finally:
        release_db_connection(conn)


def compute_twr(snaps, since=None, realized=None):
    """
    時間加權報酬率（TWR）。

    為什麼不能直接用「市值變化」：加碼會讓市值變大，但那是你自己投錢進去，
    不是投資賺來的。要把資金進出扣掉才是真正的報酬：

        單日報酬 = (今日市值 − 昨日市值 − 當日資金淨流入) ÷ 昨日市值

    資金流不能只看成本變化。買進時「成本增加＝當下市值」沒問題，
    但賣出時你拿回的是「市價」、成本卻是按「原始成本」減少——
    賺錢的部位一賣，差額會被誤算成虧損（實測 +10% 會變成 0%）。
    所以賣出的部分改用 realized_trades 裡的實際賣出金額：

        當日買進金額 = 成本變化 + 當日賣掉部位的原始成本
        資金淨流入   = 當日買進金額 − 當日實際賣得金額

    回傳 [(日期, 累積報酬%)]，資料不足時回傳空清單。
    """
    realized = realized or {}
    pts = [s for s in snaps if s.get("value") and s["value"] > 0]
    if since:
        pts = [s for s in pts if s["date"] >= since]
    if len(pts) < 2:
        return []

    cum, out = 1.0, [(pts[0]["date"], 0.0)]
    for prev, cur in zip(pts, pts[1:]):
        sold_cost, proceeds = realized.get(cur["date"], (0.0, 0.0))
        d_cost = (cur.get("cost") or 0) - (prev.get("cost") or 0)
        bought = d_cost + sold_cost          # 還原出當日的買進金額
        flow = bought - proceeds             # 淨流入（負數代表提領）
        denom = prev["value"]
        if denom <= 0:
            continue
        r = (cur["value"] - prev["value"] - flow) / denom
        # 單日 ±50% 以上多半是資料異常（例如整批重輸持股），跳過不計入
        if abs(r) > 0.5:
            continue
        cum *= (1 + r)
        out.append((cur["date"], (cum - 1) * 100))
    return out


def taiex_series(snaps, since=None):
    """從快照裡的大盤收盤算同期累積漲跌幅，當作排行榜的對照組。"""
    pts = [s for s in snaps if s.get("taiex")]
    if since:
        pts = [s for s in pts if s["date"] >= since]
    if len(pts) < 2:
        return []
    base = pts[0]["taiex"]
    return [(s["date"], (s["taiex"] - base) / base * 100) for s in pts]


def window_return(curve, days=30):
    """
    取曲線最後 N 天的報酬率。

    累積報酬曲線是連乘出來的，所以某一段的報酬不能直接相減，
    要用比值還原：(1 + 期末) ÷ (1 + 期初) − 1。
    直接相減在報酬率大的時候會明顯失真——
    例如從 +100% 到 +120%，實際只漲了 10%，相減卻會得到 20%。
    """
    if len(curve) < 2:
        return None
    # 以「今天」為基準往回算，不能用曲線最後一點——
    # 那樣 cutoff 會跟著資料一起往前移，永遠都在窗內，
    # 於是 50 天前就停止更新的曲線也會被當成「近 30 天」而給出數字。
    cutoff = taiwan_today() - timedelta(days=days)
    seg = [(d, v) for d, v in curve if d >= cutoff]
    if len(seg) < 2:
        return None
    start, end = seg[0][1] / 100, seg[-1][1] / 100
    # 期初累積報酬接近 -100% 時分母趨近 0，算出來的百分比會爆掉，
    # 而那種情況本身就是資料異常，不該硬給一個數字
    if start <= -0.99:
        return None
    return ((1 + end) / (1 + start) - 1) * 100


def max_drawdown(curve):
    """
    從累積報酬曲線算最大回檔（%）。

    為什麼要列這個：只用報酬率排名會獎勵冒險——重壓一檔賭對了就登頂，
    但那跟「操作得好」是兩件事。把最大回檔放在旁邊，
    看得出同樣 +20% 的人，一個中途只回檔 5%、另一個回檔 30%，
    後者承受的痛苦與運氣成分高得多。
    """
    if len(curve) < 2:
        return None
    peak, mdd = curve[0][1], 0.0
    for _d, v in curve:
        peak = max(peak, v)
        mdd = max(mdd, peak - v)
    return mdd


def summarize_member_holdings(uid, prices, inst, positions=None, ind_map=None,
                              joined_on=None):
    """
    整理一位成員的持股摘要：最大持股、表現最好的持股、產業集中度。
    只有本人勾選「公開持股」時才會被呼叫。

    「最佳持股」的報酬率改用加入排行榜日後的歷史收盤價計算，
    不再使用買進成本；這樣才和排行榜的加入後報酬口徑一致。
    """
    positions = (positions if positions is not None
                 else merge_positions(get_positions(uid)))
    if not positions:
        return None

    ind_map = (ind_map if ind_map is not None else get_industry_map() or {})
    rows, total = [], 0.0
    for p in positions:
        pr = prices.get(p["code"])
        if not pr:
            continue
        value = pr["close"] * p["shares"]
        total += value
        # 用加入排行榜日之後的第一個有效交易日收盤價當基準。
        # 加入日可能是週末或假日，因此不能要求序列一定存在該日，
        # 要取加入日之後第一根真實日 K；找不到就不捏造報酬率。
        since_return = None
        if joined_on and pr.get("close_dates") and pr.get("closes"):
            start_date = (joined_on.date()
                          if isinstance(joined_on, datetime) else joined_on)
            history = [(d, c) for d, c in zip(
                pr.get("close_dates", []), pr.get("closes", []))
                       if d >= start_date and c is not None]
            if history:
                base_close = history[0][1]
                if base_close:
                    since_return = (pr["close"] - base_close) / base_close * 100

        rows.append({
            "code": p["code"],
            "name": short_company_name(stock_display_name(p["code"], inst, pr["name"])),
            "value": value,
            "ret": since_return,
            "industry": industry_name(ind_map[p["code"]]) if ind_map.get(p["code"]) else None,
        })
    if not rows or total <= 0:
        return None

    for r in rows:
        r["weight"] = r["value"] / total * 100

    biggest = max(rows, key=lambda r: r["weight"])
    scored = [r for r in rows if r["ret"] is not None]
    best = max(scored, key=lambda r: r["ret"]) if scored else None

    # 產業集中度：最大產業佔多少。這比列出個股洩漏的資訊少，
    # 但同樣看得出風格——重壓單一族群還是分散配置。
    by_ind = {}
    for r in rows:
        if r["industry"]:
            by_ind[r["industry"]] = by_ind.get(r["industry"], 0) + r["weight"]
    top_ind = max(by_ind.items(), key=lambda x: x[1]) if by_ind else None

    return {"biggest": biggest, "best": best, "top_industry": top_ind}


def save_leaderboard_rank_snapshots(snapshot_date=None):
    # 每日收盤後保存短線／長線名次；沒有足夠快照不補造排名。
    snapshot_date = snapshot_date or taiwan_today()
    boards, _ = build_leaderboard(top_n=100, days=365)
    rows = []
    for board_name in ("short", "long"):
        for rank, row in enumerate(boards.get(board_name, []), start=1):
            value = row.get("m30") if board_name == "short" else row.get("ret")
            rows.append((board_name, snapshot_date, row.get("user_id"), rank, value))
    if not rows:
        return 0
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        execute_values(cur, '''
            INSERT INTO leaderboard_rank_snapshots
                (board, snapshot_date, user_id, rank, return_pct)
            VALUES %s
            ON CONFLICT (board, snapshot_date, user_id) DO UPDATE SET
                rank=EXCLUDED.rank, return_pct=EXCLUDED.return_pct
        ''', rows)
        conn.commit()
        return len(rows)
    except Exception as e:
        conn.rollback()
        print(f"❌ 儲存排行榜名次快照失敗: {e}")
        return 0
    finally:
        release_db_connection(conn)


def get_rank_status(user_id, board, current_rank):
    # 取得最近一次有效排名與連續升降次數；資料不足明確回傳 None。
    if current_rank is None:
        return {"rank": None, "previous": None, "delta": None, "streak": 0, "direction": None}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('''
            SELECT rank FROM leaderboard_rank_snapshots
            WHERE board=%s AND user_id=%s AND snapshot_date < CURRENT_DATE
            ORDER BY snapshot_date DESC LIMIT 8
        ''', (board, str(user_id).strip()))
        previous_rows = [r[0] for r in cur.fetchall()]
        cur.close()
    except Exception as e:
        print(f"❌ 讀取排名變化失敗: {e}")
        previous_rows = []
    finally:
        release_db_connection(conn)
    if not previous_rows:
        return {"rank": current_rank, "previous": None, "delta": None, "streak": 0, "direction": None}
    previous = previous_rows[0]
    delta = previous - current_rank
    direction = "up" if delta > 0 else ("down" if delta < 0 else "same")
    seq = [current_rank] + previous_rows
    streak = 0
    last_sign = 1 if delta > 0 else (-1 if delta < 0 else 0)
    if last_sign:
        for before, after in zip(seq[1:], seq[:-1]):
            sign = 1 if before - after > 0 else (-1 if before - after < 0 else 0)
            if sign != last_sign:
                break
            streak += 1
    return {"rank": current_rank, "previous": previous, "delta": delta,
            "streak": streak, "direction": direction}


def _rank_status_from_previous(current_rank, previous_rows):
    if current_rank is None:
        return {"rank": None, "previous": None, "delta": None, "streak": 0, "direction": None}
    if not previous_rows:
        return {"rank": current_rank, "previous": None, "delta": None, "streak": 0, "direction": None}
    previous = previous_rows[0]
    delta = previous - current_rank
    direction = "up" if delta > 0 else ("down" if delta < 0 else "same")
    seq = [current_rank] + previous_rows
    streak = 0
    last_sign = 1 if delta > 0 else (-1 if delta < 0 else 0)
    if last_sign:
        for before, after in zip(seq[1:], seq[:-1]):
            sign = 1 if before - after > 0 else (-1 if before - after < 0 else 0)
            if sign != last_sign:
                break
            streak += 1
    return {"rank": current_rank, "previous": previous, "delta": delta,
            "streak": streak, "direction": direction}


def get_rank_status_map(rank_inputs):
    """一次讀取多位成員的最近排名快照，避免 board_rows 產生 N+1 查詢。"""
    inputs = [(str(board), str(user_id).strip(), rank)
              for board, user_id, rank in (rank_inputs or [])]
    result = {(board, user_id): _rank_status_from_previous(rank, [])
              for board, user_id, rank in inputs}
    if not inputs:
        return result
    user_ids = sorted({user_id for _board, user_id, _rank in inputs})
    boards = sorted({board for board, _user_id, _rank in inputs})
    conn = get_db_connection()
    previous_map = defaultdict(list)
    try:
        cur = conn.cursor()
        cur.execute('''
            SELECT board, user_id, rank
            FROM (
                SELECT board, user_id, rank,
                       ROW_NUMBER() OVER (
                         PARTITION BY board, user_id ORDER BY snapshot_date DESC
                       ) AS rn
                FROM leaderboard_rank_snapshots
                WHERE board = ANY(%s)
                  AND user_id = ANY(%s)
                  AND snapshot_date < %s
            ) ranked
            WHERE rn <= 8
            ORDER BY board, user_id, rn
        ''', (boards, user_ids, taiwan_today()))
        for board, user_id, rank in cur.fetchall():
            previous_map[(str(board), str(user_id).strip())].append(rank)
        cur.close()
    except Exception as e:
        print(f"❌ 批次讀取排名變化失敗: {e}")
    finally:
        release_db_connection(conn)
    for board, user_id, rank in inputs:
        result[(board, user_id)] = _rank_status_from_previous(
            rank, previous_map.get((board, user_id), []))
    return result


def get_fast_rank_summary(user_id):
    """首頁 fast 專用：只讀最近兩次已保存名次，不重算全體排行榜。"""
    uid = str(user_id).strip()
    grouped = defaultdict(list)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('''
            SELECT board, snapshot_date, rank
            FROM leaderboard_rank_snapshots
            WHERE user_id=%s AND board = ANY(%s)
            ORDER BY board ASC, snapshot_date DESC
        ''', (uid, ["short", "long"]))
        for board, snapshot_date, rank in cur.fetchall():
            if len(grouped[board]) < 2:
                grouped[board].append((snapshot_date, rank))
        cur.close()
    except Exception as exc:
        print(f"⚠️ 首頁快速讀取排行榜快照失敗: {exc}")
    finally:
        release_db_connection(conn)

    result = {}
    for board, label in (("short", "短線"), ("long", "長線")):
        entries = grouped.get(board, [])
        current = entries[0] if entries else None
        previous = entries[1] if len(entries) > 1 else None
        current_rank = current[1] if current else None
        previous_rank = previous[1] if previous else None
        if current_rank is None:
            delta, direction = None, None
        elif previous_rank is None:
            delta, direction = None, None
        else:
            delta = previous_rank - current_rank
            direction = "up" if delta > 0 else ("down" if delta < 0 else "same")
        result[board] = {
            "rank": current_rank,
            "previous": previous_rank,
            "delta": delta,
            "streak": 0,
            "direction": direction,
            "label": label,
            "snapshot_date": (current[0].isoformat()
                              if current and hasattr(current[0], "isoformat")
                              else (str(current[0]) if current else None)),
        }
    return result


def get_my_rank_summary(user_id, boards=None, rank_status_map=None):
    # 首頁用的個人排名摘要；短線與長線分開計算。
    boards = boards if boards is not None else build_leaderboard(top_n=100, days=365)[0]
    if rank_status_map is None:
        rank_inputs = []
        for board in ("short", "long"):
            rows = boards.get(board, [])
            current_rank = next((i for i, row in enumerate(rows, 1)
                                 if row.get("user_id") == str(user_id)), None)
            rank_inputs.append((board, user_id, current_rank))
        rank_status_map = get_rank_status_map(rank_inputs)
    result = {}
    for board, label in (("short", "短線"), ("long", "長線")):
        current_rows = boards.get(board, [])
        current_row = next((row for row in current_rows
                            if row.get("user_id") == str(user_id)), None)
        current_rank = next((i for i, row in enumerate(current_rows, 1)
                             if row.get("user_id") == str(user_id)), None)
        status = dict(rank_status_map.get(
            (board, str(user_id).strip()),
            _rank_status_from_previous(current_rank, [])))
        # 保留該成員的真實榜單資料，首頁與排行榜 UI 可共用，
        # 不需要再跑一次完整排行榜計算。
        status["row"] = current_row
        status["label"] = label
        result[board] = status
    return result


_leaderboard_cache = {}
_leaderboard_cache_lock = threading.Lock()
LEADERBOARD_CACHE_SECONDS = 30


def clear_leaderboard_cache():
    """成員加入、退出或設定變更後立即清掉排行榜結果。"""
    with _leaderboard_cache_lock:
        _leaderboard_cache.clear()


def build_leaderboard(top_n=20, days=365):
    """
    算出排行榜。分短線與長線兩榜，因為那本來就是兩種不同的能力——
    長期穩健和短期爆發硬塞進同一個排行，比的會變成誰先加入。

      短線榜：近 30 天報酬。所有人區間一致，最公平，新人也馬上有得比。
      長線榜：加入後的累積報酬。加得早的人天然佔優，所以一定要把
              參加天數列在旁邊，讓人自己判斷。

    不設「滿 N 天才能上榜」的門檻：一個要等一個月才看得到自己的功能，
    多數人第一天就放棄了。改成照實標示天數，天數太少的加註提醒——
    「顯示得夠清楚」比「擋住不讓進」好。
    """
    cache_key = (int(top_n), int(days))
    now = time.time()
    with _leaderboard_cache_lock:
        cached = _leaderboard_cache.get(cache_key)
        if cached and now - cached["at"] < LEADERBOARD_CACHE_SECONDS:
            return cached["value"]

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, nickname, joined_on, "
                    "COALESCE(show_holdings, FALSE) FROM leaderboard_members")
        members = cur.fetchall()
        cur.close()
    except Exception as e:
        print(f"❌ 讀取排行榜失敗: {e}")
        # 回傳結構必須跟正常情況一致，少一層會讓呼叫端拋 ValueError、
        # 整頁 500，而前端只看得到「載入失敗」，完全看不出真正原因。
        return {"long": [], "short": [], "waiting": []}, ({}, [])
    finally:
        release_db_connection(conn)

    if not members:
        return {"long": [], "short": [], "waiting": []}, ({}, [])

    # 同一次排行榜計算中，持股資料只讀一次；原本公開持股成員會在
    # 收集行情、整理公開摘要、計算持股檔數時重複查詢資料庫。
    positions_map = {
        str(m[0]): merge_positions(get_positions(m[0]))
        for m in members
    }

    # 先收集所有要公開持股的成員的代號，一次並行抓完報價
    open_uids = [m[0] for m in members if m[3]]
    codes = set()
    for uid in open_uids:
        codes |= {p["code"] for p in positions_map.get(str(uid), [])}
    # 最佳持股要看加入日後的歷史價格，不能只抓預設的近 3 個月。
    # 1 年足以涵蓋一般排行榜成員的加入期間；超過期間者會顯示資料不足，
    # 不用買進成本混算成另一種口徑。
    prices = (get_realtime_stocks_bulk(list(codes), workers=16, rng="1y")
              if codes else {})
    inst = fetch_institutional_data() or {} if codes else {}
    ind_map = get_industry_map() or {} if codes else {}

    rows, series_map, market = [], {}, []
    for uid, nick, joined, show in members:
        snaps = get_portfolio_snapshots(uid, days=days)
        curve = compute_twr(snaps, since=joined,
                            realized=get_realized_by_date(uid, days))
        mk = taiex_series(snaps, since=joined)
        if not market and mk:
            market = mk

        holds = (summarize_member_holdings(
            uid, prices, inst,
            positions=positions_map.get(str(uid), []), ind_map=ind_map,
            joined_on=joined)
                 if show else None)
        base = {
            "user_id": str(uid),
            "nickname": nick,
            "holdings": len(positions_map.get(str(uid), [])),
            "joined": joined,
            "show": show,
            "detail": holds,
        }
        if len(curve) < 2:
            rows.append({**base, "ret": None, "days": 0, "mdd": None,
                         "excess": None, "m30": None, "m30_days": 0})
            continue

        # 近 30 天實際涵蓋幾天，用來標示樣本夠不夠
        cutoff = taiwan_today() - timedelta(days=30)
        seg = [d for d, _v in curve if d >= cutoff]
        m30_days = (seg[-1] - seg[0]).days if len(seg) >= 2 else 0

        rows.append({
            **base,
            "ret": curve[-1][1],
            "days": (curve[-1][0] - curve[0][0]).days,
            "mdd": max_drawdown(curve),
            "excess": (curve[-1][1] - mk[-1][1]) if mk else None,
            "mkt_ret": mk[-1][1] if mk else None,
            "m30": window_return(curve, 30),
            "m30_days": m30_days,
        })
        # 用穩定且唯一的 user_id 作為曲線索引；暱稱可以重複，不能拿來當 key。
        series_map[str(uid)] = {"nickname": str(nick), "curve": curve}

    scored = [r for r in rows if r["ret"] is not None]
    long_board = sorted(scored, key=lambda r: r["ret"], reverse=True)[:top_n]
    short_board = sorted([r for r in scored if r["m30"] is not None],
                         key=lambda r: r["m30"], reverse=True)[:top_n]
    waiting = [r for r in rows if r["ret"] is None]
    value = ({"long": long_board, "short": short_board, "waiting": waiting},
             (series_map, market))
    with _leaderboard_cache_lock:
        _leaderboard_cache[cache_key] = {"at": time.time(), "value": value}
    return value


def get_portfolio_snapshots(user_id, days=120):
    """取近 N 天的組合快照，依日期由舊到新排序（畫圖要正序）。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT snapshot_date, total_value, total_cost, taiex_close
            FROM portfolio_snapshots WHERE user_id = %s
            ORDER BY snapshot_date DESC LIMIT %s
            """,
            (str(user_id).strip(), days),
        )
        rows = cursor.fetchall()
        cursor.close()
        rows.reverse()
        return [{"date": r[0], "value": r[1], "cost": r[2], "taiex": r[3]} for r in rows]
    except Exception as e:
        print(f"❌ 讀取組合快照失敗: {e}")
        return []
    finally:
        release_db_connection(conn)


def get_all_position_user_ids():
    """回傳目前有持股紀錄的所有 user_id，供每日快照 cron 逐一處理。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT user_id FROM positions ORDER BY user_id")
        ids = [r[0] for r in cursor.fetchall()]
        cursor.close()
        return ids
    except Exception as e:
        print(f"❌ 讀取持股使用者清單失敗: {e}")
        return []
    finally:
        release_db_connection(conn)


def get_all_watchlist_user_ids():
    """回傳有自選股的所有 user_id，供每日評分快照 cron 逐一處理。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT user_id FROM watchlists ORDER BY user_id")
        ids = [r[0] for r in cursor.fetchall()]
        cursor.close()
        return ids
    except Exception as e:
        print(f"❌ 讀取自選股使用者清單失敗: {e}")
        return []
    finally:
        release_db_connection(conn)


def normalize_code(raw):
    """
    從輸入取出股票代號。不能只留數字——主動式ETF的第六碼是英文
    （A 為股票型、D 為債券型，例如 00981A），濾掉字母就查不到了。
    回傳大寫代號，格式不符則回傳空字串。
    """
    if not raw:
        return ""
    m = re.search(r"(\d{4,6}[A-Za-z]?)", str(raw).strip())
    return m.group(1).upper() if m else ""


# --- 穩健的股價抓取引擎 ---
# 上櫃股票要試到第二個後綴（.TWO）才抓得到，等於白白多打一次 Yahoo。
# 記住每個代號成功過的後綴，之後直接從那個開始試，上櫃股的請求數直接砍半。
# 只存在記憶體，重啟就沒了，但那只是回到原本的行為，不影響正確性。
_suffix_cache = {}

# 即時行情的外部請求是網頁反覆開啟時最明顯的等待來源。
# 同一個 Render process 內，90 秒內的重整、頁面切換與 fragment 請求
# 共用同一份結果；這不會把日線資料永久存死，也不會跨日期沿用昨天的行情。
_realtime_cache = {}
_realtime_cache_lock = threading.Lock()
REALTIME_CACHE_SECONDS = 90


def get_realtime_stock(code, rng="3mo"):
    """
    rng 是 Yahoo 的資料區間。預設 3mo 足夠算位階與均線；
    持股頁要畫「買進點」時才改用 1y——持有超過三個月的部位，
    用 3mo 的序列根本涵蓋不到買進日，圖上就只能寫「買進日不在此區間內」，
    而那正是這張圖唯一比券商 App 多給的東西。
    只有需要的頁面才付這個成本，選股台掃上百檔時仍用 3mo。
    """
    code = str(code).strip()
    stock_name = STOCK_NAME_MAP.get(code, code)

    # 日期納入 key，避免跨午夜把前一交易日的結果沿用到新的一天。
    cache_day = taiwan_now().date().isoformat()
    cache_key = f"{code}:{rng}:{cache_day}"
    now = time.time()
    with _realtime_cache_lock:
        cached = _realtime_cache.get(cache_key)
        if cached and now - cached["at"] < REALTIME_CACHE_SECONDS:
            return cached["data"]

    # 已知後綴排前面試，未知就照原順序
    known = _suffix_cache.get(code)
    suffixes = [known, ".TW", ".TWO"] if known else [".TW", ".TWO"]
    seen = set()
    suffixes = [s for s in suffixes if not (s in seen or seen.add(s))]

    for suffix in suffixes:
        try:
            symbol = f"{code}{suffix}"
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                   f"?range={rng}&interval=1d")
            headers = {'User-Agent': 'Mozilla/5.0'}
            res = requests.get(url, headers=headers, timeout=8).json()

            result_meta = res.get('chart', {}).get('result', [])
            if not result_meta:
                continue

            meta = result_meta[0].get('meta', {})
            timestamps = result_meta[0].get('timestamp', [])
            indicators = result_meta[0].get('indicators', {}).get('quote', [{}])[0]
            raw_closes = indicators.get('close', [])
            raw_highs = indicators.get('high', [])
            raw_lows = indicators.get('low', [])
            raw_volumes = indicators.get('volume', [])

            # 把「日期」跟「收盤價」配對起來，過濾掉沒有成交/資料缺失的那幾筆，
            # 同時保留正確的日期對應，不能只看陣列位置。
            tw_tz = timezone(timedelta(hours=8))
            bars = []
            for i, (ts, c) in enumerate(zip(timestamps, raw_closes)):
                if c is None:
                    continue
                bar_date = datetime.fromtimestamp(ts, tw_tz).date()
                h = raw_highs[i] if i < len(raw_highs) and raw_highs[i] is not None else c
                l = raw_lows[i] if i < len(raw_lows) and raw_lows[i] is not None else c
                v = raw_volumes[i] if i < len(raw_volumes) and raw_volumes[i] is not None else 0
                bars.append((bar_date, c, h, l, v))

            today_date = datetime.now(tw_tz).date()

            close = meta.get('regularMarketPrice', 0.0)
            if not close or close == 0:
                close = bars[-1][1] if bars else 0.0

            # 判斷「日K序列」最後一筆到底是不是今天：
            # - 是今天 → 昨收 = 倒數第二筆
            # - 還停在昨天（Yahoo 資料還沒更新到今天）→ 倒數第一筆本身才是昨收，
            #   不能再往前抓倒數第二筆，不然會變成抓到前天，算出兩天以上的
            #   累積漲幅，誤標成「當日漲幅」。
            if bars and bars[-1][0] == today_date:
                prev_close = bars[-2][1] if len(bars) >= 2 else meta.get('chartPreviousClose', close)
                hist = bars[:-1]  # 計算位階時排除今天，避免今天自己抬高自己的高點
            elif bars:
                # 序列停在昨天（或更早）——可能是盤前、假日、非交易日。
                # 若目前報價就等於最後一根K的收盤，代表這根K就是「最新收盤」，
                # 昨收要再往前一根，否則會拿自己比自己，永遠顯示 0.00%。
                if abs(close - bars[-1][1]) < 0.001 and len(bars) >= 2:
                    prev_close = bars[-2][1]
                    hist = bars[:-1]
                else:
                    prev_close = bars[-1][1]
                    hist = bars
            else:
                prev_close = meta.get('chartPreviousClose', close)
                hist = []

            if not close or close == 0:
                continue

            pct = ((close - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
            high = meta.get('regularMarketDayHigh', close) or close
            low = meta.get('regularMarketDayLow', close) or close
            volume = meta.get('regularMarketVolume', 0) or 0

            # --- 位階與量能：判斷「這根K棒站在什麼位置」 ---
            h20 = [b[2] for b in hist[-20:]]
            l20 = [b[3] for b in hist[-20:]]
            h60 = [b[2] for b in hist[-60:]]
            l60 = [b[3] for b in hist[-60:]]
            c20 = [b[1] for b in hist[-20:]]
            v20 = [b[4] for b in hist[-20:] if b[4]]

            high_20d = max(h20) if h20 else None
            low_20d = min(l20) if l20 else None
            high_60d = max(h60) if h60 else None
            low_60d = min(l60) if l60 else None
            ma20 = round(sum(c20) / len(c20), 2) if c20 else None
            avg_vol_20 = (sum(v20) / len(v20)) if v20 else None
            vol_ratio = round(volume / avg_vol_20, 2) if avg_vol_20 else None

            # 距離近60日高點多遠（0% 代表就在最高點，負值代表還在下方）
            pos_vs_60d_high = round((close - high_60d) / high_60d * 100, 2) if high_60d else None

            # 連續上漲／下跌天數（含今天）。
            # up_streak == 0 代表今天並非上漲，不能當成「首根上漲」；
            # 要判斷「首根上漲」必須是 up_streak == 1。
            series = [b[1] for b in hist] + [close]
            up_streak = down_streak = 0
            for i in range(len(series) - 1, 0, -1):
                if series[i] > series[i - 1]:
                    up_streak += 1
                else:
                    break
            for i in range(len(series) - 1, 0, -1):
                if series[i] < series[i - 1]:
                    down_streak += 1
                else:
                    break

            # 支撐壓力改用實際的近期高低點與均線，不再用「今日高低價微調」
            # 若已突破近60日高點，上方沒有參考壓力可言，回傳 None 讓顯示端說明
            if high_60d and close >= high_60d:
                resistance = None
            elif high_20d and close >= high_20d:
                resistance = round(high_60d, 2) if high_60d else None
            elif high_20d:
                resistance = round(high_20d, 2)
            else:
                resistance = round(high * 1.01, 2)
            # 支撐必須在現價「下方」才有意義。
            # 原本沒有候選時會退回 low_20d，但股價跌破近 20 日低點時
            # 那個數字反而在現價上方——畫面就會出現「支撐 1655、現價 1625」
            # 這種自相矛盾的東西。依序往更低的參考位找，
            # 全都在上方就代表近期支撐已經跌破，用今日低點當最後防線。
            support_candidates = [x for x in [low_20d, ma20, low_60d]
                                  if x and x < close]
            if support_candidates:
                support = round(max(support_candidates), 2)
                broke_support = False
            else:
                support = round(low, 2) if low and low < close else round(close * 0.97, 2)
                broke_support = True

            _suffix_cache[code] = suffix  # 這個後綴有效，下次直接從它開始

            result = {
                "code": code,
                "name": stock_name,
                "close": float(close),
                "pct": float(pct),
                "high": float(high),
                "low": float(low),
                "volume": int(volume),
                "resistance": resistance,
                "support": support,
                # 近期支撐全數跌破時要讓顯示端說明，不能只丟一個數字
                "broke_support": broke_support,
                "high_20d": high_20d,
                "low_20d": low_20d,
                "high_60d": high_60d,
                "low_60d": low_60d,
                "ma20": ma20,
                "vol_ratio": vol_ratio,
                "pos_vs_60d_high": pos_vs_60d_high,
                "up_streak": up_streak,
                "down_streak": down_streak,
                # 收盤序列。rng 較長時這裡也會比較長，供持股頁畫走勢用；
                # 相關係數那邊會自己只取近 60 筆，不受影響。
                "closes": [b[1] for b in hist] + [float(close)],
                # 對應的日期。畫損益走勢時要靠它標出「你買在哪一天」——
                # 只有收盤價的話，圖上永遠只能寫「近 N 個交易日」，
                # 而「什麼時候發生的」正是圖能回答、數字回答不了的問題。
                "close_dates": [b[0] for b in hist] + [today_date],
            }
            with _realtime_cache_lock:
                _realtime_cache[cache_key] = {"at": now, "data": result}
                # 控制長期常駐 process 的記憶體，不影響正常短期命中。
                if len(_realtime_cache) > 4000:
                    cutoff = time.time() - REALTIME_CACHE_SECONDS * 2
                    for k, v in list(_realtime_cache.items()):
                        if v.get("at", 0) < cutoff:
                            _realtime_cache.pop(k, None)
            return result
        except:
            continue
    return None


def get_realtime_stocks_bulk(codes, workers=12, rng="3mo"):
    """
    並行抓多檔報價，回傳 {code: data 或 None}。

    原本是逐檔序列請求：10 檔就要等 10 次網路來回，總時間是各檔相加。
    抓報價幾乎全是「等 Yahoo 回應」，執行緒在等 I/O 時會放開 GIL，
    所以用執行緒並行是有效的（這不是 CPU 密集工作，不受 GIL 限制）。
    改成並行後總時間約等於最慢的那一檔，而不是全部相加。

    workers 刻意不開太大：同時打太多請求可能被 Yahoo 限流或拒絕，
    12 條在實務上已能把幾十檔壓進數秒內。
    """
    codes = list(dict.fromkeys(str(c).strip() for c in codes if c))
    if not codes:
        return {}
    if len(codes) == 1:  # 只有一檔就不必付出開執行緒池的成本
        return {codes[0]: get_realtime_stock(codes[0], rng)}

    def safe_fetch(c):
        # 單檔失敗不能拖垮整批，一律吞掉例外回 None，交由呼叫端顯示「查無行情」
        try:
            return get_realtime_stock(c, rng)
        except Exception as e:
            print(f"⚠️ 並行抓取失敗 {c}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=min(workers, len(codes))) as ex:
        return dict(zip(codes, ex.map(safe_fetch, codes)))


# --- 三大法人買賣超（TWSE T86，全市場，一天快取一次） ---
# 快取用「今天日期」當 key，但實際資料可能是往前找到的最近一個交易日
_t86_cache = {"cache_date": None, "data_date": None, "data": {},
              "last_attempt": 0}

def shares_to_lots(shares):
    """
    股數轉張數。必須用截斷（向零取整）而不是 Python 的 //——
    // 對負數是向下取整，-287,500 股會變成 -288 張而不是 -287，
    每一個賣超的日子都會多算一張，累積十天就是系統性偏差。
    券商與看盤軟體顯示的都是截斷值，要對得起來就得一致。
    """
    return int(shares / 1000)


def _fetch_t86_for_date(query_date):
    """向 TWSE 抓取指定日期的 T86 資料，成功回傳 dict，查無資料回傳 None。"""
    try:
        url = "https://www.twse.com.tw/rwd/zh/fund/T86"
        params = {"date": query_date, "selectType": "ALL", "response": "json"}
        res = requests.get(url, params=params, timeout=10, headers={'User-Agent': 'Mozilla/5.0'}).json()

        if res.get("stat") != "OK":
            return None

        fields = res.get("fields", [])
        rows = res.get("data", [])

        def col(name, default=None):
            return fields.index(name) if name in fields else default

        code_i = col("證券代號")
        name_i = col("證券名稱")
        foreign_i = col("外陸資買賣超股數(不含外資自營商)")
        trust_i = col("投信買賣超股數")
        dealer_i = col("自營商買賣超股數")
        total_i = col("三大法人買賣超股數")

        def to_int(s):
            try:
                return int(str(s).replace(",", ""))
            except (ValueError, TypeError):
                return 0

        result = {}
        for row in rows:
            code = row[code_i].strip()
            name = row[name_i].strip()
            foreign_net = to_int(row[foreign_i]) if foreign_i is not None else 0
            trust_net = to_int(row[trust_i]) if trust_i is not None else 0
            dealer_net = to_int(row[dealer_i]) if dealer_i is not None else 0
            total_net = to_int(row[total_i]) if total_i is not None else (foreign_net + trust_net + dealer_net)

            result[code] = {
                "name": name,
                "foreign_net_lots": shares_to_lots(foreign_net),
                "trust_net_lots": shares_to_lots(trust_net),
                "dealer_net_lots": shares_to_lots(dealer_net),
                "total_net_lots": shares_to_lots(total_net),
            }

        if not result:
            return None

        print(f"✅ T86 抓取成功（{query_date}），共 {len(rows)} 筆")
        return result
    except Exception as e:
        print(f"❌ 抓取三大法人資料錯誤（{query_date}）: {e}")
        return None

def save_t86_history(query_date, data):
    """
    把某一天的 T86 資料整批寫進 inst_history。
    query_date 格式 YYYYMMDD。同一天重複寫入會覆蓋（保持最新）。
    """
    if not data:
        return
    try:
        trade_date = datetime.strptime(query_date, "%Y%m%d").date()
    except ValueError:
        print(f"❌ save_t86_history 日期格式錯誤: {query_date}")
        return

    rows = [
        (
            code,
            trade_date,
            info.get("name", ""),
            info.get("foreign_net_lots", 0),
            info.get("trust_net_lots", 0),
            info.get("dealer_net_lots", 0),
            info.get("total_net_lots", 0),
        )
        for code, info in data.items()
    ]

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        execute_values(
            cursor,
            """
            INSERT INTO inst_history
                (code, trade_date, name, foreign_net_lots,
                 trust_net_lots, dealer_net_lots, total_net_lots)
            VALUES %s
            ON CONFLICT (code, trade_date) DO UPDATE SET
                name = EXCLUDED.name,
                foreign_net_lots = EXCLUDED.foreign_net_lots,
                trust_net_lots = EXCLUDED.trust_net_lots,
                dealer_net_lots = EXCLUDED.dealer_net_lots,
                total_net_lots = EXCLUDED.total_net_lots
            """,
            rows,
            page_size=500,
        )
        conn.commit()
        cursor.close()
        print(f"💾 已存入 T86 歷史（{query_date}），共 {len(rows)} 檔")
    except Exception as e:
        conn.rollback()
        print(f"❌ 寫入 T86 歷史失敗（{query_date}）: {e}")
    finally:
        release_db_connection(conn)


def get_consecutive_days(code, direction="buy", max_days=20):
    """
    從 inst_history 算「三大法人連續買超（或賣超）天數」。
    direction: "buy" 看連續買超，"sell" 看連續賣超。
    只算最近 max_days 個有紀錄的交易日；資料不足就回傳目前算得出的天數。
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT total_net_lots FROM inst_history
            WHERE code = %s
            ORDER BY trade_date DESC
            LIMIT %s
            """,
            (str(code).strip(), max_days),
        )
        rows = cursor.fetchall()
        cursor.close()
    except Exception as e:
        print(f"❌ 查詢連續買賣超失敗（{code}）: {e}")
        return 0
    finally:
        release_db_connection(conn)

    streak = 0
    for (lots,) in rows:
        lots = lots or 0
        if direction == "buy" and lots > 0:
            streak += 1
        elif direction == "sell" and lots < 0:
            streak += 1
        else:
            break
    return streak


def get_investor_breakdown(codes, days=10):
    """
    分別取外資、投信、自營商的近 N 日累計買超與連續買超天數。

    為什麼要拆開看：三大法人合計是三種完全不同的人加在一起，
    同樣「合計買超 3,000 張」可能是投信在建倉，也可能只是自營商的
    權證避險部位，訊號價值差很多。
      外資　量最大，但常是 MSCI 調權重、ETF 被動調整，未必代表看好
      投信　要對基金績效負責，通常做過研究才買，連續買最有參考價值
      自營　很大一部分是發行權證後的避險，今天買明天沖是常態

    回傳 {code: {"foreign": {...}, "trust": {...}, "dealer": {...}}}
    每個內含 cum（累計張數）與 streak（連續買超天數）。
    """
    codes = [str(c).strip() for c in codes if c]
    if not codes:
        return {}

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT code, trade_date, foreign_net_lots, trust_net_lots, dealer_net_lots
            FROM inst_history
            WHERE code = ANY(%s) AND trade_date >= (
                SELECT MIN(d) FROM (
                    SELECT DISTINCT trade_date AS d FROM inst_history
                    ORDER BY d DESC LIMIT %s
                ) recent
            )
            ORDER BY code, trade_date DESC
            """,
            (codes, days),
        )
        rows = cursor.fetchall()
        cursor.close()
    except Exception as e:
        print(f"❌ 查詢法人分項失敗: {e}")
        return {}
    finally:
        release_db_connection(conn)

    series = {}
    for code, _d, f, t, dl in rows:
        series.setdefault(code, []).append((f or 0, t or 0, dl or 0))

    result = {}
    for code in codes:
        hist = series.get(code, [])
        entry = {}
        for idx, key in enumerate(("foreign", "trust", "dealer")):
            vals = [h[idx] for h in hist]
            streak = 0
            for v in vals:                 # 已是日期新到舊
                if v > 0:
                    streak += 1
                else:
                    break
            entry[key] = {"cum": sum(vals), "streak": streak,
                          "today": vals[0] if vals else 0}
        result[code] = entry
    return result


def describe_investor_breakdown(bd, compact=False):
    """
    把三方拆解講成人看得懂的樣子，並點出誰是主導方。

    只在訊號夠明確時才下註解。判斷門檻用「相對比例」而非固定張數：
    200 張對中小型股是大事，對台積電只是零頭，用固定值會讓大型股
    每天都觸發同一句話，那行字就完全沒有資訊量了。
    """
    if not bd:
        return None
    f, t, d = bd["foreign"], bd["trust"], bd["dealer"]

    def line(icon, label, x):
        streak = f"　連買 {x['streak']} 日" if x["streak"] >= 2 else ""
        return f"{icon} {label} {x['cum']:+,} 張{streak}"

    lines = [line("🌐", "外資", f), line("🏦", "投信", t), line("🏭", "自營", d)]

    mags = {"外資": abs(f["cum"]), "投信": abs(t["cum"]), "自營": abs(d["cum"])}
    activity = sum(mags.values())
    note = None

    # 整體量太小就不解讀——三方各買幾十張是雜訊，不是訊號
    if activity >= 300:
        top = max(mags, key=mags.get)
        others = sorted((v for k, v in mags.items() if k != top), reverse=True)
        vals = {"外資": f, "投信": t, "自營": d}[top]

        # 主導方：要明顯超過第二名（兩倍以上），否則只是三方都有在動
        if mags[top] >= others[0] * 2 and mags[top] >= activity * 0.5:
            side = "買超" if vals["cum"] > 0 else "賣超"
            if top == "投信":
                note = (f"投信主導{side}"
                        + ("，且連續買進，通常代表有研究支撐"
                           if vals["streak"] >= 3 and vals["cum"] > 0 else ""))
            elif top == "自營":
                note = "自營主導，可能含權證避險部位，訊號價值較低"
            else:
                note = f"外資主導{side}，留意是否為指數成分調整所致"

        # 分歧：投信與外資方向相反，且「兩邊力道相當」才算真的分歧。
        # 一邊幾千張、另一邊幾百張不是分歧，那是其中一方在主導、
        # 另一方只是小幅調節——用比例判斷才分得出這兩種情況。
        lo, hi = sorted([abs(t["cum"]), abs(f["cum"])])
        if (t["cum"] * f["cum"] < 0 and hi >= activity * 0.3
                and lo >= hi * 0.5 and lo >= 500):
            who = "投信買、外資賣" if t["cum"] > 0 else "外資買、投信賣"
            note = f"{who}（{t['cum']:+,} / {f['cum']:+,} 張），力道相當，看法分歧"

    if note:
        lines.append(f"　→ {note}")
    return ("　".join(lines[:3]) if compact else "\n".join(lines))


def get_consecutive_days_batch(codes, direction="buy", max_days=20):
    """
    一次查多檔股票的連續買（賣）超天數，只打一次資料庫。
    回傳 {code: streak}。黑馬／雷達要掃幾十檔，用這個比逐檔查快很多。
    """
    codes = [str(c).strip() for c in codes]
    if not codes:
        return {}

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT code, trade_date, total_net_lots FROM inst_history
            WHERE code = ANY(%s)
            ORDER BY code, trade_date DESC
            """,
            (codes,),
        )
        rows = cursor.fetchall()
        cursor.close()
    except Exception as e:
        print(f"❌ 批次查詢連續買賣超失敗: {e}")
        return {}
    finally:
        release_db_connection(conn)

    series = {}
    for code, _trade_date, lots in rows:
        series.setdefault(code, []).append(lots or 0)

    result = {}
    for code in codes:
        streak = 0
        for lots in series.get(code, [])[:max_days]:
            if direction == "buy" and lots > 0:
                streak += 1
            elif direction == "sell" and lots < 0:
                streak += 1
            else:
                break
        result[code] = streak
    return result


# --- 產業別代碼對照（TWSE t187ap03_L 的「產業別」欄位是代碼，不是文字） ---
INDUSTRY_NAME_MAP = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "07": "化學生技醫療", "08": "玻璃陶瓷",
    "09": "造紙工業", "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業",
    "13": "電子工業", "14": "建材營造", "15": "航運業", "16": "觀光餐旅",
    "17": "金融保險", "18": "貿易百貨", "19": "綜合", "20": "其他",
    "21": "化學工業", "22": "生技醫療業", "23": "油電燃氣業", "24": "半導體業",
    "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業", "28": "電子零組件業",
    "29": "電子通路業", "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業",
    "33": "農業科技業", "34": "電子商務", "35": "綠能環保", "36": "數位雲端",
    "37": "運動休閒", "38": "居家生活",
}

def is_financial(code, ind_map):
    """
    是否為金融保險業（產業代碼 17）。
    務必 strip()：資料庫存進來的代碼可能帶空白，
    顯示用的 industry_name() 有 strip 而過濾條件沒有的話，
    就會出現「畫面顯示金融保險、卻沒被排除」的矛盾。
    """
    raw = ind_map.get(str(code).strip())
    return bool(raw) and str(raw).strip().zfill(2) == "17"


def industry_name(code):
    """把產業別代碼轉成中文名稱；查不到就回傳原代碼，不會讓資料消失。"""
    code = str(code).strip().zfill(2)
    return INDUSTRY_NAME_MAP.get(code, f"未知類別({code})")


# ── 資料來源涵蓋範圍 ──
# 證交所 OpenAPI 只含「上市」；上櫃與興櫃要另外接櫃買中心（TPEx）。
# 少了這塊，上櫃／興櫃股票會出現「名稱＝代號」「營收無資料」的情況。
TWSE_BASE = "https://openapi.twse.com.tw/v1"
TPEX_BASE = "https://www.tpex.org.tw/openapi/v1"


# 櫃買中心（TPEx）的 SSL 憑證缺少 Subject Key Identifier 擴充欄位，
# 新版 OpenSSL 預設開啟 RFC 5280 嚴格檢查會直接拒絕連線，
# 錯誤訊息是 CERTIFICATE_VERIFY_FAILED: Missing Subject Key Identifier。
# 這裡只關掉「嚴格擴充欄位檢查」，仍然驗證憑證鏈與主機名稱，
# 不使用 verify=False（那會連對方是不是本人都不檢查）。
class _RelaxedStrictAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


_session = requests.Session()
_session.mount("https://www.tpex.org.tw", _RelaxedStrictAdapter())
_session.headers.update({"User-Agent": "Mozilla/5.0"})


def _get_json(url, timeout=25):
    try:
        return _session.get(url, timeout=timeout).json()
    except Exception as e:
        print(f"❌ 抓取失敗 {url}: {e}")
        return None


def fetch_json_bulk(urls, timeout=25, workers=6):
    """
    並行抓多個 JSON 端點，回傳 {url: 資料 或 None}。
    月營收要打 3 個端點、估值要打 2 個，序列跑等於把各自的 timeout 相加，
    而它們彼此不相依，沒有理由一個等完才發下一個。
    """
    urls = list(dict.fromkeys(u for u in urls if u))
    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as ex:
        return dict(zip(urls, ex.map(lambda u: _get_json(u, timeout), urls)))


def _pick(row, *names):
    """欄位名稱各來源不一致，依序嘗試；找不到回傳空字串。"""
    for n in names:
        if n in row and row[n] not in (None, ""):
            return str(row[n]).strip()
    return ""


def fetch_and_save_industry():
    """
    抓公司基本資料（名稱＋產業別），涵蓋上市、上櫃、興櫃三個市場。
    產業別幾乎不變，抓一次就夠，之後想更新再打一次端點即可。
    回傳 (筆數, 產業別樣本清單)。
    """
    sources = [
        ("上市", f"{TWSE_BASE}/opendata/t187ap03_L"),
        ("上櫃", f"{TPEX_BASE}/mopsfin_t187ap03_O"),
        ("興櫃", f"{TPEX_BASE}/mopsfin_t187ap03_R"),
    ]

    records, counts = [], {}
    fetched = fetch_json_bulk([u for _m, u in sources])   # 三個市場並行抓
    for market, url in sources:
        rows = fetched.get(url)
        if not rows:
            counts[market] = 0
            continue
        n = 0
        for row in rows:
            # 欄位名稱各市場不同：證交所用中文，櫃買中心用英文。
            # 產業別在上櫃／興櫃叫 SecuritiesIndustryCode，
            # 漏掉它會讓 1,200 多檔上櫃興櫃股票沒有產業別，
            # 進而完全不出現在依類股分組的選股結果裡。
            code = _pick(row, "公司代號", "SecuritiesCompanyCode", "Code")
            name = _pick(row, "公司簡稱", "CompanyAbbreviation",
                         "CompanyName", "Name", "公司名稱")
            industry = _pick(row, "產業別", "SecuritiesIndustryCode", "Industry")
            if not code:
                continue
            records.append((code, name, industry.zfill(2) if industry else "", market))
            n += 1
        counts[market] = n
        with_ind = sum(1 for c, _nm, i, m in records if m == market and i)
        print(f"✅ {market}公司基本資料 {n} 筆（有產業別 {with_ind} 筆）")

    if not records:
        return 0, []

    if not records:
        return 0, []

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        execute_values(
            cursor,
            """
            INSERT INTO stock_info (code, name, industry, market)
            VALUES %s
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                industry = EXCLUDED.industry,
                market = EXCLUDED.market
            """,
            records,
            page_size=500,
        )
        conn.commit()
        cursor.close()
        print(f"💾 已存入公司基本資料，共 {len(records)} 檔")
    except Exception as e:
        conn.rollback()
        print(f"❌ 寫入公司基本資料失敗: {e}")
        return 0, []
    finally:
        release_db_connection(conn)

    ind_counts = {}
    for _c, _n, i, m in records:
        if i:
            ind_counts[m] = ind_counts.get(m, 0) + 1
    sample = [f"{m}：{c} 檔（有產業別 {ind_counts.get(m, 0)} 檔）"
              for m, c in counts.items()]
    sample += sorted({ind for _, _, ind, _ in records if ind})[:30]
    return len(records), sample


_industry_cache = {"map": None}
_name_cache = {"map": None}
_STOCK_INFO_FILE_CACHE = "/tmp/stock_bot_stock_info_cache.json"
_STOCK_INFO_FILE_CACHE_TTL = 86400
_stock_info_file_lock = threading.Lock()
_stock_info_file_loaded_mtime = 0.0


def _load_stock_info_file_cache():
    """跨 Gunicorn worker 重用已由資料庫／warmup 產生的真實 stock_info 快照。"""
    global _stock_info_file_loaded_mtime
    with _stock_info_file_lock:
        try:
            file_mtime = os.path.getmtime(_STOCK_INFO_FILE_CACHE)
            if time.time() - file_mtime > _STOCK_INFO_FILE_CACHE_TTL:
                return
            # worker 可能在 warmup 寫檔前就已處理過第一次請求；
            # 只要檔案 mtime 變新，就再次載入，不讓一次未命中永久卡住。
            if file_mtime <= _stock_info_file_loaded_mtime:
                return
            with open(_STOCK_INFO_FILE_CACHE, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            names = payload.get("names") or {}
            industries = payload.get("industries") or {}
            if names:
                _name_cache["map"] = names
            if industries:
                _industry_cache["map"] = industries
            _stock_info_file_loaded_mtime = file_mtime
        except FileNotFoundError:
            return
        except Exception as exc:
            print(f"⚠️ 讀取 stock_info 檔案快取失敗: {exc}")


def _write_stock_info_file_cache():
    """以原子替換寫入 stock_info 快照，避免 worker 讀到半份 JSON。"""
    payload = {"names": _name_cache.get("map") or {},
               "industries": _industry_cache.get("map") or {},
               "saved_at": taiwan_now().isoformat()}
    temp_path = _STOCK_INFO_FILE_CACHE + f".{os.getpid()}.tmp"
    try:
        with _stock_info_file_lock:
            with open(temp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_path, _STOCK_INFO_FILE_CACHE)
    except Exception as exc:
        print(f"⚠️ 寫入 stock_info 檔案快取失敗: {exc}")
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def get_name_map(force_reload=False):
    """
    代號→公司名稱。來自 stock_info（含上市、上櫃、興櫃），
    比程式裡那份只有十幾檔的寫死對照表完整得多。
    """
    _load_stock_info_file_cache()
    if _name_cache["map"] is not None and not force_reload:
        return _name_cache["map"]
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code, name FROM stock_info WHERE name IS NOT NULL AND name <> ''")
        _name_cache["map"] = {c: n for c, n in cursor.fetchall()}
        cursor.close()
        _write_stock_info_file_cache()
        return _name_cache["map"]
    except Exception as e:
        print(f"❌ 讀取名稱對照失敗: {e}")
        return {}
    finally:
        release_db_connection(conn)


def short_company_name(name):
    """
    公司全名太長不適合列表顯示（例：昕力資訊股份有限公司）。
    有簡稱就用簡稱；只有全名時去掉常見的組織型態後綴。
    """
    if not name:
        return name
    n = str(name).strip()
    for suffix in ("股份有限公司", "有限公司", "股份公司", "公司"):
        if n.endswith(suffix) and len(n) > len(suffix):
            n = n[: -len(suffix)]
            break
    return n.strip()


def stock_display_name(code, inst_data=None, fallback=None):
    """
    取得顯示名稱，優先序：當日法人資料 → stock_info → 寫死對照表 → 代號。
    興櫃股票不在法人資料裡，所以 stock_info 這層很重要。
    """
    code = str(code).strip()
    if inst_data:
        n = inst_data.get(code, {}).get("name")
        if n:
            return n
    n = get_name_map().get(code)
    if n:
        return short_company_name(n)
    return fallback or STOCK_NAME_MAP.get(code, code)

def get_industry_map(force_reload=False):
    """回傳 {代號: 產業別}。讀一次就快取在記憶體，並跨 worker 重用檔案快照。"""
    _load_stock_info_file_cache()
    if _industry_cache["map"] is not None and not force_reload:
        return _industry_cache["map"]

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code, industry FROM stock_info WHERE industry IS NOT NULL AND industry <> ''")
        rows = cursor.fetchall()
        cursor.close()
        _industry_cache["map"] = {code: ind for code, ind in rows}
        _write_stock_info_file_cache()
        return _industry_cache["map"]
    except Exception as e:
        print(f"❌ 讀取產業別失敗: {e}")
        return {}
    finally:
        release_db_connection(conn)


def save_revenue_history(period, data):
    """
    存下這一期的月營收快照。TWSE 只給最新一期，所以歷史只能這樣一個月一個月累積。
    累積幾期之後就能算「連續成長月數」。
    """
    if not period or not data:
        return
    rows = [
        (code, str(period),
         info.get("yoy_pct"), info.get("cum_yoy_pct"),
         info.get("mom_pct"), info.get("month_revenue"))
        for code, info in data.items()
    ]

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        execute_values(
            cursor,
            """
            INSERT INTO revenue_history
                (code, period, yoy_pct, cum_yoy_pct, mom_pct, month_revenue)
            VALUES %s
            ON CONFLICT (code, period) DO UPDATE SET
                yoy_pct = EXCLUDED.yoy_pct,
                cum_yoy_pct = EXCLUDED.cum_yoy_pct,
                mom_pct = EXCLUDED.mom_pct,
                month_revenue = EXCLUDED.month_revenue
            """,
            rows,
            page_size=500,
        )
        conn.commit()
        cursor.close()
        print(f"💾 已存入月營收快照（{period}），共 {len(rows)} 檔")
    except Exception as e:
        conn.rollback()
        print(f"❌ 寫入月營收快照失敗（{period}）: {e}")
    finally:
        release_db_connection(conn)


def get_revenue_growth_months(codes, max_months=12):
    """
    算「單月營收年增率連續為正的月數」。需要資料庫累積多期才有意義，
    現在只有一期，所有股票都會是 0 或 1，屬預期行為。
    """
    codes = [str(c).strip() for c in codes]
    if not codes:
        return {}

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT code, period, yoy_pct FROM revenue_history
            WHERE code = ANY(%s)
            ORDER BY code, period DESC
            """,
            (codes,),
        )
        rows = cursor.fetchall()
        cursor.close()
    except Exception as e:
        print(f"❌ 查詢營收連續成長失敗: {e}")
        return {}
    finally:
        release_db_connection(conn)

    series = {}
    for code, _period, yoy in rows:
        series.setdefault(code, []).append(yoy)

    result = {}
    for code in codes:
        streak = 0
        for yoy in series.get(code, [])[:max_months]:
            if yoy is not None and yoy > 0:
                streak += 1
            else:
                break
        result[code] = streak
    return result


# --- 估值資料（TWSE 每日本益比／殖利率／股價淨值比） ---
_valuation_cache = {"date": None, "data": {}}
_VALUATION_FILE_CACHE = "/tmp/stock_bot_valuation_cache.json"
_VALUATION_FILE_CACHE_TTL = 86400
_valuation_file_lock = threading.Lock()


def _load_valuation_file_cache(today):
    try:
        with _valuation_file_lock:
            if (not os.path.exists(_VALUATION_FILE_CACHE) or
                    time.time() - os.path.getmtime(_VALUATION_FILE_CACHE) > _VALUATION_FILE_CACHE_TTL):
                return None
            with open(_VALUATION_FILE_CACHE, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if payload.get("date") != today or not payload.get("data"):
                return None
            return payload["data"]
    except Exception as exc:
        print(f"⚠️ 讀取估值檔案快取失敗: {exc}")
        return None


def _write_valuation_file_cache(today, data):
    temp_path = _VALUATION_FILE_CACHE + f".{os.getpid()}.tmp"
    try:
        with _valuation_file_lock:
            with open(temp_path, "w", encoding="utf-8") as fh:
                json.dump({"date": today, "data": data}, fh,
                          ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_path, _VALUATION_FILE_CACHE)
    except Exception as exc:
        print(f"⚠️ 寫入估值檔案快取失敗: {exc}")
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def fetch_valuation():
    """
    抓全市場本益比、殖利率、股價淨值比。一天快取一次。
    注意：TWSE 的本益比是用「近四季已申報財報」算的歷史本益比，
    不是分析師預估的未來本益比，看的時候要記得這點。
    """
    today = taiwan_now().strftime("%Y%m%d")
    if _valuation_cache["date"] == today and _valuation_cache["data"]:
        return _valuation_cache["data"]

    file_data = _load_valuation_file_cache(today)
    if file_data:
        _valuation_cache["date"] = today
        _valuation_cache["data"] = file_data
        print("⚡ 估值改讀同日檔案快照，共 %s 筆" % len(file_data))
        return file_data

    def to_float(v):
        try:
            f = float(str(v).replace(",", "").strip())
            return f if f > 0 else None
        except (ValueError, TypeError):
            return None

    result = {}
    # 上市與上櫃兩個端點並行抓，不必等第一個回來才發第二個
    twse_url = f"{TWSE_BASE}/exchangeReport/BWIBBU_ALL"
    tpex_url = f"{TPEX_BASE}/tpex_mainboard_peratio_analysis"
    fetched = fetch_json_bulk([twse_url, tpex_url])

    # 上市
    for row in fetched.get(twse_url) or []:
        code = _pick(row, "Code")
        if code:
            result[code] = {"pe": to_float(row.get("PEratio")),
                            "pb": to_float(row.get("PBratio")),
                            "yield": to_float(row.get("DividendYield"))}
    # 上櫃（欄位名稱與上市不同，要另外對應）
    for row in fetched.get(tpex_url) or []:
        code = _pick(row, "SecuritiesCompanyCode", "Code")
        if code:
            result[code] = {"pe": to_float(row.get("PriceEarningRatio")),
                            "pb": to_float(row.get("PriceBookRatio")),
                            "yield": to_float(row.get("YieldRatio"))}

    if result:
        _valuation_cache["date"] = today
        _valuation_cache["data"] = result
        _write_valuation_file_cache(today, result)
        print(f"✅ 估值資料抓取成功，共 {len(result)} 筆（含上櫃）")
    return result


def score_from_valuation(pe, growth_pct):
    """
    估值分數（0-25）。用 PEG 概念：本益比 ÷ 成長率。
    這裡的成長率用「累計營收年增率」代替標準PEG的EPS成長率——
    因為免費資料拿不到預估EPS。方向正確，但不是標準PEG。

    重要：PEG 極低（<0.3）通常不是真的便宜，而是景氣循環股從谷底反彈
    造成的失真——營收年增率因去年基期低而暴衝，但本益比用的是過去四季
    獲利，兩者時間軸對不上。循環股最危險的買點恰好就是本益比看起來
    最低的時候，所以這段不給滿分，改為示警。
    回傳 (分數, PEG值或None, 說明)
    """
    if pe is None:
        return 10, None, "無本益比資料（可能虧損或剛上市）"
    if growth_pct is None or growth_pct <= 0:
        if pe <= 12:
            return 14, None, f"本益比 {pe:.1f} 偏低，但缺乏成長性"
        if pe <= 20:
            return 8, None, f"本益比 {pe:.1f}"
        return 3, None, f"本益比 {pe:.1f} 偏高且無成長"

    peg = pe / growth_pct

    # 成長率高到不合常理時，多半是去年基期極低所致，不是本業真的翻倍
    if growth_pct >= 100:
        return 12, peg, (f"本益比 {pe:.1f}，PEG {peg:.2f}\n"
                         f"　　⚠️ 年增 {growth_pct:.0f}% 恐為低基期效應，"
                         f"PEG 失真，需自行查核獲利品質")
    if peg <= 0.3:
        return 15, peg, (f"本益比 {pe:.1f}，PEG {peg:.2f}\n"
                         f"　　⚠️ PEG 異常低，留意是否為景氣循環股高獲利期")
    if peg <= 0.5:
        return 25, peg, f"本益比 {pe:.1f}，PEG {peg:.2f}，明顯低估"
    if peg <= 1.0:
        return 20, peg, f"本益比 {pe:.1f}，PEG {peg:.2f}，估值合理"
    if peg <= 1.5:
        return 13, peg, f"本益比 {pe:.1f}，PEG {peg:.2f}，估值偏高"
    if peg <= 2.5:
        return 6, peg, f"本益比 {pe:.1f}，PEG {peg:.2f}，成長已充分反映"
    return 2, peg, f"本益比 {pe:.1f}，PEG {peg:.2f}，估值昂貴"


def score_stock_by_category(code, ind_map, price, cum_yoy, val, streak,
                            cum_lots, turnover, momentum_stats):
    """
    依股票所屬類別套用對應的評分規則，讓不同類股的分數可以互相比較。
    回傳 dict，金融股回傳 category="金融" 且 total=None（不評分）。
    """
    cat = stock_category(code, ind_map)
    pe = (val or {}).get("pe")
    pb = (val or {}).get("pb")
    dy = (val or {}).get("yield")

    if cat == "金融":
        # 金融股不評分：真正該看的 ROE、利差、逾放比在免費資料中沒有，
        # 硬給分數只會讓人誤以為那個數字有意義。
        return {"category": cat, "total": None, "pe": pe, "pb": pb, "yield": dy,
                "detail": "金融股不評分，僅列事實供判讀"}

    mom_score, mom_desc = score_from_industry_momentum(
        momentum_stats.get(ind_map.get(str(code).strip())))
    streak_score_raw = score_from_streak(streak)
    chip_tech = round((score_from_net_lots(cum_lots) / 40 * 5)
                      + (score_from_technical(price["pct"], turnover) / 60 * 5))

    if cat == "電子":
        rev = round(score_from_cum_revenue_growth(cum_yoy) * 25 / 40)   # 0-25
        val_score, peg, val_desc = score_from_valuation(pe, cum_yoy)     # 0-25
        mom = mom_score                                                  # 0-20
        streak_score = round(streak_score_raw * 20 / 30)                 # 0-20
        caps = ("25", "25", "20", "20", "10")
    else:  # 傳產：成長門檻放低、估值改看 PB 與殖利率、產業動能加重
        rev = score_revenue_traditional(cum_yoy)                         # 0-20
        val_score, val_desc = score_value_traditional(pb, dy, pe)        # 0-25
        peg = (pe / cum_yoy) if (pe and cum_yoy and cum_yoy > 0) else None
        mom = round(mom_score * 25 / 20)                                 # 0-25
        streak_score = round(streak_score_raw * 20 / 30)                 # 0-20
        caps = ("20", "25", "25", "20", "10")

    return {
        "category": cat,
        "total": rev + val_score + mom + streak_score + chip_tech,
        "rev": rev, "val": val_score, "mom": mom,
        "streak_score": streak_score, "chip": chip_tech,
        "caps": caps, "peg": peg, "pe": pe, "pb": pb, "yield": dy,
        "val_desc": val_desc, "mom_desc": mom_desc,
    }


def get_industry_momentum(revenue_data, industry_map):
    """
    用真實資料推導「產業動能」：把全市場月營收依產業別加總，
    算出各產業的累計營收年增率中位數，排出哪些族群整體需求在成長。

    這是用資料反推需求，不是預測。產業營收集體暴衝，通常就代表
    該環節供不應求、產品在漲價——跟研究機構說的「供給緊張」是同一件事，
    差別在這是已發生的驗證，而且每月自動更新，不需人工維護。

    回傳 {產業代碼: {"median": 中位數年增率, "count": 家數, "rank": 名次}}
    """
    buckets = {}
    for code, info in (revenue_data or {}).items():
        ind = industry_map.get(code)
        if not ind:
            continue
        cum = info.get("cum_yoy_pct")
        if cum is None:
            continue
        buckets.setdefault(ind, []).append(cum)

    stats = {}
    for ind, values in buckets.items():
        if len(values) < 3:  # 家數太少的產業，統計值沒有代表性
            continue
        values.sort()
        n = len(values)
        median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
        # 用第75百分位（領先群跑多快），而不是中位數。
        # 大產業動輒六七十家，中位數會被一堆平庸公司拉平，各產業幾乎沒差別；
        # 看領先群才分得出哪個族群真的有一批公司在噴。
        p75 = values[min(n - 1, int(n * 0.75))]
        stats[ind] = {"median": round(median, 1), "p75": round(p75, 1), "count": n}

    for rank, (ind, _s) in enumerate(
        sorted(stats.items(), key=lambda x: x[1]["p75"], reverse=True), start=1
    ):
        stats[ind]["rank"] = rank

    return stats


def save_industry_momentum(stats):
    """
    存下這一期的產業動能排名。營收一個月才更新一期，所以這張表長得很慢，
    但沒有它就只能看到「現在誰最強」，看不到「誰正在變強」——
    後者才是抓題材轉換的依據。
    """
    if not stats:
        return
    rows = [(ind, s.get("p75"), s.get("median"), s.get("rank"), s.get("count"))
            for ind, s in stats.items()]
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        execute_values(
            cursor,
            """
            INSERT INTO industry_momentum_history
                (industry, snapshot_date, p75, median, rank, count)
            VALUES %s
            ON CONFLICT (industry, snapshot_date) DO UPDATE SET
                p75 = EXCLUDED.p75, median = EXCLUDED.median,
                rank = EXCLUDED.rank, count = EXCLUDED.count
            """,
            rows,
            template="(%s, CURRENT_DATE, %s, %s, %s, %s)",
            page_size=200,
        )
        conn.commit()
        cursor.close()
        print(f"💾 已存入產業動能排名，共 {len(rows)} 個產業")
    except Exception as e:
        conn.rollback()
        print(f"❌ 寫入產業動能排名失敗: {e}")
    finally:
        release_db_connection(conn)


def save_picks(mode, rows, top_n=5):
    """
    存下今天這個模式選出的前 N 名。同一天重複跑會覆蓋，cron 跑兩次不會重複。
    存「當下價格」是關鍵——之後要算報酬得知道推薦當天是多少錢，
    事後再回頭抓歷史價會對不上（推薦時是盤中價，收盤價又是另一個數字）。
    """
    if not rows:
        return 0
    picks = [(mode, r["code"], i, r.get("score"), r.get("name"),
              r.get("industry"), r.get("close"))
             for i, r in enumerate(rows[:top_n], start=1)]
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        execute_values(
            cursor,
            """
            INSERT INTO pick_history
                (mode, code, pick_date, rank, score, name, industry, price)
            VALUES %s
            ON CONFLICT (mode, code, pick_date) DO UPDATE SET
                rank = EXCLUDED.rank, score = EXCLUDED.score,
                name = EXCLUDED.name, industry = EXCLUDED.industry,
                price = EXCLUDED.price
            """,
            picks,
            template="(%s, %s, CURRENT_DATE, %s, %s, %s, %s, %s)",
            page_size=100,
        )
        conn.commit()
        cursor.close()
        print(f"💾 已存入 {mode} 選股名單，共 {len(picks)} 檔")
        return len(picks)
    except Exception as e:
        conn.rollback()
        print(f"❌ 寫入選股名單失敗: {e}")
        return 0
    finally:
        release_db_connection(conn)


def get_picks_since(mode, days=90):
    """取近 N 天的選股名單，供成效計算。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT code, pick_date, rank, score, name, industry, price
            FROM pick_history
            WHERE mode = %s AND pick_date >= CURRENT_DATE - %s
              AND price IS NOT NULL AND price > 0
            ORDER BY pick_date DESC, rank
            """,
            (mode, days),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [{"code": r[0], "date": r[1], "rank": r[2], "score": r[3],
                 "name": r[4], "industry": r[5], "price": r[6]} for r in rows]
    except Exception as e:
        print(f"❌ 讀取選股名單失敗: {e}")
        return []
    finally:
        release_db_connection(conn)


def evaluate_picks(mode, days=90):
    """
    計算選股成效：把每一筆推薦的當時價格跟現價比，並依「推薦後經過幾天」分組。

    誠實度的幾個要求：
    ・用現價算報酬，所以一筆推薦只能歸入一個天期——70 天前的推薦拿現價算，
      得到的是 70 天的報酬不是 5 天的。因此由長到短判斷，取它已經走過的
      最長區間，標籤也照實寫成區間而非定點。
    ・同時算大盤同期報酬做對照。多頭時什麼都在漲，沒有對照組的話
      「平均 +5%」看不出是選股有效還是單純市場好。
    ・樣本數一併呈現，讓人自己判斷這個數字可不可信。
    """
    picks = get_picks_since(mode, days)
    if not picks:
        return None

    price_map = get_realtime_stocks_bulk(
        list({p["code"] for p in picks}), workers=16)
    today = taiwan_today()

    # 大盤對照：用快照裡存的加權指數，沒有就退回不比較
    taiex_by_date = {}
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT snapshot_date, taiex_close FROM portfolio_snapshots
            WHERE taiex_close IS NOT NULL AND snapshot_date >= CURRENT_DATE - %s
            """,
            (days,))
        taiex_by_date = {r[0]: r[1] for r in cursor.fetchall()}
        cursor.close()
    except Exception as e:
        print(f"⚠️ 讀取大盤對照失敗: {e}")
    finally:
        release_db_connection(conn)

    taiex_now = None
    t = fetch_taiex_summary()
    if t and t.get("close"):
        try:
            taiex_now = float(str(t["close"]).replace(",", ""))
        except (TypeError, ValueError):
            taiex_now = None

    horizons = [(60, "60 日以上"), (20, "20–59 日"), (5, "5–19 日")]
    buckets = {label: [] for _d, label in horizons}
    market = {label: [] for _d, label in horizons}

    for p in picks:
        cur = price_map.get(p["code"])
        if not cur:
            continue
        elapsed = (today - p["date"]).days
        ret = (cur["close"] - p["price"]) / p["price"] * 100
        for d, label in horizons:       # 由長到短，落在第一個符合的區間
            if elapsed >= d:
                buckets[label].append((ret, p))
                base = taiex_by_date.get(p["date"])
                if base and taiex_now:
                    market[label].append((taiex_now - base) / base * 100)
                break

    result = {}
    for _d, label in reversed(horizons):   # 顯示時由短到長
        vals = [r for r, _p in buckets[label]]
        if not vals:
            continue
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        median = (vals_sorted[n // 2] if n % 2
                  else (vals_sorted[n // 2 - 1] + vals_sorted[n // 2]) / 2)
        mk = market[label]
        result[label] = {
            "n": n,
            "avg": sum(vals) / n,
            "median": median,
            "win_rate": len([v for v in vals if v > 0]) / n * 100,
            "best": max(buckets[label], key=lambda x: x[0]),
            "worst": min(buckets[label], key=lambda x: x[0]),
            "market": (sum(mk) / len(mk)) if mk else None,
        }

    pending = len([p for p in picks if (today - p["date"]).days < 5])
    return {"horizons": result, "total_picks": len(picks), "pending": pending}


def score_from_industry_momentum(ind_stats):
    """
    產業動能分數（0-20）。看的是「這檔股票所在的產業，領先群跑得多快」，
    而不是這一家公司自己好不好——後者已由營收成長那項評分。
    回傳 (分數, 說明文字)
    """
    if not ind_stats:
        return 8, "產業動能資料不足"

    p75 = ind_stats["p75"]
    median = ind_stats["median"]
    rank = ind_stats.get("rank")
    count = ind_stats["count"]
    detail = f"（前25%為 {p75:+.1f}%、中位 {median:+.1f}%，{count} 家，排名第 {rank}）"

    if p75 >= 80:
        return 20, f"族群領先群強勁噴發{detail}"
    if p75 >= 50:
        return 17, f"族群領先群高速成長{detail}"
    if p75 >= 30:
        return 14, f"族群領先群成長明確{detail}"
    if p75 >= 15:
        return 10, f"族群領先群穩健成長{detail}"
    if p75 > 0:
        return 5, f"族群成長有限{detail}"
    return 1, f"族群整體衰退{detail}"


# ── 類股分類與各自的評分規則 ──
# 電子、傳產、金融三類的財務結構差很多，用同一套標準會系統性失真：
#   電子成長股 → 看 PEG，營收年增 15% 只算普通
#   傳產循環股 → 看 PB 與殖利率，營收年增 15% 已經很好；
#                而且景氣循環股最危險的時候正好是本益比最低的時候
#   金融股     → 「營收」是利息與投資收益，且真正該看的 ROE、利差、
#                逾放比在免費資料裡都沒有，因此不評分，只列事實
ELECTRONIC_INDUSTRIES = {
    "13",  # 電子工業（舊總類）
    "24",  # 半導體業
    "25",  # 電腦及週邊設備業
    "26",  # 光電業
    "27",  # 通信網路業
    "28",  # 電子零組件業
    "29",  # 電子通路業
    "30",  # 資訊服務業
    "31",  # 其他電子業
    "36",  # 數位雲端
}


def stock_category(code, ind_map):
    """回傳 電子 / 金融 / 傳產。查不到產業別時歸為傳產（保守）。"""
    raw = ind_map.get(str(code).strip())
    ind = str(raw).strip().zfill(2) if raw else ""
    if ind == "17":
        return "金融"
    if ind in ELECTRONIC_INDUSTRIES:
        return "電子"
    return "傳產"


def score_revenue_traditional(cum_yoy_pct):
    """
    傳產的營收成長分數（0-20）。門檻比電子低：
    電子 +15% 只算普通，傳產 +15% 已經相當好。
    """
    if cum_yoy_pct is None:
        return 6
    if cum_yoy_pct >= 25:
        return 20
    if cum_yoy_pct >= 15:
        return 17
    if cum_yoy_pct >= 10:
        return 14
    if cum_yoy_pct >= 5:
        return 10
    if cum_yoy_pct > 0:
        return 6
    if cum_yoy_pct > -10:
        return 3
    return 0


def score_value_traditional(pb, dividend_yield, pe):
    """
    傳產的估值分數（0-25）。以股價淨值比與殖利率為主，本益比只作輔助。
    循環股的本益比在獲利高點時最低，單看 PE 會在最危險的時候給最高分。
    回傳 (分數, 說明)
    """
    score, parts = 0, []

    if pb is None:
        score += 8
        parts.append("無淨值比資料")
    elif pb <= 0.8:
        score += 14
        parts.append(f"PB {pb:.2f}，低於淨值")
    elif pb <= 1.2:
        score += 12
        parts.append(f"PB {pb:.2f}，接近淨值")
    elif pb <= 2.0:
        score += 8
        parts.append(f"PB {pb:.2f}")
    elif pb <= 3.5:
        score += 4
        parts.append(f"PB {pb:.2f} 偏高")
    else:
        score += 1
        parts.append(f"PB {pb:.2f} 明顯偏高")

    if dividend_yield is None:
        score += 3
        parts.append("無殖利率資料")
    elif dividend_yield >= 6:
        score += 11
        parts.append(f"殖利率 {dividend_yield:.1f}%")
    elif dividend_yield >= 4:
        score += 9
        parts.append(f"殖利率 {dividend_yield:.1f}%")
    elif dividend_yield >= 2:
        score += 6
        parts.append(f"殖利率 {dividend_yield:.1f}%")
    elif dividend_yield > 0:
        score += 3
        parts.append(f"殖利率 {dividend_yield:.1f}% 偏低")
    else:
        score += 1
        parts.append("未配息")

    if pe:
        parts.append(f"PE {pe:.1f}")
    return min(25, score), "，".join(parts)


def score_from_cum_revenue_growth(cum_yoy_pct):
    """
    累計營收年增率轉分數（0-40）。用「今年至今 vs 去年同期」而非單月，
    比較不會被單月出貨遞延或去年基期異常扭曲，適合長線判斷。
    """
    if cum_yoy_pct is None:
        return 8  # 無資料給基本分，不因缺資料被過度懲罰
    if cum_yoy_pct >= 50:
        return 40
    if cum_yoy_pct >= 30:
        return 34
    if cum_yoy_pct >= 20:
        return 28
    if cum_yoy_pct >= 10:
        return 22
    if cum_yoy_pct >= 5:
        return 16
    if cum_yoy_pct > 0:
        return 10
    if cum_yoy_pct > -10:
        return 4
    return 0


def score_from_streak(streak):
    """連續買超天數轉分數（0-30）。連續性比單日大買更能代表法人真的在佈局。"""
    if streak >= 8:
        return 30
    if streak >= 5:
        return 25
    if streak >= 3:
        return 18
    if streak >= 2:
        return 10
    if streak >= 1:
        return 5
    return 0


def get_top_by_investor(investor="trust", direction="buy", days=10, top_n=10,
                        min_days=None):
    """
    依「單一法人」的近 N 日累計買賣超排名，而不是三大法人合計。

    合計會把三種完全不同的人加在一起：外資常是指數被動調整、
    自營多為權證避險、投信才是做過研究的主動買盤。
    要看「誰在認養」就必須分開排，合計排行看不出來。

    「認養」的定義不只是量大，還要有持續性——連續幾天小買，
    比單日大買更像在建倉。所以同時要求累計量與買超天數。

    investor: foreign / trust / dealer
    direction: buy（買超）/ sell（賣超）
    min_days: 至少要有幾天站在該方向；None 表示不限
    回傳 [(code, name, 累計張數, 該方向天數, 總天數), ...]
    """
    col = {"foreign": "foreign_net_lots",
           "trust": "trust_net_lots",
           "dealer": "dealer_net_lots"}.get(investor, "trust_net_lots")
    sign = ">" if direction == "buy" else "<"
    order = "DESC" if direction == "buy" else "ASC"
    having = f"SUM(h.{col}) {sign} 0"
    if min_days:
        having += f" AND COUNT(*) FILTER (WHERE h.{col} {sign} 0) >= {int(min_days)}"

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            WITH recent AS (
                SELECT DISTINCT trade_date FROM inst_history
                ORDER BY trade_date DESC LIMIT %s
            )
            SELECT h.code,
                   MAX(h.name) AS name,
                   SUM(h.{col}) AS cum,
                   COUNT(*) FILTER (WHERE h.{col} {sign} 0) AS hit_days,
                   COUNT(*) AS total_days
            FROM inst_history h
            JOIN recent r ON h.trade_date = r.trade_date
            WHERE length(h.code) = 4 AND h.code ~ '^[0-9]+$'
              AND h.code NOT LIKE '00%%'
            GROUP BY h.code
            HAVING {having}
            ORDER BY SUM(h.{col}) {order}
            LIMIT %s
            """,
            (days, top_n),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [(r[0], r[1], r[2] or 0, r[3] or 0, r[4] or 0) for r in rows]
    except Exception as e:
        print(f"❌ 查詢 {investor} 排行失敗: {e}")
        return []
    finally:
        release_db_connection(conn)


def get_both_side_codes(direction="buy", days=10, top_n=10, min_days=6):
    """
    外資與投信「同方向」的標的。

    兩者立場不同卻同時站在同一邊，比單一法人的動作更值得注意——
    外資的被動調整與投信的主動研究同時指向同一檔，
    巧合的機率比較低。
    回傳 [(code, name, 外資張數, 投信張數, 合計), ...]
    """
    sign = ">" if direction == "buy" else "<"
    order = "DESC" if direction == "buy" else "ASC"
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            WITH recent AS (
                SELECT DISTINCT trade_date FROM inst_history
                ORDER BY trade_date DESC LIMIT %s
            )
            SELECT h.code, MAX(h.name),
                   SUM(h.foreign_net_lots) AS f,
                   SUM(h.trust_net_lots) AS t,
                   COUNT(*) FILTER (
                       WHERE h.foreign_net_lots {sign} 0
                         AND h.trust_net_lots {sign} 0) AS both_days
            FROM inst_history h
            JOIN recent r ON h.trade_date = r.trade_date
            WHERE length(h.code) = 4 AND h.code ~ '^[0-9]+$'
              AND h.code NOT LIKE '00%%'
            GROUP BY h.code
            HAVING SUM(h.foreign_net_lots) {sign} 0
               AND SUM(h.trust_net_lots) {sign} 0
               AND COUNT(*) FILTER (
                       WHERE h.foreign_net_lots {sign} 0
                         AND h.trust_net_lots {sign} 0) >= %s
            ORDER BY (SUM(h.foreign_net_lots) + SUM(h.trust_net_lots)) {order}
            LIMIT %s
            """,
            (days, min_days, top_n),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [(r[0], r[1], r[2] or 0, r[3] or 0, (r[2] or 0) + (r[3] or 0))
                for r in rows]
    except Exception as e:
        print(f"❌ 查詢雙方同向失敗: {e}")
        return []
    finally:
        release_db_connection(conn)


def build_chips_report(days=10):
    """
    籌碼超人：依「誰在買」分開列出，而不是把三大法人加在一起。

    排名用「金額」不是「張數」。用張數排會讓低價股整片霸榜——
    15 元的金融股買 10 萬張只要 15 億，1500 元的股票同樣金額只有 1,000 張。

    排版有兩個為了 LINE 而做的調整：
    1. 代號包在全形括號裡。「6805　116」這種「數字 空格 數字」會被 LINE
       判斷成電話號碼而變成藍色超連結，點下去還會跳出撥號——
       包起來就不會觸發偵測。
    2. 每區只給 3 檔、區塊間空行。LINE 沒有表格也不等寬，
       一檔佔兩行、每區五檔的話整則超過 50 行，滑到後面就忘了前面在看什麼。
    """
    hist_days = get_history_days_count()
    if hist_days < 3:
        return ("❌ 法人歷史還不夠\n\n"
                "籌碼超人要看「近 10 日誰在持續買賣」，"
                "至少需要幾個交易日的累積。\n"
                "資料每天由排程自動累積，過幾天再試。")

    inst = fetch_institutional_data() or {}
    actual = min(days, hist_days)
    TOP = 3

    raw = {
        "trust_buy": get_top_by_investor("trust", "buy", actual, 20, min_days=6),
        "foreign_buy": get_top_by_investor("foreign", "buy", actual, 20, min_days=6),
        "trust_sell": get_top_by_investor("trust", "sell", actual, 20, min_days=6),
    }
    both_buy = get_both_side_codes("buy", actual, 20, min_days=5)
    both_sell = get_both_side_codes("sell", actual, 20, min_days=5)

    all_codes = {c for rows in raw.values() for c, *_ in rows}
    all_codes |= {c for c, *_ in both_buy} | {c for c, *_ in both_sell}
    prices = get_realtime_stocks_bulk(list(all_codes), workers=16)

    def amount(code, lots):
        pr = prices.get(code)
        return (abs(lots) * 1000 * pr["close"] / 100_000_000) if pr else None

    def line(code, name, amt, hit=None, total=None):
        # 代號用全形括號包住，避免被當成電話號碼
        nm = (name or stock_display_name(code, inst))[:5]
        tail = f"・{hit}/{total}天" if hit is not None else ""
        return f"{nm}（{code}）{amt:,.0f}億{tail}"

    def block(title, note, rows, top_n=TOP):
        scored = []
        for code, name, cum, hit, total in rows:
            amt = amount(code, cum)
            if amt is not None:
                scored.append((amt, code, name, hit, total))
        scored.sort(reverse=True)
        out = [title, note] if note else [title]
        if not scored:
            out.append("近期無符合標的")
        out += [line(c, n, a, h, t) for a, c, n, h, t in scored[:top_n]]
        out.append("")
        return out

    def block_both(title, note, rows, top_n=TOP):
        scored = []
        for code, name, f_lots, t_lots, tot in rows:
            amt = amount(code, tot)
            if amt is not None:
                scored.append((amt, code, name))
        scored.sort(reverse=True)
        out = [title, note]
        if not scored:
            out.append("近期無符合標的")
        out += [line(c, n, a) for a, c, n in scored[:top_n]]
        out.append("")
        return out

    lines = [f"🦸 籌碼超人　近{actual}日", "把三大法人拆開看誰在買", ""]
    lines += block("🏦 投信認養",
                   "國內基金。要對績效負責，通常做過研究才買，連續買最有參考價值",
                   raw["trust_buy"])
    lines += block("🌐 外資認養",
                   "量最大，但常是指數調整或 ETF 被動買進，未必代表看好",
                   raw["foreign_buy"])
    lines += block_both("🔥 外資投信同買",
                        "兩種立場不同的資金同時站買方，巧合機率較低",
                        both_buy)
    lines += block("📉 投信調節",
                   "做研究的那批人正在減碼",
                   raw["trust_sell"])
    lines += block_both("❄️ 外資投信同賣",
                        "兩邊同時撤退，通常不是巧合",
                        both_sell)

    lines += [
        "─" * 12,
        "億＝該法人近10日買賣金額",
        "天數＝10日內幾天站同方向",
        "",
        "為什麼要拆開看：同樣「三大法人買超3千張」，",
        "可能是投信在建倉，也可能只是自營商的",
        "權證避險部位，兩者意義差很多。",
        "",
        "認養需 6/10 天以上持續同向，",
        "單日爆量隔天就跑的不算。",
        "只看籌碼，不含基本面與估值——",
        "法人買不代表便宜，法人賣不代表變壞。",
        "※ 上市＋上櫃，非投資建議",
    ]
    report = "\n".join(lines)
    return report[:4750] + "\n…（已截斷）" if len(report) > 4800 else report


def get_cumulative_net_buy_for_codes(codes, days=10):
    """查指定股票近 N 個交易日的累計買超與買超天數。回傳 {code: (累計張數, 買超天數)}。"""
    codes = [str(c).strip() for c in codes]
    if not codes:
        return {}
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            WITH recent AS (
                SELECT DISTINCT trade_date FROM inst_history
                ORDER BY trade_date DESC LIMIT %s
            )
            SELECT h.code,
                   SUM(h.total_net_lots),
                   COUNT(*) FILTER (WHERE h.total_net_lots > 0)
            FROM inst_history h
            JOIN recent r ON h.trade_date = r.trade_date
            WHERE h.code = ANY(%s)
            GROUP BY h.code
            """,
            (days, codes),
        )
        rows = cursor.fetchall()
        cursor.close()
        return {r[0]: (r[1] or 0, r[2] or 0) for r in rows}
    except Exception as e:
        print(f"❌ 查詢自選股累計買超失敗: {e}")
        return {}
    finally:
        release_db_connection(conn)


def get_cumulative_net_buy(days=10, top_n=80, codes=None):
    """
    從 inst_history 撈「近 N 個交易日累計買超」前 top_n 名。
    這是黑馬候選池的來源——長線佈局往往是「量小但持續」，
    看單日買超排行會漏掉那種每天買 800 張、連買 15 天的股票。
    codes 若給定，只在該清單內排名——這對「依類股分別選股」很重要：
    全市場共用一份排行時，電子股的買超量遠大於傳產與金融，
    傳產股會被擠到幾百名之外，篩選後就整頁空白。
    回傳 [(code, name, 累計張數, 有買超的天數), ...]
    """
    # 不用「%s IS NULL OR ...」這種寫法：psycopg2 無法推斷該參數的型別，
    # 會直接丟型別錯誤，而錯誤被 except 吃掉後只會回傳空清單，
    # 表面上看起來像「這個類股沒有標的」，實際上是查詢從沒成功過。
    code_clause = ""
    params = [days]
    if codes is not None:
        if not codes:
            return []
        code_clause = "AND h.code = ANY(%s)"
        params.append(list(codes))
    params.append(top_n)

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            WITH recent AS (
                SELECT DISTINCT trade_date FROM inst_history
                ORDER BY trade_date DESC LIMIT %s
            )
            SELECT h.code,
                   MAX(h.name) AS name,
                   SUM(h.total_net_lots) AS cum_lots,
                   COUNT(*) FILTER (WHERE h.total_net_lots > 0) AS buy_days
            FROM inst_history h
            JOIN recent r ON h.trade_date = r.trade_date
            WHERE length(h.code) = 4 AND h.code ~ '^[0-9]+$'
              AND h.code NOT LIKE '00%%'
              {code_clause}
            GROUP BY h.code
            HAVING SUM(h.total_net_lots) > 0
            ORDER BY cum_lots DESC
            LIMIT %s
            """,
            tuple(params),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [(r[0], r[1], r[2] or 0, r[3] or 0) for r in rows]
    except Exception as e:
        print(f"❌ 查詢累計買超失敗: {e}")
        return []
    finally:
        release_db_connection(conn)


def check_data_integrity(days=30):
    """
    檢查法人歷史有沒有缺交易日。

    為什麼需要這個：缺資料不會報錯。「近 N 個交易日」是用
    SELECT DISTINCT trade_date ... LIMIT N 取的，少了一天它就默默
    往前多抓一天，統計照樣算得出來、數字也看起來合理，
    只有拿去跟券商對帳才會發現。這種錯誤在正式環境可以躺很久。

    判斷方式：把資料庫裡的日期序列跟「該區間內的工作日」比對。
    國定假日無法從程式判斷（每年不同），所以只回報「疑似」缺漏，
    由人自己確認那天是不是真的休市。

    回傳 (實際天數, 疑似缺漏的日期清單, 最新日期)
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT trade_date FROM inst_history
            WHERE trade_date >= CURRENT_DATE - %s
            ORDER BY trade_date DESC
            """, (days,))
        dates = [r[0] for r in cursor.fetchall()]
        cursor.close()
    except Exception as e:
        print(f"❌ 檢查資料完整性失敗: {e}")
        return 0, [], None
    finally:
        release_db_connection(conn)

    if not dates:
        return 0, [], None

    have = set(dates)
    newest, oldest = dates[0], dates[-1]

    # 只檢查有資料的區間內部，不往未來或更早推——
    # 區間外沒資料是正常的（還沒抓 / 超出保留期限），不該報成缺漏
    missing, d = [], oldest
    while d <= newest:
        if d.weekday() < 5 and d not in have:   # 平日卻沒資料
            missing.append(d)
        d += timedelta(days=1)

    return len(dates), missing, newest


def get_history_days_count():
    """目前資料庫累積了幾個交易日的法人歷史（用來判斷連續指標可不可信）。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT trade_date) FROM inst_history")
        n = cursor.fetchone()[0]
        cursor.close()
        return n or 0
    except Exception as e:
        print(f"❌ 查詢歷史天數失敗: {e}")
        return 0
    finally:
        release_db_connection(conn)


def fetch_tpex_institutional():
    """
    抓上櫃三大法人買賣超（TPEx openapi）。TWSE 的 T86 只含上市，
    少了這塊，上櫃股在籌碼相關的功能裡等於一片空白——
    連買天數永遠是 0、黑馬候選池也永遠不會出現上櫃股。

    這個端點只給「最新一個交易日」，不接受日期參數，
    所以歷史只能每天靠 cron 往前累積，無法像 T86 那樣回補。

    欄位名稱有兩個坑：
    1. JSON key 帶有多餘空格且命名不一致（例如
       'ForeignInvestorsInclude MainlandAreaInvestors-Difference' 中間有空格），
       所以用 _pick() 依序嘗試多個候選名稱，不能寫死一個。
    2. 「外資」有兩種口徑：含與不含外資自營商。這裡取「不含」的那個，
       跟 TWSE T86 的「外陸資買賣超股數(不含外資自營商)」對齊，
       否則兩個市場的外資數字定義不同，混在一起比較沒有意義。

    回傳 (資料 dict, 資料日期 YYYYMMDD) ；失敗回傳 (None, None)。
    """
    rows = _get_json(f"{TPEX_BASE}/tpex_3insti_daily_trading", timeout=20)
    if not rows:
        return None, None

    def to_int(s):
        try:
            return int(str(s).replace(",", "").strip())
        except (ValueError, TypeError):
            return 0

    # 民國年轉西元：1150814 → 20260814
    data_date = None
    raw_date = _pick(rows[0], "Date")
    if len(raw_date) == 7 and raw_date.isdigit():
        data_date = f"{int(raw_date[:3]) + 1911}{raw_date[3:]}"

    result = {}
    for row in rows:
        code = _pick(row, "SecuritiesCompanyCode", "Code")
        if not code:
            continue
        name = _pick(row, "CompanyName", "Name")

        foreign = to_int(_pick(
            row,
            "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference",
            "ForeignInvestorsIncludeMainlandAreaInvestors(ForeignDealersexcluded)-Difference"))
        foreign_dealer = to_int(_pick(row, "ForeignDealers-Difference",
                                      "Foreign Dealers-Difference"))
        trust = to_int(_pick(row, "SecuritiesInvestmentTrustCompanies-Difference"))
        dealer = to_int(_pick(row, "Dealers-Difference"))
        total = to_int(_pick(row, "TotalDifference"))

        # 沒有合計欄位時自行加總（外資不含自營，所以要另外把外資自營加回來）
        if not total:
            total = foreign + foreign_dealer + trust + dealer

        result[code] = {
            "name": name or code,
            "foreign_net_lots": shares_to_lots(foreign),
            "trust_net_lots": shares_to_lots(trust),
            "dealer_net_lots": shares_to_lots(dealer + foreign_dealer),
            "total_net_lots": shares_to_lots(total),
        }

    if not result:
        return None, None
    print(f"✅ 上櫃法人買賣超抓取成功（{data_date}），共 {len(result)} 檔")
    return result, data_date


def _load_latest_institutional_history():
    """從 inst_history 讀最近一個已保存的真實法人交易日。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT h.code, h.trade_date, h.name,
                   h.foreign_net_lots, h.trust_net_lots,
                   h.dealer_net_lots, h.total_net_lots
            FROM inst_history h
            JOIN (SELECT MAX(trade_date) AS latest_date FROM inst_history) d
              ON h.trade_date = d.latest_date
            ORDER BY h.code
        """)
        rows = cursor.fetchall()
        cursor.close()
    except Exception as exc:
        print(f"⚠️ 讀取最新法人歷史快照失敗: {exc}")
        return {}, None
    finally:
        release_db_connection(conn)

    if not rows:
        return {}, None
    data_date = rows[0][1].strftime("%Y%m%d") if rows[0][1] else None
    data = {
        row[0]: {
            "name": row[2] or row[0],
            "foreign_net_lots": row[3] or 0,
            "trust_net_lots": row[4] or 0,
            "dealer_net_lots": row[5] or 0,
            "total_net_lots": row[6] or 0,
        }
        for row in rows
    }
    return data, data_date


def fetch_institutional_data():
    """
    抓當日三大法人買賣超，涵蓋上市（TWSE T86）與上櫃（TPEx）。

    上市的部分若今天還沒公布（盤中、假日），會自動往前找最近一個
    有資料的交易日，最多往前找 5 天。上櫃端點只給最新一日，不能指定日期。
    一天只需成功抓取一次，之後直接用快取。
    """
    tw_now = taiwan_now()
    today = tw_now.strftime("%Y%m%d")

    # 快取判斷不能只看「今天抓過了嗎」。
    # 早上抓的時候今天的 T86 還沒公布，往前找會拿到昨天的資料，
    # 然後這份昨天的資料就被當成「今天抓過了」而沿用一整天——
    # 即使下午三點半後今天的資料已經出來也不會更新。
    # 所以還要看「快取裡的資料是不是今天的」：不是的話，
    # 過了公布時間就定期重試，而不是等隔天。
    cached = _t86_cache.get("data")
    cache_fresh = _t86_cache.get("cache_date") == today and cached
    data_is_today = _t86_cache.get("data_date") == today
    last_try = _t86_cache.get("last_attempt", 0)

    # T86 約在收盤後陸續公布，15:00 前不必重試
    past_publish = tw_now.hour >= 15
    should_retry = (cache_fresh and not data_is_today and past_publish
                    and time.time() - last_try > 900)   # 每 15 分鐘重試一次

    # 同一程序已命中且尚未進入重試窗口時直接返回，不多做資料庫查詢。
    if cache_fresh and not should_retry:
        return cached

    # warmup 可能在另一個 Render worker 完成；若 DB 已有最新真實快照，
    # 不必因為程序記憶體是空的就再次打 TWSE／TPEx。週末更不應重試外部端點，
    # 因為資料不會在週末變成新的交易日。
    history_data, history_date = _load_latest_institutional_history()
    if history_data and (tw_now.weekday() >= 5 or history_date == today):
        _t86_cache["cache_date"] = today
        _t86_cache["data_date"] = history_date
        _t86_cache["data"] = history_data
        print(f"⚡ 法人改讀資料庫最新快照（{history_date}），共 {len(history_data)} 檔")
        return history_data

    _t86_cache["last_attempt"] = time.time()
    merged, data_date = {}, None
    for days_back in range(0, 6):
        query_date = (taiwan_now() - timedelta(days=days_back)).strftime("%Y%m%d")
        data = _fetch_t86_for_date(query_date)
        if data:
            merged.update(data)
            data_date = query_date
            save_t86_history(query_date, data)
            break

    # 上櫃：即使上市那邊抓失敗也照抓，兩個市場互相獨立，
    # 沒理由因為一邊沒資料就讓另一邊也一起沒有。
    try:
        tpex, tpex_date = fetch_tpex_institutional()
        if tpex:
            merged.update(tpex)
            # 只有在「上櫃日期跟上市同一天」時才存歷史。
            # 兩邊日期不同時（例如上市今天還沒公布、上櫃已經有了）若照存，
            # 資料庫會多出一個「只有上櫃股票」的交易日，
            # 而「近 N 個交易日」是用全市場的 DISTINCT trade_date 去數的——
            # 那一天會佔掉一個名額卻對上市股票貢獻 0，
            # 等於讓上市股票的統計區間悄悄少了一天，數字全部對不上。
            if tpex_date and tpex_date == data_date:
                save_t86_history(tpex_date, tpex)
            elif tpex_date and not data_date:
                # 上市完全沒資料時才單獨存上櫃，此時不會造成混合窗口問題
                save_t86_history(tpex_date, tpex)
                data_date = tpex_date
            elif tpex_date != data_date:
                print(f"⚠️ 上櫃資料日期（{tpex_date}）與上市（{data_date}）不同，"
                      f"本次不寫入歷史以免造成單一市場的交易日")
    except Exception as e:
        print(f"❌ 上櫃法人資料抓取失敗: {e}")

    if merged:
        _t86_cache["cache_date"] = today
        _t86_cache["data_date"] = data_date
        _t86_cache["data"] = merged
        return merged

    print("⚠️ 上市往前找了 5 天、上櫃也無資料")
    return _t86_cache["data"]


def format_data_date(yyyymmdd):
    """把 20260818 轉成 08/18；轉不了就原樣回傳，不要因為格式問題就沒有日期。"""
    d = str(yyyymmdd or "")
    if len(d) == 8 and d.isdigit():
        return f"{d[4:6]}/{d[6:8]}"
    return d or "未知"

# --- 真實評分邏輯 ---
def score_from_net_lots(lots):
    if lots >= 5000: return 40
    if lots >= 2000: return 35
    if lots >= 1000: return 28
    if lots >= 300: return 20
    if lots >= 50: return 12
    if lots > 0: return 5
    return 0

def calc_turnover_billion(close, volume_shares):
    """成交金額（億元）。改用金額而非「張數」評分，避免高價股（如緯穎）
    因為一張要價高、成交張數天生偏少，在量能分數上被系統性低估。"""
    if not close or not volume_shares:
        return 0.0
    return (close * volume_shares) / 100_000_000

def score_from_technical(pct, turnover_billion):
    pct_score = max(0, min(30, pct * 3))  # 貼近台股±10%漲跌停，10%封頂拿滿分
    vol_score = max(0, min(30, turnover_billion))  # 1億元＝1分，30億元封頂
    return round(pct_score + vol_score)

def fmt_resistance(r):
    """壓力為 None 代表股價已突破近60日高點，上方無參考壓力。"""
    return f"{r}" if r is not None else "已突破前高（無壓力參考）"


def fmt_support(price):
    """支撐顯示。近期支撐全跌破時要講清楚，否則只看到一個數字會誤以為還撐得住。"""
    s = price.get("support")
    if price.get("broke_support"):
        return f"{s}（已跌破近期支撐）"
    return f"{s}"


def build_position_desc(price):
    """
    描述「這根K棒站在什麼位置」，讓突破、追高、無量漲停能被區分開來。
    只陳述事實，不下買賣結論。
    """
    parts = []
    close = price.get("close")
    h20, h60 = price.get("high_20d"), price.get("high_60d")
    ma20 = price.get("ma20")
    vol_ratio = price.get("vol_ratio")
    up_streak = price.get("up_streak", 0)
    pos = price.get("pos_vs_60d_high")

    # 位階
    if h60 and close >= h60:
        parts.append("・突破近60日高點（創季線以來新高）")
    elif h20 and close >= h20:
        parts.append("・突破近20日高點")
    elif pos is not None:
        parts.append(f"・距近60日高點 {pos:+.1f}%")

    # 與均線關係
    if ma20 and close:
        diff = (close - ma20) / ma20 * 100
        if diff >= 15:
            parts.append(f"・站上20日均線 {diff:+.1f}%，乖離偏大")
        else:
            parts.append(f"・20日均線 {ma20}（{diff:+.1f}%）")

    # 量能相對自己過去的水準
    if vol_ratio:
        if vol_ratio >= 3:
            parts.append(f"・量能為20日均量的 {vol_ratio} 倍，爆量")
        elif vol_ratio >= 1.5:
            parts.append(f"・量能為20日均量的 {vol_ratio} 倍，明顯放大")
        elif vol_ratio < 0.8:
            parts.append(f"・量能僅20日均量的 {vol_ratio} 倍，量縮")
        else:
            parts.append(f"・量能為20日均量的 {vol_ratio} 倍")

    # 連漲／連跌天數。注意 up_streak==0 是「今天沒漲」，不是「首根上漲」。
    down_streak = price.get("down_streak", 0)
    if up_streak >= 5:
        parts.append(f"・已連續上漲 {up_streak} 天")
    elif up_streak >= 2:
        parts.append(f"・連續上漲 {up_streak} 天")
    elif up_streak == 1:
        parts.append("・今日為近期首根上漲K棒")
    elif down_streak >= 5:
        parts.append(f"・已連續下跌 {down_streak} 天")
    elif down_streak >= 2:
        parts.append(f"・連續下跌 {down_streak} 天")
    elif down_streak == 1:
        parts.append("・今日翻黑，為近期首根下跌K棒")

    return "\n".join(parts) if parts else "・位階資料不足"


def build_technical_desc(pct, volume_lots, net_lots, turnover_billion=None):
    parts = []
    if pct >= 3:
        parts.append(f"・當日大漲 {pct:.2f}%，漲勢強勁")
    elif pct >= 1:
        parts.append(f"・當日上漲 {pct:.2f}%")
    elif pct <= -3:
        parts.append(f"・當日重挫 {pct:.2f}%，賣壓沉重")
    elif pct < 0:
        parts.append(f"・當日下跌 {pct:.2f}%")
    else:
        parts.append(f"・當日持平（{pct:+.2f}%）")

    if turnover_billion is None:
        parts.append(f"・成交量 {volume_lots:,} 張")
    elif turnover_billion >= 50:
        parts.append(f"・成交金額 {turnover_billion:.1f} 億（{volume_lots:,} 張），量能爆發")
    elif turnover_billion >= 15:
        parts.append(f"・成交金額 {turnover_billion:.1f} 億（{volume_lots:,} 張），量能明顯放大")
    else:
        parts.append(f"・成交金額 {turnover_billion:.1f} 億（{volume_lots:,} 張）")

    if net_lots > 0:
        parts.append(f"・三大法人合計買超 {net_lots:,} 張")
    elif net_lots < 0:
        parts.append(f"・三大法人合計賣超 {abs(net_lots):,} 張，籌碼面偏空")
    else:
        parts.append("・三大法人買賣持平")

    return "\n".join(parts)

def build_risk_desc(pct, net_lots):
    if net_lots < 0 and pct > 0:
        return "・法人籌碼與股價走勢背離（法人賣超但股價上漲），須留意隔日反轉風險"
    if pct >= 5:
        return "・短線漲幅已大，乖離率偏高，慎防獲利了結賣壓"
    if net_lots > 0 and pct > 0:
        return "・籌碼與價格同步走強，仍須留意大盤系統性風險"
    return "・盤勢仍有變數，操作務必自行設好停損停利"

def fetch_quote(symbol):
    """抓單一標的最新收盤與漲跌。回傳 (價格, 漲跌幅%, 漲跌絕對值) 或 None。"""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
        res = requests.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'}).json()
        result = res.get('chart', {}).get('result', [])
        if not result:
            return None
        meta = result[0].get('meta', {})
        close = meta.get('regularMarketPrice')
        closes = [c for c in (result[0].get('indicators', {}).get('quote', [{}])[0]
                              .get('close', []) or []) if c is not None]
        if not close and closes:
            close = closes[-1]
        if not close:
            return None
        # 前一交易日收盤：從尾端往回找第一筆「跟現價不同」的收盤。
        # Yahoo 的日K陣列尾端偶爾會出現重複的收盤價（同一天被塞兩筆），
        # 若死抓倒數第二筆就會拿到跟現價一樣的數字，算出假的 0.00%。
        prev = None
        for c in reversed(closes):
            if abs(c - close) > max(0.005, abs(close) * 1e-6):
                prev = c
                break
        if prev is None:
            prev = meta.get('chartPreviousClose')
        if not prev:
            return None
        return close, (close - prev) / prev * 100, close - prev
    except Exception as e:
        print(f"❌ 抓取 {symbol} 失敗: {e}")
        return None


# 盤前簡報要看的標的。想增減直接改這裡，格式是 (顯示名稱, Yahoo代號)
BRIEF_INDICES = [
    ("道瓊", "^DJI"), ("那斯達克", "^IXIC"),
    ("S&P 500", "^GSPC"), ("費城半導體", "^SOX"),
]
BRIEF_MACRO = [
    ("美10年債殖利率", "^TNX"), ("VIX 恐慌指數", "^VIX"),
    ("美元指數", "DX-Y.NYB"), ("布蘭特原油", "BZ=F"),
]
BRIEF_STOCKS = [
    ("輝達 NVDA", "NVDA"), ("博通 AVGO", "AVGO"), ("超微 AMD", "AMD"),
    ("美光 MU", "MU"), ("Lumentum LITE", "LITE"), ("台積電ADR", "TSM"),
]


def fetch_quotes_bulk(symbols, workers=12):
    """並行抓多個 Yahoo 代號的報價，回傳 {symbol: (價格, 漲跌幅%, 漲跌值) 或 None}。"""
    symbols = list(dict.fromkeys(s for s in symbols if s))
    if not symbols:
        return {}

    def safe(s):
        try:
            return fetch_quote(s)
        except Exception as e:
            print(f"⚠️ 並行抓取報價失敗 {s}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=min(workers, len(symbols))) as ex:
        return dict(zip(symbols, ex.map(safe, symbols)))


def generate_morning_brief():
    """
    盤前總經簡報：美股指數、殖利率與波動率、台廠相關重要個股、總經新聞標題。
    數據全部來自實際報價；CPI／非農這類發布結果與解讀不自行生成，
    改列新聞標題與連結，由你自己判讀。
    """
    lines = [f"☀️ 盤前總經簡報　{taiwan_now().strftime('%m/%d')}", "═" * 13]

    # 十幾個代號一次並行抓完，取代原本一個一個等的做法
    all_syms = ([s for _l, s in BRIEF_INDICES] + [s for _l, s in BRIEF_MACRO]
                + [s for _l, s in BRIEF_STOCKS] + ["^TNX"])
    quotes = fetch_quotes_bulk(all_syms)

    def fmt_rows(title, targets, as_yield=False):
        block = [f"\n【{title}】"]
        got = False
        for label, sym in targets:
            q = quotes.get(sym)
            if not q:
                continue
            got = True
            close, pct, diff = q
            arrow = "⚪" if abs(pct) < 0.005 else ("🔴" if pct > 0 else "🟢")
            if as_yield:
                # 殖利率用「基點」表達比百分比變化直觀（升息循環常用語言）
                block.append(f"{arrow} {label}：{close:.3f}%（{diff*100:+.1f} bps）")
            else:
                block.append(f"{arrow} {label}：{close:,.2f}（{pct:+.2f}%）")
        return block if got else [f"\n【{title}】\n資料暫缺"]

    lines += fmt_rows("美股指數", BRIEF_INDICES)

    # 殖利率與 VIX 分開處理：^TNX 是殖利率本身，用 bps 表示變化才有意義
    lines.append("\n【債市與風險指標】")
    tnx = quotes.get("^TNX")
    if tnx:
        close, pct, diff = tnx
        arrow = "⚪" if abs(diff) < 0.0005 else ("🔴" if diff > 0 else "🟢")
        lines.append(f"{arrow} 美10年債殖利率：{close:.3f}%（{diff*100:+.1f} bps）")
    for label, sym in BRIEF_MACRO[1:]:
        q = quotes.get(sym)
        if not q:
            continue
        close, pct, _diff = q
        arrow = "⚪" if abs(pct) < 0.005 else ("🔴" if pct > 0 else "🟢")
        lines.append(f"{arrow} {label}：{close:,.2f}（{pct:+.2f}%）")

    lines += fmt_rows("重點個股", BRIEF_STOCKS)

    # 總經新聞：只給標題與連結，不生成解讀
    news = fetch_stock_news("CPI OR 非農 OR 聯準會 OR 美債殖利率", max_items=3, within_hours=36)
    if news:
        lines.append("\n【總經焦點】")
        for n in news:
            src = f"（{n['source']}）" if n["source"] else ""
            lines.append(f"・{n['title']}{src}")
            if n["link"]:
                lines.append(f"　{n['link']}")

    lines.append("\n═" * 1)
    lines.append("※ 為前一交易日收盤數據")
    brief = "\n".join(lines)
    return brief[:4750] + "\n…（已截斷）" if len(brief) > 4800 else brief


# --- 月營收（TWSE OpenAPI t187ap05_L，全上市公司，一個月只有一期，用「資料年月」當快取key） ---
_revenue_cache = {"period": None, "data": {}, "checked_at": 0}
REVENUE_CACHE_CHECK_SECONDS = 600


def _load_latest_revenue_history():
    """讀取資料庫已保存的最新月營收快照，不把資料庫內容冒充成今日更新。"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT r.code, r.period, r.yoy_pct, r.cum_yoy_pct,
                   r.mom_pct, r.month_revenue
            FROM revenue_history r
            JOIN (SELECT MAX(period) AS latest_period FROM revenue_history) p
              ON r.period = p.latest_period
            ORDER BY r.code
        """)
        rows = cursor.fetchall()
        cursor.close()
    except Exception as exc:
        print(f"⚠️ 讀取最新月營收歷史快照失敗: {exc}")
        return {}, None
    finally:
        release_db_connection(conn)

    if not rows:
        return {}, None
    period = str(rows[0][1]) if rows[0][1] is not None else None
    data = {
        row[0]: {
            "yoy_pct": row[2],
            "cum_yoy_pct": row[3],
            "mom_pct": row[4],
            "month_revenue": row[5],
        }
        for row in rows
    }
    return data, period


def fetch_monthly_revenue():
    """
    抓最新一期月營收，涵蓋上市、上櫃、興櫃。
    證交所只給上市，上櫃與興櫃在櫃買中心，缺了就會出現「營收無資料」。
    """
    # 月營收通常一天只需確認一次；原本雖然有 period 快取，
    # 但檢查快取前仍會先打三個外部端點，導致每次完整首頁都重做網路等待。
    # 以短期檢查節流保留資料更新能力，也避免同一程序內重複抓取。
    now = time.time()
    if (_revenue_cache["data"] and
            now - _revenue_cache.get("checked_at", 0) < REVENUE_CACHE_CHECK_SECONDS):
        return _revenue_cache["data"]

    # Render 多 worker 或重啟後，記憶體快取可能是空的；先讀已保存的最新月份。
    # 這能避免使用者請求重新等待三個外部端點，並在10分鐘後再正常確認新月份。
    if not _revenue_cache["data"]:
        history_data, history_period = _load_latest_revenue_history()
        if history_data:
            _revenue_cache["period"] = history_period
            _revenue_cache["data"] = history_data
            _revenue_cache["checked_at"] = now
            print("⚡ 月營收改讀資料庫最新快照（%s），共 %s 筆" %
                  (history_period or "未知月份", len(history_data)))
            return history_data

    _revenue_cache["checked_at"] = now

    def to_float(v):
        try:
            return float(str(v).replace(",", ""))
        except (ValueError, TypeError):
            return None

    sources = [
        f"{TWSE_BASE}/opendata/t187ap05_L",   # 上市
        f"{TPEX_BASE}/mopsfin_t187ap05_O",    # 上櫃
        f"{TPEX_BASE}/t187ap05_R",            # 興櫃
    ]

    # 三個端點並行抓：序列跑的話光是等就要三次來回（每次 timeout 20 秒），
    # 這是「當天第一個使用者」等特別久的主因之一。
    fetched = fetch_json_bulk(sources, timeout=20)

    result, period = {}, None
    for url in sources:
        rows = fetched.get(url)
        if not rows:
            continue
        period = period or _pick(rows[0], "資料年月", "Period")
        for row in rows:
            code = _pick(row, "公司代號", "SecuritiesCompanyCode", "Code")
            if not code:
                continue
            result[code] = {
                "yoy_pct": to_float(_pick(row, "營業收入-去年同月增減(%)")),
                "cum_yoy_pct": to_float(_pick(row, "累計營業收入-前期比較增減(%)")),
                "mom_pct": to_float(_pick(row, "營業收入-上月比較增減(%)")),
                "month_revenue": to_float(_pick(row, "營業收入-當月營收")),
            }

    if not result:
        return _revenue_cache["data"]

    if _revenue_cache["period"] == period and len(_revenue_cache["data"]) >= len(result):
        return _revenue_cache["data"]

    _revenue_cache["period"] = period
    _revenue_cache["data"] = result
    print(f"✅ 月營收抓取成功（{period}），共 {len(result)} 筆（含上櫃、興櫃）")
    save_revenue_history(period, result)
    return result


def score_from_revenue_growth(yoy_pct):
    """依營收年增率評分，作為「有題材／獲利」的量化依據（0-50分）。"""
    if yoy_pct is None:
        return 0
    if yoy_pct >= 50:
        return 50
    if yoy_pct >= 30:
        return 40
    if yoy_pct >= 15:
        return 30
    if yoy_pct >= 5:
        return 20
    if yoy_pct > 0:
        return 10
    return 0

# --- 大盤指數（TWSE 官方 OpenAPI，固定回傳最新一個交易日收盤資訊） ---
# 今日首頁與盤前摘要都會用到大盤；短時間內重整不需要重複打外部 API。
_taiex_cache = {"at": 0, "data": None}
TAIEX_CACHE_SECONDS = 60

def fetch_taiex_summary():
    """
    抓加權指數。改用 Yahoo 的日K序列（^TWII）而不是 TWSE 的 MI_INDEX。

    原因是「日期對得起來」：MI_INDEX 這個 OpenAPI 端點不回傳資料日期，
    更新時間也跟 T86 不同步——大盤停在昨天、法人已經是今天的時候，
    畫面會把兩者標成同一天，使用者只看得到「數字跟收盤對不上」
    卻不知道是哪個環節的問題。

    Yahoo 的日K有時間戳，可以明確知道這個收盤是哪一天的，
    而且跟個股報價同源，畫面上的數字才會一致。
    回傳 dict（含 date），失敗回傳 None。
    """
    now = time.time()
    with _realtime_cache_lock:
        if _taiex_cache["data"] is not None and now - _taiex_cache["at"] < TAIEX_CACHE_SECONDS:
            return _taiex_cache["data"]
    try:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
               "?range=5d&interval=1d")
        res = requests.get(url, timeout=8,
                           headers={'User-Agent': 'Mozilla/5.0'}).json()
        result = res.get("chart", {}).get("result", [])
        if not result:
            return None
        meta = result[0].get("meta", {})
        ts = result[0].get("timestamp", []) or []
        closes = [c for c in (result[0].get("indicators", {})
                              .get("quote", [{}])[0].get("close", []) or [])
                  if c is not None]
        if not closes:
            return None

        tw_tz = timezone(timedelta(hours=8))
        close = meta.get("regularMarketPrice") or closes[-1]
        # 最後一根K棒的日期就是這個收盤的日期
        bar_date = (datetime.fromtimestamp(ts[-1], tw_tz).strftime("%Y%m%d")
                    if ts else None)

        # 前收：從尾端往回找第一筆與現價不同的，避免尾端重複值算出假的 0.00%
        prev = None
        for c in reversed(closes):
            if abs(c - close) > max(0.005, abs(close) * 1e-6):
                prev = c
                break
        if prev is None:
            prev = meta.get("chartPreviousClose")
        if not prev:
            return None

        diff = close - prev
        result = {
            "close": f"{close:,.2f}",
            "sign": "+" if diff > 0 else ("-" if diff < 0 else ""),
            "pts": f"{abs(diff):,.2f}",
            "pct": f"{diff / prev * 100:+.2f}",
            "date": bar_date,
        }
        with _realtime_cache_lock:
            _taiex_cache["at"] = time.time()
            _taiex_cache["data"] = result
        return result
    except Exception as e:
        print(f"❌ 抓取大盤指數錯誤: {e}")
        return None


# --- 三大法人合計買賣超金額（跟 T86 同體系的 rwd JSON API） ---
def fetch_institutional_total():
    try:
        url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"
        res = requests.get(url, params={"response": "json"}, timeout=10, headers={'User-Agent': 'Mozilla/5.0'}).json()
        if res.get("stat") != "OK":
            return None

        fields = res.get("fields", [])
        rows = res.get("data", [])

        def col(name, default=None):
            return fields.index(name) if name in fields else default

        name_i = col("單位名稱")
        buy_i = col("買進金額")
        sell_i = col("賣出金額")
        net_i = col("買賣差額")

        def to_int(s):
            try:
                return int(str(s).replace(",", ""))
            except (ValueError, TypeError):
                return None

        breakdown = {}
        total_net = None
        for row in rows:
            if name_i is None:
                continue
            name = row[name_i].strip()
            net_val = to_int(row[net_i]) if net_i is not None else None
            if net_val is None and buy_i is not None and sell_i is not None:
                b, s = to_int(row[buy_i]), to_int(row[sell_i])
                net_val = (b - s) if (b is not None and s is not None) else None
            if net_val is None:
                continue

            # 證交所回傳的資料本身就內含一列「合計」，直接採用它，
            # 不能把它跟其他列一起加總，否則總額會被重複計算一次。
            if "合計" in name:
                total_net = net_val
                continue

            breakdown[name] = net_val

        if not breakdown:
            return None

        # 萬一某天沒有「合計」列，才自行加總各單位
        if total_net is None:
            total_net = sum(breakdown.values())
        return {"total": total_net, "breakdown": breakdown}
    except Exception as e:
        print(f"❌ 抓取法人合計金額錯誤: {e}")
        return None

# --- 盤後解盤：使用者手動輸入關鍵字才觸發，不自動推播 ---
def build_market_recap():
    inst_data = fetch_institutional_data()
    if not inst_data:
        return "❌ 目前無法取得盤後資料，可能是非交易日或資料尚未公布，請稍後再試。"

    # 一定要標資料日期。沒有日期的話，非交易日或資料還沒公布時
    # 看到的是上一個交易日的數字，但畫面長得跟今天的一模一樣，
    # 使用者只會覺得「怎麼沒更新」而不知道原因。
    tw_today = taiwan_now().strftime("%m/%d")
    inst_dd = format_data_date(_t86_cache.get("data_date"))

    taiex = fetch_taiex_summary()
    taiex_dd = format_data_date(taiex.get("date")) if taiex else None

    lines = ["📊 盤後解盤"]

    # 大盤與法人來自不同資料源，發布時間不同步——各自標日期，
    # 不要用一個日期涵蓋整份報告。兩者同一天時才合併成一行寫。
    if taiex and taiex.get("close"):
        arrow = ("▲" if taiex.get("sign") == "+"
                 else ("▼" if taiex.get("sign") == "-" else "－"))
        tag = "" if taiex_dd == inst_dd else f"　{taiex_dd}"
        lines.append(f"─" * 14)
        lines.append(f"大盤 {taiex['close']}　{arrow}{taiex.get('pts','?')}"
                     f"（{taiex.get('pct','?')}%）{tag}")
    else:
        lines.append("─" * 14)
        lines.append("大盤：資料暫缺")

    if taiex_dd and taiex_dd == inst_dd:
        lines[0] = f"📊 盤後解盤　{inst_dd} 收盤"
    else:
        lines[0] = f"📊 盤後解盤"
        lines.insert(1, f"法人 {inst_dd}"
                        + (f"　大盤 {taiex_dd}" if taiex_dd else ""))

    stale = "" if inst_dd == tw_today else f"（法人非今日；今天是 {tw_today}）"
    if stale:
        lines.insert(1, stale)

    inst_total = fetch_institutional_total()
    if inst_total:
        total_yi = inst_total["total"] / 100_000_000
        lines.append(f"三大法人合計：{total_yi:+.1f}億")
        for name, val in inst_total["breakdown"].items():
            # 名稱裡的半形括號會讓「(2886) +22」這類樣式被當成電話號碼，
            # 這裡雖然沒有數字相鄰，仍統一成全形以免日後改動時踩到
            nm = name.replace("(", "（").replace(")", "）")
            lines.append(f"　{nm}　{val/100_000_000:+.1f}億")
    lines.append("─" * 14)

    stock_rows = [
        (code, info) for code, info in inst_data.items()
        if len(code) == 4 and code.isdigit()
        and not code.startswith("00")  # 排除 ETF
    ]
    buy_leaders = sorted(stock_rows, key=lambda x: x[1]["total_net_lots"], reverse=True)[:3]
    sell_leaders = sorted(stock_rows, key=lambda x: x[1]["total_net_lots"])[:3]

    lines.append("🟢 法人買超前3")
    for code, info in buy_leaders:
        lines.append(f"{info['name']}（{code}）+{info['total_net_lots']:,}張")

    lines.append("")
    lines.append("🔴 法人賣超前3")
    for code, info in sell_leaders:
        lines.append(f"{info['name']}（{code}）{info['total_net_lots']:,}張")

    if stale:
        lines += ["─" * 14,
                  "※ 今日資料尚未公布或非交易日，",
                  "　 以上為最近一個交易日的數字。",
                  "　 T86 約在收盤後陸續發布。"]
    return "\n".join(lines)


def build_market_recap_line_message(user_id, base_url=None):
    """LINE 盤後摘要：保留真實資料，改用分區卡片呈現並附網頁完整分析按鈕。"""
    recap_text = build_market_recap()
    token = create_web_token(user_id)
    if not token:
        return TextSendMessage(text=recap_text)

    web_url = (f"{public_web_base_url(base_url)}/web/portfolio?t="
               f"{quote(token, safe='')}")
    raw_lines = [line.rstrip() for line in recap_text.splitlines()]
    clean_lines = [line for line in raw_lines if line.strip("─").strip()]
    if clean_lines and clean_lines[0].startswith("📊"):
        clean_lines = clean_lines[1:]

    def find_line(prefix, start=0):
        return next((idx for idx in range(start, len(clean_lines))
                     if clean_lines[idx].startswith(prefix)), len(clean_lines))

    buy_idx = find_line("🟢")
    sell_idx = find_line("🔴", buy_idx)
    note_idx = find_line("※", sell_idx)
    top_lines = clean_lines[:buy_idx]
    buy_lines = clean_lines[buy_idx + 1:sell_idx] if buy_idx < len(clean_lines) else []
    sell_lines = clean_lines[sell_idx + 1:note_idx] if sell_idx < len(clean_lines) else []
    note_lines = clean_lines[note_idx:] if note_idx < len(clean_lines) else []

    def text_block(text, color="#454C55", size="sm", margin="none"):
        return {"type": "text", "text": text or "資料暫缺", "size": size,
                "color": color, "wrap": True, "margin": margin,
                "lineSpacing": "3px"}

    def recap_section(title, lines, color):
        if not lines:
            return None
        return {"type": "box", "layout": "vertical", "spacing": "xs",
                "margin": "md", "paddingAll": "12px", "cornerRadius": "10px",
                "backgroundColor": "#F7F7F3", "contents": [
                    {"type": "text", "text": title, "weight": "bold", "size": "sm",
                     "color": color},
                    text_block("\n".join(lines), margin="sm")
                ]}

    sections = []
    top_block = recap_section("收盤與法人資料", top_lines, "#6E5228")
    buy_block = recap_section("🟢 法人買超前 3", buy_lines, "#155C42")
    sell_block = recap_section("🔴 法人賣超前 3", sell_lines, "#A82A20")
    for block in (top_block, buy_block, sell_block):
        if block:
            sections.append(block)
    if note_lines:
        sections.append(text_block("\n".join(note_lines), color="#767D85", size="xs", margin="md"))

    bubble = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "spacing": "none",
                 "paddingAll": "18px", "contents": [
            {"type": "text", "text": "📊 盤後收盤摘要", "weight": "bold",
             "size": "xl", "color": "#1B2027"},
            {"type": "text", "text": "資料依公開來源日期整理", "size": "xs",
             "color": "#767D85", "margin": "xs"},
            {"type": "separator", "margin": "md", "color": "#E8EAE6"},
            *sections,
            {"type": "separator", "margin": "lg", "color": "#E8EAE6"},
            {"type": "text", "text": "想看完整持股損益、今日判讀、走勢與風險分析，請開啟網頁版。",
             "size": "xs", "color": "#767D85", "wrap": True, "margin": "md"},
            {"type": "button", "style": "primary", "height": "sm",
             "color": "#6E5228", "margin": "md",
             "action": {"type": "uri", "label": "查看網頁版完整分析",
                        "uri": web_url}}
        ]},
        "styles": {"body": {"backgroundColor": "#FFFFFF"}}
    }
    return FlexSendMessage(alt_text=recap_text[:400], contents=bubble)


# --- 自選股健檢：用長線指標評估「這檔還健康嗎」，不是評估「今天漲不漲」 ---
def score_watchlist_chips(cum_lots, buy_days, streak):
    """
    籌碼分數（0-30）。看的是近10日法人的累計方向與持續性，
    不是單日買賣超——單日雜訊太大，隔天就翻臉的情況很常見。
    """
    if cum_lots > 0:
        base = min(20, 8 + cum_lots / 1500)  # 累計買超越多分數越高，20分封頂
        base += min(10, streak * 2)          # 連續買超再加成
        return round(min(30, base))
    if cum_lots == 0:
        return 12
    # 累計賣超，依賣超幅度扣分
    return max(0, round(10 + cum_lots / 3000))


def score_watchlist_position(price):
    """
    位階分數（0-25）。股價相對近期高點與均線的位置。
    高檔乖離大不代表不好，但持有時該知道自己站在哪。
    """
    score = 0
    close = price.get("close")
    pos = price.get("pos_vs_60d_high")
    ma20 = price.get("ma20")

    # 距離季線高點的位置：貼近高點代表趨勢強，但過度乖離要留意
    if pos is not None:
        if pos >= 0:
            score += 15   # 創新高，趨勢最強
        elif pos >= -8:
            score += 13
        elif pos >= -20:
            score += 9
        elif pos >= -35:
            score += 5
        else:
            score += 2    # 距高點超過35%，明顯轉弱
    else:
        score += 7

    # 站上或跌破20日均線
    if ma20 and close:
        diff = (close - ma20) / ma20 * 100
        if diff >= 20:
            score += 6    # 乖離過大，短線風險
        elif diff >= 0:
            score += 10   # 站穩均線之上
        elif diff >= -8:
            score += 5
        else:
            score += 1    # 跌破均線且距離拉開
    else:
        score += 5

    return min(25, score)


def build_watchlist_advice(total, chip_score, pos_score, rev_score, val_score,
                           cum_lots, streak, price, cum_yoy, pe):
    """
    把四個面向的訊號合成一句可操作的觀察。
    規則式，不是預測——講的是「現在這組數字代表什麼、該注意什麼」。
    """
    close = price.get("close")
    ma20 = price.get("ma20")
    pos = price.get("pos_vs_60d_high")
    ma_diff = ((close - ma20) / ma20 * 100) if (ma20 and close) else None

    # 最優先：基本面惡化
    if cum_yoy is not None and cum_yoy < 0 and cum_lots < 0:
        return "⚠️ 營收衰退且法人同步出場，兩個面向一起轉弱，建議重新檢視持有理由"

    # 貴 + 弱：本益比偏高但股價已回落一段，通常代表市場在下修對它的成長預期。
    # 這個組合比「跌破月線」更有資訊量（它說明了為什麼弱），所以排在前面。
    #
    # 但不能只看 val_score 低就說「本益比偏高」——分數低有兩種原因：
    #   (a) 本益比真的高
    #   (b) 營收年增暴衝導致 PEG 失真，被降分示警（低基期效應）
    # 情況 (b) 的本益比可能只有 9 倍，硬說「偏高」是明顯錯誤的判讀。
    # 所以要拿實際的 PE 值把關，不是只信分數。
    expensive = pe is not None and pe >= 25
    if expensive and pos is not None and pos <= -20:
        if cum_lots < 0:
            return "🔻 本益比偏高但股價已回落一段，法人同步減碼，市場恐在重新評價其成長性"
        return "🤔 本益比偏高但股價已回落一段，市場可能在重新評價成長性，留意估值是否過去給太高"

    # 低本益比 × 高成長：多半是景氣循環股的獲利高點，或去年基期極低。
    # 這種組合看起來便宜，實際上最危險，值得單獨講一句。
    if (pe is not None and pe <= 15 and cum_yoy is not None and cum_yoy >= 80
            and pos is not None and pos <= -20):
        return ("⚠️ 本益比低但營收年增暴衝，多為低基期或獲利高點造成的失真；"
                "股價已回落一段，留意獲利能否延續")

    # 籌碼與趨勢同時轉弱
    if cum_lots < 0 and ma_diff is not None and ma_diff < 0:
        return "⚠️ 法人賣超且跌破月線，短線偏弱，若持有應設好停損位"

    if cum_lots < 0:
        return "🔻 法人近期站在賣方，但價格結構尚未破壞，可觀察是否只是短線調節"

    # 高乖離示警
    if ma_diff is not None and ma_diff >= 20:
        return "🔥 基本面與籌碼皆強，但短線乖離過大，此時進場成本偏高，等回測均線較穩"

    # 強勢且結構健康
    if total >= 70 and pos is not None and pos >= -8:
        return "✅ 籌碼、位階、基本面三方同向，趨勢完整，持有可續抱並以月線為防守"

    # 基本面好但還在低位（最值得留意的一種）
    if rev_score >= 18 and pos is not None and pos <= -20 and cum_lots > 0:
        return "💡 基本面佳但股價仍距高點一段，法人已在承接，屬落後補漲的觀察對象"

    if val_score <= 8 and rev_score >= 18:
        return "💰 成長性不錯但估值已偏貴，追價空間有限，等回檔較有利"

    if streak >= 3 and pos is not None and pos > -20:
        return "📈 法人持續買超且價格站在相對高位，趨勢延續中"

    if rev_score <= 10:
        return "📉 營收成長動能偏弱，基本面缺乏支撐，僅適合短線看待"

    return "😐 各面向訊號中性，暫無明顯方向，續觀察法人動向與月線支撐"


def compute_watchlist_scores(codes):
    """
    算一批股票的自選股評分。抽成獨立函式，讓「顯示報告」與「每日存快照」
    共用同一套算法——分數若兩邊各算各的，隔天比對出來的變化就沒有意義了。
    回傳 {code: {各項分數與當下的數據}}，查無行情的代號不會出現在結果裡。
    """
    codes = [str(c).strip() for c in codes if c]
    if not codes:
        return {}

    institutional_data = fetch_institutional_data() or {}
    revenue_data = fetch_monthly_revenue() or {}
    valuation_data = fetch_valuation() or {}
    streaks = get_consecutive_days_batch(codes)
    cum_map = get_cumulative_net_buy_for_codes(codes, days=10)
    price_map = get_realtime_stocks_bulk(codes)   # 並行抓，取代逐檔序列請求

    result = {}
    for code in codes:
        stock = price_map.get(code)
        if not stock:
            continue

        cum_lots, buy_days = cum_map.get(code, (0, 0))
        streak = streaks.get(code, 0)

        chip_score = score_watchlist_chips(cum_lots, buy_days, streak)   # 0-30
        pos_score = score_watchlist_position(stock)                       # 0-25

        cum_yoy = revenue_data.get(code, {}).get("cum_yoy_pct")
        rev_score = round(score_from_cum_revenue_growth(cum_yoy) * 25 / 40)  # 0-25

        pe = valuation_data.get(code, {}).get("pe")
        val_raw, peg, _desc = score_from_valuation(pe, cum_yoy)
        val_score = round(val_raw * 20 / 25)                              # 0-20

        result[code] = {
            "code": code,
            "name": stock_display_name(code, institutional_data, stock["name"]),
            "stock": stock,
            "total": chip_score + pos_score + rev_score + val_score,
            "chip": chip_score, "position": pos_score,
            "revenue": rev_score, "valuation": val_score,
            "cum_lots": cum_lots, "buy_days": buy_days, "streak": streak,
            "cum_yoy": cum_yoy, "pe": pe,
        }
    return result


def save_watchlist_scores(user_id, scores):
    """存下今天的自選股分數。同一天重複寫入會覆蓋，cron 跑兩次也不會重複。"""
    if not scores:
        return
    rows = [(str(user_id).strip(), s["code"], s["total"], s["chip"],
             s["position"], s["revenue"], s["valuation"], s["stock"]["close"])
            for s in scores.values()]
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        execute_values(
            cursor,
            """
            INSERT INTO watchlist_scores
                (user_id, code, snapshot_date, total, chip, position,
                 revenue, valuation, close)
            VALUES %s
            ON CONFLICT (user_id, code, snapshot_date) DO UPDATE SET
                total = EXCLUDED.total, chip = EXCLUDED.chip,
                position = EXCLUDED.position, revenue = EXCLUDED.revenue,
                valuation = EXCLUDED.valuation, close = EXCLUDED.close
            """,
            rows,
            template="(%s, %s, CURRENT_DATE, %s, %s, %s, %s, %s, %s)",
            page_size=200,
        )
        conn.commit()
        cursor.close()
    except Exception as e:
        conn.rollback()
        print(f"❌ 寫入自選股評分快照失敗: {e}")
    finally:
        release_db_connection(conn)


def get_previous_scores(user_id, codes, days_back=7):
    """
    取每檔最近一筆「今天以前」的分數，用來比對變化。
    不硬性取昨天：週末與假日沒有快照，取最近一筆有紀錄的才不會整片空白。
    回傳 {code: {"total":…, "date":…, 各分項}}
    """
    codes = [str(c).strip() for c in codes if c]
    if not codes:
        return {}
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT ON (code)
                   code, snapshot_date, total, chip, position, revenue, valuation
            FROM watchlist_scores
            WHERE user_id = %s AND code = ANY(%s)
              AND snapshot_date < CURRENT_DATE
              AND snapshot_date >= CURRENT_DATE - %s
            ORDER BY code, snapshot_date DESC
            """,
            (str(user_id).strip(), codes, days_back),
        )
        rows = cursor.fetchall()
        cursor.close()
        return {r[0]: {"date": r[1], "total": r[2], "chip": r[3],
                       "position": r[4], "revenue": r[5], "valuation": r[6]}
                for r in rows}
    except Exception as e:
        print(f"❌ 讀取歷史評分失敗: {e}")
        return {}
    finally:
        release_db_connection(conn)


def describe_score_change(cur, prev):
    """
    把分數變化講成一句話，並指出是哪個面向在動。
    只講變化夠大的（±5 分以上）——每天一兩分的波動是雜訊，
    每檔都報一次會讓真正重要的變化被淹沒。
    回傳 (箭頭符號, 說明文字) 或 (None, None)
    """
    if not prev:
        return None, None
    diff = cur["total"] - prev["total"]
    if abs(diff) < 5:
        return None, None

    # 找出貢獻最多的面向，讓「為什麼變」有依據而不只是報數字
    parts = [("籌碼", cur["chip"] - prev["chip"]),
             ("位階", cur["position"] - prev["position"]),
             ("營收", cur["revenue"] - prev["revenue"]),
             ("估值", cur["valuation"] - prev["valuation"])]
    driver, dval = max(parts, key=lambda x: abs(x[1]))
    reason = f"，主要來自{driver}{dval:+d}" if abs(dval) >= 3 else ""
    arrow = "📈" if diff > 0 else "📉"
    return arrow, f"{prev['total']}→{cur['total']} 分（{diff:+d}）{reason}"


def build_single_stock_report(code, user_id=None):
    """
    單檔完整健檢。LINE 直接輸入代號就走這裡——
    原本只顯示報價與位階，要看評分還得先加進自選再查健檢，多了兩個步驟。
    查詢一檔股票時想知道的本來就是「這檔現在如何」，沒理由分散在兩個指令。

    user_id 有給時會一併顯示是否已在自選、以及分數變化。
    """
    stock = get_realtime_stock(code)
    if not stock:
        return f"❌ 查無代號 {code} 的行情，請確認代號是否正確。"

    inst = fetch_institutional_data() or {}
    scores = compute_watchlist_scores([code])
    s = scores.get(code)
    ind_map = get_industry_map() or {}
    ind = ind_map.get(code)
    name = short_company_name(stock_display_name(code, inst, stock["name"]))

    lines = [f"📊 {code} {name}"]
    if ind:
        lines[0] += f"　{industry_name(ind)}"
    lines.append("─" * 14)
    lines.append(f"💰 {stock['close']:.2f}（{stock['pct']:+.2f}%）"
                 f"　高低 {stock['high']:.2f}/{stock['low']:.2f}")
    lines.append(f"📦 {int(stock['volume'] / 1000):,} 張"
                 f"　🛡️{fmt_support(stock)} 🚧{fmt_resistance(stock['resistance'])}")

    if s:
        total = s["total"]
        flag = "🟢" if total >= 70 else ("🟡" if total >= 45 else "🔴")
        lines.append("")
        lines.append(f"{flag} 綜合評分：{total}／100")
        lines.append(f"　籌碼{s['chip']}/30　位階{s['position']}/25　"
                     f"營收{s['revenue']}/25　估值{s['valuation']}/20")

        # 分數變化：只有自選股才有歷史快照可比
        if user_id:
            prev = get_previous_scores(user_id, [code]).get(code)
            arrow, change_txt = describe_score_change(s, prev)
            if arrow:
                lines.append(f"{arrow} {change_txt}")

        cum_yoy, pe = s["cum_yoy"], s["pe"]
        rev_txt = f"營收年增 {cum_yoy:+.1f}%" if cum_yoy is not None else "營收無資料"
        pe_txt = f"PE {pe:.1f}" if pe else "PE 無"
        lines.append(f"　{rev_txt}　{pe_txt}")

    lines.append("")
    lines.append("【法人籌碼】近10日")
    bd = get_investor_breakdown([code]).get(code)
    desc = describe_investor_breakdown(bd)
    lines.append(desc if desc else "　尚無法人歷史資料")

    lines.append("")
    lines.append("【位階】")
    lines.append(build_position_desc(stock))

    if s:
        lines.append("")
        lines.append("【觀察】")
        lines.append(build_watchlist_advice(
            s["total"], s["chip"], s["position"], s["revenue"], s["valuation"],
            s["cum_lots"], s["streak"], stock, s["cum_yoy"], s["pe"]))

    news = fetch_stock_news(name, max_items=2)
    if news:
        lines.append("")
        lines.append("📰 相關新聞")
        for n in news:
            src = f"（{n['source']}）" if n["source"] else ""
            lines.append(f"・{n['title']}{src}")
            if n["link"]:
                lines.append(f"　{n['link']}")

    if user_id:
        in_wl = code in get_user_watchlist(user_id)
        lines.append("")
        lines.append(f"※ 已在自選清單" if in_wl
                     else f"※ 輸入「加 {code}」加入自選")

    report = "\n".join(lines)
    return report[:4750] + "\n…（已截斷）" if len(report) > 4800 else report


def build_healthcheck_report(user_id):
    codes = get_user_watchlist(user_id)
    if not codes:
        return "📂 自選股清單是空的\n輸入「加 3081」新增自選"

    institutional_data = fetch_institutional_data()
    scores = compute_watchlist_scores(codes)
    prev_scores = get_previous_scores(user_id, codes)
    tags = get_watchlist_tags(user_id)
    breakdowns = get_investor_breakdown(codes)

    rows = []
    for code in codes:
        tag = tags.get(code)
        s = scores.get(code)
        if not s:
            rows.append((tag, -1, f"⚪ {code} 查無行情"))
            continue

        stock = s["stock"]
        name = s["name"]
        cum_lots, buy_days, streak = s["cum_lots"], s["buy_days"], s["streak"]
        chip_score, pos_score = s["chip"], s["position"]
        rev_score, val_score = s["revenue"], s["valuation"]
        cum_yoy, pe = s["cum_yoy"], s["pe"]
        total = s["total"]
        flag = "🟢" if total >= 70 else ("🟡" if total >= 45 else "🔴")

        # 一句話點出目前最該注意的事實。
        # 優先講投信——三大法人合計會把投信的動作稀釋掉，
        # 但投信要對基金績效負責，連續買通常比合計數字更有參考價值。
        bd = breakdowns.get(code)
        trust = bd["trust"] if bd else None
        if trust and trust["streak"] >= 3:
            note = f"投信連 {trust['streak']} 日買超（累計 {trust['cum']:+,} 張）"
        elif cum_lots < 0:
            note = f"法人近10日賣超 {abs(cum_lots):,} 張"
        elif streak >= 3:
            note = f"法人連 {streak} 日買超"
        elif cum_lots > 0:
            note = f"法人近10日買超 {cum_lots:,} 張（{buy_days} 天）"
        else:
            note = "法人近期無明顯動作"

        pos_txt = (f"距高點 {stock['pos_vs_60d_high']:+.1f}%"
                   if stock.get("pos_vs_60d_high") is not None else "位階資料不足")
        rev_txt = f"營收年增 {cum_yoy:+.1f}%" if cum_yoy is not None else "營收無資料"
        pe_txt = f"PE {pe:.1f}" if pe else "PE 無"

        advice = build_watchlist_advice(total, chip_score, pos_score, rev_score,
                                        val_score, cum_lots, streak, stock, cum_yoy, pe)

        # 分數變化：跟最近一次快照比。變化比絕對分數更有訊號價值——
        # 「一直都是 70 分」跟「從 55 分升上來」是完全不同的兩件事。
        arrow, change_txt = describe_score_change(s, prev_scores.get(code))
        change_line = f"{arrow} {change_txt}\n" if arrow else ""

        # 法人三方拆解，只在有明確主導方或分歧時才佔一行版面
        bd_desc = describe_investor_breakdown(bd)
        bd_line = ""
        if bd_desc and "→" in bd_desc:
            bd_line = bd_desc.split("→")[-1].strip() + "\n"

        text = (
            f"{flag} {name} {code}　{total}分\n"
            f"{change_line}"
            f"{stock['close']:.2f}（{stock['pct']:+.2f}%）　{pos_txt}\n"
            f"{note}\n"
            f"{rev_txt}　{pe_txt}　🛡️{fmt_support(stock)} 🚧{fmt_resistance(stock['resistance'])}\n"
            f"{bd_line}"
            f"{advice}"
        )
        rows.append((tag, total, text))

    # 依分類分組。長線與短線該用不同標準判讀——長線在意營收與估值，
    # 短線在意籌碼與位階——分開列才不會混著看。
    # 組內仍依分數排序；未分類的放最後。
    grouped = {}
    for tag, total, text in rows:
        grouped.setdefault(tag, []).append((total, text))

    order = [t for t in WATCHLIST_TAGS if t in grouped]
    if None in grouped:
        order.append(None)

    blocks = []
    has_tags = any(t is not None for t in grouped)
    for tag in order:
        items = sorted(grouped[tag], key=lambda x: x[0], reverse=True)
        if has_tags:
            icon = TAG_ICONS.get(tag, "📌")
            label = tag or "未分類"
            blocks.append(f"{icon}　{label}（{len(items)}）\n" + "─" * 12)
        blocks.append("\n\n".join(text for _, text in items))

    body = "\n\n".join(blocks)
    tag_hint = ("" if has_tags else
                "\n分類：輸入「加 2330 長線」或「分類 2330 短線」")
    report = (
        f"📋 自選股健檢（{len(codes)}檔）\n\n{body}\n\n"
        f"評分＝籌碼30＋位階25＋營收25＋估值20\n"
        f"🟢70+ 🟡45-69 🔴<45{tag_hint}\n"
        f"※ 觀察為數據歸納，非投資建議，請自行判斷"
    )

    if len(report) > 4800:
        report = report[:4750] + "\n\n…（清單過長，已截斷）"
    return report

# --- 個股新聞（Google News RSS，免費、可帶關鍵字查詢） ---
def fetch_stock_news(keyword, max_items=2, within_hours=30):
    """
    抓某個關鍵字的最新新聞。只回傳標題、來源、連結、發布時間——
    不抓內文也不轉貼全文，版權上安全，實務上你也只需要標題判斷要不要點進去。
    within_hours 用來過濾掉舊聞，預設只看 30 小時內的。
    """
    import xml.etree.ElementTree as ET
    from urllib.parse import quote

    query = quote(f"{keyword} 股價 OR 營收 OR 法人")
    url = (f"https://news.google.com/rss/search?q={query}"
           f"&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    try:
        res = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        root = ET.fromstring(res.content)
    except Exception as e:
        print(f"❌ 抓取新聞失敗（{keyword}）: {e}")
        return []

    now = datetime.now(timezone.utc)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub = item.findtext("pubDate")

        if not title:
            continue

        # Google News 的標題格式是「標題 - 媒體名」，把媒體名切出來
        if " - " in title and not source:
            title, source = title.rsplit(" - ", 1)

        if pub:
            try:
                pub_dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                if (now - pub_dt).total_seconds() > within_hours * 3600:
                    continue
            except ValueError:
                pass

        items.append({"title": title.strip(), "source": source.strip(), "link": link})
        if len(items) >= max_items:
            break
    return items


def build_news_digest(user_id):
    """
    盤後新聞摘要：只推跟使用者自選股有關的新聞。
    沒有新聞的股票直接略過，不硬湊版面。
    """
    codes = get_user_watchlist(user_id)
    if not codes:
        return None

    inst_data = fetch_institutional_data()
    lines = [f"📰 自選股新聞（{taiwan_now().strftime('%m/%d')}）", "─" * 14]

    # 每檔都要打一次 Google News RSS，序列跑加上禮貌等待會很久。
    # 改成小量並行（4 條）：既縮短時間，又不會對 Google News 一次灌太多請求。
    names = {code: stock_display_name(code, inst_data) for code in codes}

    def fetch_one(code):
        try:
            return fetch_stock_news(names[code], max_items=2)
        except Exception as e:
            print(f"⚠️ 並行抓新聞失敗 {code}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=min(4, len(codes))) as ex:
        news_map = dict(zip(codes, ex.map(fetch_one, codes)))

    found = 0
    for code in codes:
        name = names[code]
        news = news_map.get(code) or []
        if not news:
            continue
        found += 1
        lines.append(f"\n🔹 {name} {code}")
        for n in news:
            src = f"（{n['source']}）" if n["source"] else ""
            lines.append(f"・{n['title']}{src}")
            if n["link"]:
                lines.append(f"　{n['link']}")

    if not found:
        lines.append("今日自選股無相關新聞")
    else:
        lines.append("\n─" * 1)
        lines.append("※ 僅列標題與連結，詳情請點原文")

    digest = "\n".join(lines)
    if len(digest) > 4800:
        digest = digest[:4750] + "\n\n…（內容過長，已截斷）"
    return digest


# ── 推播額度保護 ──
# LINE 免費方案每月 200 則「主動推播」（回覆訊息不計入）。
# 以每人每個交易日 1 則計算，一個月約 20 個交易日 → 最多約 9 人。
#
# 沒有這道保護的話，開通人數一多，額度會在月中某天突然用完，
# 而且是「前面幾個人收到、後面的人沒收到」這種難以察覺的失敗——
# 使用者不會來抱怨，只會覺得這個服務時好時壞。
# 寧可一開始就明確擋下，並在回應裡講清楚超出多少。
PUSH_MONTHLY_QUOTA = 200
PUSH_TRADING_DAYS = 21          # 一個月的交易日數，抓保守值
PUSH_MAX_USERS = PUSH_MONTHLY_QUOTA // PUSH_TRADING_DAYS   # ≈ 9 人


def push_to_users(users, build_fn, label):
    """
    對名單推播，並在超過額度上限時只送前 N 位。

    名單順序來自資料庫（依 user_id 固定排序），所以「前 N 位」每次都一樣，
    不會今天這幾個收到、明天換另幾個——後者更糟，因為每個人都只收到一半。
    回傳統計字串。
    """
    over = max(0, len(users) - PUSH_MAX_USERS)
    targets = users[:PUSH_MAX_USERS]

    sent, failed, empty = 0, 0, 0
    for uid in targets:
        msg = build_fn(uid)
        if not msg:
            empty += 1
            continue
        try:
            outbound = (msg if isinstance(msg, (TextSendMessage, FlexSendMessage))
                        else TextSendMessage(text=str(msg)))
            line_bot_api.push_message(uid, outbound)
            sent += 1
        except Exception as e:
            print(f"❌ {label}推播失敗 {uid}: {e}")
            failed += 1

    result = f"{label} done. sent={sent}, failed={failed}, empty={empty}"
    if over:
        warn = (f"⚠️ 已開通 {len(users)} 人，超過免費方案可負擔的 "
                f"{PUSH_MAX_USERS} 人，本次只推播前 {PUSH_MAX_USERS} 位。"
                f"請用「停用 N」減少開通人數。")
        print(warn)
        result += f" | {warn}"
    return result


@app.route("/cron/push-news", methods=["POST", "GET"])
def cron_push_news():
    """盤後推播自選股新聞。建議每個交易日 15:00 跑一次，一天一則。"""
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)
    return run_in_background(
        "新聞推播",
        lambda: push_to_users(get_notify_users(), build_news_digest, "新聞推播")), 200


# --- 排程推播訊息建構與 Cron 端點 ---
def build_digest(user_id):
    codes = get_user_watchlist(user_id)
    if not codes:
        return None
    inst_data = fetch_institutional_data()
    price_map = get_realtime_stocks_bulk(codes)   # 並行抓，取代逐檔序列請求
    lines = [f"☀️ 【每日自選股摘要】", "─" * 14]
    for code in codes:
        data = price_map.get(code)
        if data:
            name = stock_display_name(code, inst_data, data["name"])
            light = "🔴" if data['pct'] >= 0 else "🟢"
            lines.append(
                f"\n{light} {name} {code}｜{data['close']:.2f}（{data['pct']:+.2f}%）\n"
                f"🛡️ 支撐：{fmt_support(data)} | 🚧 壓力：{fmt_resistance(data['resistance'])}"
            )
        else:
            lines.append(f"\n⚪ {code} 查無行情")
    return "\n".join(lines)

def build_morning_push(user_id):
    """保留純文字版本，供沒有網頁入口時的相容 fallback 使用。"""
    return build_today_attention_push(user_id)


DEFAULT_WEB_BASE_URL = "https://stock-bot-6xct.onrender.com"
_morning_macro_cache = {"date": None, "lines": [], "data": None, "news": None}
_morning_macro_cache_lock = threading.Lock()


def public_web_base_url(base_url=None):
    """取得 LINE 推播可開啟的公開網址；背景 cron 沒有 request context。"""
    return (base_url or os.environ.get("WEB_BASE_URL")
            or os.environ.get("RENDER_EXTERNAL_URL")
            or DEFAULT_WEB_BASE_URL).rstrip("/")


def _morning_macro_data():
    """取得盤前 LINE 的美股與總經資料；同一天共用一次結果。"""
    today = taiwan_today()
    with _morning_macro_cache_lock:
        if (_morning_macro_cache.get("date") == today
                and _morning_macro_cache.get("data") is not None):
            return list(_morning_macro_cache["data"])

    symbols = ([s for _label, s in BRIEF_INDICES]
               + [s for _label, s in BRIEF_MACRO])
    quotes = fetch_quotes_bulk(symbols)
    data = []
    for group, targets in (("美股指數", BRIEF_INDICES),
                           ("風險指標", BRIEF_MACRO)):
        for label, symbol in targets:
            q = quotes.get(symbol)
            if q:
                close, pct, diff = q
                data.append({"group": group, "label": label, "symbol": symbol,
                             "close": close, "pct": pct, "diff": diff})
            else:
                data.append({"group": group, "label": label, "symbol": symbol,
                             "close": None, "pct": None, "diff": None})
    with _morning_macro_cache_lock:
        _morning_macro_cache["date"] = today
        _morning_macro_cache["data"] = list(data)
    return data


def _morning_macro_lines():
    """純文字 fallback 用的盤前美股與總經摘要。"""
    lines = ["☀️ 盤前／總經"]
    for group in ("美股指數", "風險指標"):
        values = []
        for item in _morning_macro_data():
            if item["group"] != group:
                continue
            if item["pct"] is None:
                values.append(f"{item['label']} 資料暫缺")
            else:
                arrow = "⚪" if abs(item["pct"]) < 0.005 else ("🔴" if item["pct"] > 0 else "🟢")
                values.append(f"{arrow} {item['label']} {item['pct']:+.2f}%")
        lines.append(("美股　" if group == "美股指數" else "風險　") + "　".join(values))
    return lines


def _morning_macro_news():
    """取得最多兩則近期總經新聞；沒有真實新聞就不顯示新聞區塊。"""
    today = taiwan_today()
    with _morning_macro_cache_lock:
        if (_morning_macro_cache.get("date") == today
                and _morning_macro_cache.get("news") is not None):
            return list(_morning_macro_cache["news"])
    try:
        items = fetch_stock_news(
            "CPI OR 非農 OR 聯準會 OR 美債殖利率",
            max_items=2, within_hours=36
        ) or []
    except Exception as exc:
        print(f"⚠️ 盤前總經新聞抓取失敗：{exc}")
        items = []
    news = []
    for item in items[:2]:
        if not item.get("title"):
            continue
        news.append({"title": str(item.get("title")),
                     "source": str(item.get("source") or ""),
                     "link": str(item.get("link") or "")})
    with _morning_macro_cache_lock:
        _morning_macro_cache["date"] = today
        _morning_macro_cache["news"] = list(news)
    return news


def _valid_news_uri(value):
    """只允許新聞原文的 HTTP／HTTPS 網址進入 LINE URI action。"""
    try:
        parsed = urlparse(str(value or "").strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            return None
        return parsed.geturl()
    except Exception:
        return None


def _morning_macro_news_lines():
    lines = []
    for item in _morning_macro_news():
        source = f"（{item['source']}）" if item["source"] else ""
        line = f"・{item['title']}{source}"
        uri = _valid_news_uri(item.get("link"))
        if uri:
            line += f"\n　{uri}"
        lines.append(line)
    return lines


def _macro_metric_box(item):
    """Flex 的單一指標格，避免多個指標塞在同一行後錯位。"""
    if item["pct"] is None:
        value = "資料暫缺"
        value_color = "#767D85"
    else:
        value = f"{item['pct']:+.2f}%"
        value_color = "#B52F2F" if item["pct"] > 0 else ("#087A4B" if item["pct"] < 0 else "#767D85")
    return {
        "type": "box", "layout": "vertical", "flex": 1,
        "backgroundColor": "#F7F7F3", "cornerRadius": "8px",
        "paddingAll": "10px", "contents": [
            {"type": "text", "text": item["label"], "size": "xs",
             "color": "#454C55", "wrap": True, "maxLines": 2},
            {"type": "text", "text": value, "size": "sm", "weight": "bold",
             "color": value_color, "margin": "sm"},
        ]
    }


def _macro_metric_rows(group):
    items = [item for item in _morning_macro_data() if item["group"] == group]
    rows = []
    for index in range(0, len(items), 2):
        pair = items[index:index + 2]
        contents = [_macro_metric_box(item) for item in pair]
        if len(contents) == 1:
            contents.append({"type": "box", "layout": "vertical", "flex": 1,
                             "contents": []})
        rows.append({"type": "box", "layout": "horizontal", "spacing": "sm",
                     "margin": "sm" if rows else "none", "contents": contents})
    return rows


def build_morning_push_message(user_id, base_url=None):
    """建立盤前 LINE 訊息：事件、美股與總經摘要，完整分析由按鈕導向今日網頁。"""
    events = merge_change_events(user_id, limit=3)

    news_lines = _morning_macro_news_lines()
    plain_text = build_today_attention_push(user_id) + "\n\n" + "\n".join(_morning_macro_lines())
    if news_lines:
        plain_text += "\n\n📰 總經焦點\n" + "\n".join(news_lines)
    token = create_web_token(user_id)
    if not token:
        return TextSendMessage(text=plain_text)

    web_url = (f"{public_web_base_url(base_url)}/web/premarket?t="
               f"{quote(token, safe='')}")
    contents = [{
        "type": "text", "text": "🔥 今日值得注意", "weight": "bold",
        "size": "xl", "color": "#1B2027"
    }]
    if events:
        for idx, event in enumerate(events[:3], 1):
            contents.append({
                "type": "text",
                "text": f"{['①', '②', '③'][idx - 1]} {event['title']}",
                "size": "sm", "color": "#454C55", "wrap": True,
                "margin": "md"
            })
    else:
        contents.append({
            "type": "text", "text": "😴 今日市場訊號偏少",
            "size": "sm", "color": "#767D85", "wrap": True,
            "margin": "md"
        })

    contents.append({"type": "separator", "margin": "lg", "color": "#E8EAE6"})
    contents.append({"type": "text", "text": "☀️ 盤前／總經", "weight": "bold",
                     "size": "md", "color": "#6E5228", "margin": "lg"})
    contents.append({"type": "text", "text": "美股指數", "weight": "bold",
                     "size": "xs", "color": "#767D85", "margin": "md"})
    contents.extend(_macro_metric_rows("美股指數"))
    contents.append({"type": "text", "text": "風險指標", "weight": "bold",
                     "size": "xs", "color": "#767D85", "margin": "lg"})
    contents.extend(_macro_metric_rows("風險指標"))
    news = _morning_macro_news()
    if news:
        contents.append({"type": "separator", "margin": "lg", "color": "#E8EAE6"})
        contents.append({"type": "text", "text": "📰 總經焦點", "weight": "bold",
                         "size": "xs", "color": "#767D85", "margin": "lg"})
        for item in news:
            source = f"（{item['source']}）" if item["source"] else ""
            news_component = {"type": "text",
                              "text": f"・{item['title']}{source}",
                              "size": "xs", "color": "#454C55", "wrap": True,
                              "maxLines": 3, "margin": "sm"}
            uri = _valid_news_uri(item.get("link"))
            if uri:
                news_component["color"] = "#4A5F7A"
                news_component["decoration"] = "underline"
                news_component["action"] = {"type": "uri", "uri": uri}
            contents.append(news_component)
    contents += [
        {"type": "separator", "margin": "lg", "color": "#E8EAE6"},
        {"type": "button", "style": "primary", "height": "sm",
         "color": "#6E5228", "margin": "lg",
         "action": {"type": "uri", "label": "查看完整盤前分析",
                    "uri": web_url}}
    ]
    bubble = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical",
                  "contents": contents, "paddingAll": "18px",
                  "backgroundColor": "#FFFFFF"},
        "styles": {"body": {"backgroundColor": "#FFFFFF"}}
    }
    return FlexSendMessage(alt_text=plain_text, contents=bubble)


@app.route("/cron/push-watchlist", methods=["POST", "GET"])
def cron_push_watchlist():
    """早上推播盤前簡報＋自選股摘要。受 PUSH_MAX_USERS 額度保護。"""
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)
    return run_in_background(
        "盤前推播",
        lambda: push_to_users(get_notify_users(), build_morning_push_message, "盤前推播")), 200

@app.route("/cron/detect-premarket-changes", methods=["POST", "GET"])
def cron_detect_premarket_changes():
    """執行盤前變化偵測；可由管理者指定一個平日資料日做安全補抓。"""
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)

    requested_raw = (request.args.get("date") or "").strip()
    today = taiwan_today()
    if requested_raw:
        try:
            requested_date = date.fromisoformat(requested_raw)
        except ValueError:
            return "date 必須使用 YYYY-MM-DD 格式。", 400
        if requested_date.weekday() >= 5:
            return "指定資料日必須是台股平日，週六日不執行台股資料 Job。", 400
        if requested_date > today:
            return "指定資料日不能晚於台灣今天。", 400
        # 測試只允許補抓近期資料，避免把過舊日期誤當成日常盤後批次。
        if (today - requested_date).days > 14:
            return "指定資料日距離今天超過 14 天，請確認日期後再試。", 400
        job_name = f"盤前變化測試 {requested_date.isoformat()}"
        result = run_in_background(
            job_name,
            lambda d=requested_date: run_daily_change_detection(d))
        return f"已排入指定資料日測試：{requested_date.isoformat()}（不會觸發 LINE 推播）\n{result}", 200

    # 正式排程若在週末被手動觸發，直接拒絕，不建立週末快照。
    if today.weekday() >= 5:
        return "今天不是台股交易日，未啟動盤前變化偵測；如需補抓平日，請加上 date=YYYY-MM-DD。", 400
    return run_in_background("盤前變化偵測", run_daily_change_detection), 200

@app.route("/cron/fetch-t86", methods=["POST", "GET"])
def cron_fetch_t86():
    """每個交易日收盤後自動抓一次 T86 並存進歷史表，不依賴使用者操作。"""
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)

    return run_in_background("抓法人資料", _do_fetch_t86), 200


def _do_fetch_t86():
    # 清掉快取強制重抓，確保拿到今天最新公布的資料
    _t86_cache["cache_date"] = None
    data = fetch_institutional_data()
    if not data:
        return "無資料（非交易日或尚未公布）"
    n_days, missing, newest = check_data_integrity(30)
    warn = ""
    if missing:
        warn = f"　⚠️ 近30天疑似缺 {'、'.join(d.strftime('%m/%d') for d in missing[:5])}"
    return (f"日期 {_t86_cache.get('data_date')}、{len(data)} 檔（上市＋上櫃）、"
            f"近30天已存 {n_days} 個交易日{warn}")


# ── 背景工作 ──
# cron-job.org 的請求有 30 秒上限，但每日快照要逐一處理每個使用者的持股、
# 逐檔抓報價，一定超過。排程的意義是「準時觸發」而不是「等它做完」，
# 所以端點立刻回應，實際工作丟到背景執行緒。
#
# 狀態存資料庫而不是記憶體：gunicorn 開多個 worker 時，工作在 A 執行、
# 查詢卻可能連到 B，記憶體版本會看到空的；服務重啟也會全部消失。
JOB_STALE_MINUTES = 30   # 超過這麼久還標示執行中，視為當掉的殘留


def _job_batch_id(name):
    """以台灣交易日識別批次；同日重試可續跑，隔日會從第一階段重新開始。"""
    return f"{name}:{taiwan_today().isoformat()}"


def _job_mark_start(name):
    """
    標記工作開始。若同名工作仍在執行且未逾時，不重複啟動；若是同一交易日
    的失敗或逾時重試，保留 progress_stage／progress_index 讓工作可以續跑。
    """
    batch_id = _job_batch_id(name)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO job_runs
                (name, running, started_at, run_id, progress_stage,
                 progress_index, progress_total)
            VALUES (%s, TRUE, NOW(), %s, 'industry', 0, NULL)
            ON CONFLICT (name) DO UPDATE SET
                running = TRUE, started_at = NOW(), result = NULL,
                finished_at = NULL, seconds = NULL,
                run_id = EXCLUDED.run_id,
                progress_stage = CASE
                    WHEN job_runs.run_id = EXCLUDED.run_id
                    THEN COALESCE(job_runs.progress_stage, 'industry')
                    ELSE 'industry' END,
                progress_index = CASE
                    WHEN job_runs.run_id = EXCLUDED.run_id
                    THEN COALESCE(job_runs.progress_index, 0)
                    ELSE 0 END,
                progress_total = CASE
                    WHEN job_runs.run_id = EXCLUDED.run_id
                    THEN job_runs.progress_total
                    ELSE NULL END
            WHERE job_runs.running = FALSE
               OR job_runs.started_at IS NULL
               OR job_runs.started_at < NOW() - INTERVAL '%s minutes'
            RETURNING name
            """,
            (name, batch_id, JOB_STALE_MINUTES),
        )
        got = cur.fetchone() is not None
        conn.commit()
        cur.close()
        return got
    except Exception as e:
        conn.rollback()
        print(f"⚠️ 標記工作開始失敗（照樣執行）: {e}")
        return True      # 記錄失敗不該擋住真正的工作
    finally:
        release_db_connection(conn)


def _job_get_progress(name):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT run_id, COALESCE(progress_stage, 'industry'),
                   COALESCE(progress_index, 0), progress_total
            FROM job_runs WHERE name=%s
        """, (name,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return {"run_id": row[0], "stage": row[1],
                "index": row[2] or 0, "total": row[3]}
    except Exception as e:
        print(f"⚠️ 讀取工作進度失敗: {e}")
        return None
    finally:
        release_db_connection(conn)


def _job_mark_progress(name, stage, index=0, total=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE job_runs
            SET progress_stage=%s, progress_index=%s, progress_total=%s
            WHERE name=%s AND running=TRUE
        """, (str(stage), int(index), total, name))
        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        print(f"⚠️ 記錄工作進度失敗: {e}")
    finally:
        release_db_connection(conn)


def _job_mark_done(name, result, seconds):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE job_runs SET running = FALSE, finished_at = NOW(),
                   seconds = %s, result = %s,
                   progress_stage = CASE WHEN name = '每日快照'
                                         THEN COALESCE(progress_stage, 'industry')
                                         ELSE 'done' END
            WHERE name = %s
            """,
            (round(seconds, 1), str(result)[:2000], name),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        print(f"⚠️ 標記工作完成失敗: {e}")
    finally:
        release_db_connection(conn)


def log_trigger_source(name):
    """
    記錄是誰觸發了這個端點。

    推播會直接送訊息給使用者，所以「為什麼在我沒排程的時間發出去」
    必須查得出來。只看得到端點被呼叫、卻不知道來源，就只能猜。
    User-Agent 通常就足以分辨：cron-job.org 會帶自己的識別，
    瀏覽器手動打開會帶 Mozilla/…，其他排程服務也各有特徵。
    """
    try:
        ip = (request.headers.get("X-Forwarded-For", "")
              .split(",")[0].strip() or request.remote_addr or "?")
        ua = request.headers.get("User-Agent", "(無)")[:120]
        print(f"🔔 觸發 {name}｜來源 {ip}｜UA {ua}｜"
              f"時間 {taiwan_now().strftime('%Y-%m-%d %H:%M:%S')}（伺服器時區）")
    except Exception as e:
        print(f"⚠️ 記錄觸發來源失敗: {e}")


def run_in_background(name, fn):
    """把耗時工作丟到背景並立刻回應，避免 cron 端 30 秒超時。"""
    log_trigger_source(name)
    if not _job_mark_start(name):
        return f"{name} 仍在執行中，本次略過。"

    def _wrap():
        t0 = time.time()
        try:
            result = fn()
            _job_mark_done(name, result, time.time() - t0)
            print(f"✅ 背景工作 {name} 完成（{time.time() - t0:.1f}s）：{result}")
        except Exception as e:
            _job_mark_done(name, f"失敗：{e}", time.time() - t0)
            print(f"❌ 背景工作 {name} 失敗: {e}")

    threading.Thread(target=_wrap, daemon=True).start()
    return f"{name} 已在背景啟動。稍候用 /job-status?token=... 看結果。"


@app.route("/job-status", methods=["POST", "GET"])
def job_status():
    """查背景工作的執行結果。用法：/job-status?token=..."""
    if request.args.get("token") != os.environ.get("CRON_SECRET"):
        abort(403)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT name, running, started_at, finished_at, seconds, result,
                   run_id, progress_stage, progress_index, progress_total
            FROM job_runs ORDER BY COALESCE(finished_at, started_at) DESC
            """)
        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        return plain_text_page([f"查詢失敗：{e}"]), 500
    finally:
        release_db_connection(conn)

    if not rows:
        return plain_text_page([
            "還沒有任何背景工作執行過。", "",
            "先觸發一次，例如：",
            "　/cron/snapshot-portfolio?token=...",
            "　/cron/fetch-t86?token=...",
            "　/cron/warmup?token=...",
            "", "跑完後再回來這一頁就會看到結果。"]), 200

    now_srv = taiwan_now()
    tw = taiwan_now()
    lines = ["背景工作狀態", "=" * 58, "",
             f"伺服器現在時間　{now_srv.strftime('%m/%d %H:%M')}",
             f"台灣現在時間　　{tw.strftime('%m/%d %H:%M')}",
             ("（兩者相同，時間可直接對照）" if abs(now_srv.hour - tw.hour) == 0
              else f"（相差 {(tw.hour - now_srv.hour) % 24} 小時，"
                   f"下方時間為伺服器時區）"),
             ""]
    now_tw = taiwan_now()
    for (name, running, started, finished, secs, result,
         run_id, progress_stage, progress_index, progress_total) in rows:
        if running:
            started_tw = as_taiwan_datetime(started)
            mins = ((now_tw - started_tw).total_seconds() / 60
                    if started_tw else 0)
            stale = "　⚠️ 疑似當掉" if mins > JOB_STALE_MINUTES else ""
            progress = (f"｜{progress_stage} {progress_index}"
                        + (f"/{progress_total}" if progress_total is not None else ""))
            lines.append(f"[執行中] {name}"
                         f"（已 {mins:.0f} 分鐘）{progress}{stale}")
        else:
            when = _admin_format_time(finished) if finished else "?"
            progress = (f"｜批次 {run_id}｜{progress_stage}"
                        if run_id or progress_stage else "")
            lines.append(f"[完成] {name}　{when}　耗時 {secs or 0:.1f} 秒{progress}")
        if result:
            for chunk in str(result).split("、"):
                lines.append(f"    {chunk}")
        lines.append("")
    return plain_text_page(lines), 200


@app.route("/cron/snapshot-portfolio", methods=["POST", "GET"])
def cron_snapshot_portfolio():
    """
    每個交易日收盤後存快照：組合市值、自選股評分、產業動能排名、選股名單。

    這幾樣的共同點是「當下都算得出來，但過去算不出來」——
    沒有每天存，就永遠只能顯示靜態數字，看不出任何變化趨勢。

    實際工作丟到背景執行：要逐一處理每個使用者的持股並逐檔抓報價，
    一定超過 cron-job.org 的 30 秒上限。排程的意義是準時觸發，
    不是等它做完，所以立刻回應即可。
    建議跟 /cron/fetch-t86 排在附近時段（收盤後），一天跑一次。
    """
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)
    return run_in_background("每日快照", _do_daily_snapshot), 200


def _do_daily_snapshot():
    """實際的快照工作；依階段 checkpoint，逾時重試時從上次完成處續跑。"""
    job_name = "每日快照"
    progress = _job_get_progress(job_name) or {
        "stage": "industry", "index": 0, "total": None}
    stage_order = {"industry": 0, "picks": 1, "portfolio": 2,
                   "watchlist": 3, "rank": 4, "done": 5}
    current_stage = progress.get("stage") or "industry"
    if current_stage == "done":
        return f"{taiwan_today()} 的每日快照已完成，避免重複抓取。"

    def reached(stage):
        return stage_order.get(current_stage, 0) > stage_order[stage]

    taiex_close = None
    try:
        taiex = fetch_taiex_summary()
        if taiex and taiex.get("close"):
            taiex_close = float(str(taiex["close"]).replace(",", ""))
    except (TypeError, ValueError, Exception) as e:
        print(f"⚠️ 讀取大盤收盤值失敗: {e}")

    ind_saved = 0
    if not reached("industry"):
        try:
            stats = get_industry_momentum(fetch_monthly_revenue() or {},
                                          get_industry_map() or {})
            save_industry_momentum(stats)
            ind_saved = len(stats)
        except Exception as e:
            print(f"❌ 產業動能快照失敗: {e}")
            raise
        _job_mark_progress(job_name, "picks", 0, 2)
        current_stage = "picks"

    picks_saved = 0
    if not reached("picks"):
        modes = ("blackhorse", "radar")
        start = progress.get("index", 0) if current_stage == "picks" else 0
        for idx, mode in enumerate(modes):
            if idx < start:
                continue
            try:
                rows, _skipped, _mom = compute_screener_rows(mode)
                if mode == "radar":
                    rows = sorted(rows, key=lambda r: (
                        2 if r["breakout"] == "季線新高" else (1 if r["breakout"] else 0),
                        r.get("vol_ratio") or 0, r["streak"], r["pct"]), reverse=True)
                else:
                    rows = sorted(rows, key=lambda r: (
                        r["score"] if r["score"] is not None else -1), reverse=True)
                picks_saved += save_picks(mode, rows, top_n=5)
            except Exception as e:
                print(f"❌ {mode} 選股名單快照失敗: {e}")
                raise
            _job_mark_progress(job_name, "picks", idx + 1, len(modes))
        _job_mark_progress(job_name, "portfolio", 0, 0)
        current_stage = "portfolio"

    user_ids = get_all_position_user_ids()
    saved, skipped = 0, 0
    if not reached("portfolio"):
        start = progress.get("index", 0) if current_stage == "portfolio" else 0
        _job_mark_progress(job_name, "portfolio", start, len(user_ids))
        for idx, uid in enumerate(user_ids):
            if idx < start:
                continue
            try:
                positions = merge_positions(get_positions(uid))
                if not positions:
                    skipped += 1
                else:
                    total_value, total_cost = 0.0, 0.0
                    price_map = get_realtime_stocks_bulk([p["code"] for p in positions])
                    for p in positions:
                        pr = price_map.get(p["code"])
                        if pr:
                            total_value += pr["close"] * p["shares"]
                        total_cost += p["cost"] * p["shares"]
                    if total_value <= 0 or not save_portfolio_snapshot(
                            uid, total_value, total_cost, taiex_close):
                        skipped += 1
                    else:
                        saved += 1
            except Exception as e:
                # 單一使用者出錯不該讓其他人的快照一起沒了
                print(f"❌ 組合快照失敗 {uid}: {e}")
                skipped += 1
            _job_mark_progress(job_name, "portfolio", idx + 1, len(user_ids))
        _job_mark_progress(job_name, "watchlist", 0, 0)
        current_stage = "watchlist"

    wl_users = get_all_watchlist_user_ids()
    wl_saved = 0
    if not reached("watchlist"):
        start = progress.get("index", 0) if current_stage == "watchlist" else 0
        _job_mark_progress(job_name, "watchlist", start, len(wl_users))
        for idx, uid in enumerate(wl_users):
            if idx < start:
                continue
            codes = get_user_watchlist(uid)
            if codes:
                try:
                    scores = compute_watchlist_scores(codes)
                    if scores:
                        save_watchlist_scores(uid, scores)
                        wl_saved += 1
                except Exception as e:
                    print(f"❌ 自選股評分快照失敗 {uid}: {e}")
            _job_mark_progress(job_name, "watchlist", idx + 1, len(wl_users))
        _job_mark_progress(job_name, "rank", 0, 1)
        current_stage = "rank"

    rank_saved = 0
    if not reached("rank"):
        rank_saved = save_leaderboard_rank_snapshots()
        _job_mark_progress(job_name, "done", 1, 1)

    return (f"組合本次續跑處理 {saved}（略過 {skipped}，共 {len(user_ids)}）、"
            f"自選本次 {wl_saved}/{len(wl_users)}、產業 {ind_saved}、"
            f"選股名單 {picks_saved}、排行榜名次 {rank_saved}、大盤 {taiex_close}")


# ── 資料保留期限 ──
# Supabase 免費方案 500MB。inst_history 每天約 2,000 筆、一年約 60MB，
# 單靠它可以撐好幾年；真正會隨使用者數線性成長的是 watchlist_scores
# （使用者數 × 自選檔數 × 天數），開放給多人使用後必須設上限。
#
# 期限訂在「遠大於實際查詢範圍」而不是「剛好夠用」：
# 程式只查 inst_history 最近 20 天，但保留兩年，
# 留著是為了日後想做回測時有東西可用——資料刪掉就再也回不來了
# （TWSE T86 可以回補，但 TPEx 上櫃那個端點只給最新一日，刪了就沒了）。
RETENTION_DAYS = {
    "inst_history": ("trade_date", 730),        # 2 年，保留回測空間
    "watchlist_scores": ("snapshot_date", 400),  # 約 1 年多，只用於比對變化
    "portfolio_snapshots": ("snapshot_date", 1095),  # 3 年，每人每天才一筆
    "pick_history": ("pick_date", 1095),         # 3 年，量極小且是成效追蹤的依據
    "industry_momentum_history": ("snapshot_date", 1095),
    "web_sessions": ("expires_at", 0),           # 過期即可刪
    "web_codes": ("expires_at", 0),
    "leaderboard_rank_snapshots": ("snapshot_date", 1095),
    "activity_log": ("occurred_at", 730),
    "line_event_dedup": ("received_at", 14),
    "premarket_events": ("created_at", 730),
    "premarket_snapshots": ("snapshot_date", 730),
}


def get_db_stats():
    """
    各表的筆數與磁碟用量。開放使用後要能一眼看出哪張表在暴衝，
    而不是等到寫入開始失敗才發現額度用完。
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # 只看 public schema。Supabase 內建的 auth／storage 等 schema 有幾十張
        # 系統表（oauth_clients、sso_domains…），全列出來會把自己的資料表淹沒，
        # 而那些也不是你能控制或需要清理的。
        cursor.execute("""
            SELECT relname,
                   n_live_tup,
                   pg_total_relation_size(relid)
            FROM pg_stat_user_tables
            WHERE schemaname = 'public'
            ORDER BY pg_total_relation_size(relid) DESC
        """)
        rows = cursor.fetchall()
        cursor.execute("SELECT pg_database_size(current_database())")
        total = cursor.fetchone()[0]
        cursor.close()
        return rows, total
    except Exception as e:
        print(f"❌ 讀取資料庫統計失敗: {e}")
        return [], 0
    finally:
        release_db_connection(conn)


@app.route("/db-stats", methods=["POST", "GET"])
def db_stats():
    """資料庫用量診斷。用法：/db-stats?token=..."""
    if request.args.get("token") != os.environ.get("CRON_SECRET"):
        abort(403)

    rows, total = get_db_stats()
    if not rows:
        return "查詢失敗，請看 Render Logs。", 200

    def mb(b):
        return f"{b / 1024 / 1024:.1f} MB"

    lines = [f"資料庫總用量：{mb(total)}　（Supabase 免費方案上限 500 MB）",
             "（含 Supabase 內建的 auth 等系統 schema；下表只列你自己的資料表）",
             "=" * 58, "",
             f"{'資料表':26}{'筆數':>11}{'大小':>11}   保留期限",
             "-" * 58]
    own = 0
    for name, n, size in rows:
        keep = RETENTION_DAYS.get(name)
        policy = f"保留 {keep[1]} 天" if keep and keep[1] else (
            "過期即刪" if keep else "不刪")
        own += size or 0
        lines.append(f"{name:26}{n or 0:>10,} 筆{mb(size):>11}   {policy}")

    pct = total / (500 * 1024 * 1024) * 100
    lines += ["-" * 58,
              f"自己的資料表合計：{mb(own)}",
              f"整個資料庫：{mb(total)}　已使用約 {pct:.1f}%"]
    if pct > 70:
        lines.append("⚠️ 超過七成，建議跑 /cron/cleanup 或縮短保留期限")
    else:
        lines.append("用量正常，暫時不需要處理")
    return plain_text_page(lines), 200


@app.route("/cron/cleanup", methods=["POST", "GET"])
def cron_cleanup():
    """
    依保留期限清掉舊資料。建議每週跑一次（例如週日凌晨）。

    只刪超過期限的，不動近期資料；每張表分開執行，
    某一張刪失敗不影響其他張——清理是維運工作，
    不該因為一個錯誤就整批停擺。
    """
    if request.args.get("token") != os.environ.get("CRON_SECRET"):
        abort(403)

    results = []
    for table, (col, days) in RETENTION_DAYS.items():
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            if days:
                cursor.execute(
                    f"DELETE FROM {table} WHERE {col} < CURRENT_DATE - %s", (days,))
            else:
                # web_sessions／web_codes 存的是到期時間，過期就沒有保留價值
                cursor.execute(f"DELETE FROM {table} WHERE {col} < NOW()")
            deleted = cursor.rowcount
            conn.commit()
            cursor.close()
            results.append(f"{table}: 刪除 {deleted:,} 筆")
        except Exception as e:
            conn.rollback()
            print(f"❌ 清理 {table} 失敗: {e}")
            results.append(f"{table}: 失敗（{e}）")
        finally:
            release_db_connection(conn)

    _rows, total = get_db_stats()
    return ("清理完成\n" + "\n".join(results)
            + f"\n\n目前總用量：{total / 1024 / 1024:.1f} MB"), 200


@app.route("/cron/warmup", methods=["POST", "GET"])
def cron_warmup():
    """
    預熱當天的中繼資料快取（法人、月營收、估值、產業別、名稱對照）。

    這些資料一天只需抓一次，但快取是「當天第一次呼叫時」才填的——
    代表每天第一個使用者要獨自承擔全部的抓取時間（可能數十秒），
    後面的人才享受得到快取。把這件事交給機器人自己在開盤前做完，
    使用者任何時候進來都是熱的。

    建議排在交易日早上 08:00 之前跑一次，收盤抓完 T86 之後再跑一次。
    """
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)

    return run_in_background("預熱快取", _do_warmup), 200


def _warm_current_position_quotes():
    """預熱目前持股的 3mo 真實行情，直接填入既有90秒記憶體快取。"""
    if taiwan_today().weekday() >= 5:
        return 0, 0, 0, "週末略過"
    user_ids = get_all_position_user_ids()
    codes = set()
    for uid in user_ids:
        try:
            for position in merge_positions(get_positions(uid)):
                code = str(position.get("code") or "").strip()
                if code:
                    codes.add(code)
        except Exception as exc:
            print(f"⚠️ 預熱使用者 {uid} 持股失敗: {exc}")
    if not codes:
        return len(user_ids), 0, 0, "沒有持股"
    prices = get_realtime_stocks_bulk(sorted(codes), workers=12, rng="3mo")
    valid = sum(1 for value in prices.values() if value)
    return len(user_ids), len(codes), valid, "完成"


def _do_warmup():
    done = []
    for label, fn in [
        ("法人", fetch_institutional_data),
        ("月營收", fetch_monthly_revenue),
        ("估值", fetch_valuation),
        ("產業別", get_industry_map),
        ("名稱對照", get_name_map),
    ]:
        try:
            data = fn()
            done.append(f"{label} {len(data) if data else 0}")
        except Exception as e:
            print(f"❌ 預熱 {label} 失敗: {e}")
            done.append(f"{label} 失敗")

    # 今日完整首頁最慢的外部資料是持股即時行情；交易日先預熱到既有
    # 90 秒記憶體快取，使用者開頁時直接命中。週末不把最新收盤誤當成今日行情。
    try:
        user_count, code_count, valid_count, state = _warm_current_position_quotes()
        done.append(f"持股行情 {valid_count}/{code_count} 檔（{user_count} 人，{state}）")
    except Exception as e:
        print(f"❌ 預熱持股行情失敗: {e}")
        done.append("持股行情 失敗")

    # 順便把選股台的候選池也算好，使用者進來就是快取命中
    for mode in ("blackhorse", "radar"):
        try:
            rows, _s, _m = compute_screener_rows(mode)
            done.append(f"{mode} {len(rows)} 檔")
        except Exception as e:
            print(f"❌ 預熱 {mode} 失敗: {e}")
    return "、".join(done)


@app.route("/sync-industry", methods=["POST", "GET"])
def sync_industry():
    """抓取並儲存上市公司產業別。一次性作業，之後想更新再打一次。"""
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)

    count, sample = fetch_and_save_industry()
    if not count:
        return "抓取失敗或無資料，請看 Render Logs。", 200

    get_industry_map(force_reload=True)
    get_name_map(force_reload=True)
    return (
        f"公司基本資料同步完成\n"
        f"共存入：{count} 檔（上市＋上櫃＋興櫃）\n\n"
        + "\n".join(sample)
    ), 200


def plain_text_page(lines):
    """
    診斷輸出用等寬字體呈現。

    直接回純文字時，手機瀏覽器會用比例字體並自動把換行吃掉，
    逐列對齊的表格會擠成一團完全無法閱讀——診斷頁最需要的就是對齊。
    包一層 <pre> 並宣告 UTF-8，欄位才會排整齊。
    """
    body = "\n".join(str(x) for x in lines)
    body = (body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return f"""<!DOCTYPE html><html lang="zh-Hant"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>診斷</title></head>
<body style="margin:0;background:#12161B;color:#D8DBD4">
<pre style="font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace;
 font-size:12px;line-height:1.6;padding:14px;margin:0;
 white-space:pre;overflow-x:auto">{body}</pre>
</body></html>"""


@app.route("/check-pool", methods=["POST", "GET"])
def check_pool():
    """
    診斷選股台候選池。用法：/check-pool?token=...&cat=傳產
    逐層顯示每個階段剩下幾檔，一眼看出是卡在分類、買超查詢還是流動性門檻。
    """
    if request.args.get("token") != os.environ.get("CRON_SECRET"):
        abort(403)
    cat = request.args.get("cat", "傳產")

    ind_map = get_industry_map() or {}
    lines = [f"類股：{cat}", "=" * 30, f"stock_info 有產業別的代號數：{len(ind_map)}"]

    counts = {}
    for c in ind_map:
        k = stock_category(c, ind_map)
        counts[k] = counts.get(k, 0) + 1
    lines.append(f"各類股數量：{counts}")

    cat_codes = [c for c in ind_map if stock_category(c, ind_map) == cat]
    lines.append(f"本類股代號數：{len(cat_codes)}")
    lines.append(f"前 10 個：{cat_codes[:10]}")

    cum = get_cumulative_net_buy(days=10, top_n=120, codes=cat_codes)
    lines.append(f"近10日累計買超查詢結果：{len(cum)} 檔")
    if cum:
        lines.append("前 5 名：" + "、".join(
            f"{n}({c}) {cl:,}張/{bd}天" for c, n, cl, bd in cum[:5]))
    else:
        lines.append("⚠️ 查詢回傳空清單——看 Render Logs 是否有『查詢累計買超失敗』")

    hist_days = get_history_days_count()
    lines.append(f"inst_history 累積交易日數：{hist_days}")

    min_close = 10 if cat == "電子" else 8
    min_turnover = 1.0 if cat == "電子" else 0.3
    lines.append(f"流動性門檻：股價≥{min_close}、成交金額≥{min_turnover}億")

    ok = noprice = lowprice = lowturn = 0
    pool_prices = get_realtime_stocks_bulk([c for c, _n, _cl, _bd in cum[:30]])
    for c, _n, _cl, _bd in cum[:30]:
        pr = pool_prices.get(c)
        if not pr:
            noprice += 1
            continue
        if pr["close"] < min_close:
            lowprice += 1
            continue
        if calc_turnover_billion(pr["close"], pr["volume"]) < min_turnover:
            lowturn += 1
            continue
        ok += 1
    lines.append(f"前30名逐檔檢查：通過 {ok}、查無行情 {noprice}、"
                 f"股價過低 {lowprice}、成交金額不足 {lowturn}")
    return plain_text_page(lines), 200


@app.route("/check-source", methods=["POST", "GET"])
def check_source():
    """
    診斷單一代號在各資料源的狀況。
    用法：/check-source?token=...&code=7781
    會顯示資料庫裡存了什麼，以及各來源的原始欄位名稱——
    欄位名稱各市場不一致，抓不到時多半是名稱對不上而不是沒資料。
    """
    if request.args.get("token") != os.environ.get("CRON_SECRET"):
        abort(403)
    code = normalize_code(request.args.get("code", "")) or "7781"
    lines = [f"診斷代號：{code}", "=" * 30, ""]

    # 資料庫
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT code, name, industry, market FROM stock_info WHERE code = %s",
                    (code,))
        row = cur.fetchone()
        cur.execute("SELECT COUNT(*), COUNT(name) FROM stock_info")
        total, named = cur.fetchone()
        cur.execute("SELECT market, COUNT(*) FROM stock_info GROUP BY market")
        by_market = cur.fetchall()
        cur.close()
        lines.append(f"[stock_info] 總筆數 {total}，有名稱 {named}")
        lines.append(f"  各市場：{dict(by_market)}")
        lines.append(f"  本代號：{row if row else '不存在'}")
    except Exception as e:
        lines.append(f"[stock_info] 查詢失敗：{e}")
    finally:
        release_db_connection(conn)

    lines.append("")
    sources = [
        ("上市基本資料", f"{TWSE_BASE}/opendata/t187ap03_L", ("公司代號",)),
        ("上櫃基本資料", f"{TPEX_BASE}/mopsfin_t187ap03_O", ("公司代號", "SecuritiesCompanyCode")),
        ("興櫃基本資料", f"{TPEX_BASE}/mopsfin_t187ap03_R", ("公司代號", "SecuritiesCompanyCode")),
    ]
    for label, url, keys in sources:
        rows = _get_json(url)
        if not rows:
            lines.append(f"[{label}] 抓取失敗或無資料")
            lines.append("")
            continue
        lines.append(f"[{label}] 共 {len(rows)} 筆")
        lines.append(f"  欄位：{list(rows[0].keys())[:12]}")
        hit = None
        for r in rows:
            for k in keys:
                if str(r.get(k, "")).strip() == code:
                    hit = r
                    break
            if hit:
                break
        lines.append(f"  找到本代號：{hit if hit else '否'}")
        lines.append("")

    return plain_text_page(lines), 200


@app.route("/check-inst", methods=["POST", "GET"])
def check_inst():
    """
    診斷單一代號的法人歷史。用法：/check-inst?token=...&code=6669

    把資料庫裡實際存的每日數字逐列印出來，並標出「近10日」查詢實際
    用到哪幾天——數字對不上時，多半不是加總錯，而是取到的日期範圍
    跟你以為的不一樣（例如某些日期只有上櫃資料、或回補漏了某天）。

    每個查詢各自借連線、各自處理錯誤：把三個查詢包在同一個 try 裡的話，
    任何一步出錯都會讓整頁只剩一行錯誤訊息，而且錯誤還可能被包裝成
    看似無關的樣子（例如連線在前一個錯誤後進入異常狀態，
    下一個查詢就報 SSL 錯誤），完全看不出真正壞在哪一步。
    """
    if request.args.get("token") != os.environ.get("CRON_SECRET"):
        abort(403)
    code = normalize_code(request.args.get("code", "")) or "2330"
    try:
        days = max(1, min(60, int(request.args.get("days") or 15)))
    except ValueError:
        days = 15

    def q(sql, params, label):
        """跑一個查詢，回傳 (結果, 錯誤訊息)。錯誤不往外拋，讓其他步驟照跑。"""
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return rows, None
        except Exception as e:
            print(f"❌ check-inst [{label}] 失敗: {e}")
            return [], f"{type(e).__name__}: {e}"
        finally:
            release_db_connection(conn)

    lines = [f"代號 {code} 的法人歷史", "=" * 58, ""]
    errors = []

    all_rows, err = q(
        "SELECT DISTINCT trade_date FROM inst_history "
        "ORDER BY trade_date DESC LIMIT %s", (days,), "交易日清單")
    if err:
        errors.append(f"[交易日清單] {err}")
    all_dates = [r[0] for r in all_rows]

    rows, err = q(
        "SELECT trade_date, foreign_net_lots, trust_net_lots, "
        "dealer_net_lots, total_net_lots FROM inst_history "
        "WHERE code = %s ORDER BY trade_date DESC LIMIT %s",
        (code, days), "個股明細")
    if err:
        errors.append(f"[個股明細] {err}")

    counts = {}
    if all_dates:
        cnt_rows, err = q(
            "SELECT trade_date, COUNT(*) FROM inst_history "
            "WHERE trade_date >= %s GROUP BY trade_date",
            (all_dates[-1],), "每日檔數")
        if err:
            errors.append(f"[每日檔數] {err}")
        counts = dict(cnt_rows)

    if errors:
        lines.append("⚠️ 部分查詢失敗：")
        lines += [f"　{e}" for e in errors]
        lines.append("")

    if not all_dates:
        lines.append("資料庫裡沒有任何法人歷史，先跑 /cron/fetch-t86 或 /backfill。")
        return plain_text_page(lines), 200

    lines.append(f"資料庫最近 {len(all_dates)} 個交易日："
                 f"{all_dates[0]} ~ {all_dates[-1]}")
    lines.append("")
    lines.append("日期          外資    投信    自營    合計   全市場  10日窗")
    lines.append("-" * 58)

    win10 = set(all_dates[:10])
    have = {r[0] for r in rows}
    sums = [0, 0, 0, 0]
    for d, f, t, dl, tot in rows:
        f, t, dl, tot = (f or 0), (t or 0), (dl or 0), (tot or 0)
        n = counts.get(d, 0)
        thin = " <少" if 0 < n < 1500 else ""
        if d in win10:
            sums[0] += f; sums[1] += t; sums[2] += dl; sums[3] += tot
        lines.append(f"{d}  {f:>7,} {t:>7,} {dl:>7,} {tot:>7,}  {n:>6,}{thin}"
                     f"   {'Y' if d in win10 else ''}")

    lines += ["-" * 58,
              f"10日窗加總　外資 {sums[0]:+,}　投信 {sums[1]:+,}　"
              f"自營 {sums[2]:+,}　合計 {sums[3]:+,}",
              ""]

    missing = [d for d in all_dates[:10] if d not in have]
    if missing:
        lines.append(f"⚠️ 窗內有 {len(missing)} 天缺這檔的紀錄："
                     + "、".join(str(d) for d in missing))
    else:
        lines.append("窗內每一天都有這檔的紀錄")

    # 交易日連續性檢查：資料庫少了某一天時，10 日窗會悄悄往前多抓一天，
    # 數字照樣算得出來也看起來合理，只有跟外部資料對帳才會發現。
    gaps = []
    for i in range(len(all_dates) - 1):
        delta = (all_dates[i] - all_dates[i + 1]).days
        if delta > 4:      # 跨週末最多 3 天，超過代表中間漏了交易日
            gaps.append(f"{all_dates[i + 1]} → {all_dates[i]}（相隔 {delta} 天）")
    if gaps:
        lines += ["", "⚠️ 日期序列有異常間隔，可能漏抓了交易日："]
        lines += [f"　{g}" for g in gaps]
        lines.append("　用 /backfill?days=5&offset=N 回補")

    thin_days = [d for d in all_dates[:10] if 0 < counts.get(d, 0) < 1500]
    if thin_days:
        lines += ["", "⚠️ 以下日期全市場檔數偏少，可能只存到單一市場："]
        lines += [f"　{d}　{counts.get(d, 0):,} 檔" for d in thin_days]

    return plain_text_page(lines), 200


@app.route("/check-industry", methods=["POST", "GET"])
def check_industry():
    """列出每個產業別代碼＋對照名稱＋3家範例公司，用來人工確認對照表正確。"""
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code, name, industry FROM stock_info ORDER BY industry, code")
        rows = cursor.fetchall()
        cursor.close()
    except Exception as e:
        return f"查詢失敗: {e}", 500
    finally:
        release_db_connection(conn)

    grouped = {}
    for code, name, ind in rows:
        grouped.setdefault(ind, []).append(f"{name}({code})")

    lines = []
    for ind in sorted(grouped):
        samples = "、".join(grouped[ind][:3])
        lines.append(f"{ind} = {industry_name(ind)}　共{len(grouped[ind])}檔　例：{samples}")

    return "產業別對照確認\n\n" + "\n".join(lines), 200


@app.route("/backfill", methods=["POST", "GET"])
def backfill_t86():
    """
    回補過去的 T86 歷史資料。
    用法：/backfill?token=你的CRON_SECRET&days=10&offset=0
      days   一次回補幾天（預設 10，上限 15，避免 Render timeout）
      offset 從幾天前開始往回算（第一次 0，第二次 10，第三次 20...）
    重複回補同一天不會產生重複資料。
    """
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)

    try:
        days = min(int(request.args.get("days", 5)), 15)
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return "參數錯誤：days 與 offset 必須是數字", 400

    saved, skipped = [], []
    for i in range(offset, offset + days):
        query_date = (taiwan_now() - timedelta(days=i)).strftime("%Y%m%d")
        data = _fetch_t86_for_date(query_date)
        if data:
            save_t86_history(query_date, data)
            saved.append(f"{query_date}({len(data)})")
        else:
            skipped.append(query_date)
        time.sleep(1.2)  # 禮貌等待，避免被 TWSE 擋

    total_days = get_history_days_count()
    return (
        f"回補完成\n"
        f"成功：{len(saved)} 天 → {', '.join(saved) if saved else '無'}\n"
        f"無資料（假日或未公布）：{len(skipped)} 天 → {', '.join(skipped) if skipped else '無'}\n"
        f"目前資料庫累積：{total_days} 個交易日\n"
        f"下一批請用 offset={offset + days}"
    ), 200


# 會跑比較久的指令，送出後先叫載入動畫
SLOW_COMMANDS = {
    "黑馬", "雷達", "籌碼", "籌碼超人", "認養",
    "自選", "WATCHLIST", "健檢", "自選健檢",
    "盤前", "早安", "解盤", "盤後解盤", "盤後", "新聞", "自選新聞",
}


def start_loading_animation(user_id, seconds=60):
    """
    叫出 LINE 聊天室裡的官方載入動畫（三個點跳動），最長 60 秒。

    用背景執行緒送出，不等它回應：這支 API 現在每則訊息都會呼叫，
    若同步等待，光是這個網路來回就會讓每則訊息都多幾百毫秒到 3 秒的延遲，
    「加動畫」反而讓整體變慢。動畫只是視覺回饋，晚一點出現或沒出現
    都不該影響真正的查詢。

    只在一對一聊天有效，群組會回錯誤，所以整段包在 try 裡。
    linebot SDK 版本較舊時可能沒有這個方法，因此直接打 REST API。
    """
    def _fire():
        try:
            requests.post(
                "https://api.line.me/v2/bot/chat/loading/start",
                headers={
                    "Authorization": f"Bearer {os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')}",
                    "Content-Type": "application/json",
                },
                json={"chatId": str(user_id).strip(),
                      "loadingSeconds": min(60, max(5, int(seconds) // 5 * 5))},
                timeout=3,
            )
        except Exception as e:
            print(f"⚠️ 載入動畫啟動失敗 {user_id}: {e}")

    try:
        threading.Thread(target=_fire, daemon=True).start()
    except Exception as e:
        print(f"⚠️ 載入動畫執行緒啟動失敗: {e}")


def build_quick_reply():
    """
    輸入框上方的快捷列。它會跟著最新一則訊息，不會像選單卡片那樣被
    後續訊息往上推走——使用者看完長長的黑馬報告後，不必往回捲找選單。
    LINE 上限 13 顆，這裡只放最常用的。
    """
    items = [
        ("📋 選單", "選單"),
        ("☀️ 盤前", "盤前"),
        ("🐎 黑馬", "黑馬"),
        ("🚨 雷達", "雷達"),
        ("🦸 籌碼", "籌碼"),
        ("📂 自選", "自選"),
        ("📊 解盤", "解盤"),
        ("📰 新聞", "新聞"),
        ("🌐 網頁", "網頁"),
    ]
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label=label, text=text))
        for label, text in items
    ])


def build_admin_quick_reply():
    items = [
        ("使用者名單", "使用者名單"),
        ("今日活躍", "今日活躍"),
        ("沉睡使用者", "沉睡使用者"),
        ("功能統計", "功能統計"),
        ("可能流失", "流失"),
        ("回管理中心", "管理"),
    ]
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label=label, text=text))
        for label, text in items
    ])


def build_menu_flex(is_admin_user=False):
    """
    彩色選單（Flex Message）。LINE 純文字無法上色，分色只能用 Flex。
    版面上把「按鈕」與「說明」左右並排，比上下堆疊省一半高度，
    說明文字仍保留給第一次使用的人看。
    """
    # (分區名稱, 主色, 按鈕底色（主色的淡色版）, [(指令, 說明)])
    groups = [
        ("市場動態", "#3A6EA5", "#E8EFF7", [
            ("盤前", "美股、殖利率、VIX 與自選摘要"),
            ("解盤", "盤後大盤與三大法人資金"),
        ]),
        ("網頁版", "#6B4E9E", "#EFEAF7", [
            ("網頁", "組合分析、交易紀錄、選股成效"),
        ]),
        ("我的自選", "#2E7D5B", "#E6F1EC", [
            ("自選", "持股評分、位階與支撐壓力"),
            ("新聞", "自選股相關新聞與連結"),
        ]),
        ("選股策略", "#B5822A", "#F7EFDF", [
            ("黑馬", "營收成長＋估值＋產業動能"),
            ("雷達", "帶量突破、法人買超強勢股"),
            ("籌碼超人", "投信、外資各自在認養與撤退的標的"),
        ]),
        ("推播設定", "#7A8290", "#EDEFF1", [
            ("申請推播", "🔒 VIP 限定　每日盤前自動發送\n非 VIP 可直接點上方「盤前」查看相同內容"),
            ("推播關", "停止自動發送"),
        ]),
    ]
    # 盤前手動查詢對所有人開放；主動推播的開通／停用控制只放在管理者選單。
    if not is_admin_user:
        groups = [group for group in groups if group[0] != "推播設定"]

    def row(label, desc, tint):
        """一列＝左邊指令按鈕（該分區的淡色底），右邊說明文字。"""
        return {
            "type": "box", "layout": "horizontal", "margin": "lg",
            "alignItems": "center", "spacing": "md",
            "contents": [
                {
                    "type": "button", "style": "secondary", "height": "md",
                    "color": tint, "flex": 4, "adjustMode": "shrink-to-fit",
                    "action": {"type": "message", "label": label, "text": label},
                },
                {
                    # xxs 對長輩太小，放大到 sm 並把灰階加深以提高對比
                    "type": "text", "text": desc, "size": "sm", "flex": 7,
                    "color": "#6B737B", "wrap": True, "gravity": "center",
                },
            ],
        }

    body = [
        {"type": "text", "text": "台股 BOT", "weight": "bold",
         "size": "xxl", "color": "#1B2027"},
        {"type": "text", "text": "點按鈕即可執行，或直接輸入股票代號　·　查詢約需 20 秒",
         "size": "sm", "color": "#8E959C", "margin": "sm", "wrap": True},
        {"type": "separator", "margin": "lg", "color": "#E8EAE6"},
    ]

    for title, color, tint, items in groups:
        body.append({
            "type": "box", "layout": "horizontal", "margin": "lg",
            "alignItems": "center", "spacing": "sm",
            "contents": [
                {"type": "box", "layout": "vertical", "width": "4px", "height": "18px",
                 "backgroundColor": color, "cornerRadius": "2px", "contents": []},
                {"type": "text", "text": title, "size": "md", "weight": "bold",
                 "color": color},
            ],
        })
        for label, desc in items:
            body.append(row(label, desc, tint))

    # 這段是「怎麼用」的核心說明，原本是灰色小字容易被略過，
    # 改成有底色的區塊＋深色文字，並把要輸入的內容獨立成一行放大。
    def howto(label, cmd, note):
        """
        一組「怎麼打」的說明。指令與說明改成上下堆疊而非並排——
        並排時只要指令一長（例如「分類 2330 短線」），右邊的說明就會
        被擠到換行且對不齊，長度不一的幾行看起來會參差不齊。
        """
        return {
            "type": "box", "layout": "vertical", "margin": "lg", "spacing": "none",
            "contents": [
                {"type": "text", "text": label, "size": "xs",
                 "color": "#8E959C", "weight": "bold"},
                {"type": "text", "text": cmd, "size": "lg", "weight": "bold",
                 "color": "#1B2027", "wrap": True, "margin": "xs"},
                {"type": "text", "text": note, "size": "xs",
                 "color": "#8E959C", "wrap": True, "margin": "xs"},
            ],
        }

    body += [
        {"type": "separator", "margin": "xl", "color": "#E8EAE6"},
        {"type": "text", "text": "也可以直接打字", "size": "md",
         "weight": "bold", "color": "#1B2027", "margin": "lg"},
        {"type": "box", "layout": "vertical", "margin": "md",
         "backgroundColor": "#F4F5F2", "cornerRadius": "4px",
         "paddingAll": "14px", "spacing": "sm",
         "contents": [
             howto("加入自選", "加 2330", "也可同時分類：加 2330 長線"),
             howto("設定分類", "分類 2330 短線", "長線／短線／觀察"),
             howto("移除自選", "刪 2330", "從清單移除"),
             howto("查詢個股", "2330", "只打代號即可"),
         ]},
        {"type": "separator", "margin": "lg", "color": "#EEF0EC"},
        {"type": "text", "text": "作者：蔡秉軒　敬上", "size": "sm",
         "color": "#8E959C", "margin": "md", "align": "end"},
    ]

    bubble = {
        "type": "bubble",
        "body": {
            "type": "box", "layout": "vertical", "contents": body,
            "paddingAll": "22px", "backgroundColor": "#FFFFFF",
        },
        "styles": {"body": {"backgroundColor": "#FFFFFF"}},
    }
    return FlexSendMessage(alt_text="台股 BOT 選單", contents=bubble)


# ============================================================
# 網頁版：版型與頁面
# 設計沿用「紙本月報」風格：冷灰紙底、黃銅結構色，
# 紅綠「只」用於漲跌，不用在按鈕或標籤，避免語意混淆。
# ============================================================
BASE_CSS = """
:root{
  --paper:#E8E9E4; --paper-2:#DBDDD6; --ink:#12161B;
  --ink-soft:#454C55; --ink-faint:#767D85; --rule:#B9BDB4;
  --up:#A82A20; --down:#155C42; --brass:#6E5228; --brass-2:#8A6A3B;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--paper);color:var(--ink);line-height:1.55;
  font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
  .wrap{max-width:720px;margin:0 auto;padding:0 20px 80px}
  .realized-collapse{margin:22px 0;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
  .realized-collapse>summary{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:15px 0;cursor:pointer;list-style:none;font-weight:700;color:var(--ink)}
  .realized-collapse>summary::-webkit-details-marker{display:none}
  .realized-collapse>summary::before{content:'＋';display:inline-block;width:22px;color:var(--brass);font-size:18px}
  .realized-collapse[open]>summary::before{content:'−'}
  .realized-collapse>summary small{margin-left:auto;color:var(--ink-faint);font-size:12px;font-weight:400}
  .realized-body{padding:2px 0 14px}

.num{font-variant-numeric:tabular-nums;
  font-family:"SF Mono",ui-monospace,Menlo,Consolas,monospace}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--ink-faint)}
header{padding:30px 0 20px}
.eyebrow{font-size:11px;letter-spacing:.2em;color:var(--brass);
  text-transform:uppercase;margin-bottom:8px}
h1{font-size:27px;letter-spacing:.02em;font-weight:700;line-height:1.25}
.dateline{margin-top:6px;font-size:12.5px;color:var(--ink-faint)}
nav{display:flex;gap:16px;padding:14px 0;border-top:1px solid var(--rule);
  border-bottom:1px solid var(--rule);font-size:13.5px;margin-bottom:8px;
  flex-wrap:wrap}
nav a{color:var(--ink-soft);text-decoration:none}
nav a.on{color:var(--ink);font-weight:500;border-bottom:2px solid var(--brass);
  padding-bottom:2px}
.page-back{margin:-12px 0 10px}
.page-back a{display:inline-flex;align-items:center;gap:4px;color:var(--brass);
  text-decoration:none;font-size:13px;font-weight:600;padding:5px 0}
.page-back a::first-letter{font-size:20px;line-height:1}
.page-back a:hover{text-decoration:underline}
h2{font-size:16px;font-weight:600;letter-spacing:.02em}
.section-head{display:flex;align-items:baseline;justify-content:space-between;
  margin:30px 0 10px}
.section-note{font-size:12px;color:var(--ink-faint)}
.rows{border-top:1px solid var(--rule)}
.row{display:grid;grid-template-columns:1fr auto;gap:3px 12px;
  padding:15px 0;border-bottom:1px solid var(--rule)}
.name{font-size:15.5px;font-weight:500}
.code{font-size:12px;color:var(--ink-faint);margin-left:6px}
.price{text-align:right;font-size:15.5px;font-weight:500}
.chg{text-align:right;font-size:12.5px}
.meta{grid-column:1/-1;display:flex;gap:16px;flex-wrap:wrap;
  font-size:12px;color:var(--ink-soft);margin-top:4px}
.meta em{font-style:normal;color:var(--ink-faint)}
.sub{color:var(--ink-faint);font-size:11px;margin-left:3px}
.bar{grid-column:1/-1;height:3px;background:var(--paper-2);margin-top:8px}
.bar div{height:100%;background:var(--brass-2)}
form.add{margin-top:26px;padding:18px;background:var(--paper-2)}
form.add h3{font-size:14px;font-weight:600;margin-bottom:12px}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
label{display:block;font-size:11.5px;color:var(--ink-soft);margin-bottom:3px}
input,select{width:100%;padding:9px 10px;font-size:14px;background:#FDFDFC;
  border:1px solid var(--rule);border-radius:2px;color:var(--ink);
  font-family:inherit}
input:focus,select:focus{outline:2px solid var(--brass);outline-offset:-1px}
button{margin-top:12px;padding:10px 20px;font-size:14px;font-family:inherit;
  background:var(--ink);color:var(--paper);border:0;border-radius:2px;cursor:pointer}
button:hover{background:#000}
.del{background:#FFF;color:var(--ink-soft);font-size:11.5px;
  padding:3px 10px;margin:0;border:1px solid var(--rule);border-radius:2px;
  cursor:pointer;line-height:1.4}
.del:hover{background:var(--up);color:#FFF;border-color:var(--up)}
.sell{background:#FFF;color:var(--ink-soft);font-size:11.5px;
  padding:3px 10px;margin:0;border:1px solid var(--rule);border-radius:2px;
  cursor:pointer;line-height:1.4}
.sell:hover{background:var(--brass);color:#FFF;border-color:var(--brass)}
.qty-input{width:64px;padding:4px 6px;font-size:12px;border:1px solid var(--rule);
  border-radius:2px;font-family:inherit;color:var(--ink)}
.disclosure{margin-top:10px}
.disclosure summary{color:var(--brass);cursor:pointer;font-size:12.5px}
/* .meta 是 flex 容器，裡面的 details 要用 flex-basis 才會獨佔一行；
   grid-column 在 flex 容器裡不生效，會讓它變成擠在同一行的普通項目。 */
.meta>.trend{flex:0 0 100%;margin-top:2px}
.meta>.trend summary{font-size:11.5px}
.lots{grid-column:1/-1;margin-top:6px;font-size:12px}
.lots summary{color:var(--brass);cursor:pointer;font-size:11.5px}
.lot{padding:7px 0 7px 12px;color:var(--ink-soft);
  border-left:2px solid var(--rule);margin-top:6px}
.empty{padding:40px 0;text-align:center;color:var(--ink-faint);font-size:14px}
.msg{margin:14px 0;padding:11px 14px;background:var(--paper-2);
  border-left:2px solid var(--brass);font-size:13px}
footer{margin-top:36px;padding-top:18px;border-top:1px solid var(--rule);
  font-size:15px;color:var(--ink-soft);line-height:1.9}
.totals{display:flex;gap:26px;flex-wrap:wrap;padding:18px 0 8px}
.totals>div{min-width:88px}
.band{display:flex;height:48px;width:100%;overflow:hidden;border-radius:2px;
  margin-top:4px}
.band span{display:flex;align-items:center;justify-content:center;
  font-size:11.5px;font-weight:500;white-space:nowrap;overflow:hidden}
.legend{display:flex;flex-wrap:wrap;gap:5px 18px;margin-top:10px;
  font-size:12.5px;color:var(--ink-soft)}
.legend i{display:inline-block;width:9px;height:9px;margin-right:5px;
  border-radius:1px}
.callout{margin-top:14px;padding:13px 15px;background:var(--paper-2);
  border-left:2px solid var(--brass);font-size:13.5px;line-height:1.7}
.alert{padding:13px 0;border-bottom:1px solid var(--rule);font-size:13.5px;
  line-height:1.7;display:flex;gap:11px}
.alert .tag{font-size:10.5px;letter-spacing:.1em;color:var(--brass);
  padding-top:3px;white-space:nowrap}
.tabs{display:flex;gap:6px;margin:18px 0 6px;align-items:center;flex-wrap:wrap}
.tabs-gap{flex:0 0 14px}
.sector{margin-top:22px}
.sector-head{display:flex;align-items:baseline;justify-content:space-between;
  gap:10px;padding:8px 11px;background:var(--paper-2);
  border-left:3px solid var(--brass)}
.sector-name{font-size:14.5px;font-weight:600}
.sector-mom{font-size:11.5px;color:var(--ink-soft);text-align:right}
.tabs a{padding:7px 18px;font-size:14px;text-decoration:none;
  color:var(--ink-soft);background:var(--paper-2);border-radius:2px}
.tabs a.on{background:var(--ink);color:var(--paper);font-weight:500}
.mode-note{font-size:12px;color:var(--ink-faint);margin-bottom:14px;line-height:1.6}
/* ── 排行榜 App 化 ── */
.rank-situation{position:relative;overflow:hidden;margin:18px 0 20px;padding:18px 16px 16px;background:#FFF;
  border:1px solid #C7C2B5;border-radius:16px;box-shadow:0 7px 20px rgba(35,39,35,.08)}
.rank-situation:before{content:'';position:absolute;left:0;right:0;top:0;height:4px;background:var(--brass)}
.rank-situation-title{display:flex;align-items:center;justify-content:space-between;
  gap:10px;margin-bottom:14px}
.rank-situation-title h2{font-size:20px;margin:0;letter-spacing:.01em}
.rank-situation-badge{font-size:11px;color:var(--brass);border:1px solid #B7A27B;
  background:#FBF8F0;border-radius:7px;padding:4px 8px;white-space:nowrap}
.rank-situation-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:0}
.rank-situation-item{padding:0 9px;border-left:1px solid var(--rule);min-width:0}
.rank-situation-item:first-child{padding-left:0;border-left:0}
.rank-situation-item:last-child{padding-right:0}
.rank-situation-item small{display:block;color:var(--ink-faint);font-size:11px;white-space:nowrap}
.rank-situation-item b{display:block;font-size:21px;line-height:1.25;margin-top:6px;white-space:nowrap;letter-spacing:.01em}
.rank-situation-item .rank-situation-sub{display:block;font-size:11px;color:var(--ink-faint);margin-top:4px;white-space:nowrap}
.rank-situation-empty{padding:8px 0;color:var(--ink-soft);font-size:13px}
.rank-switch-note{font-size:11px;color:var(--ink-faint);margin:7px 0 14px}
.rank-list-caption{display:flex;justify-content:flex-end;align-items:center;
  gap:10px;margin:9px 0 4px;color:var(--ink-faint);font-size:11.5px}
.rank-card{padding:16px 0;border-bottom:1px solid #C9CCC4}
.rank-card:last-child{border-bottom:0}
.rank-card.rank-champion{position:relative;overflow:hidden;margin:10px -11px 17px;padding:17px 13px 13px;
  border:1px solid #B4862D;border-radius:15px;background:radial-gradient(circle at 50% 0%,#FFF8DD 0%,#F7EAC0 46%,#F4E6BC 100%);
  box-shadow:0 8px 22px rgba(110,82,40,.22)}
.rank-card.rank-champion:before{content:'';position:absolute;inset:5px;border:1px solid rgba(181,137,43,.55);
  border-radius:11px;pointer-events:none}
.rank-card.rank-champion:after{content:'✦  ✦  ✦';position:absolute;right:18px;top:9px;color:#B4862D;
  font-size:10px;letter-spacing:4px;opacity:.7;pointer-events:none}
.rank-honour{position:relative;display:flex;align-items:center;gap:10px;margin-bottom:12px;color:#6E5228}
.rank-honour-icon{display:grid;place-items:center;width:74px;height:74px;line-height:1;
  filter:drop-shadow(0 3px 3px rgba(110,82,40,.20))}
.rank-honour-icon svg{display:block;width:74px;height:74px}
.rank-honour b{display:block;font-size:14px;letter-spacing:.08em}
.rank-honour small{display:block;color:#8A6A3B;font-size:11px;margin-top:2px}
.rank-card.rank-champion .rank-number{font-size:21px;color:#6E5228;font-weight:700}
.rank-card.rank-champion .name{font-size:19px;font-weight:700}
.rank-card.rank-champion .rank-return{font-size:24px}
.rank-card.rank-champion .rank-meta{color:#6B604B}
.rank-card.rank-champion .rank-honour{position:relative;justify-content:center;text-align:center;flex-direction:column;gap:4px;
  min-height:126px;padding:9px 12px 12px;margin:0 -2px 14px;background:linear-gradient(180deg,#FFF9E1,#F7E5B4);
  border:1px solid rgba(176,132,45,.58);border-radius:12px;box-shadow:inset 0 0 0 3px rgba(255,255,255,.38),
  0 3px 10px rgba(125,88,28,.10)}
.rank-card.rank-champion .rank-honour:before{content:'✦  ✦  ✦';position:absolute;top:7px;left:0;right:0;
  color:#B4862D;font-size:10px;letter-spacing:7px;opacity:.72}
.rank-card.rank-champion .rank-honour:after{content:'';position:absolute;left:18%;right:18%;bottom:8px;
  border-bottom:1px solid rgba(176,132,45,.48)}
.rank-card.rank-champion .rank-honour-icon{width:88px;height:88px}
.rank-card.rank-champion .rank-honour-icon svg{width:88px;height:88px}
.rank-card.rank-champion .rank-honour b{font-size:15px}
.rank-tier{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.rank-tier-medal{font-size:25px;line-height:1;filter:drop-shadow(0 1px 1px rgba(35,39,35,.14))}
.rank-tier b{font-size:12px;letter-spacing:.08em;color:var(--ink-soft)}
.rank-tier small{font-size:11px;color:var(--ink-faint)}
.rank-card.rank-silver{margin:0 -11px 10px;padding:14px 11px;background:linear-gradient(135deg,#FAFBFA,#EEF0EE);
  border:1px solid #B8BEC1;border-radius:12px;box-shadow:0 3px 10px rgba(35,39,35,.08)}
.rank-card.rank-bronze{margin:0 -11px 10px;padding:14px 11px;background:linear-gradient(135deg,#FFF9F2,#F4E4D6);
  border:1px solid #B78662;border-radius:12px;box-shadow:0 3px 10px rgba(118,76,46,.10)}
.rank-champion-prompt{text-align:center;color:#8A6A3B;font-size:12px;letter-spacing:.04em;padding:0 0 5px}
.rank-row-main{display:grid;grid-template-columns:auto 1fr auto auto;gap:8px;align-items:center}
.rank-number{font-size:16px;min-width:31px;text-align:center}
.rank-card .name{font-size:15.5px;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rank-return{text-align:right;font-size:17px;font-weight:600;white-space:nowrap}
.rank-movement{font-size:12px;white-space:nowrap;justify-self:end}
.rank-meta{display:flex;flex-wrap:wrap;gap:7px 14px;margin:7px 0 0 39px;
  color:var(--ink-soft);font-size:12px}
.rank-meta span{white-space:nowrap}
.rank-meta em{font-style:normal;color:var(--ink-faint)}
.rank-detail{margin:9px 0 0 39px;border-top:1px solid var(--paper-2);padding-top:7px}
.rank-detail summary{color:var(--brass);font-size:12px;cursor:pointer;list-style:none}
.rank-detail summary::-webkit-details-marker{display:none}
.rank-detail summary:after{content:'⌄';float:right;font-size:16px;line-height:12px}
.rank-detail[open] summary:after{content:'⌃'}
.rank-detail-body{display:flex;flex-wrap:wrap;gap:7px 14px;margin-top:8px;color:var(--ink-soft);font-size:12px}
.rank-detail-body span{white-space:nowrap}
.rank-detail-body em{font-style:normal;color:var(--ink-faint)}
.rank-private{display:block;margin:9px 0 0 39px;color:var(--ink-faint);font-size:12px}
.rank-mine{background:#F5F0E5;border-left:3px solid var(--brass);border-radius:10px;
  padding:14px 11px 14px 9px;margin:0 -11px}
.rank-tabs{display:flex;gap:4px;margin:18px 0 8px;padding:4px;background:#D7D9D2;
  border-radius:11px;flex-wrap:nowrap}
.rank-tabs a{flex:1;text-align:center;padding:8px 7px;background:transparent;border-radius:8px;
  color:var(--ink-soft);font-size:13px;white-space:nowrap}
.rank-tabs a.on{background:#FFF;color:var(--ink);font-weight:600;box-shadow:0 2px 7px rgba(35,39,35,.10)}
.rank-tabs a:hover{background:#F7F7F3;color:var(--ink)}
.rank-chart-head{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.rank-chart-head h2{margin:0}
@media(max-width:640px){
  .rank-situation-grid{grid-template-columns:repeat(2,1fr);gap:14px 0}
  .rank-situation-item:nth-child(3){padding-left:0;border-left:0}
  .rank-situation-item:nth-child(2){padding-right:0}
  .rank-situation-item:nth-child(3),.rank-situation-item:nth-child(4){padding-top:10px;border-top:1px solid var(--rule)}
  .rank-row-main{grid-template-columns:auto minmax(0,1fr) auto;gap:7px}
  .rank-movement{grid-column:2/-1;margin-top:-4px}
  .rank-meta,.rank-detail,.rank-private{margin-left:38px}
  .rank-card.rank-champion{margin-left:-8px;margin-right:-8px;padding:16px 11px 12px}
  .rank-card.rank-champion .rank-return{font-size:21px}
  .rank-card.rank-champion .name{font-size:17px}
}
.dist{display:flex;flex-wrap:wrap;gap:14px;padding:11px 13px;
  background:var(--paper-2);font-size:12.5px;color:var(--ink-soft);
  border-left:2px solid var(--brass)}
.dist-item b{color:var(--ink);font-size:14px}
.dist-note{color:var(--ink-faint);margin-left:auto}
.controls{margin:14px 0 6px}
.controls .fields{grid-template-columns:repeat(auto-fit,minmax(110px,1fr))}
.badge{font-size:10.5px;color:var(--brass);border:1px solid var(--brass);
  border-radius:2px;padding:1px 5px;margin-left:6px;vertical-align:2px}
.badge.muted{color:var(--ink-faint);border-color:var(--rule)}
.cat{display:inline-block;width:17px;height:17px;line-height:17px;
  text-align:center;font-size:10.5px;border-radius:2px;margin-right:6px;
  vertical-align:1px;color:#FFF;font-weight:500}
.cat-電子{background:#3A6EA5}
.cat-傳產{background:#7A6A3B}
.cat-金融{background:#2E7D5B}
.num.hot{color:var(--up);font-weight:600}
.num.warm{color:var(--brass);font-weight:600}
.profile-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:1px;background:var(--rule);border:1px solid var(--rule);margin-top:4px}
.pf{background:var(--paper);padding:9px 11px;display:flex;
  justify-content:space-between;gap:10px;font-size:12px;align-items:baseline}
.pf-k{white-space:nowrap}
.pf-k{color:var(--ink-faint)}
.pf-v{color:var(--ink);font-weight:500;text-align:right}
.pf-empty{color:var(--ink-faint);font-weight:400}
.q{padding:14px 0;border-bottom:1px solid var(--rule)}
.q-title{font-size:13.5px;font-weight:500;margin-bottom:8px}
.opt{display:block;font-size:13.5px;color:var(--ink-soft);padding:4px 0;
  cursor:pointer;margin:0}
.opt input{width:auto;margin-right:7px}
.req{font-size:10.5px;color:var(--brass);letter-spacing:.08em;margin-left:5px}
.opt-tag{font-size:10.5px;color:var(--ink-faint);letter-spacing:.08em;
  margin-left:5px}
.hint{font-size:12px;color:var(--ink-soft);background:var(--paper-2);
  padding:10px 13px;border-left:2px solid var(--brass);margin-top:8px}
/* ── 比較表 ──
   欄位數隨標的數變動，手機放不下就橫向捲動，
   而不是硬把字縮到看不清楚。第一欄固定，捲動時才知道在看哪個指標。 */
.cmp-wrap{overflow-x:auto;margin-top:4px}
.cmp{border-collapse:collapse;width:100%;font-size:13.5px;
  font-variant-numeric:tabular-nums}
.cmp th,.cmp td{padding:9px 10px;text-align:right;white-space:nowrap;
  border-bottom:1px solid var(--rule)}
.cmp thead th{font-weight:600;font-size:13px;border-bottom:1px solid var(--ink)}
.cmp thead th .code{display:block;font-size:11px;color:var(--ink-faint);
  margin:2px 0 0}
.cmp th.rk{text-align:left;font-weight:400;color:var(--ink-soft);
  position:sticky;left:0;background:var(--paper);min-width:92px}
.cmp th.rk span{display:block;font-size:10.5px;color:var(--ink-faint)}
.cmp td.best{background:var(--paper-2);font-weight:600;color:var(--ink)}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0 4px}
.tagchip{display:inline-block;padding:5px 12px;font-size:12.5px;
  text-decoration:none;color:var(--ink-soft);background:var(--paper-2);
  border-radius:2px}
.tagchip.on{background:var(--brass);color:#FFF}
.total-label{font-size:12px;color:var(--ink-soft)}
.total-value{font-size:24px;font-weight:600;margin-top:2px}
.total-sub{font-size:12.5px}
/* ── 賣出面板 ──
   賣出要填四個欄位（股數、賣價、手續費、稅），全部攤在列表上會把版面撐爆，
   所以收在 details 裡，需要時才展開。 */
.sellbox{grid-column:1/-1;margin-top:8px}
.sellbox>summary{display:inline-block;font-size:11.5px;color:var(--ink-soft);
  background:#FFF;border:1px solid var(--rule);border-radius:2px;
  padding:3px 12px;cursor:pointer;list-style:none}
.sellbox>summary::-webkit-details-marker{display:none}
.sellbox>summary:hover{background:var(--brass);color:#FFF;
  border-color:var(--brass)}
.sellpanel{margin-top:10px;padding:14px;background:var(--paper-2);
  border-left:2px solid var(--brass)}
.sellpanel .fields{grid-template-columns:repeat(auto-fit,minmax(96px,1fr));
  gap:9px}
.sellpanel label{font-size:11px}
.sellpanel input{padding:8px 9px;font-size:14px}
.sellpanel .row-actions{display:flex;gap:9px;align-items:center;
  margin-top:11px;flex-wrap:wrap}
.sellpanel button{margin:0;padding:8px 18px;font-size:13.5px}
.sellpanel .cancel-link{display:inline-block;margin:0;padding:8px 16px;
  border:1px solid var(--line);border-radius:6px;color:var(--ink-faint);
  background:var(--paper);font-size:13.5px;text-decoration:none}
.sellpanel .cancel-link:hover{color:var(--ink);border-color:var(--brass)}
.sell-hint{font-size:11px;color:var(--ink-faint);margin-top:9px;line-height:1.65}
.lot-actions{display:flex;gap:8px;margin-top:7px;flex-wrap:wrap}

/* ── 載入畫面 ──
   紙本月報的調性不適合轉圈圈或跳動的小動畫，所以用一條黃銅色進度條
   ＋會淡入淡出的投資語錄。等待時有東西可讀，比盯著空白畫面好。 */
.loading{padding:26px 0 10px}
.load-track{height:3px;background:var(--paper-2);overflow:hidden;
  border-radius:2px}
.load-bar{height:100%;width:0;background:var(--brass);
  transition:width .5s ease}
.load-stage{margin-top:11px;font-size:12.5px;color:var(--ink-faint);
  display:flex;justify-content:space-between;gap:12px}
.load-stage b{color:var(--ink-soft);font-weight:500}
.quote{margin-top:34px;padding:22px 24px;background:var(--paper-2);
  border-left:2px solid var(--brass);min-height:150px;
  position:relative;overflow:hidden}
/* 每則語錄疊在同一個位置，靠 opacity 輪流顯示。
   都用絕對定位才不會互相把版面推開；容器已有 min-height 撐住高度。 */
.quote-item{position:absolute;top:22px;left:24px;right:24px;opacity:0}
.quote-text{font-size:16px;line-height:1.85;color:var(--ink);
  letter-spacing:.01em}
.quote-en{font-size:13px;line-height:1.7;color:var(--ink-soft);
  margin-top:9px;font-style:italic}
.quote-by{font-size:12px;color:var(--ink-faint);margin-top:13px;
  letter-spacing:.04em}
.load-note{margin-top:20px;font-size:11.5px;color:var(--ink-faint);
  line-height:1.7}
/* ── App shell：手機優先的固定導覽與安全區 ── */
html{background:#F2F3F0;touch-action:manipulation}
body{background:#F2F3F0;overflow-x:hidden;padding-bottom:calc(76px + env(safe-area-inset-bottom));-webkit-tap-highlight-color:transparent}
a,button,input,select{touch-action:manipulation}
a,button{transition:transform .12s ease,opacity .12s ease,box-shadow .12s ease,background-color .12s ease}
a:active,button:active{transform:scale(.98);opacity:.78}.tap-loading{opacity:.72;cursor:wait}
.tap-pulse{animation:tap-pulse .22s ease-out}
.feedback-success{border-color:#BBD8C2!important;background:#F3FAF4!important;animation:feedback-in .22s ease-out}
.feedback-error{border-color:#E5B9B3!important;background:#FFF5F3!important;animation:feedback-in .22s ease-out}
@keyframes tap-pulse{0%{box-shadow:0 0 0 0 rgba(139,105,52,.28)}100%{box-shadow:0 0 0 7px rgba(139,105,52,0)}}
@keyframes feedback-in{0%{opacity:.45;transform:translateY(2px)}100%{opacity:1;transform:translateY(0)}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important}.tap-pulse,.feedback-success,.feedback-error{animation:none!important}.quote-item:first-child{opacity:1!important;animation:none!important}}
button[disabled],input[disabled],select[disabled]{opacity:.58;cursor:wait}
.wrap{max-width:760px;padding:94px 16px calc(104px + env(safe-area-inset-bottom))}
.app-header{position:fixed;top:0;left:0;right:0;width:min(760px,100%);margin:0 auto;z-index:50;
background:rgba(242,243,240,.98);backdrop-filter:blur(14px);padding:12px 16px 10px;border-bottom:1px solid rgba(185,189,180,.85);
box-shadow:0 3px 12px rgba(18,22,27,.07)}
@media(max-width:699px){.app-header{backdrop-filter:none;background:#F2F3F0}.app-bottom-nav{backdrop-filter:none;background:rgba(255,255,255,.98)}}
.app-header .eyebrow{margin-bottom:2px;font-size:10px;letter-spacing:.18em}
.app-header h1{font-size:21px;letter-spacing:.01em}
.app-header .dateline{font-size:11px;margin-top:2px}
.top-nav{display:none;gap:6px;overflow-x:auto;white-space:nowrap;padding:10px 0;margin:0 -2px 4px;border:0;scrollbar-width:none}
.top-nav::-webkit-scrollbar{display:none}
.top-nav a{padding:7px 11px;border-radius:999px;background:#E3E5DF;color:var(--ink-soft);font-size:12.5px;text-decoration:none}
.top-nav a.on{background:var(--ink);color:#FFF;border:0;padding-bottom:7px}
.app-bottom-nav{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;justify-content:center;background:rgba(255,255,255,.95);backdrop-filter:blur(16px);border-top:1px solid #D8DAD4;padding:8px 8px calc(8px + env(safe-area-inset-bottom));box-shadow:0 -5px 18px rgba(18,22,27,.08)}
.app-bottom-nav .bottom-inner{width:min(760px,100%);display:grid;grid-template-columns:repeat(5,1fr);gap:3px}
.app-bottom-nav a{display:flex;flex-direction:column;align-items:center;gap:2px;color:var(--ink-faint);font-size:11px;text-decoration:none;padding:3px 0;border-radius:10px}
.app-bottom-nav a b{font-size:17px;font-weight:500;line-height:1}
.app-bottom-nav a.on{color:var(--brass);background:#F0EEE8;font-weight:600}
.daily-card{border-radius:18px;box-shadow:0 4px 18px rgba(18,22,27,.055)}
.more-hero{padding:18px 2px 12px}.more-hero .eyebrow{font-size:10px;letter-spacing:.18em;color:var(--brass)}.more-hero h1{margin:6px 0 4px;font-size:30px}.more-hero p{margin:0;color:var(--ink-soft);font-size:13px}
.more-group{background:#FFF;border:1px solid #E1E3DE;border-radius:18px;padding:6px 14px;margin:12px 0;box-shadow:0 4px 18px rgba(18,22,27,.045)}
.more-group-title{padding:10px 2px 7px;font-size:12px;color:var(--brass);font-weight:600;letter-spacing:.08em}.more-item{display:flex;align-items:center;gap:11px;padding:13px 2px;border-top:1px solid #ECEDE8;color:var(--ink);text-decoration:none}.more-item:first-of-type{border-top:0}.more-item .more-icon{width:28px;height:28px;display:grid;place-items:center;border-radius:9px;background:#F1F0EA;color:var(--brass);font-size:16px;flex:0 0 28px}.more-item span:nth-child(2){flex:1;min-width:0}.more-item b,.more-item small{display:block}.more-item b{font-size:14px}.more-item small{margin-top:3px;color:var(--ink-soft);font-size:11.5px}.more-item strong{font-size:22px;color:var(--ink-faint);font-weight:400}.more-note{margin:18px 4px;color:var(--ink-faint);font-size:11.5px;line-height:1.7}
@media(min-width:700px){.top-nav{display:flex}}
.rank-spotlight{background:#FFF;border:1px solid #E1E3DE;border-radius:18px;padding:18px;margin:14px 0;box-shadow:0 4px 18px rgba(18,22,27,.055)}
.rank-spotlight .daily-section-title span{font-size:12px;color:var(--ink-faint)}
.my-rank-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.my-rank-card{background:#F5F5F1;border-radius:12px;padding:14px;min-height:112px}
.my-rank-card small,.my-rank-card span{display:block;color:var(--ink-soft);font-size:12px}
.my-rank-card b{display:block;font-size:22px;margin:8px 0 4px}
.rank-move{font-size:13px;font-weight:600;white-space:nowrap}.rank-move.muted{color:var(--ink-faint);font-weight:400}.rank-move.up{color:var(--up)}.rank-move.down{color:var(--down)}
@media(max-width:640px){.my-rank-grid{grid-template-columns:1fr}}
@media(min-width:700px){body{padding-bottom:0}.app-bottom-nav{display:none}.wrap{padding-bottom:56px}}
"""

NEED_LOGIN_HTML = """
<div class="msg">
  這個網頁登入狀態已失效。從 LINE 開啟時，請回到 LINE 輸入「網頁」重新取得連結；<b>不需要設定帳號密碼</b>。
</div>
<div class="section-head"><h2>用登入碼登入</h2>
  <span class="section-note">任何瀏覽器都可以</span></div>
<form method="post" action="/web/code" class="add">
  <h3>輸入 6 位數登入碼</h3>
  <div class="fields">
    <div><label>登入碼</label>
      <input name="code" inputmode="numeric" autocomplete="one-time-code"
             maxlength="6" placeholder="000000" required
             style="font-size:22px;letter-spacing:.3em;text-align:center"></div>
  </div>
  <button type="submit">登入</button>
  <div class="sell-hint">
    回到 LINE 的「台股 BOT」，輸入 <b>網頁</b>，訊息裡就有登入碼；
    只想重拿一組的話輸入 <b>登入碼</b>。有效 30 分鐘。<br>
    在 LINE 裡開網頁若顯示不正常，用這個方式就能在 Safari、Chrome
    等外部瀏覽器登入。
  </div>
</form>
"""


# ── 濫用防護 ──
# 開放給不特定人使用後，沒有任何限制的話，一個人寫腳本狂打「黑馬」
# 就能把 Render 的運算額度與 Yahoo 的請求配額吃光，其他人全部受影響。
#
# 用記憶體計數而非資料庫：這只是擋住明顯的濫用，不需要跨重啟精準持久化，
# 而且每次請求都去查一次資料庫本身就是額外負擔。
# Render 重啟後計數歸零是可接受的——重啟不是常態。
_rate_buckets = {}
RATE_LIMITS = {
    # 動作類型: (時間窗秒數, 該窗內最多幾次)
    "heavy": (60, 6),     # 黑馬、雷達、選股台這類要掃上百檔的
    "normal": (60, 20),   # 一般查詢
}


def rate_limit_ok(key, kind="normal"):
    """
    回傳是否放行。超過就擋下，並回傳還要等幾秒。
    回傳 (是否放行, 還需等待秒數)
    """
    window, limit = RATE_LIMITS.get(kind, RATE_LIMITS["normal"])
    now = time.time()
    bucket = _rate_buckets.setdefault((key, kind), [])

    # 清掉時間窗外的紀錄。順便控制記憶體：只留窗內的時間戳
    bucket[:] = [t for t in bucket if now - t < window]
    if len(bucket) >= limit:
        return False, int(window - (now - bucket[0])) + 1
    bucket.append(now)

    # 定期清掉完全沒有活動的 key，避免長期累積
    if len(_rate_buckets) > 5000:
        for k in [k for k, v in _rate_buckets.items() if not v]:
            _rate_buckets.pop(k, None)
    return True, 0


HEAVY_COMMANDS = {"黑馬", "雷達", "籌碼", "籌碼超人", "認養", "盤前", "早安"}


def render_page(title, body, nav_active=None, user_name=None):
    """所有網頁共用的外框。用字串組裝就好，這個規模不需要模板引擎。"""
    # 盤前是獨立頁面，不能因為底部沒有專屬分頁就誤亮「今日」。
    active_nav = nav_active
    def tab(href, label, key):
        on = " class=\"on\"" if key == active_nav else ""
        return f'<a href="{href}"{on}>{label}</a>'

    def bottom_tab(href, icon, label, key):
        on = " on" if key == active_nav else ""
        return f'<a href="{href}" class="{on.strip()}"><b>{icon}</b>{label}</a>'

    nav = ""
    if nav_active:
        nav = ("<nav class=\"top-nav\">"
               + tab("/web/portfolio", "今日", "portfolio")
               + tab("/web/leaderboard", "排行榜", "leaderboard")
               + tab("/web/positions", "持股", "positions")
               + tab("/web/trades", "紀錄", "trades")
               + tab("/web/screener", "選股", "screener")
               + tab("/web/compare", "比較", "compare")
               + tab("/web/settings", "設定", "settings")
               + "</nav>")

    more_on = active_nav in {"settings", "trades", "compare", "more"}
    bottom_nav = ("<div class=\"app-bottom-nav\"><div class=\"bottom-inner\">"
                  + bottom_tab("/web/portfolio", "⌂", "今日", "portfolio")
                  + bottom_tab("/web/positions", "▣", "持股", "positions")
                  + bottom_tab("/web/screener", "⌁", "選股", "screener")
                  + bottom_tab("/web/leaderboard", "≡", "排行", "leaderboard")
                  + f'<a href="/web/more" class="{"on" if more_on else ""}"><b>⋯</b>更多</a>'
                  + "</div></div>")

    # LINE WebView 偶爾不保留 cookie；導覽列也必須帶著有效 token，不能只有內容區帶。
    nav = preserve_web_token(nav)
    bottom_nav = preserve_web_token(bottom_nav)
    body = inject_csrf_inputs(body)
    body = preserve_web_token(body)
    page_back = ""
    if nav_active and nav_active != "portfolio":
        page_back = preserve_web_token(
            '<div class="page-back"><a href="/web/portfolio">‹ 回首頁</a></div>')
    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}｜台股 BOT</title>
<style>{BASE_CSS}</style>
</head><body><div class="wrap">
<header class="app-header">
  {page_back}
  <div class="eyebrow">TAIWAN STOCK BOT</div>
  <h1>{title}</h1>
  <div class="dateline">{taiwan_now().strftime('%Y / %m / %d')}
    {'　' + user_name if user_name else ''}</div>
</header>
{nav}
{body}
<script>
(function() {{
  // LINE WebView 可能清掉 cookie；把有效網址 token 保存到同一個網域，
  // 只有真正進入登入失效頁時才自動恢復，不干擾一般頁面。
  try {{
    var query = new URLSearchParams(window.location.search);
    var incomingToken = query.get('t');
    if (incomingToken) localStorage.setItem('stockbot_web_token', incomingToken);
    var loginText = (document.title + ' ' + document.body.innerText).slice(0, 600);
    var isLoginPage = /需要登入|登入狀態已失效/.test(loginText);
    var savedToken = localStorage.getItem('stockbot_web_token');
    var alreadyRecovered = query.get('auth_recover') === '1';
    if (savedToken && isLoginPage && !alreadyRecovered && window.location.pathname !== '/web/code') {{
      window.location.replace(window.location.pathname + '?t=' + encodeURIComponent(savedToken) + '&auth_recover=1');
      return;
    }}
    if (isLoginPage && alreadyRecovered) localStorage.removeItem('stockbot_web_token');
  }} catch (authError) {{
    // localStorage 在部分私密 WebView 可能被禁止，仍由 cookie／網址 token 工作。
  }}

  function prefersReducedMotion() {{
    try {{ return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches; }}
    catch (ignore) {{ return false; }}
  }}

  // iOS／部分 LINE WebView 不提供 vibrate；不支援時只保留視覺回饋，不報錯。
  function haptic(kind) {{
    if (prefersReducedMotion() || !navigator.vibrate) return;
    try {{
      var pattern = kind === 'success' ? [8, 35, 8]
                  : kind === 'error' ? [16, 35, 16]
                  : kind === 'medium' ? 14 : 7;
      navigator.vibrate(pattern);
    }} catch (ignore) {{}}
  }}

  function pulse(target) {{
    if (!target || prefersReducedMotion()) return;
    target.classList.remove('tap-pulse');
    void target.offsetWidth;
    target.classList.add('tap-pulse');
    window.setTimeout(function() {{ target.classList.remove('tap-pulse'); }}, 260);
  }}

  function isFeedbackTarget(target) {{
    return target && (target.matches('button,[data-haptic]') ||
      target.matches('.daily-focus a,.daily-card a,.app-bottom-nav a'));
  }}

  // 點擊後立即給回饋；不攔截錨點、下載與外部連結。
  document.addEventListener('click', function(e) {{
    var target = e.target.closest ? e.target.closest('a,button,[data-haptic]') : null;
    if (!target || target.dataset.haptic === 'none') return;
    if (isFeedbackTarget(target)) {{
      pulse(target);
      haptic(target.dataset.haptic || 'soft');
    }}
    var a = target.matches('a') ? target : (target.closest ? target.closest('a') : null);
    if (!a) return;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#' || a.target === '_blank' || a.dataset.noBusy === '1') return;
    a.classList.add('tap-loading');
  }}, true);

  // POST 表單只允許送出一次，避免新增／賣出／設定被重複點擊。
  document.addEventListener('submit', function(e) {{
    var form = e.target;
    if (!form || form.dataset.submitted === '1') {{
      e.preventDefault();
      return;
    }}
    form.dataset.submitted = '1';
    form.classList.add('tap-loading');
    haptic('medium');
    var button = form.querySelector('button[type="submit"],input[type="submit"]');
    if (button) {{
      button.dataset.oldText = button.textContent || button.value || '';
      if (button.tagName === 'INPUT') button.value = '處理中…';
      else button.textContent = '處理中…';
      button.disabled = true;
    }}
  }}, true);

  // 新頁面載入後，讓成功／失敗訊息有一致的顏色、短動畫與觸覺回饋。
  document.querySelectorAll('.msg,.callout').forEach(function(box) {{
    var text = box.textContent || '';
    if (/成功|完成|已儲存|已新增|已刪除/.test(text)) {{
      box.classList.add('feedback-success'); haptic('success');
    }} else if (/失敗|錯誤|無法|過期/.test(text)) {{
      box.classList.add('feedback-error'); haptic('error');
    }}
  }});
}})();
</script>
<footer>
以上為你輸入之持股的數據整理，不構成投資建議。<br>
你輸入的持股只用於產生你自己的分析，作者不會查看個別使用者的持股內容。<br>
資料來源：臺灣證券交易所、櫃買中心、Yahoo Finance。作者：蔡秉軒
</footer>
{bottom_nav}
</div></body></html>"""


def fmt_pct(v):
    if v is None:
        return '<span class="flat">—</span>'
    cls = "flat" if abs(v) < 0.005 else ("up" if v > 0 else "down")
    return f'<span class="num {cls}">{v:+.2f}%</span>'


# ── 載入時輪播的投資語錄 ──
# 等待時給點東西讀，比盯著空白或骨架灰塊有意思。
# 挑的都是講「紀律、耐心、風險」的短句——跟這個工具想傳達的態度一致，
# 不放那種鼓吹重壓或保證獲利的句子。
INVESTING_QUOTES = [
    # 耐心與時間
    ("股市是把錢從沒耐心的人手上，轉移到有耐心的人手上的裝置。", "", "華倫・巴菲特"),
    ("時間是好公司的朋友，是平庸公司的敵人。", "", "華倫・巴菲特"),
    ("大錢不是靠買賣賺來的，是靠等待。", "", "傑西・李佛摩"),
    ("我最喜歡的持有期限是永遠。",
     "Our favorite holding period is forever.", "華倫・巴菲特"),
    ("有人今天能乘涼，是因為很久以前有人種了樹。", "", "華倫・巴菲特"),
    ("複利是世界第八大奇蹟。", "", "常被歸於愛因斯坦"),
    ("你不能為了讓孩子早點出生，就找九個女人懷孕一個月。", "", "華倫・巴菲特"),
    ("賺大錢的關鍵不在於思考，而在於坐得住。", "", "查理・蒙格"),
    ("在股市裡，時間是你的朋友，衝動是你的敵人。", "", "約翰・柏格"),
    ("投資應該像看油漆變乾或看草生長一樣無聊。",
     "Investing should be more like watching paint dry.", "保羅・薩繆森"),

    # 風險與虧損
    ("風險來自於你不知道自己在做什麼。",
     "Risk comes from not knowing what you're doing.", "華倫・巴菲特"),
    ("第一條規則是永遠不要賠錢，第二條是永遠別忘記第一條。", "", "華倫・巴菲特"),
    ("退潮時，才知道誰在裸泳。", "", "華倫・巴菲特"),
    ("我們無法預測，但可以做好準備。",
     "You can't predict. You can prepare.", "霍華・馬克斯"),
    ("在投資裡，讓你舒服的事情，很少讓你賺錢。", "", "霍華・馬克斯"),
    ("風險不是波動，而是永久損失資本的可能。", "", "霍華・馬克斯"),
    ("承擔風險沒問題，但別讓單一風險把你踢出局。", "", "彼得・伯恩斯坦"),
    ("投資最危險的四個字：這次不一樣。",
     "The four most expensive words: this time it's different.", "約翰・坦伯頓"),
    ("你要活得夠久，才能等到複利發揮作用。", "", "摩根・豪瑟"),
    ("生存是通往成功的必要條件。", "", "納西姆・塔雷伯"),
    ("永遠不要拿你有的、你需要的，去賭你沒有的、你不需要的。", "", "華倫・巴菲特"),

    # 人性與紀律
    ("別人貪婪時恐懼，別人恐懼時貪婪。", "", "華倫・巴菲特"),
    ("投資人最大的敵人，往往是他自己。",
     "The investor's chief problem is likely himself.", "班傑明・葛拉漢"),
    ("投資成功不需要高智商，需要的是控制住會讓人出事的衝動。", "", "華倫・巴菲特"),
    ("行情總在絕望中誕生，在半信半疑中成長，在憧憬中成熟，在充滿希望中毀滅。",
     "", "約翰・坦伯頓"),
    ("賠錢的真正原因，是等不及而在最壞的時候賣出。", "", "彼得・林區"),
    ("如果你無法忍受股價腰斬，就不該投資股票。", "", "彼得・林區"),
    ("每個人都有腦力賺股市的錢，但不是每個人都有那個胃。", "", "彼得・林區"),
    ("市場能維持非理性的時間，比你能維持不破產的時間更久。", "", "常被歸於凱因斯"),
    ("行情在悲觀中誕生，在懷疑中成長。", "", "約翰・坦伯頓"),
    ("投資是門把情緒排除在決策之外的功夫。", "", "班傑明・葛拉漢"),
    ("最重要的不是你多聰明，而是你能不能不做蠢事。", "", "查理・蒙格"),
    ("我這輩子的成功，很大程度來自於不做傻事，而不是特別聰明。", "", "查理・蒙格"),
    ("認識自己的能力圈，然後待在裡面。", "", "華倫・巴菲特"),

    # 價值與估值
    ("市場短期是投票機，長期是體重計。", "", "班傑明・葛拉漢"),
    ("價格是你付出的，價值是你得到的。",
     "Price is what you pay. Value is what you get.", "華倫・巴菲特"),
    ("用普通的價格買一家好公司，遠勝過用好價格買一家普通公司。", "", "華倫・巴菲特"),
    ("投資操作是經過分析、能保障本金並取得適當報酬的行為；不符合的就是投機。",
     "", "班傑明・葛拉漢"),
    ("好標的、好買點、好賣點，三者缺一不可。", "", "霍華・馬克斯"),
    ("買得便宜不是為了買得便宜，是為了留出犯錯的空間。", "", "賽斯・卡拉曼"),
    ("安全邊際是投資的核心概念。", "", "班傑明・葛拉漢"),
    ("再好的公司，出價太高也會變成糟糕的投資。", "", "霍華・馬克斯"),

    # 研究與功課
    ("懂你手上持有的是什麼，也懂你為什麼持有它。",
     "Know what you own, and know why you own it.", "彼得・林區"),
    ("在你買進之前，先能用兩分鐘說清楚買它的理由。", "", "彼得・林區"),
    ("投資你懂的東西，而不是你聽說的東西。", "", "彼得・林區"),
    ("你不必在每一件事上都正確，只要在少數幾件事上不犯大錯。", "", "彼得・林區"),
    ("讀年報，讀你要投資那家公司的年報，也讀它競爭對手的。", "", "華倫・巴菲特"),
    ("我這輩子沒見過不讀書的聰明人，一個都沒有。", "", "查理・蒙格"),
    ("預測未來最好的方式，是理解現在。", "", "彼得・杜拉克"),

    # 分散與配置
    ("分散是對無知的保護；若你清楚自己在做什麼，它就沒什麼必要。", "", "華倫・巴菲特"),
    ("別把所有雞蛋放在同一個籃子裡——除非你能看好那個籃子。", "", "安德魯・卡內基"),
    ("資產配置決定了你長期報酬的絕大部分。", "", "蓋瑞・布林森"),
    ("不要在乾草堆裡找針，直接買下整個乾草堆。", "", "約翰・柏格"),
    ("成本很重要，你付出去的每一分，都是你拿不回來的報酬。", "", "約翰・柏格"),

    # 態度與長期
    ("在別人絕望時買進，在別人樂觀時賣出，需要極大的意志力，回報也最豐厚。",
     "", "約翰・坦伯頓"),
    ("投資是少數幾件你越少動作、結果越好的事。", "", "摩根・豪瑟"),
    ("財富是你沒有花掉的那些錢。", "", "摩根・豪瑟"),
    ("控制你能控制的：成本、行為、時間長度。", "", "約翰・柏格"),
    ("計畫最重要的部分，是為計畫趕不上變化預留空間。", "", "摩根・豪瑟"),
    ("市場不會因為你需要錢，就在那天對你友善。", "", "霍華・馬克斯"),
    ("停損不是承認失敗，是承認你不知道接下來會怎樣。", "", "傑西・李佛摩"),
    ("一個投資人真正需要的，是在別人失去理智時保持理智。", "", "班傑明・葛拉漢"),
    # 認錯與修正
    ("虧損自己會照顧自己，獲利卻不會——會跑掉的是獲利。", "", "投資諺語"),
    ("承認錯誤不會讓你變窮，堅持錯誤才會。", "", "投資諺語"),
    ("當事實改變，我就改變想法。你呢？", "", "常被歸於凱因斯"),
    ("最貴的不是買錯，是買錯之後不肯認。", "", "投資諺語"),
    ("停損是成本，不是失敗；不停損才是失敗。", "", "投資諺語"),
    ("好的投資人常常改變主意，因為他們追求的是正確而不是面子。", "", "投資諺語"),
    ("在市場裡，堅持己見的代價由你自己付。", "", "投資諺語"),

    # 資訊與雜訊
    ("每天的股價新聞，九成是雜訊，一成是資訊，難的是分辨。", "", "投資諺語"),
    ("你看到的消息，價格通常已經反映過了。", "", "效率市場假說"),
    ("愈是斬釘截鐵的預測，愈值得懷疑。", "", "霍華・馬克斯"),
    ("知道自己不知道什麼，比知道什麼更重要。", "", "投資諺語"),
    ("預測市場走向的人分兩種：不知道的，和不知道自己不知道的。", "", "約翰・高伯瑞"),
    ("消息面決定短期價格，基本面決定長期價值。", "", "投資諺語"),
    ("如果一個投資機會需要你立刻決定，那多半不是機會。", "", "投資諺語"),

    # 部位與資金管理
    ("決定你能撐多久的不是眼光，是部位大小。", "", "投資諺語"),
    ("先想你能承受多少，再想你想賺多少。", "", "投資諺語"),
    ("重壓一次對的，不如穩定做對很多次。", "", "投資諺語"),
    ("留一點現金，不是為了報酬，是為了選擇權。", "", "投資諺語"),
    ("借來的錢會改變你的判斷，因為時間不再站在你這邊。", "", "投資諺語"),
    ("永遠不要因為一筆交易而讓自己失去繼續交易的資格。", "", "保羅・都鐸・瓊斯"),
    ("最重要的規則是守住本金，第二重要的還是守住本金。", "", "保羅・都鐸・瓊斯"),

    # 心態與情緒
    ("市場最會做的事，就是讓最多人不舒服。", "", "投資諺語"),
    ("恐懼與貪婪之間，只隔著一份紀律。", "", "投資諺語"),
    ("賺錢時的自信，通常來自運氣而非能力。", "", "投資諺語"),
    ("你不需要對每一檔股票有意見。", "", "華倫・巴菲特"),
    ("最好的投資決定，常常是什麼都不做。", "", "投資諺語"),
    ("急著回本的心情，是虧更多的開始。", "", "投資諺語"),
    ("別人賺錢跟你沒有關係，這句話很難，但很值錢。", "", "投資諺語"),
    ("投資是一場跟自己的比賽，不是跟別人的。", "", "投資諺語"),
    ("看盤看得越勤，越容易做出你事後會後悔的決定。", "", "投資諺語"),

    # 選股與產業
    ("買股票就是買公司的一部分，不是買一張會跳動的紙。", "", "班傑明・葛拉漢"),
    ("再厲害的騎師，騎一匹爛馬也贏不了。", "", "華倫・巴菲特"),
    ("要投資一家連傻瓜都能經營的公司，因為總有一天會有傻瓜來經營。", "", "彼得・林區"),
    ("護城河比一時的成長率更值錢。", "", "華倫・巴菲特"),
    ("景氣循環股最危險的時候，正是本益比看起來最低的時候。", "", "彼得・林區"),
    ("成長本身沒有價值，除非它需要的投入小於它產生的現金。", "", "華倫・巴菲特"),
    ("熱門產業裡的平庸公司，比冷門產業裡的優秀公司更危險。", "", "投資諺語"),
    ("公司的產品你若說不出它賣給誰、解決什麼問題，就別買。", "", "投資諺語"),

    # 週期與時間
    ("樹不會長到天上去。", "", "投資諺語"),
    ("循環永遠存在，只是每次的理由聽起來都很新。", "", "霍華・馬克斯"),
    ("最好的買點通常出現在最悲觀的時候，那也是最難下手的時候。", "", "投資諺語"),
    ("多頭市場裡，每個人都覺得自己是股神。", "", "投資諺語"),
    ("牛市在悲觀中誕生，在懷疑中成長，在樂觀中成熟，在興奮中死亡。", "", "約翰・坦伯頓"),
    ("下跌時記住上漲的日子，上漲時記住下跌的日子。", "", "投資諺語"),
    ("市場沒有新鮮事，只有你沒讀過的歷史。", "", "傑西・李佛摩"),

    # 成本與費用
    ("你控制不了報酬，但你控制得了成本。", "", "約翰・柏格"),
    ("在投資的世界，你付出的越多，得到的越少。", "", "約翰・柏格"),
    ("頻繁交易最穩定的受益者是券商。", "", "投資諺語"),
    ("稅與手續費是確定的損失，報酬是不確定的收益。", "", "投資諺語"),

    # 紀律與方法
    ("沒有寫下來的計畫，在盤中就不算計畫。", "", "投資諺語"),
    ("先想清楚什麼情況下你會賣，再決定買不買。", "", "投資諺語"),
    ("一套普通但你能執行的方法，勝過一套完美但你做不到的。", "", "投資諺語"),
    ("結果好不代表決策對，決策對不代表結果好。", "", "安妮・杜克"),
    ("在不確定的世界裡，過程比單次結果更值得檢討。", "", "投資諺語"),
    ("紀律的價值，在你最不想遵守的那天才會顯現。", "", "投資諺語"),
    ("如果你說不出自己為什麼賺錢，那你也守不住它。", "", "投資諺語"),
]


def render_quote_block():
    """
    隨機挑幾則語錄輪播。

    keyframes 必須依語錄則數動態產生：固定一組 keyframes 是行不通的，
    因為「每則該亮多久」取決於總共有幾則——四則輪播時每則只能亮四分之一
    的循環時間，若沿用單則的節奏（幾乎整個循環都亮著），四則就會同時
    出現而疊在一起。

    每則都用絕對定位疊在同一個位置，靠 opacity 決定誰可見；
    容器給固定高度，才不會在切換時把下面的內容推上推下。
    """
    picked = random.sample(INVESTING_QUOTES, min(4, len(INVESTING_QUOTES)))
    n = len(picked)
    per = 9          # 每則顯示秒數
    total = n * per  # 一輪的總長度
    share = 100.0 / n  # 每則佔整個循環的百分比

    # 淡入淡出各佔該則時段的 8%，中間是完全不透明
    fade = share * 0.08
    keyframes = []
    for i in range(n):
        start = i * share
        initial_opacity = 1 if i == 0 else 0
        keyframes.append(f"""
@keyframes q{i} {{
  0%{{opacity:{initial_opacity}}}
  {start:.2f}%{{opacity:{initial_opacity}}}
  {start + fade:.2f}%{{opacity:1}}
  {start + share - fade:.2f}%{{opacity:1}}
  {start + share:.2f}%{{opacity:0}}
  100%{{opacity:0}}
}}""")

    blocks = []
    for i, (zh, en, who) in enumerate(picked):
        blocks.append(
            f'<div class="quote-item" style="animation:q{i} {total}s linear infinite">'
            f'<div class="quote-text">{zh}</div>'
            f'{f"<div class=quote-en>{en}</div>" if en else ""}'
            f'<div class="quote-by">— {who}</div>'
            f'</div>')

    return (f'<style>{"".join(keyframes)}</style>'
            f'<div class="quote">{"".join(blocks)}</div>')


def render_loading_shell(title, nav_active, stages, note="", staged=False):
    """
    先秒回的「殼」：導覽列、進度條、投資語錄都立刻出現，
    真正的內容再由瀏覽器另外去要（fragment=1），回來後替換掉這一塊。

    為什麼不直接讓伺服器算完再回：那段時間瀏覽器是完全空白的，
    使用者不知道是在跑還是壞了，20 秒的空白比 20 秒的進度條難熬得多。

    進度條走的是「預估」而非真實進度——真實進度要後端持續回報，
    對這個規模的專案不划算，而且體感差異很小。重點是讓人知道還在跑。
    stages 是階段文字清單，會依序顯示。
    """
    stages_js = ",".join(f'"{s}"' for s in stages)
    staged_literal = "true" if staged else "false"
    fast_suffix = "\n          + '&fast=1'" if staged else ""
    detail_status_html = (
        '<div id="detail-status" class="load-note" '
        'style="display:none;margin:8px 0 0">正在補上即時持股分析…</div>'
        if staged else "")
    shell = f"""
<div id="loading" class="loading">
  <div class="load-track"><div class="load-bar" id="loadbar"></div></div>
  <div class="load-stage">
    <span id="loadstage">正在準備…</span>
    <b id="loadpct">0%</b>
  </div>
  {render_quote_block()}
  <div class="load-note">{note}</div>
</div>
<div id="content"></div>
{detail_status_html}
<script>
(function () {{
  var stages = [{stages_js}];
  var bar = document.getElementById('loadbar');
  var stageEl = document.getElementById('loadstage');
  var pctEl = document.getElementById('loadpct');
  var pct = 0, done = false, elapsed = 0;

  // 進度條永遠不會真的停住：越接近尾端爬得越慢，但仍持續前進。
  // 完全卡在 90% 看起來就像當掉了——使用者分不出「還在跑」和「壞了」，
  // 那比慢本身更讓人想直接關掉頁面。
  var timer = setInterval(function () {{
    if (done) return;
    elapsed += 0.26;
    var step = pct < 50 ? 2.2 : (pct < 75 ? 0.8 : (pct < 90 ? 0.28 : 0.05));
    pct = Math.min(99, pct + step);   // 漸進趨近 99，永遠到不了但一直在動
    bar.style.width = pct + '%';
    pctEl.textContent = Math.round(pct) + '%';

    var i = Math.min(stages.length - 1, Math.floor(pct / (92 / stages.length)));
    var label = stages[i];
    // 超過預期時間就換句話講，別讓同一行字乾瞪眼
    if (elapsed > 45) {{
      label = '資料量較大，仍在處理中…';
    }} else if (elapsed > 25) {{
      label = stages[stages.length - 1] + '（快好了）';
    }}
    stageEl.textContent = label;
  }}, 260);

  function finish(html) {{
    done = true;
    clearInterval(timer);
    bar.style.width = '100%';
    pctEl.textContent = '100%';
    setTimeout(function () {{
      var content = document.getElementById('content');
      content.innerHTML = html;
      document.getElementById('loading').style.display = 'none';
      if ({staged_literal}) {{
        var status = document.getElementById('detail-status');
        if (status) status.style.display = 'block';
        var detailUrl = window.location.pathname + window.location.search
                      + (window.location.search ? '&' : '?') + 'fragment=1&detail=1';
        fetch(detailUrl, {{ credentials: 'same-origin' }})
          .then(function (r) {{
            if (r.status === 401) throw new Error('登入狀態已失效');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.text();
          }})
          .then(function (detailHtml) {{
            content.innerHTML = detailHtml;
            if (status) status.style.display = 'none';
          }})
          .catch(function (e) {{
            // 首屏快照已經可用；深度分析失敗只提示，不把首屏清空。
            if (status) status.textContent = '完整持股分析暫時載入失敗，請稍後重新整理。';
            console.error(e);
          }});
      }}
    }}, 70);
  }}

  var url = window.location.pathname + window.location.search
          + (window.location.search ? '&' : '?') + 'fragment=1'{fast_suffix};

  fetch(url, {{ credentials: 'same-origin' }})
    .then(function (r) {{
      if (r.status === 401) {{
        // Cookie 可能被 LINE WebView 清掉。先用同網域保存的 token
        // 嘗試恢復一次；若仍失效，才回到登入說明，不把完整登入頁
        // 塞進目前頁面的 content。
        var q = new URLSearchParams(window.location.search);
        var saved = null;
        try {{ saved = localStorage.getItem('stockbot_web_token'); }} catch (_) {{}}
        if (saved && q.get('auth_recover') !== '1'
            && window.location.pathname !== '/web/code') {{
          window.location.replace(window.location.pathname + '?t='
            + encodeURIComponent(saved) + '&auth_recover=1');
        }} else {{
          window.location.replace('/web/login');
        }}
        return null;
      }}
      if (!r.ok) {{
        var err = new Error('HTTP ' + r.status);
        err.status = r.status;
        throw err;
      }}
      return r.text();
    }})
    .then(function (html) {{
      if (html !== null) finish(html);
    }})
    .catch(function (e) {{
      done = true;
      clearInterval(timer);
      // 把錯誤內容顯示出來。只寫「載入失敗」的話，
      // 伺服器端到底是 500 還是網路斷線完全看不出來，
      // 每次都得去翻 Render Logs 才知道發生什麼事。
      stageEl.textContent = '載入失敗：' + (e && e.message ? e.message : e);
      pctEl.textContent = '';
      var hint = document.createElement('div');
      hint.className = 'sub';
      hint.style.marginTop = '8px';
      hint.textContent = 'HTTP 500 代表伺服器端出錯，請看 Render Logs；'
                       + '其他多半是網路問題，重新整理即可。';
      stageEl.parentNode.appendChild(hint);
      console.error(e);
    }});
}})();
</script>"""
    # 骨架也要走 render_page，才會帶上樣式與導覽列——
    # 沒有外框的話使用者第一眼看到的會是一段沒有樣式的裸 HTML。
    return render_page(title, shell, nav_active=nav_active)


def wants_fragment():
    """瀏覽器載入殼之後回頭要內容時會帶 fragment=1。"""
    return request.args.get("fragment") == "1"


def respond_page(title, body, nav_active):
    """
    內容算完後的回應：fragment 請求只回內容片段（給 JS 塞進頁面），
    否則回完整頁面——這樣即使 JS 被停用或有人直接開 fragment 網址，
    畫面仍然是完整可用的，不會變成一段沒有樣式的裸 HTML。
    """
    if wants_fragment():
        return preserve_web_token(inject_csrf_inputs(body))
    return render_page(title, body, nav_active=nav_active)


@app.route("/web/login")
def web_login():
    """帶 ?t=權杖進來；cookie 與網址 token 同時保留，適配 LINE WebView。"""
    token = request.args.get("t", "")
    uid = resolve_web_token(token)
    if not uid:
        return render_page("需要登入", NEED_LOGIN_HTML), 401
    safe_token = quote(token, safe="")
    resp = make_response(redirect(f"/web/portfolio?t={safe_token}"))
    resp.set_cookie("stockbot_token", token,
                    max_age=WEB_SESSION_DAYS * 86400,
                    path="/", httponly=True, samesite="None", secure=True)
    return resp


@app.route("/web/code", methods=["GET", "POST"])
def web_code_login():
    """
    用 6 位數登入碼登入，給外部瀏覽器使用。

    LINE 內建瀏覽器的 cookie 與 Safari／Chrome 不互通，所以「點連結登入」
    只在 LINE 裡有效，一換瀏覽器就變回未登入。改成讓使用者自己輸入短碼，
    任何裝置、任何瀏覽器都能登入，也不必再回 LINE 重拿一次連結。
    """
    if request.method == "GET":
        return render_page("登入", NEED_LOGIN_HTML)

    token, uid = redeem_web_code(request.form.get("code", ""))
    if not token:
        body = ('<div class="msg">登入碼不正確、已過期，或已經使用過了。'
                '請回 LINE 輸入「登入碼」取得新的一組。</div>' + NEED_LOGIN_HTML)
        return render_page("登入", body), 401

    safe_token = quote(token, safe="")
    resp = make_response(redirect(f"/web/portfolio?t={safe_token}"))
    resp.set_cookie("stockbot_token", token,
                    max_age=WEB_SESSION_DAYS * 86400,
                    path="/", httponly=True, samesite="None", secure=True)
    return resp


@app.route("/web")
@web_login_required
def web_home(uid):
    # 網頁預設入口固定進入「今日」；沒有持股時，今日頁仍會提供新增持股入口。
    token = request.args.get("t")
    suffix = f"?t={quote(token, safe='')}" if token else ""
    return redirect(f"/web/portfolio{suffix}")


@app.route("/web/premarket")
@web_login_required
def web_premarket(uid):
    """顯示最近可用盤前批次的完整資料；沒有快照時清楚呈現資料狀態。"""
    data = build_today_change_web_data(uid)
    snapshot = data.get("snapshot")
    state = data.get("state") or {}
    events = data.get("events") or []
    esc = html.escape

    category_labels = {
        "blackhorse": "黑馬", "radar": "雷達", "breakout": "突破／跌破",
        "institutional": "法人", "market": "大盤／美股", "news": "新聞",
        "watchlist": "自選股", "watchlist_position": "自選股位階"
    }

    def text(value, fallback="尚無資料"):
        return esc(str(value)) if value not in (None, "", []) else fallback

    def pct_text(value):
        if value is None:
            return "尚無資料"
        try:
            number = float(value)
            color = "#B52F2F" if number > 0 else ("#087A4B" if number < 0 else "#767D85")
            return f'<span style="color:{color};font-weight:700">{number:+.2f}%</span>'
        except (TypeError, ValueError):
            return text(value)

    rows = []
    for event in events:
        evidence = esc(json.dumps(event.get("evidence") or {}, ensure_ascii=False, indent=2, default=str))
        category = category_labels.get(event.get("category"), event.get("category") or "其他")
        rows.append(
            f"<tr><td><b>{text(event.get('severity'))}</b></td>"
            f"<td>{text(category)}</td><td>{text(event.get('title'))}</td>"
            f"<td>{text(event.get('detail'))}<details><summary>比較證據</summary>"
            f"<pre>{evidence}</pre></details></td></tr>"
        )
    if not rows:
        empty_events = ("目前尚未建立盤前事件資料。" if not snapshot else
                        (state.get("title") or "今日沒有符合條件的新事件。"))
        rows.append(f'<tr><td colspan="4">{text(empty_events)}</td></tr>')

    display_date = data.get("date")
    requested_date = data.get("requested_date")
    state_title = state.get("title") or ("盤前資料已載入" if snapshot else "盤前資料尚未建立")
    state_detail = state.get("detail") or ("以下內容來自最近可用的真實盤前快照。" if snapshot else
                                             "完成盤後資料更新與變化偵測後，這裡會顯示完整內容。")
    if data.get("is_weekend") and snapshot:
        state_detail = f"目前是週末，以下顯示最近可用的盤前批次；{state_detail}"
    status_color = "#6E5228" if snapshot else "#767D85"

    meta = (f'<div class="premarket-meta">盤前顯示日：<b>{text(display_date)}</b>'
            f'　資料來源日：<b>{text(snapshot.get("source_date") if snapshot else None)}</b>'
            f'　前一交易日：<b>{text(snapshot.get("previous_trade_date") if snapshot else None)}</b></div>')

    if snapshot:
        blackhorse = snapshot.get("blackhorse") or []
        blackhorse_items = []
        for item in blackhorse[:5]:
            score = item.get("score")
            score_text = f"分數 {score}" if score is not None else "分數尚無資料"
            extra = item.get("breakout") or ""
            blackhorse_items.append(
                f'<div class="premarket-row"><b>#{text(item.get("rank"))} {text(item.get("name"))} '
                f'<small>({text(item.get("code"))})</small></b><span>{esc(score_text)}'
                f'{"　" + esc(str(extra)) if extra else ""}</span></div>')
        radar = snapshot.get("radar") or []
        radar_items = []
        for item in radar[:8]:
            extra = item.get("breakout") or "雷達訊號"
            radar_items.append(
                f'<div class="premarket-row"><b>{text(item.get("name"))} '
                f'<small>({text(item.get("code"))})</small></b><span>{text(extra)}</span></div>')
        market_labels = {
            "taiex_pct": "台股大盤", "^DJI_pct": "道瓊", "^IXIC_pct": "那斯達克",
            "^GSPC_pct": "S&P 500", "^SOX_pct": "費城半導體"
        }
        market_items = []
        for key, label in market_labels.items():
            if key in (snapshot.get("market") or {}):
                market_items.append(f'<div class="premarket-metric"><span>{esc(label)}</span>'
                                    f'<b>{pct_text((snapshot.get("market") or {}).get(key))}</b></div>')
        news_items = []
        for item in (snapshot.get("news") or [])[:5]:
            title = text(item.get("title"))
            source = f' <small>（{text(item.get("source"), "") }）</small>' if item.get("source") else ""
            uri = _valid_news_uri(item.get("link"))
            if uri:
                news_items.append(f'<div class="premarket-news"><a href="{esc(uri, quote=True)}" target="_blank" rel="noopener">{title}</a>{source}</div>')
            else:
                news_items.append(f'<div class="premarket-news">{title}{source}</div>')
        inst_items = []
        for code, item in list((snapshot.get("institutional") or {}).items())[:8]:
            name = item.get("name") or stock_display_name(code, fallback=code)
            net = item.get("total_net_lots")
            streak = item.get("streak")
            suffix = []
            if net is not None:
                suffix.append(f"法人合計 {net:+,} 張")
            if streak is not None:
                suffix.append(f"連續 {streak} 日")
            inst_items.append(f'<div class="premarket-row"><b>{text(name)} '
                              f'<small>({text(code)})</small></b><span>{esc("　".join(suffix) or "已有法人資料")}</span></div>')

        def section(title, content, empty="尚無資料"):
            inner = content or f'<div class="premarket-empty">{esc(empty)}</div>'
            return f'<div class="premarket-section"><h3>{esc(title)}</h3>{inner}</div>'

        snapshot_sections = (
            section("🔥 黑馬前 5", "".join(blackhorse_items), "目前快照沒有黑馬資料") +
            section("🚨 雷達訊號", "".join(radar_items), "目前快照沒有雷達資料") +
            section("📈 大盤／美股", "".join(market_items), "目前快照沒有大盤資料") +
            section("📰 相關新聞", "".join(news_items), "目前快照沒有新聞資料") +
            section("🏦 法人資料", "".join(inst_items), "目前快照沒有法人資料")
        )
        raw_snapshot = ("<details class=\"premarket-raw\"><summary>查看原始快照資料</summary>"
                        f"<pre>{esc(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))}</pre></details>")
    else:
        snapshot_sections = (
            '<div class="premarket-empty-state"><b>目前沒有可顯示的盤前快照</b>'
            '<p>系統尚未找到這個顯示日對應的真實盤前資料，因此不顯示空白 JSON，也不自行推測市場訊號。</p>'
            '<p>請確認盤後資料更新與「盤前變化偵測」工作已完成；完成後重新整理本頁即可。</p></div>'
        )
        raw_snapshot = ""

    body = f"""
    <style>
      .premarket-meta {{ color:#767D85; font-size:.92rem; line-height:1.8; margin:.5rem 0 1rem; }}
      .premarket-status {{ border-left:4px solid {status_color}; background:#F7F5EF; padding:14px 16px; margin:14px 0 18px; border-radius:10px; }}
      .premarket-status b {{ color:{status_color}; font-size:1.05rem; }}
      .premarket-status p {{ margin:.35rem 0 0; color:#5B6066; }}
      .premarket-section {{ margin:14px 0; padding:13px 14px; background:#FAFAF7; border:1px solid #E8E6DF; border-radius:11px; }}
      .premarket-section h3 {{ margin:0 0 9px; color:#6E5228; font-size:1rem; }}
      .premarket-row, .premarket-metric {{ display:flex; justify-content:space-between; gap:14px; padding:8px 0; border-top:1px solid #ECEBE6; line-height:1.55; }}
      .premarket-row:first-of-type, .premarket-metric:first-of-type {{ border-top:0; }}
      .premarket-row span, .premarket-metric span {{ color:#626970; text-align:right; }}
      .premarket-row small {{ color:#8A8F94; font-weight:400; }}
      .premarket-news {{ padding:8px 0; border-top:1px solid #ECEBE6; line-height:1.6; }}
      .premarket-news:first-of-type {{ border-top:0; }}
      .premarket-news a {{ color:#4A5F7A; text-decoration:underline; }}
      .premarket-empty, .premarket-empty-state {{ color:#767D85; line-height:1.7; }}
      .premarket-empty-state {{ padding:18px; background:#F7F7F3; border-radius:11px; }}
      .premarket-empty-state b {{ color:#6E5228; font-size:1.05rem; }}
      .premarket-empty-state p {{ margin:.55rem 0 0; }}
      .premarket-raw {{ margin-top:14px; }}
    </style>
    <section class="card">
      <h1>🔥 今日值得注意</h1>
      {meta}
      <div class="premarket-status"><b>{text(state_title)}</b><p>{text(state_detail)}</p></div>
      <table><thead><tr><th>級別</th><th>類別</th><th>事件</th><th>詳細內容</th></tr></thead>
      <tbody>{''.join(rows)}</tbody></table>
    </section>
    <section class="card"><h2>完整盤前快照</h2>
      {snapshot_sections}
      {raw_snapshot}
    </section>
    """
    return render_page("盤前變化", body, nav_active="premarket")



def render_positions_fast_summary(uid):
    """持股頁首屏只讀資料庫，避免等待外部報價才顯示已有持股。"""
    positions = merge_positions(get_positions(uid))
    style = '''<style>
.position-fast-card{background:#fff;border:1px solid #e3e2dc;border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 3px 14px rgba(35,39,35,.05)}
.position-fast-card h2{margin:0 0 12px;font-size:20px}
.position-fast-row{display:flex;gap:10px;padding:11px 0;border-top:1px solid #eee}
.position-fast-row:first-of-type{border-top:0}
.position-fast-row b{display:block;font-size:15px}
.position-fast-row small,.position-fast-note{color:var(--ink-soft);font-size:12px}
.position-fast-empty{padding:10px 0;color:var(--ink-soft);line-height:1.6}
</style>'''
    if not positions:
        return style + '''<section class="position-fast-card">
  <h2>我的持股</h2>
  <div class="position-fast-empty"><b>目前還沒有持股紀錄</b><br>
    可以到持股頁新增股票；即時價格與損益會在有資料後顯示。</div>
</section>'''
    rows = []
    for p in positions:
        code = html.escape(str(p.get("code", "")))
        name = html.escape(str(stock_display_name(p.get("code", ""))))
        shares = int(p.get("shares") or 0)
        cost = float(p.get("cost") or 0)
        rows.append(
            f'''<div class="position-fast-row">
  <div><b>{name} <span class="code">{code}</span></b>
    <small>{shares:,} 股・成本 {cost:,.2f}／股・即時報價載入中…</small></div>
</div>''')
    return style + f'''<section class="position-fast-card">
  <h2>我的持股 <small class="position-fast-note">先顯示已儲存資料</small></h2>
  {"".join(rows)}
  <div class="position-fast-empty">正在補上即時價格、損益與走勢資料…</div>
</section>'''


@app.route("/web/positions", methods=["GET", "POST"])
@web_login_required
def web_positions(uid):
    # GET 且不是要片段時，先秒回骨架讓使用者馬上看到畫面，
    # 真正的抓價工作交給後續的 fragment 請求。
    # POST 不能這樣做——表單送出必須當場處理完，否則新增／賣出會遺失。
    if request.method == "GET" and not wants_fragment():
        return render_loading_shell(
            "持股", "positions",
            ["正在讀取你的持股…", "正在抓即時報價…", "正在計算損益與權重…"],
            note="先顯示已儲存持股，再補上即時價格與損益。",
            staged=True)

    if request.method == "GET" and wants_fragment() and request.args.get("fast") == "1":
        return respond_page("持股", render_positions_fast_summary(uid), "positions")

    # 手續費設定要在處理賣出之前先讀出來，賣出當下記錄的已實現損益
    # 才能用使用者自己的折扣／最低收費計算，跟畫面上其他地方口徑一致。
    fee_disc, min_fee = get_fee_settings(get_profile(uid))

    msg = ""
    if request.method == "POST" and not valid_web_csrf():
        return respond_page("持股", '<div class="msg">安全驗證已過期，請重新整理後再送出。</div>', "positions")
    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            ok = delete_position(uid, request.form.get("id"))
            msg = "已刪除。" if ok else "刪除失敗：找不到這筆持股或資料未成功寫入。"
        elif action == "sell":
            def num(field, cast=float):
                """空字串代表「請幫我算」，回 None；填了才用使用者給的數字。"""
                v = (request.form.get(field) or "").strip()
                if not v:
                    return None
                try:
                    return cast(v)
                except ValueError:
                    return None

            sell_shares = num("sell_shares", int) or 0
            ok, err, summary = sell_position(
                uid, request.form.get("id"), sell_shares,
                sell_price=num("sell_price"),
                fee=num("fee"), tax=num("tax"))
            if not ok:
                msg = err or "賣出失敗，請稍後再試。"
            elif summary:
                # 剛填完賣價與費用，最想知道的就是這筆到底賺賠多少，
                # 只回「已記錄」等於要人自己再翻到已實現損益去對。
                name = stock_display_name(summary["code"])
                sign = "獲利" if summary["pl"] >= 0 else "虧損"
                held = (f"，持有 {summary['held_days']} 天"
                        if summary["held_days"] is not None else "")
                msg = (f"已賣出 {name} {summary['shares']:,} 股 "
                       f"@{summary['sell_price']:,.2f}（成本 {summary['cost']:,.2f}"
                       f"{held}）。<br>"
                       f"實現{sign} <b>{summary['pl']:+,.0f}</b> "
                       f"（{summary['pct']:+.2f}%），"
                       f"已扣手續費 {summary['fee']:,.0f}、"
                       f"證交稅 {summary['tax']:,.0f}。")
            else:
                msg = "已賣出，但查不到報價，這筆沒有損益紀錄。"
        else:
            code = normalize_code(request.form.get("code", ""))
            try:
                shares = int(request.form.get("shares", "0"))
                cost = float(request.form.get("cost", "0"))
            except ValueError:
                shares, cost = 0, 0.0
            # 買進手續費直接攤進每股成本，跟券商「成本價」的口徑一致
            # （券商庫存頁顯示的成本價本來就已含買進手續費）。
            # 留空代表你填的成本價已經含手續費了，不再重複加。
            buy_fee = (request.form.get("buy_fee") or "").strip()
            if not code or shares <= 0 or cost <= 0:
                msg = "請填入正確的代號、股數與成本價。"
            else:
                try:
                    bf = float(buy_fee) if buy_fee else 0.0
                except ValueError:
                    bf = 0.0
                if bf > 0:
                    cost = (cost * shares + bf) / shares
                ok = add_position(uid, code, shares, cost,
                                  request.form.get("bought_on") or None)
                if not ok:
                    msg = "新增失敗，資料沒有成功寫入，請稍後再試。"
                else:
                    msg = (f"已新增 {code}（含手續費，每股成本 {cost:,.2f}）。"
                           if bf > 0 else f"已新增 {code}。")

    positions = merge_positions(get_positions(uid))
    inst = fetch_institutional_data() or {}

    def sell_form(lot_id, max_shares, code, cur_price, lot_cost, label="賣出"):
        """
        展開式賣出面板。四個欄位都可以自己填，因為只有你知道實際成交的數字；
        賣價預帶目前市價、手續費與稅預帶牌價試算值，方便但可以覆蓋。
        """
        tax_rate = TAX_RATE_ETF if is_etf(code) else TAX_RATE_STOCK
        px = f"{cur_price:.2f}" if cur_price else ""
        gross = (cur_price or 0) * max_shares
        est_fee = round(broker_fee(gross)) if gross else 0
        est_tax = round(gross * tax_rate) if gross else 0
        return f"""
<details class="sellbox"><summary>{label}</summary>
<form method="post" class="sellpanel">
  <input type="hidden" name="action" value="sell">
  <input type="hidden" name="id" value="{lot_id}">
  <div class="fields">
    <div><label>賣出股數</label>
      <input type="number" name="sell_shares" min="1" max="{max_shares}"
             value="{max_shares}" required></div>
    <div><label>賣出價</label>
      <input type="number" step="0.01" name="sell_price" value="{px}"
             placeholder="{px or '市價'}"></div>
    <div><label>手續費</label>
      <input type="number" step="1" name="fee" placeholder="{est_fee}"></div>
    <div><label>證交稅</label>
      <input type="number" step="1" name="tax" placeholder="{est_tax}"></div>
  </div>
  <div class="row-actions">
    <a class="cancel-link" href="/web/positions" data-no-busy="1">返回／取消</a>
    <button type="submit">確認賣出</button>
  </div>
  <div class="sell-hint">
    成本 {lot_cost:,.2f}／股，全部賣出 {max_shares:,} 股。
    手續費與證交稅留空會用牌價試算（{est_fee:,} 與 {est_tax:,}）；
    填入對帳單上的實際金額，已實現損益才會跟券商對得起來。
  </div>
</form>
</details>"""

    def delete_form(lot_id, name):
        return (f'<form method="post" style="display:inline;margin:0" '
                f'onsubmit="return confirm(\'刪除是把這筆持股整筆移除，'
                f'不會記入已實現損益。確定刪除 {name}？\')">'
                f'<input type="hidden" name="action" value="delete">'
                f'<input type="hidden" name="id" value="{lot_id}">'
                f'<button class="del" type="submit">刪除</button></form>')

    def lots_html(p, name, cur_price):
        """單筆就一組賣出面板＋刪除鍵；分批買進則每一筆各自可賣出或刪除。"""
        lots = p.get("lots", [])
        if len(lots) <= 1:
            if not lots:
                return ""
            l = lots[0]
            return (f'<div class="lot-actions">{delete_form(l["id"], name)}</div>'
                    + sell_form(l["id"], l["shares"], p["code"],
                                cur_price, l["cost"]))
        items = "".join(
            f'<div class="lot">'
            f'<span class="num">{l["shares"]:,}</span> 股　'
            f'成本 <span class="num">{l["cost"]:,.2f}</span>　'
            f'{l["bought_on"].strftime("%Y/%m/%d") if l["bought_on"] else "未填日期"}'
            f'<div class="lot-actions">{delete_form(l["id"], name)}</div>'
            f'{sell_form(l["id"], l["shares"], p["code"], cur_price, l["cost"], "賣出這筆")}'
            f'</div>' for l in lots)
        return (f'<details class="lots"><summary>分 {len(lots)} 筆買進</summary>'
                f'{items}</details>')

    rows_html, total_value, total_cost = [], 0.0, 0.0
    total_day_pl = 0.0
    enriched = []
    # 持股頁的個股走勢改抓 1 年日 K；技術位階仍只用近 20／60 日計算。
    # 這樣可以看較早買進的部位，不會因約 60 個交易日的 3mo 區間而截斷。
    price_map = get_realtime_stocks_bulk(
        [p["code"] for p in positions], rng="1y")
    for p in positions:
        price = price_map.get(p["code"])
        if price:
            value = price["close"] * p["shares"]
            cost_total = p["cost"] * p["shares"]
            total_value += value
            total_cost += cost_total
            enriched.append((p, price, value, cost_total))
        else:
            enriched.append((p, None, 0.0, p["cost"] * p["shares"]))
            total_cost += p["cost"] * p["shares"]

    total_fee = sum(
        net_profit(p["code"], p["shares"], p["cost"], pr["close"],
                   p.get("lots"), fee_disc, min_fee)[2]
        for p, pr, _v, _c in enriched if pr)

    for p, price, value, cost_total in sorted(
            enriched, key=lambda x: x[2], reverse=True):
        name = stock_display_name(p["code"], inst,
                                  price["name"] if price else None)
        weight = (value / total_value * 100) if total_value else 0
        if price:
            gross_pl = (price["close"] - p["cost"]) / p["cost"] * 100
            net_amt, pl, cost_fee = net_profit(
                p["code"], p["shares"], p["cost"], price["close"],
                p.get("lots"), fee_disc, min_fee)
            pl = pl if pl is not None else gross_pl
            # 今日損益金額：用漲跌幅反推昨收，再乘持股數。
            # 直接用「今收 − 昨收」比用市值差可靠——市值差會受到當天
            # 新增或賣出持股影響，那不是股價造成的損益。
            prev_close = price["close"] / (1 + price["pct"] / 100) if price["pct"] != -100 else price["close"]
            day_pl = (price["close"] - prev_close) * p["shares"]
            total_day_pl += day_pl
            net_amt = net_amt if net_amt is not None else (value - cost_total)
            held = ((taiwan_now().date() - p["bought_on"]).days
                    if p["bought_on"] else None)
            rows_html.append(f"""
<div class="row">
  <div><span class="name">{name}</span><span class="code">{p['code']}</span></div>
  <div class="price num">{price['close']:,.2f}</div>
  <div class="meta">
    <span><em>持有</em> <span class="num">{p['shares']:,}</span> 股</span>
    <span><em>成本</em> <span class="num">{p['cost']:,.2f}</span></span>
    <span><em>今日</em> <span class="num {'up' if day_pl >= 0 else 'down'}">{day_pl:+,.0f}</span></span>
    <span><em>累計</em> <span class="num {'up' if net_amt >= 0 else 'down'}">{net_amt:+,.0f}</span>
      {fmt_pct(pl)}<span class="sub">帳面 {gross_pl:+.2f}%</span></span>
    <span><em>市值</em> <span class="num">{value:,.0f}</span></span>
    <span><em>權重</em> <span class="num">{weight:.1f}%</span></span>
    {f'<span><em>持有</em> {held} 天</span>' if held is not None else ''}
    <details class="disclosure trend" style="margin-top:2px">
      <summary>損益走勢</summary>
      {render_stock_sparkline(price, p['cost'], p['shares'], p.get('lots'))}
    </details>
    {lots_html(p, name, price['close'])}
  </div>
  <div class="chg">{fmt_pct(price['pct'])}</div>
  <div class="bar"><div style="width:{weight:.1f}%"></div></div>
</div>""")
        else:
            rows_html.append(f"""
<div class="row">
  <div><span class="name">{p['code']}</span>
       <span class="code">查無行情</span></div>
  <div class="price flat">—</div>
  <div class="meta">
    <span><em>持有</em> <span class="num">{p['shares']:,}</span> 股</span>
    {lots_html(p, p['code'], None)}
  </div>
  <div class="chg"></div>
</div>""")

    pl_total = (((total_value - total_fee - total_cost) / total_cost * 100)
                if total_cost else None)
    totals = f"""
<div class="totals">
  <div><div class="total-label">總市值</div>
       <div class="total-value num">{total_value:,.0f}</div>
       <div class="total-sub">{fmt_pct(pl_total)}</div></div>
  <div><div class="total-label">淨損益</div>
       <div class="total-value num {'up' if total_value - total_cost - total_fee >= 0 else 'down'}">
         {total_value - total_cost - total_fee:+,.0f}</div>
       <div class="total-sub" style="color:var(--ink-faint)">
         已扣交易成本 <span class="num">{total_fee:,.0f}</span></div></div>
  <div><div class="total-label">今日損益</div>
       <div class="total-value num {'up' if total_day_pl >= 0 else 'down'}">
         {total_day_pl:+,.0f}</div>
       <div class="total-sub" style="color:var(--ink-faint)">
         今收 vs 昨收</div></div>
  <div><div class="total-label">持股檔數</div>
       <div class="total-value num">{len(positions)}</div></div>
</div>""" if positions else ""

    body = f"""
{f'<div class="msg">{msg}</div>' if msg else ''}
{totals}
<div class="section-head"><h2>持股明細</h2>
  <span class="section-note">依市值排序</span></div>
<div class="rows">
{''.join(rows_html) if rows_html else '<div class="empty">還沒有持股紀錄，用下方表單新增。</div>'}
</div>

<form class="add" method="post">
  <h3>新增持股</h3>
  <div class="fields">
    <div><label>股票代號</label>
      <input name="code" inputmode="numeric" placeholder="2330" required></div>
    <div><label>股數</label>
      <input name="shares" inputmode="numeric" placeholder="1000" required></div>
    <div><label>成本價</label>
      <input name="cost" inputmode="decimal" placeholder="950.5" required></div>
    <div><label>手續費（可略）</label>
      <input name="buy_fee" inputmode="numeric" placeholder="0"></div>
    <div><label>買進日期（可略）</label>
      <input name="bought_on" type="date"></div>
  </div>
  <button type="submit">新增</button>
  <div class="sell-hint">
    直接抄券商庫存頁的「成本價」就好，那個數字已含買進手續費，手續費欄留空即可。<br>
    若填的是純成交價，在手續費欄填實際金額，會自動攤進每股成本。
  </div>
</form>"""
    return respond_page("持股", body, "positions")


# ── 問卷與門檻設定 ──
# 全部必填。
# 這些答案不是拿來裝飾的：組合分析的提醒要靠「交叉比對」才有價值——
# 例如自述資金年期 3–10 年、實際卻只抱幾週，這個矛盾只有兩題都答了才看得出來；
# 「曾在虧損時加碼」也要對照實際的分批進場紀錄才能點名。
# 少一題就少一組比對，所以不留可略過的選項。
PROFILE_FIELDS = [
    ("age_band", "你的年齡區間", True,
     ["未滿 30 歲", "30–39 歲", "40–49 歲", "50–59 歲", "60 歲以上"]),
    ("horizon", "這筆錢預計多久之後可能會用到？", True,
     ["1 年內", "1–3 年", "3–10 年", "10 年以上", "沒有特定用途"]),
    ("asset_share", "這筆投資佔你可動用資產的比重大約是？", True,
     ["不到四分之一", "約四分之一到一半", "約一半以上", "幾乎全部"]),
    ("income_type", "你的收入穩定度", True,
     ["固定薪資", "固定薪資 + 變動獎金", "接案或營業收入", "目前無固定收入"]),
    ("drawdown_experience", "過去實際經歷過最大的帳面虧損？當時做了什麼？", True,
     ["沒有經歷過明顯虧損", "虧損 10% 以內就減碼了", "撐過 20–30% 沒有動作",
      "撐過 30% 以上沒有動作", "曾經在虧損時加碼"]),
    ("check_frequency", "你多久會看一次帳戶？", True,
     ["一天多次", "每天一次", "每週", "每月或更少"]),
    ("holding_period", "你的持股平均會抱多久？", True,
     ["幾天", "幾週", "幾個月", "一年以上", "不一定"]),
    ("other_assets", "除了台股，你還有哪些部位？", True,
     ["美股", "ETF", "債券", "房地產", "定存或現金", "幾乎只有台股"]),
]

DEFAULT_LOSS_ALERT = 20
DEFAULT_POSITION_ALERT = 30
CONSERVATIVE_LOSS_ALERT = 15
CONSERVATIVE_POSITION_ALERT = 25


def get_profile(user_id):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT age_band, horizon, asset_share, income_type,
                   drawdown_experience, loss_alert_pct, position_alert_pct,
                   check_frequency, holding_period, other_assets,
                   fee_discount, min_fee
            FROM user_profile WHERE user_id = %s
        """, (str(user_id).strip(),))
        r = cursor.fetchone()
        cursor.close()
        if not r:
            return {}
        keys = ["age_band", "horizon", "asset_share", "income_type",
                "drawdown_experience", "loss_alert_pct", "position_alert_pct",
                "check_frequency", "holding_period", "other_assets",
                "fee_discount", "min_fee"]
        return dict(zip(keys, r))
    except Exception as e:
        print(f"❌ 讀取設定失敗: {e}")
        return {}
    finally:
        release_db_connection(conn)


def save_profile(user_id, data):
    cols = ["age_band", "horizon", "asset_share", "income_type",
            "drawdown_experience", "loss_alert_pct", "position_alert_pct",
            "check_frequency", "holding_period", "other_assets",
            "fee_discount", "min_fee"]
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            INSERT INTO user_profile (user_id, {', '.join(cols)}, updated_at)
            VALUES (%s, {', '.join(['%s'] * len(cols))}, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                {', '.join(f'{c} = EXCLUDED.{c}' for c in cols)},
                updated_at = NOW()
        """, (str(user_id).strip(), *[data.get(c) for c in cols]))
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        print(f"❌ 儲存設定失敗: {e}")
        return False
    finally:
        release_db_connection(conn)


def update_profile(user_id, updates):
    """
    只更新傳入的欄位，其餘沿用現有設定。
    問卷（組合分析頁）跟門檻／手續費（設定頁）現在是兩個不同表單各自送出，
    不這樣做的話後送出的表單會把先送出那邊的值覆蓋成空白。
    """
    current = get_profile(user_id)
    merged = {**current, **updates}
    return save_profile(user_id, merged)


# ── 交易成本 ──
# 台股：手續費 0.1425%（買賣各一次，券商多有折扣，且通常有最低收費），
# 證交稅賣出時收，一般股票 0.3%、ETF 0.1%。
BROKER_FEE_RATE = 0.001425
TAX_RATE_STOCK = 0.003
TAX_RATE_ETF = 0.001
DEFAULT_FEE_DISCOUNT = 1.0   # 1.0 = 無折扣
DEFAULT_MIN_FEE = 20


def is_etf(code):
    """台股 ETF 代號以 00 開頭；主動式 ETF 如 00981A 也屬之。"""
    return str(code).startswith("00")


def is_active_etf(code):
    """
    主動式ETF代號末碼為英文字母（如 00981A 股票型、00982D 債券型）。
    由經理人主動選股，不是追蹤指數的一籃子部位，
    風險特性更接近集中持股，不該被當成「已分散」看待。
    """
    code = str(code).strip()
    return is_etf(code) and code[-1:].isalpha()


def broker_fee(amount, discount=DEFAULT_FEE_DISCOUNT, min_fee=DEFAULT_MIN_FEE):
    """單筆手續費。小額交易會被最低收費拉高，這對零股影響很大。"""
    if amount <= 0:
        return 0.0
    return max(min_fee, amount * BROKER_FEE_RATE * discount)


def net_profit(code, shares, avg_cost, price, lots=None,
               discount=DEFAULT_FEE_DISCOUNT, min_fee=DEFAULT_MIN_FEE,
               cost_includes_fee=True):
    """
    扣掉交易成本後的損益：假設現在依現價全部賣出。

    預設 cost_includes_fee=True，因為多數人是直接從券商庫存頁抄「成本價」，
    而券商的成本價已把買進手續費攤進去了（例如成交均價 11.36、成本價 11.38），
    再加一次買進費用會重複計算。若填的是純成交價，把這個參數設為 False。

    算法對齊券商：預估損益 = （市值 − 賣出手續費 − 證交稅） − 成本
    回傳 (淨損益金額, 淨報酬率%, 賣出成本合計)
    """
    if not price or shares <= 0:
        return None, None, 0.0

    buy_fee = 0.0
    if not cost_includes_fee:
        if lots:
            for l in lots:
                buy_fee += broker_fee(l["shares"] * l["cost"], discount, min_fee)
        else:
            buy_fee = broker_fee(shares * avg_cost, discount, min_fee)

    gross_value = shares * price
    sell_fee = broker_fee(gross_value, discount, min_fee)
    tax = gross_value * (TAX_RATE_ETF if is_etf(code) else TAX_RATE_STOCK)

    total_cost = shares * avg_cost + buy_fee
    profit = (gross_value - sell_fee - tax) - total_cost
    pct = (profit / total_cost * 100) if total_cost else None
    return profit, pct, buy_fee + sell_fee + tax


def get_fee_settings(profile):
    return (profile.get("fee_discount") or DEFAULT_FEE_DISCOUNT,
            profile.get("min_fee") if profile.get("min_fee") is not None
            else DEFAULT_MIN_FEE)


def get_thresholds(profile):
    """
    取得提醒門檻。使用者沒自訂時給預設值；
    若他表示「沒有經歷過明顯虧損」，預設改保守一點——
    沒真的痛過的人普遍高估自己的承受度。
    """
    never = profile.get("drawdown_experience") == "沒有經歷過明顯虧損"
    loss = profile.get("loss_alert_pct")
    pos = profile.get("position_alert_pct")
    conservative = never and loss is None and pos is None
    return {
        "loss": loss or (CONSERVATIVE_LOSS_ALERT if never else DEFAULT_LOSS_ALERT),
        "position": pos or (CONSERVATIVE_POSITION_ALERT if never else DEFAULT_POSITION_ALERT),
        "conservative": conservative,
    }


def is_profile_complete(profile):
    """問卷全部答完才算完成。任何一題沒答，交叉比對就少一組。"""
    return bool(profile) and all(profile.get(k) for k, _l, _r, _o in PROFILE_FIELDS)


def radio_group(profile, key, label, required, options):
    opts = "".join(
        f'<label class="opt"><input type="radio" name="{key}" value="{o}"'
        f'{" checked" if profile.get(key) == o else ""}'
        f'{" required" if required else ""}> {o}</label>'
        for o in options
    )
    req = '<span class="req">必填</span>' if required else '<span class="opt-tag">可略過</span>'
    return f'<div class="q"><div class="q-title">{label} {req}</div>{opts}</div>'


def render_risk_card(profile, msg=None):
    """
    風險輪廓卡片，嵌在組合分析頁最上方而非獨立頁面——
    填了答案要馬上看到下面的提醒跟著變，兩者在同一頁使用者才看得出關聯。
    全部題目都必填；填完後收成摘要＋可展開編輯。
    """
    complete = is_profile_complete(profile)
    msg_html = f'<div class="msg">{msg}</div>' if msg else ""
    form_html = "".join(radio_group(profile, k, l, r, o) for k, l, r, o in PROFILE_FIELDS)

    if complete:
        pf_items = [
            ("年齡", profile.get("age_band")),
            ("資金年期", profile.get("horizon")),
            ("資產比重", profile.get("asset_share")),
            ("收入型態", profile.get("income_type")),
            ("回檔經驗", profile.get("drawdown_experience")),
            ("看盤頻率", profile.get("check_frequency")),
            ("平均持有", profile.get("holding_period")),
            ("其他部位", profile.get("other_assets")),
        ]
        summary_html = "".join(
            f'<div class="pf"><span class="pf-k">{k}</span>'
            f'<span class="pf-v">{v}</span></div>'
            for k, v in pf_items)
        return f"""
<div class="section-head"><h2>你的風險輪廓</h2>
  <span class="section-note">上方提醒依此判讀</span></div>
{msg_html}
<div class="profile-grid">{summary_html}</div>
<details class="disclosure">
  <summary>編輯風險輪廓</summary>
  <form method="post" action="/web/portfolio" style="margin-top:10px">
    {form_html}
    <button type="submit">儲存</button>
  </form>
</details>"""

    # 未填完：說明為什麼要問，以及不填會少掉什麼
    return f"""
<div class="section-head"><h2>先完成風險輪廓</h2>
  <span class="section-note">{len(PROFILE_FIELDS)} 題，全部必填</span></div>
{msg_html}
<div class="hint">
  <b>為什麼要問這些</b><br>
  這些答案不會改變數據本身，而是決定「什麼該提醒你」。<br><br>
  同樣 60% 集中在半導體：資金一年內要用、且這是全部身家的人，
  會看到強烈警示；十年不動用的人看到的是不同的說明。<br><br>
  更重要的是<b>交叉比對</b>——自述資金 3–10 年不動用、實際卻只抱幾週，
  這種矛盾只有兩題都答了才看得出來；「曾在虧損時加碼」也要對照
  你實際的分批進場紀錄，才能指出你現在是不是又在做同一件事。<br><br>
  少一題就少一組比對，所以沒有可略過的選項。
</div>
<form method="post" action="/web/portfolio">
  {form_html}
  <button type="submit" style="margin-top:14px">儲存並開始分析</button>
</form>"""


# ============================================================
# 已實現損益
# ============================================================
def render_stock_sparkline(price, cost, shares, lots=None):
    """
    單檔的損益走勢。用 get_realtime_stock 已經回傳的近 60 日收盤序列來畫——
    那份資料本來就是為了算相關係數而抓的，等於免費多得到一張圖，
    而且今天就有 60 天可看，不必等每日快照累積一兩週。

    畫的是「這筆持股的損益金額」而不是股價，因為你關心的是賺賠多少錢，
    不是這檔漲到幾元。

    圖上標三樣券商 App 看不到的東西：
      ・買進點——你買在相對高點還是低點，這是這張圖獨有的價值
      ・起訖日期——沒有時間軸的話，看不出低點是上週還是上個月
      ・最大回檔——從波段高點到低點的落差，那是你實際承受過的帳面痛感
    """
    closes = (price or {}).get("closes") or []
    dates = (price or {}).get("close_dates") or []
    if len(closes) < 5 or not cost or not shares:
        return '<div class="sub">走勢資料不足</div>'

    # 從最早的買進日開始畫。買進之前的「損益」是虛構的——
    # 那段期間你根本沒持有，畫出來只會讓人誤判自己抱了多久、
    # 也會把買進前的波動算進最大回檔。
    first_buy = None
    if lots:
        buy_dates = [l["bought_on"] for l in lots if l.get("bought_on")]
        first_buy = min(buy_dates) if buy_dates else None
    if first_buy and dates:
        start = next((i for i, d in enumerate(dates) if d >= first_buy), None)
        # 留幾根買進前的 K 棒當作視覺參考，但不要多到喧賓奪主
        if start is not None and len(dates) - start >= 5:
            start = max(0, start - 3)
            closes, dates = closes[start:], dates[start:]

    pls = [(c - cost) * shares for c in closes]
    lo, hi = min(pls), max(pls)
    if hi - lo < 1:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad

    W, H = 600, 96
    n = len(pls)
    x = lambda i: (i / (n - 1)) * W
    y = lambda v: (1 - (v - lo) / (hi - lo)) * H
    path = "M " + " L ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(pls))

    # 損益 0 的位置：在範圍內才畫，否則那條線會貼在邊緣造成誤解
    zero = (f'<line x1="0" y1="{y(0):.1f}" x2="{W}" y2="{y(0):.1f}" '
            f'stroke="var(--rule)" stroke-width="1" stroke-dasharray="3,3"/>'
            if lo <= 0 <= hi else "")
    color = "var(--up)" if pls[-1] >= 0 else "var(--down)"

    # ── 買進點 ──
    # 把買進日對應到序列位置。買進日可能落在區間之前（早就買了），
    # 那就不標——硬標在最左邊會讓人以為那天才買。
    marks, mark_notes = [], []
    if dates and lots:
        for l in lots:
            bd = l.get("bought_on")
            if not bd or bd < dates[0] or bd > dates[-1]:
                continue
            # 找最接近的交易日（買進日可能是非交易日或資料缺該日）
            idx = min(range(len(dates)), key=lambda i: abs((dates[i] - bd).days))
            marks.append(f'<circle cx="{x(idx):.1f}" cy="{y(pls[idx]):.1f}" '
                         f'r="3.5" fill="var(--paper)" stroke="var(--brass)" '
                         f'stroke-width="2"/>')
            mark_notes.append(f"{bd.strftime('%m/%d')} 買 {l['shares']:,} 股 "
                              f"@{l['cost']:,.2f}")

    # ── 最大回檔 ──
    # 從歷史高點往後找最低，取最大落差。這是實際承受過的帳面痛感，
    # 比單純的「區間」更有意義——區間的高低點可能根本不同時序。
    peak, max_dd, dd_from, dd_to = pls[0], 0.0, 0, 0
    peak_i = 0
    for i, v in enumerate(pls):
        if v > peak:
            peak, peak_i = v, i
        elif peak - v > max_dd:
            max_dd, dd_from, dd_to = peak - v, peak_i, i

    dd_html = ""
    if max_dd > 0:
        dd_html = (f'<rect x="{x(dd_from):.1f}" y="0" '
                   f'width="{max(1, x(dd_to) - x(dd_from)):.1f}" height="{H}" '
                   f'fill="var(--brass)" opacity="0.07"/>')

    d0 = dates[0].strftime("%m/%d") if dates else ""
    d1 = dates[-1].strftime("%m/%d") if dates else ""
    marks_line = ("　".join(mark_notes) if mark_notes
                  else "買進日不在此區間內" if lots else "")

    return f"""
<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" preserveAspectRatio="none"
     style="display:block;margin-top:8px">
  {dd_html}
  {zero}
  <path d="{path}" fill="none" stroke="{color}" stroke-width="1.8"/>
  {''.join(marks)}
</svg>
<div class="sub" style="display:flex;justify-content:space-between;margin-top:5px">
  <span>{d0} – {d1}（{n} 個交易日）</span>
  <span>最大回檔 <span class="num">-{max_dd:,.0f}</span></span>
</div>
{f'<div class="sub" style="margin-top:3px"><span style="color:var(--brass)">●</span> {marks_line}</div>' if marks_line else ''}"""


def render_realized_summary(user_id, inst_data):
    """
    已實現損益摘要＋最近交易明細。沒有任何賣出紀錄時回傳空字串，
    組合分析頁就不會多出一個空蕩蕩的區塊。
    """
    trades = get_realized_trades(user_id, limit=100)
    if not trades:
        return ""

    priced = [t for t in trades if t["realized_pl"] is not None]
    total_pl = sum(t["realized_pl"] for t in priced)
    wins = len([t for t in priced if t["realized_pl"] > 0])
    win_rate = (wins / len(priced) * 100) if priced else None
    hold_days = [(t["sold_on"] - t["bought_on"]).days
                 for t in trades if t["bought_on"] and t["sold_on"]]
    avg_hold = (sum(hold_days) / len(hold_days)) if hold_days else None

    def trade_row(t):
        name = stock_display_name(t["code"], inst_data)
        pl_cls = "" if t["realized_pl"] is None else (
            "up" if t["realized_pl"] >= 0 else "down")
        pl_txt = (f'<span class="num {pl_cls}">{t["realized_pl"]:+,.0f}</span>'
                  if t["realized_pl"] is not None else '<span class="flat">—</span>')
        costs = (t.get("fee") or 0) + (t.get("tax") or 0)
        return f"""
<div class="row">
  <div><span class="name">{name}</span><span class="code">{t['code']}</span></div>
  <div class="price">{fmt_pct(t['realized_pct'])}</div>
  <div class="meta">
    <span><em>股數</em> <span class="num">{t['shares']:,}</span></span>
    <span><em>成本</em> <span class="num">{t['buy_cost']:,.2f}</span></span>
    <span><em>賣價</em> <span class="num">{t['sell_price']:,.2f}</span></span>
    <span><em>損益</em> {pl_txt}</span>
    <span><em>費用稅</em> <span class="num">{costs:,.0f}</span></span>
    <span><em>賣出日</em> {t['sold_on'].strftime('%Y/%m/%d') if t['sold_on'] else '—'}</span>
  </div>
</div>"""

    return f"""
<details class="realized-collapse">
  <summary><span>已實現損益</span><small>共 {len(trades)} 筆交易・點開查看</small></summary>
  <div class="realized-body">
    <div class="totals">
      <div><div class="total-label">累計已實現損益</div>
           <div class="total-value num {'up' if total_pl >= 0 else 'down'}">{total_pl:+,.0f}</div>
           <div class="total-sub" style="color:var(--ink-faint)">已扣交易成本</div></div>
      <div><div class="total-label">勝率</div>
           <div class="total-value num">{f"{win_rate:.0f}%" if win_rate is not None else '—'}</div>
           <div class="total-sub" style="color:var(--ink-faint)">{len(priced)} 筆有損益資料</div></div>
      <div><div class="total-label">平均持有天數</div>
           <div class="total-value num">{f"{avg_hold:.0f} 天" if avg_hold is not None else '—'}</div></div>
    </div>
    <div class="rows">{''.join(trade_row(t) for t in trades[:10])}</div>
    {f'<div class="section-note" style="margin-top:8px">僅顯示最近 10 筆</div>' if len(trades) > 10 else ''}
  </div>
</details>
"""


# ============================================================
# 組合走勢：每日快照 vs 大盤
# ============================================================
def render_trend_chart(snapshots):
    """
    組合市值 vs 加權指數的走勢比較。兩者都換算成「相對第一筆快照的漲跌幅」
    畫在同一張圖上——這樣起始金額差異懸殊也能疊在一起比較，比較的是趨勢不是絕對數字。
    純 SVG 手繪組裝成字串，跟整個網頁一樣不依賴任何 JS 圖表庫。
    """
    pts = [s for s in snapshots if s["value"]]
    if len(pts) < 2:
        return ('<div class="empty">資料還在累積中，'
                '至少需要 2 天以上的快照才能畫出走勢，明天再回來看看。</div>')

    base_value = pts[0]["value"]
    port_series = [(p["value"] / base_value - 1) * 100 for p in pts]

    taiex_vals = [p["taiex"] for p in pts if p["taiex"]]
    base_taiex = taiex_vals[0] if taiex_vals else None
    taiex_series = [
        ((p["taiex"] / base_taiex - 1) * 100 if (p["taiex"] and base_taiex) else None)
        for p in pts
    ]

    all_vals = port_series + [v for v in taiex_series if v is not None]
    lo, hi = min(all_vals), max(all_vals)
    if hi - lo < 1:  # 走勢幾乎打平時避免圖被壓成一條線，硬給一點高度
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad

    W, H, ML, MR, MT, MB = 640, 160, 6, 6, 10, 10
    n = len(pts)

    def x_of(i):
        return ML + (i / (n - 1)) * (W - ML - MR) if n > 1 else ML

    def y_of(v):
        return MT + (1 - (v - lo) / (hi - lo)) * (H - MT - MB)

    def path_of(series):
        coords = [(x_of(i), y_of(v)) for i, v in enumerate(series) if v is not None]
        if not coords:
            return ""
        return "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in coords)

    zero_y = y_of(0)
    port_path = path_of(port_series)
    taiex_path = path_of(taiex_series) if base_taiex else ""

    port_last = port_series[-1]
    taiex_last = next((v for v in reversed(taiex_series) if v is not None), None)

    svg = f"""
<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" preserveAspectRatio="none">
  <line x1="{ML}" y1="{zero_y:.1f}" x2="{W - MR}" y2="{zero_y:.1f}"
        stroke="var(--rule)" stroke-width="1" stroke-dasharray="2,3"/>
  {f'<path d="{taiex_path}" fill="none" stroke="var(--ink-faint)" stroke-width="1.5" stroke-dasharray="4,3"/>' if taiex_path else ''}
  <path d="{port_path}" fill="none" stroke="var(--brass)" stroke-width="2"/>
</svg>"""

    legend = f"""
<div class="legend" style="margin-top:6px">
  <span><i style="background:var(--brass)"></i>組合 {port_last:+.1f}%</span>
  {f'<span><i style="background:var(--ink-faint)"></i>加權指數 {taiex_last:+.1f}%</span>' if taiex_last is not None else ''}
  <span style="color:var(--ink-faint);margin-left:auto">
    {pts[0]['date'].strftime('%m/%d')} – {pts[-1]['date'].strftime('%m/%d')}</span>
</div>"""

    return svg + legend


@app.route("/web/trades")
@web_login_required
def web_trades(uid):
    """
    交易紀錄。組合分析頁只露出最近 10 筆，這裡看全部並可依月份、股票篩選。
    重點在統計摘要——單看一筆一筆的損益看不出自己的做法有沒有問題，
    勝率、盈虧比、平均持有天數合起來才說得出「這套打法能不能持續」。
    """
    if not wants_fragment():
        return render_loading_shell(
            "交易紀錄", "trades",
            ["正在讀取交易紀錄…", "正在計算統計…"],
            note="只統計已賣出並留下損益數字的交易。")

    month = request.args.get("month", "")
    code = request.args.get("code", "")
    months, codes = get_trade_filters(uid)
    trades = get_realized_trades(uid, limit=500,
                                 code=code or None, month=month or None)
    inst = fetch_institutional_data() or {}

    if not trades and not months:
        return respond_page("交易紀錄", """
<div class="empty">還沒有任何交易紀錄。<br><br>
<span style="font-size:12.5px">在持股頁按「賣出」並填入賣價後，
這筆交易就會記錄在這裡。</span><br><br>
<a href="/web/positions" style="color:var(--brass)">前往持股 →</a></div>""",
                            "trades")

    st = summarize_trades(trades)

    def opt(v, t, cur):
        return f'<option value="{v}"{" selected" if str(cur) == str(v) else ""}>{t}</option>'

    controls = f"""
<form method="get" class="controls">
  <div class="fields">
    <div><label>月份</label><select name="month" onchange="this.form.submit()">
      {opt('', '全部', month)}
      {''.join(opt(m, m.replace('-', ' / '), month) for m in months)}
    </select></div>
    <div><label>股票</label><select name="code" onchange="this.form.submit()">
      {opt('', '全部', code)}
      {''.join(opt(c, f"{stock_display_name(c, inst)} {c}", code) for c in codes)}
    </select></div>
  </div>
</form>"""

    if not st:
        body = controls + '<div class="empty">這個範圍內沒有交易紀錄。</div>'
        return respond_page("交易紀錄", body, "trades")

    payoff_txt = f"{st['payoff']:.2f}" if st["payoff"] else "—"
    # 勝率與盈虧比要一起判讀：只有其中一個好不代表這套做法站得住腳
    if st["payoff"] and st["win_rate"]:
        expectancy = (st["win_rate"] / 100 * st["avg_win"]
                      - (1 - st["win_rate"] / 100) * st["avg_loss"])
        exp_txt = (f"以目前的勝率與盈虧比推算，每筆交易的期望值約 "
                   f"<b>{expectancy:+,.0f}</b> 元。")
    else:
        exp_txt = "獲利或虧損的樣本還不夠，暫時算不出期望值。"

    best, worst = st["best"], st["worst"]

    def trade_row(t):
        name = stock_display_name(t["code"], inst)
        pl = t["realized_pl"]
        cls = "" if pl is None else ("up" if pl >= 0 else "down")
        held = ((t["sold_on"] - t["bought_on"]).days
                if t["bought_on"] and t["sold_on"] else None)
        costs = (t.get("fee") or 0) + (t.get("tax") or 0)
        return f"""
<div class="row">
  <div><span class="name">{name}</span><span class="code">{t['code']}</span></div>
  <div class="price num {cls}">{pl:+,.0f}</div>
  <div class="meta">
    <span><em>報酬</em> {fmt_pct(t['realized_pct'])}</span>
    <span><em>股數</em> <span class="num">{t['shares']:,}</span></span>
    <span><em>成本</em> <span class="num">{t['buy_cost']:,.2f}</span></span>
    <span><em>賣價</em> <span class="num">{t['sell_price']:,.2f}</span></span>
    <span><em>費用稅</em> <span class="num">{costs:,.0f}</span></span>
    {f'<span><em>持有</em> {held} 天</span>' if held is not None else ''}
    <span><em>賣出</em> {t['sold_on'].strftime('%Y/%m/%d') if t['sold_on'] else '—'}</span>
  </div>
</div>"""

    # 勝負比例橫帶：一眼看出賺賠筆數的分布
    w, l = st["wins"], st["losses"]
    flat = st["count"] - w - l
    band = []
    for n, color, fg, label in [(w, "var(--up)", "#FFF", f"賺 {w}"),
                                (l, "var(--down)", "#FFF", f"賠 {l}"),
                                (flat, "var(--rule)", "#3B2F1C", f"平 {flat}")]:
        if n:
            band.append(f'<span style="flex:{n};background:{color};color:{fg}">'
                        f'{label if n / st["count"] >= 0.12 else ""}</span>')

    body = f"""
{controls}
<div class="totals">
  <div><div class="total-label">已實現損益</div>
       <div class="total-value num {'up' if st['total_pl'] >= 0 else 'down'}">
         {st['total_pl']:+,.0f}</div>
       <div class="total-sub" style="color:var(--ink-faint)">
         {st['count']} 筆・已扣成本 <span class="num">{st['costs']:,.0f}</span></div></div>
  <div><div class="total-label">勝率</div>
       <div class="total-value num">{st['win_rate']:.0f}%</div>
       <div class="total-sub" style="color:var(--ink-faint)">
         {st['wins']} 賺 / {st['losses']} 賠</div></div>
  <div><div class="total-label">盈虧比</div>
       <div class="total-value num">{payoff_txt}</div>
       <div class="total-sub" style="color:var(--ink-faint)">
         平均賺 {st['avg_win']:,.0f} / 賠 {st['avg_loss']:,.0f}</div></div>
  <div><div class="total-label">平均持有</div>
       <div class="total-value num">
         {f"{st['avg_hold']:.0f} 天" if st['avg_hold'] is not None else '—'}</div></div>
</div>

<div class="band" style="height:34px">{''.join(band)}</div>
<div class="callout">
  {exp_txt}<br>
  <span style="font-size:12.5px;color:var(--ink-faint)">
  勝率與盈虧比要一起看：勝率七成但每次小賺、輸一次全吐回去，長期仍是虧的；
  勝率四成但賺的時候賺得夠多，反而站得住腳。</span>
</div>

<div class="section-head"><h2>最好與最差</h2>
  <span class="section-note">這個範圍內</span></div>
<div class="rows">
{trade_row(best)}
{trade_row(worst) if worst is not best else ''}
</div>

<div class="section-head"><h2>全部交易</h2>
  <span class="section-note">依賣出日期排序・{len(trades)} 筆</span></div>
<div class="rows">{''.join(trade_row(t) for t in trades)}</div>
"""
    return respond_page("交易紀錄", body, "trades")


def render_leaderboard_chart(series_map, market, top_keys, highlight_key=None):
    """繪製 user_id 索引的排行榜曲線；顯示文字仍使用成員暱稱。"""
    def entry(key):
        item = series_map.get(str(key))
        if isinstance(item, dict):
            return safe_html_text(item.get("nickname")), item.get("curve") or []
        # 舊快取在部署切換期間可能仍存在，保留短暫相容性。
        return safe_html_text(key), item or []

    ordered_keys = [str(k) for k in (top_keys or [])]
    if highlight_key and series_map.get(str(highlight_key)):
        highlight_key = str(highlight_key)
        ordered_keys = [highlight_key] + [
            k for k in ordered_keys if k != highlight_key]
        ordered_keys = ordered_keys[:4]
    lines = []
    for key in ordered_keys:
        name, curve = entry(key)
        if curve:
            lines.append((key, name, curve))
    if not lines and not market:
        return ('<div class="empty">還沒有足夠的每日快照可以畫圖。<br><br>'
                '<span style="font-size:12.5px">每個交易日收盤後會存一次快照，'
                '加入排行榜後累積 2 天以上就會出現走勢。</span></div>')

    all_dates = sorted({d for _k, _n, c in lines for d, _v in c}
                       | {d for d, _v in market})
    if len(all_dates) < 2:
        return ('<div class="empty">資料還在累積中，'
                '至少需要 2 天以上的快照才能畫出走勢。</div>')

    vals = [v for _k, _n, c in lines for _d, v in c] + [v for _d, v in market]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad

    W, H = 640, 190
    xi = {d: i for i, d in enumerate(all_dates)}
    n = len(all_dates)
    X = lambda d: (xi[d] / (n - 1)) * W
    Y = lambda v: (1 - (v - lo) / (hi - lo)) * H

    parts = [f'<line x1="0" y1="{Y(0):.1f}" x2="{W}" y2="{Y(0):.1f}" '
             f'stroke="var(--rule)" stroke-width="1" stroke-dasharray="2,3"/>']
    if market:
        p = "M " + " L ".join(f"{X(d):.1f},{Y(v):.1f}" for d, v in market)
        parts.append(f'<path d="{p}" fill="none" stroke="var(--ink-faint)" '
                     f'stroke-width="1.5" stroke-dasharray="4,3"/>')

    tints = ["#6E5228", "#A82A20", "#155C42", "#8A6A3B", "#454C55"]
    legend = []
    for i, (key, name, curve) in enumerate(lines):
        color = "var(--brass)" if str(key) == str(highlight_key) else tints[i % len(tints)]
        p = "M " + " L ".join(f"{X(d):.1f},{Y(v):.1f}" for d, v in curve)
        parts.append(f'<path d="{p}" fill="none" stroke="{color}" '
                     f'stroke-width="2"/>')
        legend.append(f'<span><i style="background:{color}"></i>{name} '
                      f'{curve[-1][1]:+.1f}%</span>')
    if market:
        legend.append(f'<span><i style="background:var(--ink-faint)"></i>'
                      f'大盤 {market[-1][1]:+.1f}%</span>')

    return f"""
<svg viewBox="0 0 {W} {H}" width="100%" height="{H}" preserveAspectRatio="none"
     style="display:block">{''.join(parts)}</svg>
<div class="legend" style="margin-top:8px">{''.join(legend)}
  <span style="color:var(--ink-faint);margin-left:auto">
    {all_dates[0].strftime('%m/%d')} – {all_dates[-1].strftime('%m/%d')}</span>
</div>"""


@app.route("/web/leaderboard", methods=["GET", "POST"])
@web_login_required
def web_leaderboard(uid):
    """
    全站績效排行榜。

    三個刻意的限制：
    ・預設不參加，要自己填暱稱加入——排行榜會把報酬率給別人看，
      跟「持股只用於你自己的分析」是兩件事，必須明確 opt-in。
    ・只顯示報酬率、持股檔數、加入天數，不顯示任何金額與持股內容。
    ・報酬率從加入那天起算，不用歷史成本，否則比的是誰入市早不是誰操作好。
    """
    msg = ""
    if request.method == "POST" and not valid_web_csrf():
        return respond_page("排行榜", '<div class="msg">安全驗證已過期，請重新整理後再送出。</div>', "leaderboard")
    if request.method == "POST":
        if request.form.get("action") == "leave":
            leave_leaderboard(uid)
            msg = "已退出排行榜。"
        else:
            ok, err = join_leaderboard(
                uid, request.form.get("nickname", ""),
                show_holdings=bool(request.form.get("show_holdings")))
            msg = "已加入排行榜。" if ok else (err or "加入失敗")

    if not wants_fragment():
        return render_loading_shell(
            "排行榜", "leaderboard",
            ["正在讀取成員名單…", "正在計算每人的報酬率…", "正在整理排名…"],
            note="報酬率以時間加權計算，加碼與贖回不影響結果。")

    me = get_leaderboard_member(uid)
    boards, (series_map, market) = build_leaderboard(top_n=20)
    rank_inputs = []
    for board_name in ("short", "long"):
        for current_rank, row in enumerate(boards.get(board_name, []), 1):
            rank_inputs.append((board_name, row.get("user_id"), current_rank))
    rank_status_map = get_rank_status_map(rank_inputs)
    view = request.args.get("board", "short")   # 預設短線：新人也馬上有得比
    is_short = view != "long"
    active_board = "short" if is_short else "long"
    active_key = "m30" if is_short else "ret"
    active_label = "短線｜近 30 天" if is_short else "長線｜加入後累計"

    # ── 參加／退出 ──
    if me:
        joined_txt = me["joined_on"].strftime("%Y/%m/%d") if me["joined_on"] else "—"
        chk = " checked" if me.get("show_holdings") else ""
        state = "公開中" if me.get("show_holdings") else "未公開"
        panel = f"""
<div class="callout">
  你以 <b>{safe_html_text(me['nickname'])}</b> 的身分參加中，起算日 {joined_txt}。
  持股內容：<b>{state}</b>
  <div class="sub" style="margin-top:8px">
    重新加入不會重設起算日——否則賠錢時退出再加入就能把負報酬洗掉。
  </div>
</div>
<form class="add" method="post">
  <h3>修改設定</h3>
  <div class="fields">
    <div><label>顯示暱稱</label>
      <input name="nickname" maxlength="12" value="{safe_html_text(me['nickname'])}" required></div>
  </div>
  <label class="opt" style="margin-top:10px">
    <input type="checkbox" name="show_holdings"{chk}>
    公開我的最大持股與最佳持股
  </label>
  <div class="sell-hint">
    勾選後其他人會看到你的<b>最大持股（代號與權重）</b>、
    <b>報酬最好的一檔</b>與<b>最大產業佔比</b>。<br>
    仍然不會顯示任何金額、股數或完整持股清單。不勾選就只顯示報酬率。
  </div>
  <button type="submit">儲存</button>
</form>
<form method="post" style="margin-top:12px"
      onsubmit="return confirm('退出後你的成績會從榜上移除。確定嗎？')">
  <input type="hidden" name="action" value="leave">
  <button class="del" type="submit">退出排行榜</button>
</form>"""
    else:
        panel = """
<form class="add" method="post">
  <h3>參加排行榜</h3>
  <div class="fields">
    <div><label>顯示暱稱（其他人看得到）</label>
      <input name="nickname" maxlength="12" placeholder="例如：阿軒" required></div>
  </div>
  <label class="opt" style="margin-top:10px">
    <input type="checkbox" name="show_holdings">
    順便公開我的最大持股與最佳持股（可不勾）
  </label>
  <button type="submit">加入</button>
  <div class="sell-hint">
    參加後其他成員會看到你的<b>暱稱、報酬率、最大回檔、持股檔數、加入天數</b>。<br>
    <b>不會</b>顯示任何金額、股數或完整持股清單。隨時可以退出。<br>
    報酬率從你加入那天起算，之前的損益不列入。
  </div>
</form>"""

    # ── 榜單 ──
    # 個人戰況仍沿用前 100 名摘要；榜單卡片本身則只批次查詢畫面上的前 20 名。
    my_rank = get_my_rank_summary(uid)

    def render_rank_status(status, compact=False):
        if status.get("rank") is None:
            return '<span class="rank-move muted">尚未上榜</span>'
        if status.get("delta") is None:
            return '<span class="rank-move muted">等待前一日</span>'
        delta = status["delta"]
        if delta > 0:
            text = f"↑ {delta} 名"
            if status.get("streak", 0) >= 2:
                text += f"・連續 {status['streak']} 次上升"
            return f'<span class="rank-move up">{text}</span>'
        if delta < 0:
            text = f"↓ {abs(delta)} 名"
            if status.get("streak", 0) >= 2:
                text += f"・連續 {status['streak']} 次下降"
            return f'<span class="rank-move down">{text}</span>'
        return '<span class="rank-move flat">— 無變化</span>'

    def render_my_rank_card(board_name, label):
        status = my_rank[board_name]
        if status.get("rank") is None:
            title = "尚未上榜"
            detail = "有足夠有效快照後會顯示你的排名。"
        elif status.get("delta") is None:
            title = f"#{status['rank']}"
            detail = "已建立目前排名，等待前一日快照比較。"
        else:
            title = f"#{status['rank']}　{render_rank_status(status)}"
            detail = f"昨日 #{status['previous']}"
        return f'''<div class="my-rank-card"><small>{label}</small><b>{title}</b><span>{detail}</span></div>'''

    def board_rows(rows, key, status_map):
        """用手機優先的簡潔卡片呈現榜單，明細只在使用者主動展開時讀取。"""
        if not rows:
            return ('<div class="empty">這個榜還沒有資料。<br><br>'
                    '<span style="font-size:12.5px">加入後累積 2 天以上的'
                    '每日快照就會出現。</span></div>')
        champion_svg = '''<svg viewBox="0 0 120 120" role="img" aria-label="冠軍獎盃" xmlns="http://www.w3.org/2000/svg">
  <defs><linearGradient id="cupGold" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#FFF3A7"/><stop offset=".32" stop-color="#E8AE32"/>
    <stop offset=".68" stop-color="#A86713"/><stop offset="1" stop-color="#F7D76A"/>
  </linearGradient><linearGradient id="cupShadow" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#8E5010"/><stop offset="1" stop-color="#D69B27"/>
  </linearGradient></defs>
  <path d="M31 23h58v18c0 18-12 31-29 34C43 72 31 59 31 41V23Z" fill="url(#cupGold)" stroke="#875010" stroke-width="2"/>
  <path d="M31 28H17c0 20 8 30 23 32M89 28h14c0 20-8 30-23 32" fill="none" stroke="#A96B18" stroke-width="7" stroke-linecap="round"/>
  <path d="M42 24c8 10 28 10 36 0" fill="none" stroke="#FFF1A0" stroke-width="3" opacity=".9"/>
  <path d="M60 75v15M40 94h40" stroke="#8A5215" stroke-width="7" stroke-linecap="round"/>
  <path d="M36 96h48v9H36z" fill="url(#cupShadow)" stroke="#875010" stroke-width="2"/>
  <path d="M60 9l3 7 8 1-6 5 2 8-7-4-7 4 2-8-6-5 8-1 3-7Z" fill="#F4C84E" stroke="#A66B16" stroke-width="1.5"/>
  <path d="M18 88c-8-12-7-25 1-36M102 88c8-12 7-25-1-36" fill="none" stroke="#B98222" stroke-width="2" opacity=".8"/>
  <path d="M20 76l-8-3M20 66l-8-5M22 56l-7-7M100 76l8-3M100 66l8-5M98 56l7-7" stroke="#D39A2C" stroke-width="2" stroke-linecap="round"/>
  <circle cx="24" cy="48" r="2" fill="#FFF3A7"/><circle cx="96" cy="48" r="2" fill="#FFF3A7"/>
</svg>'''
        out = []
        for i, r in enumerate(rows):
            mine = " rank-mine" if str(r.get("user_id")) == str(uid) else ""
            current_rank = i + 1
            rank = f"#{current_rank}"
            if current_rank == 1:
                tier_class = " rank-champion"
                honour = f'''<div class="rank-honour">
  <span class="rank-honour-icon">{champion_svg}</span>
  <div><b>冠軍席位</b><small>今日冠軍・目前第一名</small></div>
</div>'''
            elif current_rank == 2:
                tier_class = " rank-silver"
                honour = '''<div class="rank-tier"><span class="rank-tier-medal">🥈</span>
  <b>亞軍</b><small>第二名</small></div>'''
            elif current_rank == 3:
                tier_class = " rank-bronze"
                honour = '''<div class="rank-tier"><span class="rank-tier-medal">🥉</span>
  <b>季軍</b><small>第三名</small></div>'''
            else:
                tier_class = ""
                honour = ""
            main_v = r[key]
            cls = "up" if main_v >= 0 else "down"
            rank_state = status_map.get(
                ("short" if key == "m30" else "long", str(r.get("user_id")).strip()),
                _rank_status_from_previous(current_rank, []))
            movement = render_rank_status(rank_state)

            # 短線看近 30 天實際涵蓋天數；長線看加入後實際涵蓋天數。
            span = r["m30_days"] if key == "m30" else r["days"]
            span_html = (f'<span class="badge">樣本 {span} 天｜參考排名</span>'
                         if span < 10 else f'<span>樣本 {span} 天</span>')

            supporting = []
            if key == "m30" and r.get("ret") is not None:
                sc = "up" if r["ret"] >= 0 else "down"
                supporting.append(
                    f'<span><em>加入後</em> <span class="num {sc}">{r["ret"]:+.1f}%</span></span>')
            elif key == "ret" and r.get("m30") is not None:
                sc = "up" if r["m30"] >= 0 else "down"
                supporting.append(
                    f'<span><em>近30天</em> <span class="num {sc}">{r["m30"]:+.1f}%</span></span>')
            if r.get("excess") is not None:
                w = "贏" if r["excess"] >= 0 else "輸"
                mkt = r.get("mkt_ret")
                mkt_txt = f"（大盤 {mkt:+.1f}%）" if mkt is not None else ""
                supporting.append(
                    f'<span><em>vs 大盤</em> {w} {abs(r["excess"]):.1f}%{mkt_txt}</span>')
            if r.get("mdd") is not None:
                supporting.append(
                    f'<span><em>最大回檔</em> <span class="num">-{r["mdd"]:.1f}%</span></span>')
            supporting.append(f'<span><em>持股</em> {r["holdings"]} 檔</span>')

            d = r.get("detail")
            if d:
                detail_bits = []
                b_ = d["biggest"]
                detail_bits.append(
                    f'<span><em>最大持股</em> {b_["name"]}（{b_["code"]}）{b_["weight"]:.0f}%</span>')
                if d.get("best"):
                    bs = d["best"]
                    bcls = "up" if bs["ret"] >= 0 else "down"
                    detail_bits.append(
                        f'<span><em>最佳｜加入後報酬</em> {bs["name"]}（{bs["code"]}）'
                        f'<span class="num {bcls}">{bs["ret"]:+.1f}%</span></span>')
                if d.get("top_industry"):
                    nm, w2 = d["top_industry"]
                    detail_bits.append(f'<span><em>最大產業</em> {nm} {w2:.0f}%</span>')
                detail = (f'<details class="rank-detail"><summary>查看持股明細</summary>'
                          f'<div class="rank-detail-body">{"".join(detail_bits)}</div></details>')
            else:
                detail = '<span class="rank-private">持股明細未公開</span>'

            out.append(f"""
<div class="rank-card{tier_class}{mine}">
  {honour}
  <div class="rank-row-main">
    <span class="rank-number">{rank}</span>
    <span class="name">{safe_html_text(r['nickname'])}</span>
    <span class="rank-return num {cls}">{main_v:+.2f}%</span>
    <span class="rank-movement">{movement}</span>
  </div>
  <div class="rank-meta">{''.join(supporting)}<span>{span_html}</span></div>
  {detail}
</div>""")
            if current_rank == 1:
                out.append('<div class="rank-champion-prompt">下一個站上這裡的人，會是誰？</div>')
        return f'<div class="rows rank-rows">{"".join(out)}</div>'

    board = board_rows(boards[active_board], active_key, rank_status_map)
    active_status = my_rank[active_board]
    active_row = active_status.get("row")
    if active_row:
        active_value = active_row.get(active_key)
        active_days = active_row.get("m30_days") if is_short else active_row.get("days")
        active_days = active_days or 0
        active_cls = "up" if active_value is not None and active_value >= 0 else (
            "down" if active_value is not None else "flat")
        active_txt = (f"{active_value:+.2f}%" if active_value is not None else "—")
        previous_txt = (f"#{active_status['previous']}"
                        if active_status.get("previous") else "—")
        previous_note = (active_label if active_status.get("previous")
                         else "尚無前一日快照")
        excess = active_row.get("excess")
        if excess is None:
            vs_txt, vs_cls = "尚無資料", "flat"
        else:
            vs_txt = (f"贏 {abs(excess):.1f}%" if excess >= 0
                      else f"輸 {abs(excess):.1f}%")
            vs_cls = "up" if excess >= 0 else "down"
        rank_title = (f"#{active_status['rank']}"
                      if active_status.get("rank") else "尚未上榜")
        rank_move = (render_rank_status(active_status)
                     if active_status.get("delta") is not None
                     else '<span class="rank-move muted">尚無前一日快照</span>')
        sample_note = (f"樣本 {active_days} 天｜參考排名"
                       if active_days < 10 else f"樣本 {active_days} 天")
        situation_body = f'''<div class="rank-situation-grid">
  <div class="rank-situation-item"><small>目前排名</small><b>{rank_title}</b>
    <span class="rank-situation-sub">{rank_move}</span></div>
  <div class="rank-situation-item"><small>{"近 30 天" if is_short else "加入後"}</small>
    <b class="num {active_cls}">{active_txt}</b>
    <span class="rank-situation-sub">{sample_note}</span></div>
  <div class="rank-situation-item"><small>昨日排名</small><b>{previous_txt}</b>
    <span class="rank-situation-sub">{previous_note}</span></div>
  <div class="rank-situation-item"><small>相對大盤</small><b class="{vs_cls}">{vs_txt}</b>
    <span class="rank-situation-sub">{f"大盤 {active_row.get('mkt_ret'):+.1f}%" if active_row.get('mkt_ret') is not None else "尚無大盤資料"}</span></div>
</div>'''
    elif me:
        situation_body = '''<div class="rank-situation-empty">
          已加入排行榜，正在累積有效每日快照；資料不足時不先捏造排名或報酬。
        </div>'''
    else:
        situation_body = '''<div class="rank-situation-empty">
          你尚未加入排行榜。加入後會從加入日開始累積自己的排名與報酬曲線。
        </div>'''
    my_rank_html = f'''<section class="rank-situation">
  <div class="rank-situation-title"><h2>🏆 我的排名戰況</h2>
    <span class="rank-situation-badge">{active_label}</span></div>
  {situation_body}
</section>'''

    waiting_html = ""
    if boards["waiting"]:
        items = "".join(
            f'<div class="row"><div><span class="name">{safe_html_text(r["nickname"])}</span></div>'
            f'<div class="price flat">計算中</div>'
            f'<div class="meta"><span>需累積 2 天以上的每日快照</span></div></div>'
            for r in boards["waiting"])
        waiting_html = f"""
<div class="section-head"><h2>剛加入</h2>
  <span class="section-note">尚無足夠快照</span></div>
<div class="rows">{items}</div>"""

    tabs = f"""
<div class="tabs rank-tabs">
  <a href="/web/leaderboard?board=short"
     class="{'on' if is_short else ''}">短線　近30天</a>
  <a href="/web/leaderboard?board=long"
     class="{'' if is_short else 'on'}">長線　累計</a>
</div>
<div class="rank-switch-note">{
  '短線：所有人統一比較近 30 天；樣本少於 10 天會標示為參考排名。'
  if is_short else
  '長線：從各自加入日後累計；加入天數不同，請搭配樣本天數判讀。'}
</div>"""

    chart_keys = [str(r["user_id"]) for r in boards[active_board]][:5]
    my_curve_key = (str(uid) if me and str(uid) in series_map else None)
    chart_note = ("含我的曲線・前 4 名・大盤"
                  if my_curve_key else "前 5 名 vs 大盤")
    chart = render_leaderboard_chart(
        series_map, market, chart_keys, highlight_key=my_curve_key)

    settings_title = "修改排行榜設定" if me else "加入排行榜"
    settings_note = ("修改暱稱、持股公開範圍或退出排行榜"
                     if me else "從今天開始累積你的排名與報酬")
    settings_html = f'''<section class="leaderboard-settings">
  <details class="disclosure"><summary>{settings_title}　<span class="sub">{settings_note}</span></summary>
    {panel}
  </details>
</section>'''

    body = f"""
{f'<div class="msg">{msg}</div>' if msg else ''}
{my_rank_html}
{tabs}
<div class="rank-list-caption">
  <span>{len(boards['long'])} 位參加中・依報酬排序</span></div>
{board}
{waiting_html}

<div class="section-head"><h2>走勢比較</h2>
  <span class="section-note">{chart_note}</span></div>
<div class="callout" style="padding:14px 15px 8px">{chart}</div>

{settings_html}

<details class="disclosure leaderboard-method"><summary>這些數字怎麼來的</summary>
<div class="callout" style="margin-top:10px">
  <b>計算口徑與資料說明</b><br>
  <span style="font-size:12.5px;color:var(--ink-faint)">
  ・<b>短線榜</b>比近 30 天，所有人區間一致；<b>長線榜</b>比加入後的累計，
    加得早的人天然佔優，所以務必連天數一起看。兩榜是兩種不同的能力，
    不該塞進同一個排行。<br>
  ・報酬率用<b>時間加權</b>計算：加碼會讓市值變大但那不是賺來的，
    程式會把資金進出扣掉，所以加碼或贖回都不影響名次。<br>
  ・一律從<b>加入排行榜那天</b>起算。用歷史成本的話，
    輸入三年前買的股票就能直接屠榜。<br>
  ・<b>最大回檔</b>是報酬曲線從高點到低點的最大跌幅。只看報酬會獎勵冒險——
    重壓一檔賭對了就登頂，但那跟操作得好是兩件事。<br>
  ・<b>vs 大盤</b>是<b>超額報酬</b>——你的報酬減掉大盤同期報酬，
    括號裡是大盤自己的漲跌。例如「贏 7.2%（大盤 +5.2%）」代表
    你賺了 12.4%，其中 5.2% 是大盤帶上去的、7.2% 才是你自己做出來的。
    多頭時人人都賺，這個數字才看得出有沒有做對事情。<br>
  ・<b>持股資訊需本人勾選才會顯示</b>，預設不公開，而且只給最大持股、
    最佳持股與最大產業，不會有金額、股數或完整清單。<br>
  ・<b>數據由使用者自行輸入，未經驗證。</b>天數太少的會標「僅 N 天」——
    3 天賺 8% 跟 30 天賺 8% 的可信度完全不同。<br>
  ・排行榜是給自己一個參照，不是比賽。看別人的數字之前，
    先確認自己的操作有沒有理由。</span>
</div>
</details>"""
    return respond_page("排行榜", body, "leaderboard")


@app.route("/web/compare")
@web_login_required
def web_compare(uid):
    """
    個股比較。從 LINE 移過來的——並排比較天生是表格，
    LINE 純文字要用縮排硬排，四檔就爆版；網頁一列一個指標才看得清楚。

    預設帶入使用者的自選股，省得每次重打代號。
    """
    raw = request.args.get("codes", "")
    codes = [normalize_code(c) for c in re.findall(r"\d{4,6}[A-Za-z]?", raw)]
    codes = [c for c in dict.fromkeys(codes) if c][:4]

    if not wants_fragment():
        return render_loading_shell(
            "比較", "compare",
            ["正在讀取自選清單…", "正在抓報價與估值…", "正在整理對照表…"],
            note="最多可同時比較 4 檔。")

    watchlist = get_user_watchlist(uid)
    inst = fetch_institutional_data() or {}

    # 選擇區：自選股一鍵勾選，不必記代號
    picked = set(codes)
    chips = []
    for c in watchlist[:20]:
        nm = short_company_name(stock_display_name(c, inst))
        on = " on" if c in picked else ""
        rest = [x for x in codes if x != c] if c in picked else codes + [c]
        chips.append(f'<a class="tagchip{on}" '
                     f'href="/web/compare?codes={"+".join(rest[:4])}">{nm}</a>')

    form = f"""
<div class="section-head"><h2>選擇比較標的</h2>
  <span class="section-note">最多 4 檔</span></div>
{f'<div class="chips">{"".join(chips)}</div>' if chips else ''}
<form class="add" method="get" action="/web/compare">
  <div class="fields">
    <div><label>股票代號（空格分隔）</label>
      <input name="codes" value="{' '.join(codes)}"
             placeholder="2330 2454 6669" inputmode="numeric"></div>
  </div>
  <button type="submit">比較</button>
</form>"""

    if len(codes) < 2:
        return respond_page("比較", form + """
<div class="empty">選兩檔以上開始比較。<br><br>
<span style="font-size:12.5px">會並排列出營收成長、估值、籌碼與位階，
並標出每一項數字較優的那檔。</span></div>""", "compare")

    revenue = fetch_monthly_revenue() or {}
    valuation = fetch_valuation() or {}
    ind_map = get_industry_map() or {}
    streaks = get_consecutive_days_batch(codes)
    cum_map = get_cumulative_net_buy_for_codes(codes, days=10)
    price_map = get_realtime_stocks_bulk(codes)

    items, missing = [], []
    for code in codes:
        pr = price_map.get(code)
        if not pr:
            missing.append(code)
            continue
        val = valuation.get(code, {})
        rev = revenue.get(code, {})
        cum_lots, _bd = cum_map.get(code, (0, 0))
        ind = ind_map.get(code)
        items.append({
            "code": code,
            "name": short_company_name(stock_display_name(code, inst, pr["name"])),
            "industry": industry_name(ind) if ind else "未分類",
            "close": pr["close"], "pct": pr["pct"],
            "pe": val.get("pe"), "pb": val.get("pb"), "yield": val.get("yield"),
            "cum_yoy": rev.get("cum_yoy_pct"),
            "pos": pr.get("pos_vs_60d_high"), "vol_ratio": pr.get("vol_ratio"),
            "streak": streaks.get(code, 0), "cum_lots": cum_lots,
            "turnover": calc_turnover_billion(pr["close"], pr["volume"]),
        })

    if len(items) < 2:
        return respond_page("比較", form + f"""
<div class="empty">可比較的標的不足。<br>
查無行情：{'、'.join(missing)}</div>""", "compare")

    n = len(items)
    head = "".join(f'<th>{it["name"]}<span class="code">{it["code"]}</span></th>'
                   for it in items)

    def row(label, key, fmt, better="high", note=""):
        """
        一列一個指標。標出該項較優的那檔，但缺資料的不參與比較——
        不能因為沒資料就被當成最好或最差。
        better=None 代表這項沒有好壞之分，只列不標。
        """
        vals = [it.get(key) for it in items]
        best = None
        if better:
            valid = [(i, v) for i, v in enumerate(vals) if v is not None]
            if len(valid) >= 2:
                best = (max if better == "high" else min)(valid, key=lambda x: x[1])[0]
        cells = ""
        for i, v in enumerate(vals):
            cls = ' class="best"' if i == best else ""
            cells += f'<td{cls}>{fmt(v) if v is not None else "—"}</td>'
        return (f'<tr><th class="rk">{label}'
                f'{f"<span>{note}</span>" if note else ""}</th>{cells}</tr>')

    rows = [
        row("現價", "close", lambda v: f"{v:,.2f}", better=None),
        row("今日", "pct", lambda v: f"{v:+.2f}%", better=None),
        row("產業", "industry", lambda v: v, better=None),
        row("營收年增", "cum_yoy", lambda v: f"{v:+.1f}%", "high", "累計"),
        row("本益比", "pe", lambda v: f"{v:.1f}", "low"),
        row("股價淨值比", "pb", lambda v: f"{v:.2f}", "low"),
        row("殖利率", "yield", lambda v: f"{v:.2f}%", "high"),
        row("距60日高", "pos", lambda v: f"{v:+.1f}%", "high"),
        row("法人連買", "streak", lambda v: f"{v} 日", "high"),
        row("近10日買超", "cum_lots", lambda v: f"{v:+,}", "high", "張"),
        row("量能倍數", "vol_ratio", lambda v: f"{v:.2f}", "high", "vs 20日"),
        row("成交金額", "turnover", lambda v: f"{v:.1f}", "high", "億"),
    ]

    body = form + f"""
<div class="section-head"><h2>比較</h2>
  <span class="section-note">{n} 檔</span></div>
<div class="cmp-wrap">
<table class="cmp"><thead><tr><th class="rk"></th>{head}</tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</div>
{f'<div class="sub" style="margin-top:8px">查無行情：{"、".join(missing)}</div>' if missing else ''}
<div class="callout" style="margin-top:18px">
  <b>底色</b>代表該項數字較優，但<b>不代表整體較好</b>。<br>
  <span style="font-size:12.5px;color:var(--ink-faint)">
  哪一項重要取決於你要長抱還是短打——成長股本益比天生偏高、
  價值股營收成長天生偏低，把兩者放在同一個標準下比較會得到誤導性的結論。<br>
  缺資料的欄位不參與比較，不會被當成最好或最差。</span>
</div>"""
    return respond_page("比較", body, "compare")


@app.route("/web/settings", methods=["GET", "POST"])
@web_login_required
def web_settings(uid):
    msg = ""
    if request.method == "POST" and not valid_web_csrf():
        return respond_page("設定", '<div class="msg">安全驗證已過期，請重新整理後再送出。</div>', "settings")
    if request.method == "POST":
        updates = {}
        for k in ("loss_alert_pct", "position_alert_pct"):
            v = request.form.get(k)
            updates[k] = int(v) if v and v.isdigit() else None
        msg = "設定已儲存。" if update_profile(uid, updates) else "儲存失敗，請稍後再試或回報問題。"

    p = get_profile(uid)
    th = get_thresholds(p)

    def sel(key, current, options):
        return "".join(
            f'<option value="{v}"{" selected" if str(current) == str(v) else ""}>{t}</option>'
            for v, t in options)

    body = f"""
{f'<div class="msg">{msg}</div>' if msg else ''}
<form method="post">

<div class="section-head"><h2>提醒門檻</h2>
  <span class="section-note">組合分析頁會依此判斷</span></div>
<div class="fields" style="margin-bottom:6px">
  <div><label>帳面虧損達多少時提醒</label>
    <select name="loss_alert_pct">
      <option value="">使用預設（{th['loss']}%）</option>
      {sel('loss_alert_pct', p.get('loss_alert_pct'),
           [(10, '10%'), (15, '15%'), (20, '20%'), (30, '30%')])}
    </select></div>
  <div><label>單一持股佔比超過多少時提醒</label>
    <select name="position_alert_pct">
      <option value="">使用預設（{th['position']}%）</option>
      {sel('position_alert_pct', p.get('position_alert_pct'),
           [(20, '20%'), (25, '25%'), (30, '30%'), (40, '40%')])}
    </select></div>
</div>
{'<div class="hint">你表示尚無實際回檔經驗，預設門檻已自動調得較保守。</div>'
 if th['conservative'] else ''}

<button type="submit">儲存設定</button>
</form>

<div class="hint" style="margin-top:22px">
  <b>關於交易成本</b><br>
  手續費與證交稅改成在交易當下直接填寫——買進時填在「新增持股」的手續費欄，
  賣出時填在賣出面板裡。只有你看得到對帳單上的實際金額，
  用填的比用折扣推算準確得多。<br><br>
  持股頁的「淨損益」是未實現的估計值（假如現在賣掉大概拿到多少），
  以牌價 0.1425% 與證交稅試算；實際賣出時一律以你填的數字為準。
</div>

<div class="hint" style="margin-top:14px">
  想調整你的風險輪廓（資金年期、資產比重等問卷），
  請到<a href="/web/portfolio" style="color:var(--brass)">組合分析</a>頁最上方編輯。
</div>"""
    return render_page("設定", body, nav_active="settings")


@app.route("/web/more")
@web_login_required
def web_more(uid):
    """一般使用者的工具與說明中心。"""
    body = """
<div class="more-hero">
  <div class="eyebrow">台股 BOT</div>
  <h1>更多</h1>
  <p>把不需要每天查看的工具與說明，整理在這裡。</p>
</div>

<div class="more-group">
  <div class="more-group-title">分析工具</div>
  <a class="more-item" href="/web/trades"><span class="more-icon">▤</span><span><b>交易紀錄</b><small>查看已實現損益與交易統計</small></span><strong>›</strong></a>
  <a class="more-item" href="/web/compare"><span class="more-icon">⌕</span><span><b>股票比較</b><small>一次比較最多 4 檔股票</small></span><strong>›</strong></a>
</div>

<div class="more-group">
  <div class="more-group-title">我的設定</div>
  <a class="more-item" href="/web/portfolio#risk"><span class="more-icon">◌</span><span><b>風險輪廓</b><small>修改投資年期、資產配置與持有習慣</small></span><strong>›</strong></a>
  <a class="more-item" href="/web/settings"><span class="more-icon">⚙</span><span><b>提醒門檻</b><small>調整損失與持股集中度提醒</small></span><strong>›</strong></a>
</div>

<div class="more-group">
  <div class="more-group-title">說明</div>
  <a class="more-item" href="/web/leaderboard#rules"><span class="more-icon">?</span><span><b>排行榜規則</b><small>了解短線、長線與排名變化</small></span><strong>›</strong></a>
  <a class="more-item" href="/web/portfolio#sources"><span class="more-icon">◎</span><span><b>資料來源與使用說明</b><small>資料更新方式、隱私與免責聲明</small></span><strong>›</strong></a>
</div>

<div class="more-note">部分功能僅在其他入口或管理端使用，不會顯示於此處。</div>
"""
    return render_page("更多", body, nav_active="more")


# ============================================================
# 組合分析
# ============================================================
def pearson(xs, ys):
    """兩組報酬率的相關係數。長度不足或無波動時回傳 None，不硬算。"""
    n = min(len(xs), len(ys))
    if n < 10:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def daily_returns(closes):
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1]]


def avg_correlation(price_map):
    """組合內兩兩相關係數的平均。這是判斷『假分散』的核心指標。"""
    codes = [c for c, p in price_map.items() if p and p.get("closes")]
    # 只取近 60 筆：持股頁可能抓了一年的序列，但相關係數看的是
    # 「最近的連動程度」，用一整年會把早就改變的關係也算進來。
    rets = {c: daily_returns(price_map[c]["closes"][-61:]) for c in codes}
    vals = []
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            r = pearson(rets[codes[i]], rets[codes[j]])
            if r is not None:
                vals.append(r)
    return (sum(vals) / len(vals)) if vals else None


def effective_holdings(n, avg_corr):
    """
    把「檔數 + 平均相關係數」換算成等效的獨立持股數。
    公式取自等權重組合的變異數：n / (1 + (n-1)·ρ)。
    八檔高度連動的電子股，等效可能只有兩檔。
    """
    if n <= 1 or avg_corr is None:
        return None
    denom = 1 + (n - 1) * max(avg_corr, 0)
    return n / denom if denom > 0 else None


def build_profile_alerts(profile, holdings, top, ordered_industries, th):
    """
    把問卷答案轉成「對你而言」的提醒。
    同樣一個 68% 集中度，對十年不動的人和明年要用錢的人意義完全不同——
    這裡不改變事實，只改變該提醒什麼、以及用什麼標準衡量。
    """
    out = []
    if not profile:
        return out

    horizon = profile.get("horizon")
    share = profile.get("asset_share")
    income = profile.get("income_type")
    freq = profile.get("check_frequency")
    hold = profile.get("holding_period")
    others = profile.get("other_assets")
    top_w = top["weight"] if top else 0
    # 判斷「集中風險」時要排除ETF：ETF本身就是一籃子股票，
    # 它是分散的來源而非集中的來源，把它當成單一族群會得出相反的結論。
    # 主動式ETF例外——它由經理人選股，不是一籃子部位，仍要算進集中度判斷。
    real_inds = [x for x in (ordered_industries or [])
                 if not x[0].startswith("ETF") and x[0] != "未分類"]
    top_ind = real_inds[0] if real_inds else None
    etf_weight = sum(w for name, w in (ordered_industries or [])
                     if name.startswith("ETF"))

    # 短年期 × 高集中：時間不夠攤平波動
    if horizon in ("1 年內", "1–3 年") and top_ind and top_ind[1] >= 30:
        out.append(("資金年期",
                    f"你標記這筆錢{horizon}可能會用到，但{top_ind[0]}佔 "
                    f"{top_ind[1]:.0f}%。族群回檔時，這個年期未必來得及等到回升。"))

    # 長年期：反過來說明波動不必然是問題
    if horizon == "10 年以上" and top_ind and top_ind[1] >= 35:
        out.append(("資金年期",
                    f"集中在{top_ind[0]}（{top_ind[1]:.0f}%）波動會較大，"
                    f"但你標記這筆錢 10 年以上不會動用，時間本身是可用的緩衝。"))

    # 身家比重放大集中度的實質風險
    if share == "幾乎全部" and top_w >= th["position"] * 0.7:
        out.append(("資產比重",
                    f"這筆投資是你幾乎全部的可動用資產，"
                    f"而單一持股已佔 {top_w:.0f}%。單一事件的影響會直接反映在生活上。"))
    elif share == "不到四分之一" and top_w >= th["position"] * 0.7:
        out.append(("資產比重",
                    f"單一持股佔組合 {top_w:.0f}%，但這筆投資佔你可動用資產不到四分之一，"
                    f"換算後的實質曝險相對有限。"))

    # 收入穩定度影響「被迫賣出」的風險
    if income in ("接案或營業收入", "目前無固定收入") and top_ind and top_ind[1] >= 35:
        out.append(("收入穩定度",
                    f"你的收入並非固定薪資，而組合有 {top_ind[1]:.0f}% 集中在單一族群。"
                    f"若收入與股市同時轉弱，可能被迫在低點賣出。"))

    # 只有台股 → 集中度沒有其他資產分散
    if others == "幾乎只有台股" and top_ind and top_ind[1] >= 35:
        out.append(("資產分散",
                    f"你的部位幾乎只有台股，{top_ind[0]}又佔 {top_ind[1]:.0f}%，"
                    f"整體風險沒有其他資產類別可以分攤。"))

    # 自述年期與實際持有習慣矛盾
    if horizon in ("3–10 年", "10 年以上") and hold in ("幾天", "幾週"):
        out.append(("習慣落差",
                    f"你標記這筆錢{horizon}才會用到，但平均持股只抱{hold}。"
                    f"長期規劃與實際操作之間有落差，值得留意是哪一邊需要調整。"))

    # 「曾經在虧損時加碼」是問卷裡最有預測力的一題，
    # 這裡用實際的分批進場紀錄去驗證他現在是不是又在做同一件事。
    if profile.get("drawdown_experience") == "曾經在虧損時加碼":
        averaging = []
        for h in holdings:
            lots = h.get("lots") or []
            if len(lots) < 2:
                continue
            ordered_lots = sorted(lots, key=lambda l: (l["bought_on"] or date.min))
            # 後買的成本比先買的低，且目前仍虧損 → 正在往下攤平
            if (ordered_lots[-1]["cost"] < ordered_lots[0]["cost"]
                    and h.get("pl") is not None and h["pl"] < 0):
                averaging.append(h["name"])
        if averaging:
            out.append(("加碼行為",
                        f"你曾表示在虧損時加碼過。目前{'、'.join(averaging)}"
                        f"正是這個型態：後買的成本低於先買，且仍在虧損。"))
        else:
            out.append(("加碼行為",
                        "你曾表示在虧損時加碼過。目前的持股中沒有出現"
                        "「越跌越買且仍虧損」的型態。"))

    # 資金年期未定：說明為何沒有年期相關提醒
    if horizon == "沒有特定用途":
        out.append(("資金年期",
                    "你未指定這筆錢的使用時點，因此沒有年期相關的提醒。"
                    "若有明確用途時點，集中度的判讀會不一樣。"))

    # 被動ETF佔比高：這是分散，不是集中，該說明而不是警示
    if etf_weight >= 25:
        out.append(("ETF 佔比",
                    f"被動ETF佔組合 {etf_weight:.0f}%。這部分本身已分散於一籃子標的，"
                    f"因此上述集中度是以個股與主動式ETF計算，未把被動ETF視為單一族群。"))

    # 看盤頻率高 × 已有虧損部位
    losers = [h for h in holdings if h["pl"] is not None and h["pl"] < 0]
    if freq == "一天多次" and len(losers) >= 2:
        out.append(("看盤頻率",
                    f"你每天多次查看帳戶，目前有 {len(losers)} 檔在虧損。"
                    f"高頻檢視在波動期容易放大情緒，判斷前先確認依據有沒有變。"))

    return out


def render_portfolio_fast_summary(uid):
    """今日首頁第一段：先顯示既有快照、事件與排名，並明確提示完整分析仍在整合。"""
    fast_started = time.monotonic()
    snapshot_date = _premarket_display_date(taiwan_today())
    context = _get_daily_home_context(uid, snapshot_date)
    context_done = time.monotonic()
    snapshot = context.get("snapshot") or {}
    timeline = context.get("timeline") or {}
    events = (timeline.get("new", []) + timeline.get("ongoing", []))[:3]
    signal_state = _daily_signal_state(snapshot, timeline)

    if events:
        event_html = "".join(
            f'''<div class="daily-fast-event">
              <span class="daily-fast-number">{idx}</span>
              <div><b>{html.escape(str(event.get("title", "")))}</b>
              <div class="daily-fast-detail">{html.escape(str(event.get("detail", "")))}</div></div>
            </div>'''
            for idx, event in enumerate(events, 1)
        )
    else:
        icon = "🕘" if signal_state["kind"] == "not_updated" else (
            "📌" if signal_state["kind"] == "baseline" else "😴")
        event_html = (f'<div class="daily-fast-empty"><b>{icon} '
                      f'{html.escape(signal_state["title"])}</b><br>'
                      f'<span>{html.escape(signal_state["detail"])}</span></div>')

    market = snapshot.get("market") or {}
    market_items = [("大盤", market.get("taiex_pct")),
                    ("道瓊", market.get("^DJI_pct")),
                    ("那斯達克", market.get("^IXIC_pct")),
                    ("費城半導體", market.get("^SOX_pct"))]
    market_html = "".join(
        f'<span>{label}<b>{fmt_pct(value) if value is not None else "資料尚未更新"}</b></span>'
        for label, value in market_items
    )

    rank_status = get_fast_rank_summary(uid)
    print("⏱️ 今日首頁 fast：快照＋事件 %.0fms、排名 %.0fms、合計 %.0fms" % (
        (context_done - fast_started) * 1000,
        (time.monotonic() - context_done) * 1000,
        (time.monotonic() - fast_started) * 1000))
    rank_html = []
    for board in ("short", "long"):
        status = rank_status[board]
        if status.get("rank") is None:
            value = "尚未上榜"
            note = "累積有效快照後開始顯示"
        elif status.get("delta") is None:
            value = f'#{status["rank"]}'
            note = "等待前一日排名快照"
        elif status.get("direction") == "up":
            value = f'#{status["rank"]}　<span class="up">↑ {status["delta"]} 名</span>'
            note = f'昨日 #{status["previous"]}'
        elif status.get("direction") == "down":
            value = f'#{status["rank"]}　<span class="down">↓ {abs(status["delta"])} 名</span>'
            note = f'昨日 #{status["previous"]}'
        else:
            value = f'#{status["rank"]}　<span class="flat">— 無變化</span>'
            note = f'昨日 #{status["previous"]}'
        rank_html.append(
            f'<div class="daily-fast-rank"><small>{html.escape(str(status["label"]))}</small>'
            f'<b>{value}</b><span>{note}</span></div>')

    return f'''<style>
.daily-fast-sync{{display:flex;gap:11px;align-items:flex-start;background:#F5F0E5;border:1px solid #D9C9A7;border-left:4px solid var(--brass);border-radius:12px;padding:14px 15px;margin:-4px 0 14px;box-shadow:0 3px 12px rgba(35,39,35,.05)}}.daily-fast-sync-dot{{width:10px;height:10px;margin-top:5px;border-radius:50%;background:var(--brass);box-shadow:0 0 0 4px rgba(139,105,52,.12);flex:none}}.daily-fast-sync b{{display:block;color:var(--ink);font-size:15px;line-height:1.35}}.daily-fast-sync-copy span{{display:block;margin-top:4px;color:var(--ink-soft);font-size:12px;line-height:1.65}}.daily-fast-hero{{background:linear-gradient(135deg,#f4f0e7,#e7ece8);padding:22px 18px 18px;margin:-8px -2px 14px;border-bottom:1px solid #d7d4ca}}.daily-fast-hero .eyebrow{{letter-spacing:.14em;color:var(--brass);font-size:11px}}.daily-fast-hero h1{{font-size:26px;line-height:1.25;margin:8px 0 14px}}.daily-fast-market{{display:flex;gap:8px;flex-wrap:wrap}}.daily-fast-market span{{background:rgba(255,255,255,.72);padding:8px 10px;border-radius:8px;font-size:12px}}.daily-fast-market b{{display:block;font-size:16px;margin-top:3px}}.daily-fast-card{{background:#fff;border:1px solid #e3e2dc;border-radius:12px;padding:15px;margin:12px 0;box-shadow:0 3px 14px rgba(35,39,35,.05)}}.daily-fast-title{{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:7px}}.daily-fast-title h2{{margin:0;font-size:19px}}.daily-fast-event{{display:flex;gap:10px;padding:11px 0;border-top:1px solid #eee}}.daily-fast-number{{background:var(--brass);color:#fff;border-radius:50%;width:22px;height:22px;text-align:center;line-height:22px;flex:none;font-size:12px}}.daily-fast-detail{{font-size:12.5px;color:var(--ink-soft);margin-top:3px}}.daily-fast-empty{{padding:11px 0;color:var(--ink-soft);font-size:13px}}.daily-fast-empty span{{font-size:12px}}.daily-fast-ranks{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}}.daily-fast-rank{{background:#f5f5f1;border-radius:8px;padding:11px}}.daily-fast-rank small,.daily-fast-rank>span{{display:block;color:var(--ink-soft);font-size:11px}}.daily-fast-rank b{{display:block;font-size:18px;margin:4px 0}}@media(max-width:640px){{.daily-fast-ranks{{grid-template-columns:1fr}}}}
</style><section class="daily-fast-sync" aria-live="polite">
  <span class="daily-fast-sync-dot" aria-hidden="true"></span>
  <div class="daily-fast-sync-copy"><b>系統正在跑・正在整合完整首頁</b>
    <span>這是先行摘要，不是完整首頁；即時持股、損益、貢獻／拖累與今日判讀完成後會自動補上。</span>
  </div>
</section>
<section class="daily-fast-hero">
  <div class="eyebrow">TODAY · {snapshot_date.strftime('%Y / %m / %d')}</div>
  <h1>今天先看最重要的變化</h1>
  <div class="daily-fast-market">{market_html}</div>
</section>
<section class="daily-fast-card"><div class="daily-fast-title"><h2>🔥 今日值得注意</h2><a href="/web/premarket" style="color:var(--brass);font-size:12px">查看完整變化 →</a></div>{event_html}</section>
<section class="daily-fast-card"><div class="daily-fast-title"><h2>🏆 我的排名變化</h2><a href="/web/leaderboard" style="color:var(--brass);font-size:12px">查看排行榜 →</a></div><div class="daily-fast-ranks">{"".join(rank_html)}</div></section>'''


def render_daily_home_top(uid, holdings, total_value, total_cost, price_map, pl_total, taiex=None):
    # 新版首頁上半部：先講今天，再提供完整分析入口。
    calendar_today = taiwan_today()
    display_date = _premarket_display_date(calendar_today)
    context = _get_daily_home_context(uid, display_date)
    display_snapshot = context.get("snapshot") or {}
    timeline = context.get("timeline") or {}
    signal_state = _daily_signal_state(display_snapshot, timeline)
    events = (timeline.get("new", []) + timeline.get("ongoing", []))[:3]
    taiex = (fetch_taiex_summary() if taiex is None else taiex) or {}
    market_pct = None
    for key in ("pct", "change_pct", "percent"):
        if taiex.get(key) is not None:
            try:
                market_pct = float(taiex[key])
            except (TypeError, ValueError):
                pass
            break
    def holding_day_pct(holding):
        try:
            value = (holding.get("price") or {}).get("pct")
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    holding_changes = [(h, holding_day_pct(h)) for h in holdings]
    valid_changes = [(h, pct) for h, pct in holding_changes if pct is not None]
    portfolio_pct = (sum(h["weight"] * pct for h, pct in valid_changes) / 100
                     if valid_changes else None)
    relative = portfolio_pct - market_pct if portfolio_pct is not None and market_pct is not None else None

    # 「最大貢獻／最大拖累」看的是對組合的實際影響，不是單看個股漲跌幅。
    # 例如權重 40% 下跌 2%，通常比權重 1% 下跌 10% 更拖累整體組合。
    gain_entry = max(
        ((h, pct, h["weight"] * pct / 100) for h, pct in valid_changes if pct > 0),
        key=lambda item: item[2], default=None)
    loss_entry = min(
        ((h, pct, h["weight"] * pct / 100) for h, pct in valid_changes if pct < 0),
        key=lambda item: item[2], default=None)
    biggest_gain = gain_entry[0] if gain_entry else None
    biggest_gain_pct = gain_entry[1] if gain_entry else None
    biggest_gain_contribution = gain_entry[2] if gain_entry else None
    biggest_loss = loss_entry[0] if loss_entry else None
    biggest_loss_pct = loss_entry[1] if loss_entry else None
    biggest_loss_contribution = loss_entry[2] if loss_entry else None
    positive_entries = sorted(
        ((h, pct, h["weight"] * pct / 100) for h, pct in valid_changes if pct > 0),
        key=lambda item: item[2], reverse=True)[:5]
    negative_entries = sorted(
        ((h, pct, h["weight"] * pct / 100) for h, pct in valid_changes if pct < 0),
        key=lambda item: item[2])[:5]
    # 首頁只需要顯示自己的排名與升降，不需要為此阻塞整個完整排行榜重算。
    # 使用最近兩次已保存的收盤快照；排行榜頁本身仍保留完整即時計算。
    rank_started = time.monotonic()
    rank_status = get_fast_rank_summary(uid)
    print("⏱️ 今日完整頁：排名摘要 %.0fms" % ((time.monotonic() - rank_started) * 1000))

    def timeline_rows(items, status_label, status_class, start=1):
        rows = []
        for idx, event in enumerate(items, start):
            level = html.escape(event.get("severity", "B"))
            rows.append(f'''<div class="daily-event timeline-{status_class} level-{level}">
              <span class="event-status">{status_label}</span>
              <div><b>{html.escape(event.get("title", ""))}</b>
              <div class="event-detail">{html.escape(event.get("detail", ""))}</div></div>
            </div>''')
        return "".join(rows)

    timeline_html = timeline_rows(timeline["new"], "新", "new")
    timeline_html += timeline_rows(timeline["ongoing"], "續", "ongoing", len(timeline["new"]) + 1)
    if timeline["resolved"]:
        timeline_html += '<div class="timeline-divider">✓ 昨日事件已解除</div>'
        timeline_html += timeline_rows(timeline["resolved"][:2], "解", "resolved")

    if timeline_html:
        events_html = timeline_html
    else:
        state_icon = "🕘" if signal_state["kind"] == "not_updated" else ("📌" if signal_state["kind"] == "baseline" else "😴")
        events_html = f'''<div class="daily-empty">
      <b>{state_icon} {html.escape(signal_state["title"])}</b><br><span>{html.escape(signal_state["detail"])}</span>
    </div>'''

    def rank_line(board):
        s = rank_status[board]
        if s["rank"] is None:
            return f'''<div class="rank-mini"><span>{s["label"]}</span><b>尚未上榜</b><small>累積有效快照後開始顯示</small></div>'''
        if s["delta"] is None:
            return f'''<div class="rank-mini"><span>{s["label"]}</span><b>#{s["rank"]}</b><small>收盤快照後更新排名變化</small></div>'''
        previous_label = "前次收盤"
        if s["direction"] == "up":
            movement = f'<em class="up">↑ {s["delta"]} 名</em>'
            streak = f"・連續 {s['streak']} 次上升" if s["streak"] >= 2 else ""
        elif s["direction"] == "down":
            movement = f'<em class="down">↓ {abs(s["delta"])} 名</em>'
            streak = f"・連續 {s['streak']} 次下降" if s["streak"] >= 2 else ""
        else:
            movement, streak = '<em class="flat">— 無變化</em>', ""
        return f'''<div class="rank-mini"><span>{s["label"]}</span><b>#{s["rank"]} {movement}</b><small>{previous_label} #{s["previous"]}{streak}</small></div>'''

    gain_html = (
        f"{html.escape(str(biggest_gain['name']))} {fmt_pct(biggest_gain_pct)}"
        f"<small class=\"contribution-note\">組合 +{biggest_gain_contribution:.2f} 個百分點</small>"
        if biggest_gain and biggest_gain_pct is not None and biggest_gain_contribution is not None
        else "—")
    loss_html = (
        f"{html.escape(str(biggest_loss['name']))} {fmt_pct(biggest_loss_pct)}"
        f"<small class=\"contribution-note\">組合 {biggest_loss_contribution:.2f} 個百分點</small>"
        if biggest_loss and biggest_loss_pct is not None and biggest_loss_contribution is not None
        else "—")

    # 第一屏只放一個由真實資料決定的「今天先看這裡」，不自行生成訊號。
    # 優先順序：盤前事件 > 持股下跌 > 持股上漲 > 排名變化 > 無重大提醒。
    focus_href, focus_cta = "/web/premarket", "查看今日變化"
    focus_class = "focus-market"
    if events:
        focus_kicker = "市場有新變化"
        focus_title = str(events[0].get("title") or "今日有新的市場變化")
        focus_detail = (f"今日已偵測 {len(events)} 個變化，先看優先級最高的事件。")
    else:
        loss_pct = biggest_loss_pct
        gain_pct = biggest_gain_pct
        if biggest_loss and loss_pct is not None and biggest_loss_contribution is not None:
            focus_kicker = "先看你的持股"
            focus_title = f"{html.escape(str(biggest_loss['name']))} 今日 {loss_pct:+.2f}%"
            focus_detail = (f"這檔持股對組合拖累 {abs(biggest_loss_contribution):.2f} 個百分點，"
                            "查看完整持股與提醒。")
            focus_href, focus_cta, focus_class = "/web/positions", "查看持股", "focus-down"
        elif biggest_gain and gain_pct is not None and biggest_gain_contribution is not None:
            focus_kicker = "你的持股有變化"
            focus_title = f"{html.escape(str(biggest_gain['name']))} 今日 {gain_pct:+.2f}%"
            focus_detail = (f"這檔持股對組合貢獻 +{biggest_gain_contribution:.2f} 個百分點，"
                            "查看組合貢獻與完整資料。")
            focus_href, focus_cta, focus_class = "/web/positions", "查看持股", "focus-up"
        else:
            moved = next((rank_status[b] for b in ("short", "long")
                          if rank_status[b].get("direction") in ("up", "down")), None)
            if moved:
                focus_kicker = "你的排行榜有變化"
                direction_text = "上升" if moved.get("direction") == "up" else "下降"
                focus_title = f"{moved['label']}排名{direction_text}至 #{moved['rank']}"
                focus_detail = f"昨日 #{moved['previous']}，查看完整排行榜戰況。"
                focus_href, focus_cta, focus_class = "/web/leaderboard", "查看排行榜", "focus-rank"
            else:
                focus_kicker = "今天先確認狀態"
                focus_title = "今天沒有新的重大提醒"
                focus_detail = "市場與你的持股目前沒有符合提醒規則的新變化。"
                focus_href, focus_cta, focus_class = "/web/positions", "查看持股", "focus-quiet"

    focus_html = f'''<section class="daily-focus {focus_class}">
  <div class="daily-focus-head"><span>{html.escape(focus_kicker)}</span><small>今日焦點</small></div>
  <h2>{html.escape(focus_title)}</h2>
  <p>{html.escape(focus_detail)}</p>
  <a href="{focus_href}">{html.escape(focus_cta)} →</a>
</section>'''
    market_text = fmt_pct(market_pct) if market_pct is not None else "資料暫缺"
    portfolio_text = fmt_pct(portfolio_pct) if portfolio_pct is not None else fmt_pct(pl_total)
    relative_text = fmt_pct(relative) if relative is not None else "—"

    # 只解釋「組合相對大盤的差異」，不對大盤漲跌原因做無資料推測。
    if relative is None:
        interpretation = "今日行情資料尚不完整，暫不做相對判讀。"
    elif relative <= -0.05 and biggest_loss and biggest_loss_contribution is not None:
        interpretation = (f"今日組合落後大盤 {abs(relative):.2f} 個百分點，主要拖累來自 "
                          f"{biggest_loss['name']}（組合 {biggest_loss_contribution:.2f} 個百分點）。")
    elif relative >= 0.05 and biggest_gain and biggest_gain_contribution is not None:
        interpretation = (f"今日組合領先大盤 {relative:.2f} 個百分點，主要貢獻來自 "
                          f"{biggest_gain['name']}（組合 +{biggest_gain_contribution:.2f} 個百分點）。")
    elif abs(relative) < 0.05:
        interpretation = "今日組合與大盤表現接近，暫無明顯相對差異。"
    elif relative < 0 and biggest_loss:
        interpretation = f"今日組合略落後大盤，主要拖累來自 {biggest_loss['name']}。"
    elif relative > 0 and biggest_gain:
        interpretation = f"今日組合略領先大盤，主要貢獻來自 {biggest_gain['name']}。"
    else:
        interpretation = "今日組合與大盤已有差異，但目前缺少足夠個股資料說明來源。"
    home_judgement_html = f'''<section class="daily-card home-judgement-card">
  <div class="daily-section-title"><h2>今日判讀</h2><span>依目前資料</span></div>
  <div class="judgement-row"><span>上漲／下跌持股</span><b>{sum(1 for _h, p in valid_changes if p > 0)}／{sum(1 for _h, p in valid_changes if p < 0)} 檔</b></div>
  <div class="judgement-row"><span>組合相對大盤</span><b>{fmt_pct(relative) if relative is not None else '資料不足'}</b></div>
  <div class="judgement-row"><span>主要貢獻／拖累</span><b>{'已計算' if positive_entries or negative_entries else '行情資料不足'}</b></div>
  <p class="home-judgement-copy">{html.escape(interpretation)}</p>
</section>'''

    def contribution_detail_rows(entries, value_class):
        if not entries:
            return '<div class="impact-empty">目前沒有可用的行情資料。</div>'
        rows = []
        for idx, (holding, pct, contribution) in enumerate(entries, 1):
            direction = "增加" if contribution > 0 else "減少"
            change_word = "上漲" if pct > 0 else "下跌"
            rows.append(
                f'''<div class="impact-detail-row">
  <span class="impact-rank">{idx}</span>
  <div class="impact-detail-name"><b>{html.escape(str(holding['name']))}</b>
    <small>今天{change_word} {abs(pct):.2f}%　佔你的組合 {holding['weight']:.1f}%</small></div>
  <strong class="{value_class}">{direction}約 {abs(contribution):.2f}%</strong>
</div>''')
        return ''.join(rows)

    positive_lead = positive_entries[0] if positive_entries else None
    negative_lead = negative_entries[0] if negative_entries else None
    if positive_lead and negative_lead:
        impact_sentence = (f"今天主要是 {negative_lead[0]['name']} 拉低組合，"
                           f"{positive_lead[0]['name']} 幫忙抵銷部分跌幅。")
    elif negative_lead:
        impact_sentence = f"今天組合主要受到 {negative_lead[0]['name']} 影響而下跌。"
    elif positive_lead:
        impact_sentence = f"今天組合主要受到 {positive_lead[0]['name']} 支撐。"
    else:
        impact_sentence = "目前沒有足夠行情資料判斷是哪幾檔影響組合。"

    def impact_lead_card(entry, title, value_class):
        if not entry:
            return f'''<div class="impact-lead impact-muted"><small>{title}</small><b>目前沒有資料</b></div>'''
        holding, pct, contribution = entry
        amount_text = f"增加約 {abs(contribution):.2f}%" if contribution > 0 else f"減少約 {abs(contribution):.2f}%"
        change_word = "上漲" if pct > 0 else "下跌"
        return f'''<div class="impact-lead {value_class}">
  <small>{title}</small><h3>{html.escape(str(holding['name']))}</h3>
  <p>今天{change_word} {abs(pct):.2f}%・佔你的組合 {holding['weight']:.1f}%</p>
  <strong>對整體組合{amount_text}</strong>
</div>'''

    positive_count = len(positive_entries)
    negative_count = len(negative_entries)
    contribution_html = f'''<section class="daily-card contribution-card">
  <div class="daily-section-title"><h2>今天誰影響了你的組合？</h2><span>先看重點</span></div>
  <p class="impact-sentence">{html.escape(impact_sentence)}</p>
  <div class="impact-leads">
    {impact_lead_card(positive_lead, "幫你撐住組合", "impact-up")}
    {impact_lead_card(negative_lead, "主要拖累組合", "impact-down")}
  </div>
  <details class="impact-details"><summary>查看正貢獻明細（{positive_count} 檔）</summary>
    {contribution_detail_rows(positive_entries, 'up')}
  </details>
  <details class="impact-details"><summary>查看負貢獻明細（{negative_count} 檔）</summary>
    {contribution_detail_rows(negative_entries, 'down')}
  </details>
  <div class="contribution-footnote">「增加／減少」是依你的持股比例換算出的組合影響，不是個股報酬率，也不代表買賣建議。</div>
</section>'''

    if display_date != calendar_today and display_snapshot.get("source_date"):
        hero_eyebrow = (f"最近交易日 {display_snapshot['source_date']}　·　"
                        f"下次盤前 {display_snapshot.get('briefing_date') or display_date}")
    else:
        hero_eyebrow = f"TODAY · {display_date.strftime('%Y / %m / %d')}"

    return f'''<style>
.daily-complete-sync{{display:flex;gap:11px;align-items:flex-start;background:#F5F0E5;border:1px solid #D9C9A7;border-left:4px solid var(--brass);border-radius:12px;padding:13px 15px;margin:-4px 0 14px;box-shadow:0 3px 12px rgba(35,39,35,.05)}}.daily-complete-sync-dot{{width:10px;height:10px;margin-top:5px;border-radius:50%;background:#087A4B;box-shadow:0 0 0 4px rgba(8,122,75,.12);flex:none}}.daily-complete-sync b{{display:block;color:var(--ink);font-size:15px;line-height:1.35}}.daily-complete-sync span{{display:block;margin-top:4px;color:var(--ink-soft);font-size:12px;line-height:1.6}}.daily-hero{{background:linear-gradient(135deg,#f4f0e7,#e7ece8);padding:26px 24px 22px;margin:-8px -2px 18px;border-bottom:1px solid #d7d4ca}}.daily-hero .eyebrow{{letter-spacing:.16em;color:var(--brass);font-size:12px}}.daily-hero h1{{font-size:30px;line-height:1.2;margin:10px 0 18px}}.market-strip{{display:flex;gap:12px;flex-wrap:wrap}}.market-strip span{{background:rgba(255,255,255,.7);padding:9px 11px;border-radius:8px;font-size:13px}}.market-strip b{{display:block;font-size:18px;margin-top:3px}}.daily-card{{background:#fff;border:1px solid #e3e2dc;border-radius:12px;padding:18px;margin:14px 0;box-shadow:0 3px 14px rgba(35,39,35,.05)}}.attention-card{{border-left:4px solid var(--brass)}}.daily-section-title{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}}.daily-section-title h2{{margin:0;font-size:20px}}.daily-section-title a{{font-size:13px;color:var(--brass)}}.daily-event{{display:flex;gap:11px;padding:12px 0;border-top:1px solid #eee}}.event-number{{background:var(--brass);color:#fff;border-radius:50%;width:24px;height:24px;text-align:center;line-height:24px;flex:none}}.event-status{{min-width:28px;height:22px;padding:2px 5px;border-radius:7px;text-align:center;font-size:11px;font-weight:700;line-height:18px;flex:none;background:#eee;color:var(--ink-soft)}}.timeline-new .event-status{{background:#FCE9E6;color:var(--up)}}.timeline-ongoing .event-status{{background:#F3EEE1;color:var(--brass)}}.timeline-resolved .event-status{{background:#E8F2EA;color:var(--down)}}.timeline-divider{{margin:14px 0 0;padding-top:12px;border-top:1px solid #eee;color:var(--ink-soft);font-size:12px;font-weight:600}}.event-detail{{font-size:13px;color:var(--ink-soft);margin-top:4px}}.daily-empty{{padding:16px 0;color:var(--ink-soft)}}.daily-empty span{{font-size:13px}}.portfolio-highlights{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}.portfolio-highlights>div{{background:#f5f5f1;padding:12px;border-radius:8px}}.portfolio-highlights small{{display:block;color:var(--ink-soft);font-size:12px}}.portfolio-highlights b{{display:block;margin-top:6px;font-size:17px}}.positive,.up{{color:var(--up)}}.negative,.down{{color:var(--down)}}.flat{{color:var(--ink-soft)}}.rank-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}}.rank-mini{{background:#f5f5f1;border-radius:8px;padding:13px}}.rank-mini span,.rank-mini small{{display:block;color:var(--ink-soft);font-size:12px}}.rank-mini b{{display:block;font-size:21px;margin:5px 0}}.rank-mini em{{font-style:normal;font-size:14px}}.risk-collapse{{margin:16px 0}}.risk-collapse>summary{{cursor:pointer;color:var(--brass);font-weight:600;padding:8px 0}}.risk-collapse .card{{margin-top:10px}}.home-judgement-card{{padding:16px 15px}}.judgement-row{{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-top:1px solid #ECEDE8;font-size:13px}}.judgement-row:first-of-type{{border-top:0}}.judgement-row span{{color:var(--ink-soft)}}.judgement-row b{{text-align:right}}.home-judgement-copy{{margin:10px 0 0;padding-top:10px;border-top:1px solid #ECEDE8;color:var(--ink-soft);font-size:12px;line-height:1.65}}.home-detail-collapse{{margin:14px 0;border:1px solid #E3E2DC;border-radius:12px;background:#fff;box-shadow:0 3px 14px rgba(35,39,35,.04)}}.home-detail-collapse>summary{{cursor:pointer;padding:14px 16px;color:var(--brass);font-size:13px;font-weight:700}}.home-detail-collapse .contribution-card{{margin:0;border:0;border-top:1px solid #ECEDE8;border-radius:0;box-shadow:none}}@media(max-width:640px){{.portfolio-highlights{{grid-template-columns:1fr 1fr}}.portfolio-highlights>div:last-child{{grid-column:span 2}}.impact-leads{{grid-template-columns:1fr}}.rank-grid{{grid-template-columns:1fr}}.daily-hero h1{{font-size:26px}}}}
.daily-interpretation{{padding:11px 14px;margin:-4px 0 14px;border-left:3px solid var(--brass);background:rgba(255,255,255,.6);color:var(--ink-soft);font-size:13px;line-height:1.65}}.daily-interpretation-label{{font-size:11px;color:var(--brass);font-weight:700;letter-spacing:.08em;margin-bottom:3px}}.contribution-card{{padding:16px 15px}}.contribution-card .daily-section-title span{{font-size:11px;color:var(--ink-faint)}}.impact-sentence{{margin:0 0 12px;color:var(--ink-soft);font-size:13px;line-height:1.6}}.impact-leads{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.impact-lead{{padding:13px;border-radius:11px;background:#F7F7F3;border:1px solid #ECEDE8}}.impact-lead small{{display:block;font-size:11px;font-weight:700;color:var(--ink-soft)}}.impact-lead h3{{font-size:20px;line-height:1.3;margin:7px 0 4px;overflow-wrap:anywhere}}.impact-lead p{{margin:0 0 8px;color:var(--ink-soft);font-size:11px}}.impact-lead strong{{font-size:12px}}.impact-up{{border-color:#EDC7C2;background:#FFF7F5}}.impact-up strong{{color:var(--up)}}.impact-down{{border-color:#C9DFD0;background:#F4FBF5}}.impact-down strong{{color:var(--down)}}.impact-muted{{color:var(--ink-faint)}}.impact-details{{margin-top:10px;border-top:1px solid #ECEDE8}}.impact-details summary{{padding:11px 2px 4px;cursor:pointer;color:var(--brass);font-size:12px;font-weight:600}}.impact-detail-row{{display:flex;align-items:center;gap:8px;padding:9px 2px;border-top:1px solid #F0F0EC}}.impact-rank{{width:20px;height:20px;border-radius:50%;background:#F0EEE8;color:var(--ink-soft);font-size:11px;text-align:center;line-height:20px;flex:none}}.impact-detail-name{{min-width:0;flex:1}}.impact-detail-name b{{display:block;font-size:14px;overflow-wrap:anywhere}}.impact-detail-name small{{display:block;color:var(--ink-soft);font-size:10.5px;margin-top:2px}}.impact-detail-row strong{{font-size:12px;white-space:nowrap}}.impact-empty{{padding:9px 2px;color:var(--ink-faint);font-size:11px}}.contribution-footnote{{margin-top:10px;color:var(--ink-faint);font-size:10.5px;line-height:1.55}} </style><div class="daily-complete-sync" aria-live="polite">
  <span class="daily-complete-sync-dot" aria-hidden="true"></span>
  <div><b>今日資料已整合完成</b><span>即時行情、今日事件與排名摘要已載入；詳細貢獻明細可往下展開查看。</span></div>
</div><section class="daily-hero">
  <div class="eyebrow">{hero_eyebrow}</div>
  <h1>今天你的投資發生了什麼？</h1>
  <div class="market-strip"><span>大盤 <b>{market_text}</b></span><span>你的組合 <b>{portfolio_text}</b></span><span>相對大盤 <b>{relative_text}</b></span></div>
</section>
<section class="daily-card attention-card"><div class="daily-section-title"><h2>🔥 今日值得注意</h2><a href="/web/premarket">查看完整變化 →</a></div>{events_html}</section>
<section class="daily-card"><div class="daily-section-title"><h2>我的組合今天怎麼了？</h2><span>即時報價</span></div><div class="portfolio-highlights"><div><small>最大貢獻</small><b class="positive">{gain_html}</b></div><div><small>最大拖累</small><b class="negative">{loss_html}</b></div><div><small>總市值</small><b>{total_value:,.0f}</b></div></div></section>
{home_judgement_html}
<details class="home-detail-collapse" id="home-contribution" open><summary>查看完整正／負貢獻明細</summary>{contribution_html}</details>
<section class="daily-card"><div class="daily-section-title"><h2>🏆 我的排名變化</h2><a href="/web/leaderboard">查看排行榜 →</a></div><div class="rank-grid">{rank_line('short')}{rank_line('long')}</div></section>'''

@app.route("/web/portfolio", methods=["GET", "POST"])
@web_login_required
def web_portfolio(uid):
    # 今日首頁採用單一 shell、先行摘要再完整內容的分段流程。
    # 兩次回應都只替換同一個 content，不重複插入外框，避免 LINE WebView 疊頁。
    if request.method == "GET" and not wants_fragment():
        return render_loading_shell(
            "今日", "portfolio",
            ["正在讀取今日摘要…", "正在整合持股與即時報價…",
             "正在整合法人與月營收資料…", "正在計算集中度與相關係數…",
             "正在整理完整判讀與提醒…"],
            note="先顯示今日摘要；系統正在跑，正在整合完整即時分析。",
            staged=True)

    msg = ""
    if request.method == "POST" and not valid_web_csrf():
        return respond_page("今日", '<div class="msg">安全驗證已過期，請重新整理後再送出。</div>', "portfolio")
    if request.method == "POST":
        updates = {k: (request.form.get(k) or None) for k, _, _, _ in PROFILE_FIELDS}
        missing = [l for k, l, _r, _o in PROFILE_FIELDS if not updates.get(k)]
        if not missing:
            msg = "風險輪廓已儲存。" if update_profile(uid, updates) else "儲存失敗，請稍後再試。"
        else:
            # 明確指出漏了哪幾題，不要只說「有必填未填」讓人自己找
            msg = f"還有 {len(missing)} 題沒選：{'、'.join(missing[:3])}" + (
                " 等" if len(missing) > 3 else "")

    profile = get_profile(uid)
    risk_card = render_risk_card(profile, msg)

    # fast 片段只讀既有快照與排名，讓首頁先可用；完整片段再補即時行情與判讀。
    if request.method == "GET" and wants_fragment() and request.args.get("fast") == "1":
        if not is_profile_complete(profile):
            return respond_page("今日", risk_card, "portfolio")
        return respond_page("今日", render_portfolio_fast_summary(uid), "portfolio")

    # 問卷沒填完就只給問卷。組合分析的價值有一大半來自依你的處境判讀，
    # 少了那些答案，剩下的數字誰看都一樣，沒有必要先給。
    if not is_profile_complete(profile):
        return respond_page("今日", risk_card, "portfolio")

    positions = merge_positions(get_positions(uid))
    if not positions:
        # 沒有目前持股，但可能有賣光的歷史紀錄或組合快照可看，
        # 不能因為現在空手就把已實現損益跟走勢圖也一起藏起來。
        inst_empty = fetch_institutional_data() or {}
        realized_html_empty = render_realized_summary(uid, inst_empty)
        trend_html_empty = render_trend_chart(get_portfolio_snapshots(uid, days=120))
        body = risk_card + f"""
<div class="empty">還沒有持股紀錄。<br><br>
<a href="/web/positions" style="color:var(--brass)">先去新增持股 →</a></div>
{realized_html_empty}"""
        if realized_html_empty or trend_html_empty:
            body += f"""
<div class="section-head"><h2>組合走勢</h2>
  <span class="section-note">相對起始日漲跌幅</span></div>
<div class="callout" style="padding:14px 15px 4px">{trend_html_empty}</div>"""
        return respond_page("今日", body, "portfolio")

    full_started = time.monotonic()
    th = get_thresholds(profile)
    fee_disc, min_fee = get_fee_settings(profile)

    # 這五份共享資料彼此獨立；並行抓取可把等待時間從各次網路延遲總和
    # 降到最慢的一次。每個 loader 失敗只回空資料，不影響其他分析區塊。
    def safe_shared_loader(label, loader):
        loader_started = time.monotonic()
        try:
            value = loader() or {}
            print("⏱️ 今日共享資料：%s %.0fms" % (
                label, (time.monotonic() - loader_started) * 1000))
            return value
        except Exception as exc:
            print("⚠️ 今日共享資料載入失敗 %s（%.0fms）：%s" % (
                label, (time.monotonic() - loader_started) * 1000, exc))
            return {}

    shared_loaders = [
        ("法人", fetch_institutional_data),
        ("月營收", fetch_monthly_revenue),
        ("估值", fetch_valuation),
        ("產業", get_industry_map),
        ("大盤", fetch_taiex_summary),
    ]
    with ThreadPoolExecutor(max_workers=len(shared_loaders)) as ex:
        shared_values = list(ex.map(
            lambda item: safe_shared_loader(item[0], item[1]), shared_loaders))
    inst, revenue, valuation, ind_map, taiex = shared_values
    shared_done = time.monotonic()

    price_map = get_realtime_stocks_bulk([p["code"] for p in positions])
    price_done = time.monotonic()
    total_value, total_cost = 0.0, 0.0
    for p in positions:
        pr = price_map.get(p["code"])
        if pr:
            total_value += pr["close"] * p["shares"]
        total_cost += p["cost"] * p["shares"]

    # ── 產業集中度 ──
    by_industry, holdings = {}, []
    for p in positions:
        pr = price_map.get(p["code"])
        if not pr:
            continue
        value = pr["close"] * p["shares"]
        weight = value / total_value * 100 if total_value else 0
        ind = ind_map.get(p["code"])
        if ind:
            label = industry_name(ind)
        elif is_active_etf(p["code"]):
            # 主動式ETF由經理人選股，不是追蹤指數的一籃子部位，
            # 風險特性接近集中持股，不能跟被動ETF一樣當成「已分散」
            label = "主動式ETF"
        elif is_etf(p["code"]):
            label = "ETF（一籃子）"   # 被動ETF本身已分散，跟「查不到產業」意義不同
        else:
            label = "未分類"
        by_industry[label] = by_industry.get(label, 0) + weight
        name = stock_display_name(p["code"], inst, pr["name"])
        holdings.append({
            "code": p["code"], "name": name, "weight": weight, "value": value,
            "cost": p["cost"], "price": pr,
            "pl": (net_profit(p["code"], p["shares"], p["cost"], pr["close"],
                              p.get("lots"), fee_disc, min_fee)[1]
                   or (pr["close"] - p["cost"]) / p["cost"] * 100),
            "industry": label,
            "lots": p.get("lots"),
            "cum_yoy": revenue.get(p["code"], {}).get("cum_yoy_pct"),
            "pe": valuation.get(p["code"], {}).get("pe"),
        })

    ordered = sorted(by_industry.items(), key=lambda x: x[1], reverse=True)
    tints = ["#6E5228", "#8A6A3B", "#A98A5C", "#C3AC85", "#DCCFB4"]
    band, legend = [], []
    for i, (label, w) in enumerate(ordered):
        color = tints[i] if i < len(tints) else "#EAEBE7"
        fg = "#FFF" if i < 3 else "#3B2F1C"
        band.append(f'<span style="flex:{w:.2f};background:{color};color:{fg}">'
                    f'{label if w >= 12 else ""}{f"　{w:.0f}%" if w >= 12 else ""}</span>')
        legend.append(f'<span><i style="background:{color}"></i>{label} {w:.1f}%</span>')

    # ── 相關係數 ──
    avg_corr = avg_correlation(price_map)
    eff = effective_holdings(len(holdings), avg_corr)

    # ── 加權基本面 ──
    def weighted(key):
        num = sum(h["weight"] * h[key] for h in holdings if h[key] is not None)
        den = sum(h["weight"] for h in holdings if h[key] is not None)
        return num / den if den else None
    w_yoy, w_pe = weighted("cum_yoy"), weighted("pe")

    # ── 提醒 ──
    alerts = []
    top = max(holdings, key=lambda h: h["weight"]) if holdings else None
    if top and top["weight"] > th["position"]:
        second = sorted(holdings, key=lambda h: h["weight"], reverse=True)
        ratio = (f"，是第二大持股的 {top['weight'] / second[1]['weight']:.1f} 倍"
                 if len(second) > 1 and second[1]["weight"] else "")
        alerts.append(("集中度",
                       f"{top['name']}佔 {top['weight']:.1f}%，超過你設定的 "
                       f"{th['position']}%{ratio}。單一事件對組合的影響顯著。"))

    active_etf_weight = next((w for name, w in ordered if name == "主動式ETF"), 0)
    if active_etf_weight >= 20:
        alerts.append(("主動式ETF",
                       f"主動式ETF佔組合 {active_etf_weight:.1f}%。這類產品由經理人主動選股，"
                       f"不是追蹤指數的一籃子部位，集中度與波動風險可能接近持有單一策略，"
                       f"不宜視為分散配置。"))

    real_ordered = [x for x in ordered
                    if not x[0].startswith("ETF") and x[0] != "未分類" and x[0] != "主動式ETF"]
    if real_ordered and real_ordered[0][1] >= 30:
        drop = 25
        impact = real_ordered[0][1] / 100 * drop
        alerts.append(("產業集中",
                       f"{real_ordered[0][0]}佔 {real_ordered[0][1]:.1f}%。若該族群整體修正 "
                       f"{drop}%，組合約下跌 {impact:.1f}%"
                       + (f"，超出你設定的 {th['loss']}% 可接受範圍。"
                          if impact > th["loss"] else "。")))

    if avg_corr is not None and avg_corr >= 0.7 and eff:
        alerts.append(("分散不足",
                       f"{len(holdings)} 檔持股兩兩相關係數平均 {avg_corr:.2f}，"
                       f"實際分散效果約等於 {eff:.1f} 檔。"))

    losers = [h for h in holdings if h["pl"] <= -th["loss"]]
    for h in losers:
        alerts.append(("虧損提醒",
                       f"{h['name']}虧損 {abs(h['pl']):.1f}%，"
                       f"已達你設定的 {th['loss']}% 門檻。"))

    alerts += build_profile_alerts(profile, holdings, top, ordered, th)

    if w_pe and w_yoy is not None:
        covered = sum(h["weight"] for h in holdings if h["pe"] is not None)
        alerts.append(("基本面",
                       f"個股部分加權營收年增率 {w_yoy:+.1f}%，加權本益比 {w_pe:.1f} 倍"
                       f"（涵蓋組合的 {covered:.0f}%，ETF 無本益比故未計入）。"))

    pl_total = ((total_value - total_cost) / total_cost * 100) if total_cost else None
    calc_done = time.monotonic()

    # 提醒的入口放在總市值那一列。
    # 提醒本身留在頁面下方（要先看完數據才有判讀的基礎），但「有沒有東西要看」
    # 必須在第一屏就知道——放最下面等於沒放，而把提醒整段搬到最上面又會把
    # 總市值擠下去，那是每次打開都想先確認的數字。
    # 折衷做法：頂部只放計數與分類，點了跳到下面。
    alert_tags = []
    for tag, _txt in alerts:
        if tag not in alert_tags:
            alert_tags.append(tag)
    if alerts:
        alert_card = f"""
  <div><div class="total-label">值得注意</div>
       <div class="total-value num" style="color:var(--brass)">
         <a href="#alerts" style="color:inherit;text-decoration:none">
           {len(alerts)} 則 &rsaquo;</a></div>
       <div class="total-sub" style="color:var(--ink-faint)">
         {'・'.join(alert_tags[:3])}</div></div>"""
    else:
        alert_card = """
  <div><div class="total-label">值得注意</div>
       <div class="total-value num" style="color:var(--ink-faint)">無</div>
       <div class="total-sub" style="color:var(--ink-faint)">
         未觸及你設定的門檻</div></div>"""

    corr_txt = (f"兩兩相關係數平均 <b>{avg_corr:.2f}</b>，"
                f"實際分散效果約等於 <b>{eff:.1f} 檔</b>。"
                if avg_corr is not None and eff else
                "持股數不足或資料不齊，尚無法計算相關係數。")

    trend_started = time.monotonic()
    trend_html = render_trend_chart(get_portfolio_snapshots(uid, days=120))
    trend_done = time.monotonic()
    realized_started = time.monotonic()
    realized_html = render_realized_summary(uid, inst)
    realized_done = time.monotonic()

    daily_top_started = time.monotonic()
    daily_top = render_daily_home_top(uid, holdings, total_value, total_cost,
                                      price_map, pl_total, taiex=taiex)
    daily_top_done = time.monotonic()
    print("⏱️ 今日完整頁：共享 %.0fms、持股行情 %.0fms、組合計算 %.0fms、走勢 %.0fms、實現損益 %.0fms、首頁判讀 %.0fms、合計 %.0fms" % (
        (shared_done - full_started) * 1000,
        (price_done - shared_done) * 1000,
        (calc_done - price_done) * 1000,
        (trend_done - trend_started) * 1000,
        (realized_done - realized_started) * 1000,
        (daily_top_done - daily_top_started) * 1000,
        (daily_top_done - full_started) * 1000))
    body = f"""
{daily_top}
<details class="risk-collapse"><summary>查看我的風險輪廓</summary>
{risk_card}
</details>
<div class="section-head"><h2>完整組合分析</h2><span class="section-note">往下查看詳細資料</span></div>
<div class="totals"><div><div class="total-label">總市值</div><div class="total-value num">{total_value:,.0f}</div><div class="total-sub">{fmt_pct(pl_total)}</div></div><div><div class="total-label">持股檔數</div><div class="total-value num">{len(holdings)}</div><div class="total-sub">{len(by_industry)} 個產業</div></div><div><div class="total-label">最大單一持股</div><div class="total-value num">{top['weight']:.1f}%</div><div class="total-sub">{top['name']}</div></div>{alert_card}</div>
<div class="section-head"><h2>組合走勢</h2><span class="section-note">相對起始日漲跌幅</span></div><div class="callout" style="padding:14px 15px 4px">{trend_html}</div>
<div class="section-head"><h2>產業集中度</h2><span class="section-note">寬度即權重</span></div><div class="band">{''.join(band)}</div><div class="legend">{''.join(legend)}</div><div class="callout">{corr_txt}</div>
<div class="section-head"><h2>持股權重</h2><span class="section-note">依權重排序</span></div><div class="rows">{''.join(f'''<div class="row"><div><span class="name">{h['name']}</span><span class="code">{h['code']}</span></div><div class="price num">{h['weight']:.1f}%</div><div class="meta"><span><em>產業</em> {h['industry']}</span><span><em>損益</em> {fmt_pct(h['pl'])}</span><span><em>營收年增</em> {f"{h['cum_yoy']:+.1f}%" if h['cum_yoy'] is not None else '—'}</span><span><em>PE</em> {f"{h['pe']:.1f}" if h['pe'] else '—'}</span></div><div class="chg">{fmt_pct(h['price']['pct'])}</div><div class="bar"><div style="width:{h['weight']:.1f}%"></div></div></div>''' for h in sorted(holdings, key=lambda x: x['weight'], reverse=True))}</div>
{realized_html}
<div class="section-head" id="alerts"><h2>值得注意</h2><span class="section-note"><a href="/web/settings" style="color:var(--ink-soft)">調整門檻 →</a></span></div><div class="rows">{''.join(f'<div class="alert"><span class="tag">{tag}</span><span>{txt}</span></div>' for tag, txt in alerts) if alerts else '<div class="empty">目前沒有觸及門檻的項目。</div>'}</div>"""
    return respond_page("今日", body, "portfolio")


CATEGORY_NOTE = {
    "電子": "成長型評分：營收年增 25＋估值(PEG) 25＋產業動能 20＋法人連續性 20＋籌碼技術 10。　",
    "傳產": "循環型評分：成長門檻放低(≥25% 即滿分)，估值改看股價淨值比與殖利率——"
            "景氣循環股在獲利高點時本益比最低，用 PE 判斷便宜容易買在最危險的位置。　",
    "金融": "不評分：金融股該看的 ROE、利差、逾放比在免費資料中沒有，"
            "硬給分數會讓人誤以為那個數字有意義，因此只列事實供判讀。　",
}


# ============================================================
# 選股台：黑馬／雷達的完整版
# LINE 受限於訊息長度只能給 5 檔；網頁可以給 20 檔並支援排序篩選。
# ============================================================
# 選股結果快取。每個 mode 一份，存的是「還沒套使用者篩選條件」的完整清單。
# 這一頁真正花時間的是抓上百檔報價與評分，而那份結果對所有使用者、
# 所有篩選條件都是同一份——排序、筆數、產業、類股全是在既有清單上做取捨。
# 沒有快取的話，使用者每動一次下拉選單就要重跑一次全部流程（數十秒），
# 那才是最勸退的地方：第一次慢還能接受，每調一個條件都慢就不會有人用了。
_screener_cache = {}
SCREENER_CACHE_SECONDS = 300   # 盤中五分鐘內的報價差異對選股結論沒有影響
_screener_compute_lock = threading.Lock()


def compute_screener_rows(mode, inst=None, revenue=None, valuation=None, ind_map=None):
    """
    算出某個模式的完整候選清單。回傳 (rows, 因流動性被排除的檔數, 產業動能)。
    結果快取 5 分鐘，讓調整篩選條件變成瞬間反應。

    路由若已經先取過共享資料，可以直接傳入，避免同一個請求
    在 route 與計算函式之間重複呼叫法人／產業資料。
    """
    now = time.time()
    hit = _screener_cache.get(mode)
    if hit and now - hit["at"] < SCREENER_CACHE_SECONDS:
        return hit["rows"], hit["skipped"], hit["momentum"]

    # 快取失效時只允許一個 worker 進行全量選股；其他請求等候後重新命中快取，
    # 避免朋友同時開啟選股台時重複打 Yahoo／資料庫並放大延遲。
    with _screener_compute_lock:
        now = time.time()
        hit = _screener_cache.get(mode)
        if hit and now - hit["at"] < SCREENER_CACHE_SECONDS:
            return hit["rows"], hit["skipped"], hit["momentum"]

        inst = fetch_institutional_data() or {} if inst is None else inst
        revenue = fetch_monthly_revenue() or {} if revenue is None else revenue
        valuation = fetch_valuation() or {} if valuation is None else valuation
        ind_map = get_industry_map() or {} if ind_map is None else ind_map
        momentum = get_industry_momentum(revenue, ind_map)

        # ── 候選池 ──
        if mode == "radar":
            # 雷達看的是「今天什麼在動」，不分類股——
            # 傳產或金融只要帶量突破一樣值得注意，沒有理由先切掉。
            pool = [(c, i) for c, i in inst.items()
                    if len(c) == 4 and c.isdigit() and not c.startswith("00")
                    and i["total_net_lots"] > 0]
            pool.sort(key=lambda x: x[1]["total_net_lots"], reverse=True)
            pool = [(c, {"name": i.get("name", c), "total_net_lots": i["total_net_lots"],
                         "cum_lots": i["total_net_lots"], "buy_days": 1})
                    for c, i in pool[:120]]
        else:
            # 候選池＝三類各自取前 N 名後合併成一份排行。
            # 不用單一全市場排行：電子股的買超量級遠大於傳產與金融，
            # 混在一起排名時非電子類會被整批擠掉；
            # 但也不該讓使用者自己切類別，切到空頁面同樣沒有意義。
            # 各類取完再合併，既保證每類都有代表，又只需要看一份清單。
            quota = {"電子": 90, "傳產": 60, "金融": 30}
            pool = []
            for cat, n_take in quota.items():
                cat_codes = [c for c in ind_map if stock_category(c, ind_map) == cat]
                if not cat_codes:
                    continue
                for c, nm, cl, bd in get_cumulative_net_buy(
                        days=10, top_n=n_take, codes=cat_codes):
                    pool.append((c, {
                        "name": nm,
                        "total_net_lots": inst.get(c, {}).get("total_net_lots", 0),
                        "cum_lots": cl, "buy_days": bd}))

        streaks = get_consecutive_days_batch([c for c, _ in pool])

        # 流動性門檻依「個股所屬類別」判斷：
        # 傳產與金融的成交金額天生低於電子股，用同一組門檻會整批被濾掉。
        LIQUIDITY = {"電子": (10, 1.0), "傳產": (8, 0.3), "金融": (8, 0.3)}

        rows, skipped_liquidity = [], 0
        # 選股台的候選池動輒上百檔，序列請求是這一頁最大的延遲來源。
        # 這裡開比較多執行緒——一次把整池抓完，總時間才不會隨檔數線性增加。
        pool_prices = get_realtime_stocks_bulk([c for c, _ in pool], workers=16)
        for code, info in pool:
            price = pool_prices.get(code)
            if not price or abs(price["pct"]) > 10.5:
                continue
            min_close, min_turnover = LIQUIDITY.get(
                stock_category(code, ind_map), (8, 0.3))
            if price["close"] < min_close:
                skipped_liquidity += 1
                continue
            turnover = calc_turnover_billion(price["close"], price["volume"])
            if turnover < min_turnover:
                skipped_liquidity += 1
                continue
            if mode == "radar" and price["pct"] < 1.5:
                continue

            cum_yoy = revenue.get(code, {}).get("cum_yoy_pct")
            streak = streaks.get(code, 0)
            ind_code = ind_map.get(code)
            ind_txt = industry_name(ind_code) if ind_code else "未分類"
            industry_stats = momentum.get(ind_code) if ind_code else None

            sc = score_stock_by_category(
                code, ind_map, price, cum_yoy, valuation.get(code, {}),
                streak, info["cum_lots"], turnover, momentum)
            pe, pb, dy = sc["pe"], sc["pb"], sc["yield"]
            peg = sc.get("peg")
            total = sc["total"]
            rev_score = sc.get("rev", 0); val_score = sc.get("val", 0)
            mom_score = sc.get("mom", 0); streak_score = sc.get("streak_score", 0)
            chip_tech = sc.get("chip", 0); caps = sc.get("caps", ("", "", "", "", ""))

            breakout = ""
            if price.get("high_60d") and price["close"] >= price["high_60d"]:
                breakout = "季線新高"
            elif price.get("high_20d") and price["close"] >= price["high_20d"]:
                breakout = "破月高"

            rows.append({
                "code": code, "name": info["name"], "industry": ind_txt,
                "close": price["close"], "pct": price["pct"], "score": total,
                "rev": rev_score, "val": val_score, "mom": mom_score,
                "streak": streak, "streak_score": streak_score, "chip": chip_tech,
                "cum_yoy": cum_yoy, "pe": pe, "pb": pb, "yield": dy,
                "peg": peg, "turnover": turnover, "caps": caps,
                "category": sc["category"],
                "cum_lots": info["cum_lots"], "buy_days": info["buy_days"],
                "breakout": breakout, "vol_ratio": price.get("vol_ratio"),
                "pos": price.get("pos_vs_60d_high"),
                "up_streak": price.get("up_streak", 0),
                "data_quality": build_screener_data_quality(
                    cum_yoy, valuation.get(code, {}), industry_stats,
                    info.get("cum_lots"), price),
                "radar_state": classify_radar_state(price),
            })

        _screener_cache[mode] = {"at": now, "rows": rows,
                                 "skipped": skipped_liquidity, "momentum": momentum}
        return rows, skipped_liquidity, momentum


def build_screener_data_quality(cum_yoy, valuation, industry_stats,
                                 cum_lots, price):
    """只用現有資料標示選股資料完整度，不用缺資料猜測分數。"""
    checks = [
        ("營收", cum_yoy is not None),
        ("估值", any((valuation or {}).get(k) is not None
                      for k in ("pe", "pb", "yield"))),
        ("產業", industry_stats is not None),
        ("法人", cum_lots is not None),
        ("行情", bool(price and price.get("close") is not None)),
    ]
    missing = [label for label, ok in checks if not ok]
    valid = sum(1 for _, ok in checks if ok)
    return {
        "valid": valid,
        "total": len(checks),
        "missing": missing,
        "complete": valid == len(checks),
    }


def classify_radar_state(price):
    """把雷達訊號拆成突破／量能狀態，避免把普通上漲誤稱為帶量突破。"""
    price = price or {}
    close = price.get("close")
    high_60 = price.get("high_60d")
    high_20 = price.get("high_20d")
    vol_ratio = price.get("vol_ratio") or 0
    breakout = ""
    if high_60 and close is not None and close >= high_60:
        breakout = "季線新高"
    elif high_20 and close is not None and close >= high_20:
        breakout = "破月高"
    if breakout and vol_ratio >= 1.5:
        return "真正帶量突破"
    if breakout:
        return "突破但量能普通"
    if vol_ratio >= 1.5:
        return "帶量上漲"
    return "價格強、量能普通"


# ── 每日盤前變化偵測初始化 ──
# 放在 compute_screener_rows 定義之後，避免單檔主程式啟動時先引用尚未建立的名稱。
configure_daily_change_detector(
    get_db_connection=get_db_connection,
    release_db_connection=release_db_connection,
    compute_screener_rows=compute_screener_rows,
    fetch_taiex_summary=fetch_taiex_summary,
    fetch_quotes_bulk=fetch_quotes_bulk,
    fetch_stock_news=fetch_stock_news,
    fetch_institutional_data=fetch_institutional_data,
    get_consecutive_days=get_consecutive_days,
    get_user_watchlist=get_user_watchlist,
    compute_watchlist_scores=compute_watchlist_scores,
    get_notify_users=get_notify_users,
    get_all_watchlist_user_ids=get_all_watchlist_user_ids,
    stock_display_name=stock_display_name,
)
init_premarket_change_tables()


def build_review_body():
    """
    選股成效頁：把過去推薦過的名單拿現價回頭比對。

    刻意把「不利的數字」擺在跟有利的一樣顯眼的位置：勝率不到五成就顯示不到五成，
    輸給大盤就明講輸多少。一個不敢驗證自己的選股工具，跟隨便給建議沒有差別。
    """
    blocks = []
    for mode, label, note in [
        ("blackhorse", "黑馬",
         "長線評分：營收成長＋估值＋產業動能＋法人連續性"),
        ("radar", "雷達",
         "短線型態：帶量突破、法人買超"),
    ]:
        ev = evaluate_picks(mode)
        if not ev:
            blocks.append(f"""
<div class="section-head"><h2>{label}</h2>
  <span class="section-note">{note}</span></div>
<div class="empty">還沒有累積推薦紀錄。<br><br>
<span style="font-size:12.5px">每個交易日收盤後會存下當天的前 5 名，
最快 5 個交易日後就能看到第一組成效數字。</span></div>""")
            continue

        hz = ev["horizons"]
        if not hz:
            blocks.append(f"""
<div class="section-head"><h2>{label}</h2>
  <span class="section-note">{note}</span></div>
<div class="callout">已累積 {ev['total_picks']} 筆推薦，
但都還不滿 5 個交易日，尚無法計算報酬。<br>
<span style="font-size:12.5px;color:var(--ink-faint)">
只統計已經走完該天期的樣本——推薦才兩天就算進「5 日報酬」，
等於把還沒走完的區間混進來，數字會失真。</span></div>""")
            continue

        cards, rows_html = [], []
        for period in ("5–19 日", "20–59 日", "60 日以上"):
            s = hz.get(period)
            if not s:
                continue
            cls = "up" if s["avg"] >= 0 else "down"
            vs = ""
            if s["market"] is not None:
                diff = s["avg"] - s["market"]
                vs = (f'<div class="total-sub" style="color:var(--ink-faint)">'
                      f'大盤 {s["market"]:+.1f}%・'
                      f'{"贏" if diff >= 0 else "輸"} {abs(diff):.1f}%</div>')
            cards.append(f"""
  <div><div class="total-label">推薦後 {period}</div>
       <div class="total-value num {cls}">{s['avg']:+.1f}%</div>
       <div class="total-sub" style="color:var(--ink-faint)">
         中位 {s['median']:+.1f}%・勝率 {s['win_rate']:.0f}%・{s['n']} 筆</div>
       {vs}</div>""")

            b, bp = s["best"]
            w, wp = s["worst"]
            rows_html.append(f"""
<div class="row">
  <div><span class="name">{period}</span>
       <span class="code">最好 / 最差</span></div>
  <div class="price num">{s['n']} 筆</div>
  <div class="meta">
    <span><em>最好</em> {bp['name']}（{bp['code']}）
      <span class="num up">{b:+.1f}%</span></span>
    <span><em>最差</em> {wp['name']}（{wp['code']}）
      <span class="num down">{w:+.1f}%</span></span>
  </div>
</div>""")

        pending = (f"　另有 {ev['pending']} 筆推薦未滿 5 日，尚未計入"
                   if ev["pending"] else "")
        blocks.append(f"""
<div class="section-head"><h2>{label}</h2>
  <span class="section-note">{note}</span></div>
<div class="dist"><span class="dist-item">累計推薦
  <b>{ev['total_picks']}</b> 筆</span>
  <span class="dist-note">{pending}</span></div>
<div class="totals">{''.join(cards)}</div>
<div class="rows">{''.join(rows_html)}</div>""")

    return f"""
<div class="tabs">
  <a href="/web/screener?mode=blackhorse">黑馬</a>
  <a href="/web/screener?mode=radar">雷達</a>
  <span class="tabs-gap"></span>
  <a href="/web/screener?mode=review" class="on">成效</a>
</div>
<div class="mode-note">
  把過去推薦過的名單拿現價回頭比對，看這套評分實際上有沒有用。
  只統計已走完該天期的樣本，並附上同期大盤報酬做對照——
  多頭時什麼都在漲，沒有對照組的話「平均 +5%」看不出是選股有效還是市場好。
</div>
{''.join(blocks)}
<div class="callout" style="margin-top:24px">
  <b>怎麼看這些數字</b><br>
  <span style="font-size:12.5px;color:var(--ink-faint)">
  ・樣本數少於 30 筆時，平均值很容易被一兩檔極端值帶著跑，參考價值有限。<br>
  ・中位數比平均值抗極端值，兩者差距大就代表少數幾檔主導了整體結果。<br>
  ・真正該看的是「贏過大盤多少」，而不是絕對報酬。<br>
  ・這是回頭檢視，不是預測；過去有效不保證未來有效。</span>
</div>"""


def _latest_pick_history_rows(mode, limit=5):
    """選股冷啟動時只讀最近成功快照，明確標示資料日，不冒充即時結果。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pick_date, code, name, rank, score, price
            FROM pick_history
            WHERE mode = %s
            ORDER BY pick_date DESC, rank ASC
            LIMIT %s
            """,
            (str(mode).strip(), int(limit)),
        )
        rows = cur.fetchall()
        cur.close()
        return rows
    except Exception as exc:
        print(f"⚠️ 讀取最近選股快照失敗 {mode}: {exc}")
        return []
    finally:
        release_db_connection(conn)


def render_screener_fast_summary(mode):
    """選股台首屏：清楚標示這是前五名預覽，完整清單仍會自動載入。"""
    label = "雷達" if mode == "radar" else "黑馬"
    cached = _screener_cache.get(mode)
    cached_rows = []
    if cached and time.time() - cached.get("at", 0) < SCREENER_CACHE_SECONDS:
        cached_rows = list(cached.get("rows") or [])[:5]

    rows = []
    if cached_rows:
        source_label = "目前快取結果"
        source_date = "目前快取"
        for i, row in enumerate(cached_rows):
            rank = int(row.get("rank") or i + 1)
            name = html.escape(str(row.get("name") or row.get("code") or ""))
            code = html.escape(str(row.get("code") or ""))
            if mode != "radar":
                detail = f"分數 {row.get('score')}"
            else:
                detail = str(row.get("breakout") or "雷達訊號")
            rows.append(
                '<div class="position-fast-row"><div><b>#' + str(rank) + ' ' + name
                + ' <span class="code">' + code + '</span></b><small>'
                + html.escape(detail) + '</small></div></div>')
    else:
        history = _latest_pick_history_rows(mode, limit=5)
        source_label = "最近成功快照"
        source_date = str(history[0][0]) if history else "尚無歷史快照"
        for i, (pick_date, code, name, rank, score, price) in enumerate(history):
            display_rank = int(rank or i + 1)
            display_name = html.escape(str(name or code or ""))
            display_code = html.escape(str(code or ""))
            detail = (f"分數 {score}" if score is not None and mode != "radar"
                      else "最近成功結果")
            rows.append(
                '<div class="position-fast-row"><div><b>#' + str(display_rank)
                + ' ' + display_name + ' <span class="code">' + display_code
                + '</span></b><small>' + html.escape(detail)
                + '</small></div></div>')

    if not rows:
        rows = ['<div class="position-fast-empty">目前沒有可先顯示的快照；完整選股分析仍在建立中。</div>']

    style = """<style>
.screener-fast-card{background:#fff;border:1px solid #e3e2dc;border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 3px 14px rgba(35,39,35,.05)}
.screener-fast-card h2{margin:0 0 5px;font-size:20px}
.screener-fast-card .position-fast-row{display:flex;gap:10px;padding:11px 0;border-top:1px solid #eee}
.screener-fast-card .position-fast-row:first-of-type{border-top:0}
.screener-fast-card .position-fast-row b{display:block;font-size:15px}
.screener-fast-card .position-fast-row small{color:var(--ink-soft);font-size:12px}
.screener-fast-note{color:var(--ink-soft);font-size:12px;line-height:1.6}
.screener-fast-state{display:flex;gap:10px;align-items:flex-start;margin:14px 0;padding:12px;border-radius:9px;background:#F5F0E5;border-left:3px solid var(--brass)}
.screener-fast-state-mark{width:9px;height:9px;margin-top:5px;border-radius:50%;background:var(--brass);box-shadow:0 0 0 4px rgba(139,105,52,.12)}
.screener-fast-state b{display:block;color:var(--ink);font-size:14px}
.screener-fast-state span{display:block;margin-top:3px;color:var(--ink-soft);font-size:12px;line-height:1.6}
.screener-fast-preview{margin-top:16px;padding-top:12px;border-top:1px solid #e9e7e0}
.screener-fast-preview-title{display:flex;justify-content:space-between;gap:10px;align-items:baseline;margin-bottom:3px}
.screener-fast-preview-title b{font-size:14px;color:var(--ink)}
.screener-fast-preview-title span{font-size:11px;color:var(--ink-faint)}
.screener-fast-features{margin-top:14px;padding:11px 12px;background:#FAFAF7;border:1px dashed #d9d6ca;border-radius:8px;color:var(--ink-soft);font-size:12px;line-height:1.7}
.screener-fast-features b{display:block;color:var(--ink);font-size:12.5px;margin-bottom:3px}
.screener-fast-skeleton{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.screener-fast-skeleton span{height:20px;width:70px;border-radius:999px;background:#EDEDE8}
.screener-fast-empty{padding:10px 0;color:var(--ink-soft);line-height:1.6}
</style>"""
    return style + f"""<section class="screener-fast-card" aria-live="polite">
  <h2>{label}選股分析</h2>
  <div class="screener-fast-note">{source_label}・資料日：{html.escape(source_date)}</div>
  <div class="screener-fast-state">
    <span class="screener-fast-state-mark" aria-hidden="true"></span>
    <div><b>完整{label}分析載入中</b>
      <span>目前先顯示前 5 名預覽；系統正在補上完整清單、評分、型態、篩選、排序與產業分布。</span>
    </div>
  </div>
  <div class="screener-fast-preview">
    <div class="screener-fast-preview-title"><b>前 5 名預覽</b><span>不是完整選股結果</span></div>
    {''.join(rows)}
  </div>
  <div class="screener-fast-features">
    <b>完整選股頁稍後會顯示</b>
    完整清單 ・ 分數與資料完整度 ・ 黑馬／雷達條件 ・ 篩選與排序 ・ 產業分布
    <div class="screener-fast-skeleton" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
  </div>
</section>"""


@app.route("/web/screener")
@web_login_required
def web_screener(uid):
    mode = request.args.get("mode", "blackhorse")
    if not wants_fragment():
        # 選股台是全站最重的一頁（候選池上百檔），先秒回骨架再慢慢填
        return render_loading_shell(
            "選股台", "screener",
            (["正在讀取歷史推薦名單…", "正在抓現價比對…", "正在計算報酬與勝率…"]
             if mode == "review" else
             ["正在抓三大法人買賣超…", "正在抓月營收與估值…",
              "正在挑選候選池…",
              ("正在逐檔抓報價（上百檔）…" if mode != "radar"
               else "正在抓當日強勢股報價…"),
              "正在評分與排序…"]),
            note=("回頭比對過去推薦過的名單，需要抓這些股票的現價。"
                  if mode == "review" else
                  "先顯示最近成功快照，再補上最新選股結果。"),
            staged=(mode != "review"))

    if request.method == "GET" and wants_fragment() and request.args.get("fast") == "1" and mode != "review":
        return respond_page("選股台", render_screener_fast_summary(mode), "screener")

    limit = request.args.get("limit", "20")
    limit = int(limit) if limit.isdigit() and int(limit) in (10, 20, 50) else 20
    sort_key = request.args.get("sort", "score")
    min_score = request.args.get("min_score", "")
    max_pe = request.args.get("max_pe", "")
    industry_filter = request.args.get("industry", "")
    cat_filter = request.args.get("cat", "")   # 空=全部；僅作顯示篩選，不影響候選池
    view = request.args.get("view", "list")         # list=總排行, sector=依產業

    if mode == "review":
        return respond_page("選股台", build_review_body(), "screener")

    # 選股台每次要掃上百檔，是全站最耗資源的一頁。
    # 快取命中時很便宜，但快取過期後的重算不該讓人連續觸發。
    allowed, wait = rate_limit_ok(uid, "heavy")
    if not allowed:
        return respond_page("選股台", f"""
<div class="empty">查詢太頻繁了，請稍等 {wait} 秒再重新整理。<br><br>
<span style="font-size:12.5px">選股台每次要掃描上百檔股票並逐檔取得報價，
短時間內重複查詢會影響其他使用者。</span></div>""", "screener")

    inst = fetch_institutional_data()
    if not inst:
        return respond_page("選股台", """
<div class="empty">目前無法取得三大法人資料。<br>
可能是非交易時段或資料尚未公布，請稍後再試。</div>""", "screener")

    ind_map = get_industry_map() or {}
    rows, skipped_liquidity, momentum = compute_screener_rows(
        mode, inst=inst, ind_map=ind_map)
    rows = list(rows)   # 複製一份再篩選排序，避免就地排序動到快取裡那份

    # ── 篩選 ──
    if min_score.isdigit():
        rows = [r for r in rows
                if r["score"] is not None and r["score"] >= int(min_score)]
    try:
        if max_pe:
            rows = [r for r in rows if r["pe"] and r["pe"] <= float(max_pe)]
    except ValueError:
        pass
    if industry_filter:
        rows = [r for r in rows if r["industry"] == industry_filter]
    if cat_filter:
        rows = [r for r in rows if r["category"] == cat_filter]

    # ── 雷達專屬篩選：型態導向，不用分數 ──
    if mode == "radar":
        bk = request.args.get("breakout", "")
        if bk == "60":
            rows = [r for r in rows if r["breakout"] == "季線新高"]
        elif bk == "20":
            rows = [r for r in rows if r["breakout"] in ("季線新高", "破月高")]
        try:
            min_vol = float(request.args.get("min_vol", "") or 0)
            if min_vol:
                rows = [r for r in rows
                        if r.get("vol_ratio") and r["vol_ratio"] >= min_vol]
        except ValueError:
            pass
        min_streak = request.args.get("min_streak", "")
        if min_streak.isdigit():
            rows = [r for r in rows if r["streak"] >= int(min_streak)]

    # ── 排序 ──
    sorters = {
        "score": lambda r: (r["score"] if r["score"] is not None else -1),
        "pct": lambda r: r["pct"],
        "yoy": lambda r: r["cum_yoy"] if r["cum_yoy"] is not None else -999,
        "pe": lambda r: -r["pe"] if r["pe"] else -9999,
        "streak": lambda r: r["streak"],
        "turnover": lambda r: r["turnover"],
        "yield": lambda r: r["yield"] if r.get("yield") else -1,
        "pb": lambda r: -r["pb"] if r.get("pb") else -9999,
    }
    if cat_filter == "金融" and sort_key == "score":
        sort_key = "yield"   # 金融股沒有分數，改用殖利率當預設排序
    if mode == "radar":
        # 突破位階 → 量能倍數 → 連買天數 → 漲幅，不加總成單一分數
        def radar_key(r):
            bk = 2 if r["breakout"] == "季線新高" else (1 if r["breakout"] else 0)
            fatigue = -1 if (r.get("up_streak") or 0) >= 5 else 0
            return (bk + fatigue, r.get("vol_ratio") or 0, r["streak"], r["pct"])
        radar_sorters = {
            "pattern": radar_key,
            "vol": lambda r: (r.get("vol_ratio") or 0,),
            "pct": lambda r: (r["pct"],),
            "streak": lambda r: (r["streak"],),
            "turnover": lambda r: (r["turnover"],),
        }
        rows.sort(key=radar_sorters.get(sort_key, radar_key), reverse=True)
    else:
        rows.sort(key=sorters.get(sort_key, sorters["score"]), reverse=True)
    shown = rows[:limit]

    # ── 分數分布：判斷今天整體訊號強不強 ──
    bands = [(80, "80 以上"), (70, "70–79"), (60, "60–69"), (0, "60 以下")]
    dist, rest = [], sorted(rows, key=lambda r: (r["score"] or -1), reverse=True)
    for i, (lo, label) in enumerate(bands):
        hi = bands[i - 1][0] if i else 999
        n = len([r for r in rest if r["score"] is not None
                 and lo <= r["score"] < hi])
        dist.append((label, n))

    industries = sorted({r["industry"] for r in rows})

    # ── 依產業檢視 ──
    # 總排行容易被當紅族群整片佔滿，看不到其他產業有沒有東西。
    # 這裡改成「每個有動能的產業各出代表」，並依產業動能排序，
    # 讓「哪些族群正在成長」和「該族群裡誰最強」兩個問題分開回答。
    sector_blocks = []
    if view == "sector":
        by_ind = {}
        for r in rows:
            by_ind.setdefault(r["industry"], []).append(r)
        ranked_inds = []
        for ind_txt, members in by_ind.items():
            members.sort(key=lambda x: (x["score"] or -1), reverse=True)
            code_of = next((c for c, v in ind_map.items()
                            if industry_name(v) == ind_txt), None)
            st = momentum.get(ind_map.get(code_of)) if code_of else None
            ranked_inds.append({
                "name": ind_txt,
                "p75": st["p75"] if st else None,
                "median": st["median"] if st else None,
                "count": st["count"] if st else None,
                "members": members,
            })
        # 有產業動能資料的排前面，並依領先群成長率高低排序
        ranked_inds.sort(key=lambda x: (x["p75"] is not None,
                                        x["p75"] if x["p75"] is not None else 0),
                         reverse=True)
        sector_blocks = ranked_inds

    def opt(v, t, cur):
        return f'<option value="{v}"{" selected" if str(cur) == str(v) else ""}>{t}</option>'

    def radar_row(r):
        """
        雷達不給綜合分數。雷達回答的是「今天什麼在動、動在什麼位置」，
        那是型態問題不是估值問題；硬給一個總分只會跟黑馬混淆。
        """
        badge = (f'<span class="badge">{r["breakout"]}</span>'
                 if r["breakout"] else '<span class="badge muted">區間內</span>')
        state = r.get("radar_state") or "狀態資料不足"
        state_class = "" if state in ("真正帶量突破", "帶量上漲") else " muted"
        state_badge = f'<span class="badge{state_class}">{state}</span>'
        q = r.get("data_quality") or {}
        missing = "、".join(q.get("missing") or [])
        quality = f'資料 {q.get("valid", 0)}/{q.get("total", 0)}'
        if missing:
            quality += f'・缺 {missing}'
        vr = r.get("vol_ratio")
        vol_txt = f"{vr:.1f} 倍" if vr else "—"
        vol_cls = "hot" if vr and vr >= 2 else ("warm" if vr and vr >= 1.5 else "")
        streak_txt = f"{r['streak']} 日" if r["streak"] else "—"
        return f"""
<div class="row">
  <div><span class="name">{r['name']}</span><span class="code">{r['code']}</span>{badge}{state_badge}</div>
  <div class="price">{fmt_pct(r['pct'])}</div>
  <div class="meta">
    <span><em>價</em> <span class="num">{r['close']:,.2f}</span></span>
    <span><em>量能</em> <span class="num {vol_cls}">{vol_txt}</span>（20日均量）</span>
    <span><em>距高點</em> <span class="num">{f"{r['pos']:+.1f}%" if r['pos'] is not None else '—'}</span></span>
    <span><em>連買</em> {streak_txt}</span>
    <span><em>金額</em> <span class="num">{r['turnover']:.1f}</span> 億</span>
    <span><em>產業</em> {r['industry']}</span>
    <span><em>資料</em> {quality}</span>
  </div>
</div>"""

    CAT_TAG = {"電子": "電", "傳產": "傳", "金融": "金"}

    def stock_row(r):
        cat_tag = (f'<span class="cat cat-{r["category"]}">'
                   f'{CAT_TAG.get(r["category"], "")}</span>')
        badge = (f'<span class="badge">{r["breakout"]}</span>'
                 if r["breakout"] else "")
        q = r.get("data_quality") or {}
        missing = "、".join(q.get("missing") or [])
        quality = f'資料 {q.get("valid", 0)}/{q.get("total", 0)}'
        if missing:
            quality += f'・缺 {missing}'
        quality_badge = f'<span class="badge muted">{quality}</span>'
        if r["score"] is None:
            # 金融股不評分，只列事實
            return f"""
<div class="row">
  <div>{cat_tag}<span class="name">{r['name']}</span><span class="code">{r['code']}</span>{badge}{quality_badge}</div>
  <div class="price num">{r['close']:,.2f}</div>
  <div class="meta">
    <span><em>產業</em> {r['industry']}</span>
    <span><em>PB</em> {f"{r['pb']:.2f}" if r['pb'] else '—'}</span>
    <span><em>殖利率</em> {f"{r['yield']:.1f}%" if r['yield'] else '—'}</span>
    <span><em>PE</em> {f"{r['pe']:.1f}" if r['pe'] else '—'}</span>
    <span><em>連買</em> {r['streak']} 日</span>
    <span><em>距高點</em> {f"{r['pos']:+.1f}%" if r['pos'] is not None else '—'}</span>
    <span><em>金額</em> <span class="num">{r['turnover']:.1f}</span> 億</span>
  </div>
  <div class="chg">{fmt_pct(r['pct'])}</div>
</div>"""
        c = r["caps"]
        extra = (f'<span><em>PEG代理</em> {r["peg"]:.2f}</span>' if r["peg"]
                 else (f'<span><em>PB</em> {r["pb"]:.2f}</span>' if r["pb"] else ""))
        return f"""
<div class="row">
  <div>{cat_tag}<span class="name">{r['name']}</span><span class="code">{r['code']}</span>{badge}{quality_badge}</div>
  <div class="price num">{r['score']}<span class="sub">分</span></div>
  <div class="meta">
    <span><em>價</em> <span class="num">{r['close']:,.2f}</span> {fmt_pct(r['pct'])}</span>
    <span><em>營收年增</em> {f"{r['cum_yoy']:+.1f}%" if r['cum_yoy'] is not None else '—'}</span>
    <span><em>PE</em> {f"{r['pe']:.1f}" if r['pe'] else '—'}</span>
    {extra}
    <span><em>殖利率</em> {f"{r['yield']:.1f}%" if r['yield'] else '—'}</span>
    <span><em>連買</em> {r['streak']} 日</span>
    <span><em>金額</em> <span class="num">{r['turnover']:.1f}</span> 億</span>
  </div>
  <div class="chg sub">營收{r['rev']}/{c[0]}·估值{r['val']}/{c[1]}·產業{r['mom']}/{c[2]}·籌碼{r['streak_score']}/{c[3]}·技術{r['chip']}/{c[4]}</div>
  <div class="bar"><div style="width:{r['score']}%"></div></div>
</div>"""

    def sector_block(b, per):
        mom = (f'領先群 {b["p75"]:+.1f}%・中位 {b["median"]:+.1f}%・{b["count"]} 家'
               if b["p75"] is not None else '無產業動能資料')
        picks = "".join(stock_row(r) for r in b["members"][:per])
        return f"""
<div class="sector">
  <div class="sector-head">
    <span class="sector-name">{b['name']}</span>
    <span class="sector-mom">{mom}</span>
  </div>
  <div class="rows">{picks}</div>
</div>"""

    if mode == "radar":
        controls = f"""
<form method="get" class="controls">
  <input type="hidden" name="mode" value="radar">
  <input type="hidden" name="cat" value="{cat_filter}">
  <input type="hidden" name="view" value="list">
  <div class="fields">
    <div><label>排序</label><select name="sort" onchange="this.form.submit()">
      {opt('pattern','型態（突破→量能→連買）',sort_key)}
      {opt('vol','量能倍數',sort_key)}{opt('pct','當日漲幅',sort_key)}
      {opt('streak','法人連買天數',sort_key)}{opt('turnover','成交金額',sort_key)}
    </select></div>
    <div><label>突破狀態</label><select name="breakout" onchange="this.form.submit()">
      {opt('','不限',request.args.get('breakout',''))}
      {opt('60','僅創季線新高',request.args.get('breakout',''))}
      {opt('20','已突破月高以上',request.args.get('breakout',''))}
    </select></div>
    <div><label>量能倍數</label><select name="min_vol" onchange="this.form.submit()">
      {opt('','不限',request.args.get('min_vol',''))}
      {opt('1.5','≥ 1.5 倍',request.args.get('min_vol',''))}
      {opt('2','≥ 2 倍',request.args.get('min_vol',''))}
      {opt('3','≥ 3 倍',request.args.get('min_vol',''))}
    </select></div>
    <div><label>法人連買</label><select name="min_streak" onchange="this.form.submit()">
      {opt('','不限',request.args.get('min_streak',''))}
      {opt('2','≥ 2 日',request.args.get('min_streak',''))}
      {opt('3','≥ 3 日',request.args.get('min_streak',''))}
      {opt('5','≥ 5 日',request.args.get('min_streak',''))}
    </select></div>
    <div><label>顯示筆數</label><select name="limit" onchange="this.form.submit()">
      {opt(10,'10 筆',limit)}{opt(20,'20 筆',limit)}{opt(50,'50 筆',limit)}
    </select></div>
  </div>
</form>"""
    else:
        controls = f"""
<form method="get" class="controls">
  <input type="hidden" name="mode" value="{mode}">
  <input type="hidden" name="view" value="{view}">
  <input type="hidden" name="cat" value="{cat_filter}">
  <div class="fields">
    <div><label>排序</label><select name="sort" onchange="this.form.submit()">
      {'' if cat_filter == '金融' else opt('score','綜合分數',sort_key)}{opt('pct','當日漲幅',sort_key)}
      {opt('yoy','營收年增',sort_key)}{opt('pe','本益比（低→高）',sort_key)}
      {opt('streak','法人連買天數',sort_key)}{opt('turnover','成交金額',sort_key)}
      {opt('yield','殖利率',sort_key)}{opt('pb','股價淨值比（低→高）',sort_key)}
    </select></div>
    <div><label>顯示筆數</label><select name="limit" onchange="this.form.submit()">
      {opt(10,'10 筆',limit)}{opt(20,'20 筆',limit)}{opt(50,'50 筆',limit)}
    </select></div>
    {'' if cat_filter == '金融' else f'''<div><label>最低分數</label><select name="min_score" onchange="this.form.submit()">
      {opt('','不限',min_score)}{opt(60,'60 以上',min_score)}
      {opt(70,'70 以上',min_score)}{opt(80,'80 以上',min_score)}
    </select></div>'''}
    <div><label>本益比上限</label><select name="max_pe" onchange="this.form.submit()">
      {opt('','不限',max_pe)}{opt(15,'15 倍',max_pe)}{opt(20,'20 倍',max_pe)}
      {opt(30,'30 倍',max_pe)}{opt(50,'50 倍',max_pe)}
    </select></div>
    <div><label>產業</label><select name="industry" onchange="this.form.submit()">
      {opt('','全部',industry_filter)}
      {''.join(opt(i, i, industry_filter) for i in industries)}
    </select></div>
    <div><label>類股範圍</label><select name="cat" onchange="this.form.submit()">
      {opt('','全部',cat_filter)}
      {opt('電子','電子科技',cat_filter)}
      {opt('傳產','傳統產業',cat_filter)}
      {opt('金融','金融保險',cat_filter)}
    </select></div>
  </div>
</form>"""

    n_60 = len([r for r in rows if r["breakout"] == "季線新高"])
    n_20 = len([r for r in rows if r["breakout"] == "破月高"])
    n_vol = len([r for r in rows if (r.get("vol_ratio") or 0) >= 2])
    n_streak = len([r for r in rows if r["streak"] >= 3])
    # 金融股不評分，分數分布全是 0，改列對它有意義的統計
    n_pb1 = len([r for r in rows if r.get("pb") and r["pb"] <= 1.0])
    n_dy4 = len([r for r in rows if r.get("yield") and r["yield"] >= 4])
    fin_dist = (f'<span class="dist-item"><b>{n_pb1}</b> 檔 PB≤1</span>'
                f'<span class="dist-item"><b>{n_dy4}</b> 檔殖利率≥4%</span>'
                f'<span class="dist-item"><b>{len([r for r in rows if r["streak"] >= 3])}</b>'
                f' 檔連買≥3日</span>')

    radar_dist = (f'<span class="dist-item"><b>{n_60}</b> 檔創季線新高</span>'
                  f'<span class="dist-item"><b>{n_20}</b> 檔破月高</span>'
                  f'<span class="dist-item"><b>{n_vol}</b> 檔量能≥2倍</span>'
                  f'<span class="dist-item"><b>{n_streak}</b> 檔連買≥3日</span>')

    cat_counts = {}
    for r in rows:
        cat_counts[r["category"]] = cat_counts.get(r["category"], 0) + 1
    cat_html = "".join(
        f'<span class="dist-item"><b>{cat_counts.get(k, 0)}</b> 檔{k}</span>'
        for k in ("電子", "傳產", "金融"))

    dist_html = "".join(
        f'<span class="dist-item"><b>{n}</b> 檔 {label}</span>' for label, n in dist)

    row_fn = radar_row if mode == "radar" else stock_row
    per_sector = 2 if limit >= 20 else 1
    if mode == "radar":
        view = "list"   # 雷達不看產業動能，依產業檢視對它沒有意義
    if view == "sector":
        main_html = ("".join(sector_block(b, per_sector) for b in sector_blocks)
                     or '<div class="empty">沒有符合條件的標的，試著放寬篩選。</div>')
        count_note = f"{len(sector_blocks)} 個產業・每個產業取前 {per_sector} 名"
    else:
        main_html = ('<div class="rows">'
                     + "".join(row_fn(r) for r in shown) + '</div>'
                     if shown else
                     f'''<div class="empty">沒有符合條件的標的。<br><br>
<span style="font-size:12.5px">
{"" if mode == "radar" else (cat_filter + "類" if cat_filter else "")}目前沒有同時滿足「法人買超」與流動性門檻
（電子 10 元／1 億，傳產與金融 8 元／0.3 億）的標的，
其中 {skipped_liquidity} 檔因流動性被排除。<br>
可試著切換類股範圍，或放寬上方篩選條件。</span></div>''')
        count_note = f"共 {len(rows)} 檔符合條件"

    body = f"""
<div class="tabs">
  <a href="/web/screener?mode=blackhorse&view={view}&cat={cat_filter}"
     class="{'on' if mode != 'radar' else ''}">黑馬</a>
  <a href="/web/screener?mode=radar&view={view}&cat={cat_filter}"
     class="{'on' if mode == 'radar' else ''}">雷達</a>
  <a href="/web/screener?mode=review">成效</a>
  {'' if mode == 'radar' else f'''<span class="tabs-gap"></span>
  <a href="/web/screener?mode={mode}&view=list&cat={cat_filter}"
     class="{'on' if view != 'sector' else ''}">總排行</a>
  <a href="/web/screener?mode={mode}&view=sector&cat={cat_filter}"
     class="{'on' if view == 'sector' else ''}">依產業</a>'''}
</div>
<div class="mode-note">{
  ('' if mode == 'radar' else CATEGORY_NOTE.get(cat_filter, ''))}{
  '候選池為電子 90 檔＋傳產 60 檔＋金融 30 檔（各類分別取近 10 日累計買超前段），'
  '每檔用所屬類別的權重評分後合併排名——電子看成長與 PEG，傳產看 PB 與殖利率，金融不評分。'
  if mode != 'radar' else
  '當日法人買超且漲幅 1.5% 以上，涵蓋全部類股。雷達看的是型態與位階，'
  '不給綜合分數——「今天什麼在動」跟「什麼值得抱幾個月」是兩個問題。'}{
  '　產業依「領先群營收年增率」由高至低排列。' if view == 'sector' else ''}</div>

<div class="dist">{radar_dist if mode == "radar" else (fin_dist if cat_filter == "金融" else dist_html + cat_html)}<span class="dist-note">{count_note}</span></div>
{controls}
{main_html}
"""
    return respond_page("選股台", body, "screener")


# --- LINE Bot 訊息接收與路由分派 ---
def _line_signature_valid(raw_body, signature):
    """在寫入去重表前先驗證 LINE 簽章，避免偽造請求污染去重資料。"""
    secret = os.environ.get("LINE_CHANNEL_SECRET", "")
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, str(signature).strip())


def _line_event_dedup_key(raw_body, signature):
    """優先使用 LINE webhook event id；舊 payload 則以 body+signature fallback。"""
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        event_ids = []
        for item in payload.get("events") or []:
            if not isinstance(item, dict):
                continue
            value = item.get("webhookEventId") or item.get("eventId")
            if value:
                event_ids.append(str(value).strip())
        if event_ids:
            return "event:" + ",".join(event_ids)
    except Exception:
        pass
    digest = hashlib.sha256(raw_body + b"\\0" + str(signature).encode("utf-8")).hexdigest()
    return "payload:" + digest


def _claim_line_event(event_key):
    """跨 worker 原子領取 webhook；資料庫暫時故障時 fail-open，避免整個 Bot 停擺。"""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO line_event_dedup (event_id) VALUES (%s) "
            "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
            (event_key,))
        claimed = cur.fetchone() is not None
        conn.commit()
        cur.close()
        return claimed
    except Exception as exc:
        conn.rollback()
        print(f"⚠️ LINE webhook 去重寫入失敗，採放行處理：{exc}")
        return True
    finally:
        release_db_connection(conn)


_FAST_LINE_EVENT_TTL = 120
_fast_line_event_seen = {}
_fast_line_event_lock = threading.Lock()


def _line_payload_is_menu(raw_body):
    """只判斷純選單文字事件；其他 webhook 仍走資料庫跨 worker 去重。"""
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        events = payload.get("events") or []
        if not events:
            return False
        for item in events:
            message = item.get("message") if isinstance(item, dict) else None
            if not isinstance(message, dict) or message.get("type") != "text":
                return False
            if str(message.get("text", "")).strip().upper() not in {
                    "MENU", "選單", "幫助", "HELP"}:
                return False
        return True
    except Exception:
        return False


def _claim_fast_line_event(event_key):
    """純選單事件的短期進程內去重；避免為靜態選單回覆等待 DB。"""
    now = time.time()
    with _fast_line_event_lock:
        expired = [key for key, seen_at in _fast_line_event_seen.items()
                   if now - seen_at >= _FAST_LINE_EVENT_TTL]
        for key in expired:
            _fast_line_event_seen.pop(key, None)
        if event_key in _fast_line_event_seen:
            return False
        _fast_line_event_seen[event_key] = now
        return True


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    raw_body = request.get_data(cache=True)
    if not _line_signature_valid(raw_body, signature):
        abort(400)
    try:
        event_key = _line_event_dedup_key(raw_body, signature)
        if _line_payload_is_menu(raw_body):
            claimed = _claim_fast_line_event(event_key)
        else:
            claimed = _claim_line_event(event_key)
        if not claimed:
            print(f"ℹ️ 忽略重複 LINE webhook：{event_key[:80]}")
            return "OK"
        handler.handle(raw_body.decode("utf-8"), signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as exc:
        # 已驗證且已領取的事件不再讓平台反覆重送；完整錯誤留在 Render Logs。
        print(f"❌ LINE webhook 處理失敗：{type(exc).__name__}: {exc}")
    return "OK"

@handler.add(FollowEvent)
def handle_follow(event):
    """
    新用戶加好友時自動送出歡迎訊息與選單。
    不能假設新用戶會自己想到要輸入「選單」，也不能假設他會注意到
    聊天室下方的圖文選單——第一次接觸就把可用功能攤開來給他看。
    """
    user_id = event.source.user_id
    add_user_to_db(user_id)

    welcome = TextSendMessage(text=(
        "歡迎加入台股 BOT 📈\n\n"
        "我是你的台股公開資料整理助手，可以查行情、法人籌碼、"
        "營收、估值，也能建立自己的自選股清單。\n\n"
        "接下來會先送上功能選單，再補上使用前的重要說明；"
        "你可以先從選單開始探索。"
    ))
    usage_notice = TextSendMessage(text=(
        "⚠️ 使用前請先了解\n\n"
        "本服務只做「公開資料的整理與呈現」，"
        "不是投資建議，也不推薦任何個股。\n\n"
        "・「黑馬」「雷達」是依公開數據排序的結果，"
        "分數高不代表會漲，也不代表適合你\n"
        "・所有評分都是作者自訂的規則，沒有經過專業認證\n"
        "・資料來自證交所、櫃買中心與 Yahoo Finance，"
        "可能延遲或有誤，請以官方公告為準\n"
        "・投資有風險，盈虧由你自己承擔\n\n"
        "🔒 關於你的資料\n"
        "自選股與持股只用於產生你自己的分析，"
        "作者不會查看個別使用者的持股內容。\n\n"
        "📌 操作小提醒\n"
        "每個指令都會即時抓取最新行情、法人與財務資料，"
        "通常需要約 10-20 秒才會回覆。\n"
        "如果按鈕按下後沒有立即反應，請先等 5 秒；"
        "仍沒有反應再按一次，不要連續快速點擊，避免同一查詢重複執行。\n\n"
        "看得懂數字背後的意思再做決定，"
        "不要因為看到一個分數就進場。\n\n"
        "隨時輸入「選單」都能再叫出功能選單。\n\n"
        "———\n"
        "作者：蔡秉軒　敬上"
    ))
    try:
        menu = build_menu_flex(is_admin(user_id))
        menu.quick_reply = build_quick_reply()
        # 順序固定為：歡迎文字 → 大型功能選單 → 使用前警語與操作建議。
        line_bot_api.reply_message(event.reply_token, [welcome, menu, usage_notice])
    except Exception as e:
        print(f"❌ 歡迎訊息發送失敗 {user_id}: {e}")


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    flex_reply = None  # 若為 Flex 訊息（彩色選單），改用這個回覆
    admin_quick_reply = None
    text = event.message.text.strip()
    text_upper = text.upper()
    pure_code = normalize_code(text)  # 保留主動式ETF的英文尾碼，如 00981A

    # 「選單」是純靜態回覆，不應先等待 LINE 個人資料、使用者 upsert
    # 或活動紀錄寫入；否則資料庫池／Supabase 短暫延遲就會讓使用者
    # 看到訊息已送出，卻要按第二、第三次才收到選單。
    # 先回覆，再把非必要的紀錄放到背景執行緒，維持快速且不改變功能。
    if text_upper in ["MENU", "選單", "幫助", "HELP"]:
        allowed, wait = rate_limit_ok(user_id, "normal")
        if not allowed:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"⏳ 選單請稍等 {wait} 秒再試。", quick_reply=build_quick_reply()))
            return
        try:
            menu_reply = build_menu_flex(is_admin(user_id))
            menu_reply.quick_reply = build_quick_reply()
            line_bot_api.reply_message(event.reply_token, menu_reply)
        except Exception as exc:
            print(f"❌ 選單快速回覆失敗 {user_id}: {type(exc).__name__}: {exc}")
            try:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="選單載入失敗，請稍後再試。", quick_reply=build_quick_reply()))
            except Exception as fallback_exc:
                print(f"❌ 選單 fallback 回覆失敗 {user_id}: {fallback_exc}")
            return

        def _record_menu_activity():
            try:
                add_user_to_db(user_id)
                record_activity(user_id, "more", action="message", source="line")
            except Exception as exc:
                print(f"⚠️ 選單背景紀錄失敗 {user_id}: {exc}")

        try:
            threading.Thread(target=_record_menu_activity, daemon=True).start()
        except Exception as exc:
            print(f"⚠️ 選單背景紀錄執行緒啟動失敗 {user_id}: {exc}")
        return

    add_user_to_db(user_id)


    # 濫用防護：耗時指令有較嚴格的上限。擋下時明確告知還要等多久，
    # 而不是靜默忽略——後者會讓人以為機器人壞了而狂點，反而更糟。
    kind = "heavy" if (text in HEAVY_COMMANDS or text in SLOW_COMMANDS) else "normal"
    allowed, wait = rate_limit_ok(user_id, kind)
    if not allowed:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=(f"⏳ 查詢太頻繁了，請稍等 {wait} 秒再試。\n\n"
                  f"每個指令都要即時抓取行情與財務資料，"
                  f"短時間內重複查詢會影響其他使用者。"),
            quick_reply=build_quick_reply()))
        return

    feature = infer_line_feature(text)
    if feature:
        record_activity(user_id, feature, action="message", source="line")

    # 每則訊息都先叫出 LINE 官方的載入動畫（聊天室裡的三點跳動）。
    # 不分指令輕重都叫，是因為使用者無法預期哪個指令會慢——
    # 有些看似簡單的查詢遇到快取失效時一樣要跑十幾秒，
    # 沒有動畫時那段安靜會讓人以為訊息沒送出而重複點擊。
    # 這支 API 不計入每月推播額度，多叫幾次沒有成本。
    start_loading_animation(user_id)

    # 0. 管理指令（只有 ADMIN_USER_ID 本人可用，其他人輸入等同無效指令）
    if is_admin(user_id) and text in ["管理", "管理中心"]:
        reply = build_admin_dashboard_report()
        admin_quick_reply = build_admin_quick_reply()

    elif is_admin(user_id) and text in ["使用者名單", "使用者", "名單"]:
        reply = build_admin_user_list_report(status="all", limit=10)
        admin_quick_reply = build_admin_quick_reply()

    elif is_admin(user_id) and text in ["今日活躍", "活躍"]:
        reply = build_admin_user_list_report(status="today", limit=10)
        admin_quick_reply = build_admin_quick_reply()

    elif is_admin(user_id) and text in ["沉睡使用者", "沉睡"]:
        reply = build_admin_user_list_report(status="dormant", limit=10)
        admin_quick_reply = build_admin_quick_reply()

    elif is_admin(user_id) and text in ["功能統計", "使用統計", "統計", "數據"]:
        reply = build_admin_dashboard_report()
        admin_quick_reply = build_admin_quick_reply()

    elif is_admin(user_id) and text in ["流失", "可能流失"]:
        reply = build_admin_churn_report()
        admin_quick_reply = build_admin_quick_reply()

    elif text in ["我的ID", "我的id", "MYID"]:
        reply = f"你的 user_id：\n{user_id}"

    elif text in ["網頁", "WEB", "網頁版"]:
        # 連結與登入碼一次都給：兩者用途不同，但使用者不該為了換瀏覽器
        # 而需要知道要再打一次別的指令。
        # 連結給 LINE 內開啟用，登入碼給 Safari／Chrome 用。
        token = create_web_token(user_id)
        code = create_web_code(user_id)
        base = request.url_root.rstrip("/")
        if token:
            parts = [
                "🌐 台股 BOT 網頁版",
                "",
                "【在 LINE 裡開啟】直接點：",
                f"{base}/web/login?t={token}",
                "",
            ]
            if code:
                parts += [
                    "【用 Safari／Chrome 開啟】",
                    f"網址：{base}/web/code",
                    f"登入碼：{code}",
                    "",
                    f"（登入碼 {WEB_CODE_MINUTES} 分鐘內有效，只能用一次；"
                    f"過期再輸入「登入碼」取得新的）",
                    "",
                ]
            parts += [
                "【可以做什麼】",
                "・持股管理：買賣紀錄、加權成本、淨損益",
                "・組合分析：產業集中度、相關係數、風險提醒",
                "・交易紀錄：已實現損益、勝率、盈虧比",
                "・選股台：黑馬／雷達完整清單與成效回顧",
                "",
                f"登入後可維持 {WEB_SESSION_DAYS} 天。",
                "",
                "🔒 你輸入的持股只用於產生你自己的分析。",
            ]
            reply = "\n".join(parts)
        else:
            reply = "❌ 產生連結失敗，請稍後再試。"

    elif text in ["登入碼", "驗證碼", "CODE", "登入"]:
        code = create_web_code(user_id)
        if code:
            base = request.url_root.rstrip("/")
            reply = (f"🔑 網頁登入碼\n\n"
                     f"　　{code}\n\n"
                     f"在瀏覽器打開這個網址，輸入上面的號碼：\n"
                     f"{base}/web/code\n\n"
                     f"・有效 {WEB_CODE_MINUTES} 分鐘，只能使用一次\n"
                     f"・登入後可維持 {WEB_SESSION_DAYS} 天\n"
                     f"・Safari、Chrome、電腦瀏覽器都適用\n\n"
                     f"重新索取會讓舊的號碼失效。")
        else:
            reply = "❌ 產生登入碼失敗，請稍後再試。"

    elif is_admin(user_id) and text in ["名單", "使用者", "VIP"]:
        reply = build_user_list_report()

    elif is_admin(user_id) and text in ["統計", "數據", "使用統計"]:
        reply = build_usage_stats_report()

    elif is_admin(user_id) and (text.startswith("開通") or text.startswith("停用")):
        turn_on = text.startswith("開通")
        arg = text[2:].strip()
        rows = list_users()
        target, ambiguous = None, None

        if arg.isdigit() and 1 <= int(arg) <= len(rows):
            target = rows[int(arg) - 1]
        elif arg:
            matches = [r for r in rows if arg in r[1]]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                ambiguous = "、".join(m[1] for m in matches[:5])

        if target:
            changed = set_push_flags(target[0], notify=turn_on, requested=False)
            if changed:
                reply = (f"{'🔔 已開通' if turn_on else '🔕 已停用'}：{target[1]}\n\n"
                         + build_admin_user_list_report(status="all", limit=10))
            else:
                reply = "❌ 推播設定沒有成功寫入，請稍後再試。\n\n" + build_admin_user_list_report(status="all", limit=10)
        elif ambiguous:
            reply = f"符合「{arg}」的有多人：{ambiguous}\n請改用編號，例如「開通 3」"
        else:
            reply = f"找不到「{arg}」。輸入「名單」查看編號。"

    # 1. 加自選（可同時指定分類，例如「加 2330 長線」）
    elif "加" in text and 4 <= len(pure_code) <= 7:
        tag = normalize_tag(text.replace("加", "").replace(pure_code, "").strip())
        success = add_watchlist_db(user_id, pure_code, tag)
        c_name = stock_display_name(pure_code) or STOCK_NAME_MAP.get(pure_code, pure_code)
        if success:
            if tag:
                reply = (f"✅ 新增自選成功：{pure_code} {c_name}\n"
                         f"分類：{TAG_ICONS[tag]} {tag}")
            else:
                reply = (f"✅ 新增自選成功：{pure_code} {c_name}\n\n"
                         f"想分類的話：輸入「分類 {pure_code} 長線」\n"
                         f"可用分類：🌱長線　⚡短線　👀觀察")
        else:
            reply = f"❌ 新增自選失敗，資料庫寫入異常：{pure_code}"

    # 1.5 只改分類，不新增
    elif text.startswith("分類") and 4 <= len(pure_code) <= 7:
        tag = normalize_tag(text.replace("分類", "").replace(pure_code, "").strip())
        if not tag:
            reply = (f"請指定分類，例如「分類 {pure_code} 長線」\n\n"
                     f"可用分類：\n"
                     f"🌱 長線　適合看營收與估值\n"
                     f"⚡ 短線　適合看籌碼與位階\n"
                     f"👀 觀察　還沒進場，先追蹤")
        elif set_watchlist_tag(user_id, pure_code, tag):
            c_name = stock_display_name(pure_code) or pure_code
            reply = f"{TAG_ICONS[tag]} 已將 {pure_code} {c_name} 設為「{tag}」"
        else:
            reply = (f"❌ 自選清單裡沒有 {pure_code}\n"
                     f"先輸入「加 {pure_code} {tag}」新增")

    # 2. 刪自選
    elif "刪" in text and 4 <= len(pure_code) <= 7:
        remove_watchlist_db(user_id, pure_code)
        reply = f"🗑️ 已從自選清單移除：{pure_code}"

    # 3. 推播開關設定
    elif text in ["推播開", "開啟推播", "訂閱", "申請推播"]:
        # 每日推播為名額制：LINE 免費方案每月僅 200 則主動訊息，
        # 以每人每個交易日 1 則計算，最多只能服務約 9 人，
        # 因此改為申請制，由管理者在後台開通，使用者無法自行啟用。
        if set_requested(user_id, True):
            reply = (
                "📮 已收到每日推播的申請\n\n"
                "每日盤前推播為名額制，需由管理者開通。\n"
                "已收到你的申請，開通後隔天早上就會自動收到。\n\n"
                "在此之前，隨時輸入「盤前」都能看到相同內容。"
            )
        else:
            reply = "❌ 推播申請沒有成功寫入，請稍後再試。"
    elif text in ["推播關", "關閉推播", "取消訂閱"]:
        # 關閉不需要審核，使用者隨時可以自己退出；同時清掉未處理申請。
        if set_push_flags(user_id, notify=False, requested=False):
            reply = "🔕 已關閉每日推播。想再開啟請輸入「申請推播」。"
        else:
            reply = "❌ 推播狀態沒有成功更新，請稍後再試。"

    # 4+5. 自選清單與健檢已合併——兩者原本都在列自選股，差別只在有沒有評分，
    # 併成同一份報告，「自選」與「健檢」都指向它
    elif text in ["自選", "WATCHLIST", "健檢", "自選健檢"]:
        reply = build_healthcheck_report(user_id)

    # 6. 單獨查代號 → 直接給完整健檢
    # 原本只回報價與位階，要看評分還得先加進自選再查健檢。
    # 查一檔股票時想知道的本來就是「這檔現在如何」，沒理由分成兩個指令。
    elif 4 <= len(pure_code) <= 7 and len(text) <= 8 and " " not in text:
        reply = build_single_stock_report(pure_code, user_id)

    # 6.5 自選股新聞（手動查詢，跟盤後推播同一份內容）
    elif text in ["新聞", "自選新聞"]:
        reply = build_news_digest(user_id) or "📂 自選清單是空的，先用「加 2330」新增自選"

    # 7. 盤前速覽
    elif text in ["盤前", "早安"]:
        # 手動輸入也使用同一張短訊息＋今日網頁按鈕；網址沿用目前 request 網域。
        flex_reply = build_morning_push_message(user_id, request.url_root.rstrip("/"))
        reply = None

    # 7.5 盤後解盤（使用者手動輸入才觸發，不自動推播）
    elif text in ["解盤", "盤後解盤", "盤後"]:
        # 與盤前一致：LINE 先給收盤摘要，再由按鈕進入網頁版完整分析。
        flex_reply = build_market_recap_line_message(
            user_id, request.url_root.rstrip("/"))
        reply = None

    # 7.65 籌碼超人：把三大法人拆開看誰在認養、誰在撤退
    elif text in ["籌碼", "籌碼超人", "認養"]:
        reply = build_chips_report()

    # 8. 黑馬股（不同於雷達：以「月營收年增率」為主軸，找有題材／獲利成長的股票）
    elif text in ["黑馬", "雷達"]:
        mode = "blackhorse" if text == "黑馬" else "radar"
        sep = chr(10)
        inst_data = fetch_institutional_data()
        if not inst_data:
            reply = "❌ 目前無法取得三大法人資料，可能是非交易時段或非交易日，請稍後再試。"
        else:
            # LINE 與網頁共用同一份候選池、資料快取、評分與資料完整度欄位，
            # 避免同一天兩個入口出現不同排名。
            rows, _skipped, _momentum = compute_screener_rows(
                mode, inst=inst_data)
            if mode == "blackhorse":
                rows = [r for r in rows if r.get("score") is not None]
                rows.sort(key=lambda r: r.get("score", -1), reverse=True)
                reports = []
                for rank, r in enumerate(rows[:5], start=1):
                    q = r.get("data_quality") or {}
                    quality = f"{q.get('valid', 0)}/{q.get('total', 0)}"
                    missing = "、".join(q.get("missing") or [])
                    quality_line = (f"資料完整度：{quality}"
                                    + (f"（缺 {missing}）" if missing else ""))
                    caps = r.get("caps", ("", "", "", "", ""))
                    growth_line = (f"累計年增 {r['cum_yoy']:+.1f}%"
                                   if r.get("cum_yoy") is not None
                                   else "累計年增 尚無資料")
                    value_line = (f"PE {r['pe']:.1f}"
                                  if r.get("pe") is not None
                                  else (f"PB {r['pb']:.2f}"
                                        if r.get("pb") is not None
                                        else "估值資料不足"))
                    proxy_line = (f"營收成長估值代理值 {r['peg']:.2f}"
                                  if r.get("peg") is not None else "")
                    report_lines = [
                        f"🐎 智慧黑馬股 #{rank}", "",
                        f"股票：{r['name']}",
                        f"代號：{r['code']}",
                        f"產業：{r['industry']}", "",
                        f"黑馬指數：{r['score']}／100", "",
                        f"💡 營收成長：{r['rev']}／{caps[0]}",
                        f"　　{growth_line}",
                        f"　　{proxy_line}" if proxy_line else None,
                        f"💰 估值：{r['val']}／{caps[1]}",
                        f"　　{value_line}",
                        f"🏭 產業動能：{r['mom']}／{caps[2]}",
                        f"🔁 法人連續性：{r['streak_score']}／{caps[3]}（連續{r['streak']}日買超）",
                        f"📊 籌碼技術：{r['chip']}／{caps[4]}",
                        f"　　近10日累計買超 {r.get('cum_lots', 0):,} 張", "",
                        f"📋 {quality_line}", "",
                        "【位階】", build_position_desc(r),
                        "-----------------------------------",
                    ]
                    reports.append(sep.join(x for x in report_lines if x is not None))
                reply = (sep + sep).join(reports) + sep + sep + (
                    "※ 黑馬與網頁選股台使用同一套公開資料與排序；"
                    "分數高不代表會漲，也不代表適合你的狀況。"
                    if reports else "❌ 暫無符合條件的標的。")
            else:
                def line_radar_key(r):
                    breakout = (2 if r.get("breakout") == "季線新高"
                                else (1 if r.get("breakout") else 0))
                    fatigue = -1 if (r.get("up_streak") or 0) >= 5 else 0
                    return (breakout + fatigue, r.get("vol_ratio") or 0,
                            r.get("streak", 0), r.get("pct", 0))

                rows.sort(key=line_radar_key, reverse=True)
                reports = []
                for r in rows[:5]:
                    q = r.get("data_quality") or {}
                    quality = f"{q.get('valid', 0)}/{q.get('total', 0)}"
                    missing = "、".join(q.get("missing") or [])
                    quality_line = (f"資料完整度：{quality}"
                                    + (f"（缺 {missing}）" if missing else ""))
                    streak_line = (f"🔁 法人連續買超：{r['streak']} 日"
                                   if r.get("streak", 0) >= 2 else "")
                    report_lines = [
                        "🚨【盤中雷達】", "",
                        f"🔥 強勢股票：{r['name']}",
                        f"📌 股票代號：{r['code']}", "",
                        f"💰 現價：{r['close']:.2f}",
                        f"📈 漲幅：{r['pct']:+.2f}%",
                        f"📊 成交金額：{r['turnover']:.1f} 億",
                        f"🏦 三大法人買超：{r.get('cum_lots', 0):,} 張",
                        streak_line,
                        f"🏆 狀態：{r.get('radar_state', '狀態資料不足')}",
                        f"📋 {quality_line}", "",
                        "【位階】", build_position_desc(r), "",
                        "【注意】", build_risk_desc(r['pct'], r.get('cum_lots', 0)),
                        "-----------------------------------",
                    ]
                    reports.append(sep.join(x for x in report_lines if x))
                reply = (sep + sep).join(reports) + sep + sep + (
                    "※ 雷達與網頁選股台使用同一套公開資料與排序；"
                    "短線強勢不代表會續強，追高風險自負。"
                    if reports else "❌ 暫無符合條件的標的。")
    elif text_upper in ["MENU", "選單", "幫助", "HELP"]:
        flex_reply = build_menu_flex(is_admin(user_id))
        reply = None

    else:
        # 指令沒對上時直接把選單給他，不要只回一句「請輸入選單」
        flex_reply = build_menu_flex(is_admin(user_id))
        reply = None

    qr = admin_quick_reply or build_quick_reply()
    if flex_reply is not None:
        flex_reply.quick_reply = qr
        line_bot_api.reply_message(event.reply_token, flex_reply)
    else:
        line_bot_api.reply_message(
            event.reply_token, TextSendMessage(text=reply, quick_reply=qr))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
