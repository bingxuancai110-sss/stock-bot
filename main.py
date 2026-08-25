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


# 首頁佳句只使用本機固定文字；依台灣日期選擇，同一天重整仍維持同一句。
# 分成四組輪替，讓風險、進退、市場觀察與心態提醒依序出現。
HOMEPAGE_QUOTE_GROUPS = {
    "風險控管": (
        "未雨綢繆，防患未然。",
        "居安思危，未雨綢繆。",
        "防微杜漸，慎終如始。",
        "謹小慎微，安不忘危。",
        "如履薄冰，如臨深淵。",
        "三思而後行，謀定而後動。",
        "量力而為，適可而止。",
        "知止不殆，知足不辱。",
        "穩紮穩打，步步為營。",
        "小心駛得萬年船。",
        "留有餘地，方能轉圜。",
        "先求不敗，再圖勝局。",
        "審慎而行，穩中求進。",
        "防患未萌，未雨先備。",
        "安不忘危，治不忘亂。",
        "履霜知冰，見微知著。",
        "慎始慎終，行穩致遠。",
        "欲思其利，必慮其害。",
        "明者防禍於未萌，智者圖患於未然。",
        "輕諾必寡信，躁進易失守。",
    ),
    "進退紀律": (
        "知進知退，方能久勝。",
        "見好就收，落袋為安。",
        "當斷則斷，不斷則亂。",
        "能屈能伸，方成大器。",
        "急流勇退，明哲保身。",
        "去留有度，取捨有方。",
        "欲速不達，戒急用忍。",
        "進可攻，退可守。",
        "得意不忘形，失意不失志。",
        "取捨之間，見其格局。",
        "退一步海闊天空。",
        "事緩則圓，心定則明。",
        "勝不驕，敗不餒。",
        "寵辱不驚，去留無意。",
        "靜觀其變，伺機而動。",
        "能進能退，方可長久。",
        "過猶不及，適可而止。",
        "不疾不徐，從容應對。",
        "欲進先退，欲取先予。",
        "藏器於身，待時而動。",
    ),
    "市場觀察": (
        "審時度勢，順勢而為。",
        "因時制宜，因勢利導。",
        "見微知著，洞燭機先。",
        "風起於青萍之末。",
        "山雨欲來風滿樓。",
        "盛極必衰，物極必反。",
        "否極泰來，泰極否來。",
        "潮起潮落，盈虧有時。",
        "人無千日好，花無百日紅。",
        "水到渠成，瓜熟蒂落。",
        "螳螂捕蟬，黃雀在後。",
        "兼聽則明，偏信則暗。",
        "以靜制動，後發先至。",
        "先見之明，未雨之備。",
        "運籌帷幄，決勝千里。",
        "察勢而動，順勢而行。",
        "日中則昃，月滿則虧。",
        "長江後浪推前浪。",
        "海納百川，有容乃大。",
        "逆水行舟，不進則退。",
    ),
    "投資心態": (
        "戒貪戒躁，量力而為。",
        "淡泊明志，寧靜致遠。",
        "不以物喜，不以己悲。",
        "心平氣和，從容應對。",
        "厚積薄發，水到渠成。",
        "守拙藏鋒，韜光養晦。",
        "積小勝為大勝。",
        "一鼓作氣，再而衰，三而竭。",
        "失之東隅，收之桑榆。",
        "塞翁失馬，焉知非福。",
        "勝固欣然，敗亦可喜。",
        "吃得苦中苦，方為人上人。",
        "行穩致遠，久久為功。",
        "千里之行，始於足下。",
        "不忘初心，方得始終。",
        "精誠所至，金石為開。",
        "功不唐捐，玉汝於成。",
        "守正出奇，厚德載物。",
        "慢工出細活，磨刀不誤砍柴工。",
        "靜水流深，大智若愚。",
    ),
}


def _homepage_quote_for(display_date=None):
    """依台灣日期固定選句；不使用 random、不查資料，也不產生外部請求。"""
    groups = tuple(HOMEPAGE_QUOTE_GROUPS.values())
    if not groups:
        return "知進知退，方能久勝。"
    try:
        day = display_date if isinstance(display_date, date) else taiwan_today()
        ordinal = day.toordinal()
    except (AttributeError, TypeError, ValueError):
        ordinal = taiwan_today().toordinal()
    group = groups[ordinal % len(groups)]
    return group[(ordinal // len(groups)) % len(group)]


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


def _numeric_value_changed(old_value, new_value, tolerance=0.005):
    """比較兩個可能為 None 的數值；未知值不等於 0，也不誤判為變化。"""
    if old_value is None or new_value is None:
        return old_value != new_value
    try:
        return abs(float(new_value) - float(old_value)) > tolerance
    except (TypeError, ValueError):
        return str(old_value) != str(new_value)


def _format_level_price(value):
    """格式化支撐／壓力價位；缺值保留為待確認，不補零。"""
    if value is None:
        return "未保存有效參考"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "資料待確認"


def _premarket_json_value(value, expected):
    """把 JSONB／舊部署可能回傳的 JSON 字串轉成安全容器；不猜測缺失資料。"""
    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
    if expected == "list":
        return list(parsed) if isinstance(parsed, (list, tuple)) else []
    if expected == "dict":
        return dict(parsed) if isinstance(parsed, dict) else {}
    return parsed


def _premarket_record_list(value):
    """只保留盤前清單中的 dict 記錄，丟棄無法安全渲染的舊 scalar。"""
    return [item for item in _premarket_json_value(value, "list")
            if isinstance(item, dict)]


def _premarket_record_map(value):
    """只保留法人 map 中 value 為 dict 的記錄，避免舊資料污染整頁渲染。"""
    parsed = _premarket_json_value(value, "dict")
    return {str(code): item for code, item in parsed.items()
            if isinstance(item, dict)}


def _format_watchlist_level_change_detail(evidence):
    """把自選股位階事件轉成人可讀的支撐／壓力前後比較。"""
    evidence = evidence if isinstance(evidence, dict) else {}
    old = evidence.get("old") or {}
    new = evidence.get("new") or {}
    old = old if isinstance(old, dict) else {}
    new = new if isinstance(new, dict) else {}
    parts = []
    old_support, new_support = old.get("support"), new.get("support")
    old_resistance, new_resistance = old.get("resistance"), new.get("resistance")
    if old_support is not None or new_support is not None:
        if old_support is not None and new_support is not None:
            parts.append(f"支撐 {_format_level_price(old_support)} → {_format_level_price(new_support)}")
        elif new_support is not None:
            parts.append(f"支撐：前一日未保存 → 今日 {_format_level_price(new_support)}（自今日起開始記錄）")
        else:
            parts.append(f"支撐：前一日 {_format_level_price(old_support)} → 今日無有效參考")
    if old_resistance is not None or new_resistance is not None:
        if old_resistance is not None and new_resistance is not None:
            parts.append(f"壓力 {_format_level_price(old_resistance)} → {_format_level_price(new_resistance)}")
        elif new_resistance is not None:
            parts.append(f"壓力：前一日未保存 → 今日 {_format_level_price(new_resistance)}（自今日起開始記錄）")
        else:
            parts.append(f"壓力：前一日 {_format_level_price(old_resistance)} → 今日無有效參考")
    old_pos, new_pos = old.get("position"), new.get("position")
    if old_pos is not None and new_pos is not None and old_pos != new_pos:
        parts.append(f"位階分數 {old_pos} → {new_pos}")
    old_close, new_close = old.get("close"), new.get("close")
    if old_close is not None and new_close is not None:
        parts.append(f"收盤 { _format_level_price(old_close) } → { _format_level_price(new_close) }")
    return "；".join(parts) or "支撐／壓力有效參考不足，請以當日個股詳情核對。"


def _format_premarket_event_evidence(event):
    """顯示人話比較依據；保留資料可追溯性，但不把原始 JSON 直接丟給使用者。"""
    event = event if isinstance(event, dict) else {}
    category = event.get("category")
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    if category == "watchlist_position":
        return _format_watchlist_level_change_detail(evidence)
    if category == "watchlist":
        old = evidence.get("old") if isinstance(evidence.get("old"), dict) else {}
        new = evidence.get("new") if isinstance(evidence.get("new"), dict) else {}
        if old.get("total") is not None or new.get("total") is not None:
            return f"自選股綜合分數 {old.get('total', '待確認')} → {new.get('total', '待確認')}"
    if category == "market":
        old = evidence.get("old") if isinstance(evidence.get("old"), dict) else {}
        new = evidence.get("new") if isinstance(evidence.get("new"), dict) else {}
        return f"前一交易日：{old.get('pct', '待確認')}；今日：{new.get('pct', '待確認')}"
    detail = str(event.get("detail") or "")
    return detail or "依事件明細中的既有資料比較。"


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
    """保存盤前頁需要的真實收盤資料；事件偵測仍只讀 *_pct 欄位。"""
    market = {}
    taiex = fetch_taiex_summary() or {}
    if taiex:
        for key in ("pct", "change_pct", "percent"):
            if taiex.get(key) is not None:
                try:
                    market["taiex_pct"] = float(taiex[key])
                except (TypeError, ValueError):
                    pass
                break
        for source_key, target_key in (("close", "taiex_close"),
                                       ("pts", "taiex_diff"),
                                       ("date", "taiex_date")):
            value = taiex.get(source_key)
            if value not in (None, ""):
                if target_key.endswith("_date"):
                    market[target_key] = str(value)
                else:
                    try:
                        numeric = float(str(value).replace(",", ""))
                        if source_key == "pts" and str(taiex.get("sign") or "") == "-":
                            numeric = -abs(numeric)
                        market[target_key] = numeric
                    except (TypeError, ValueError):
                        pass
    # 台指期夜盤是台股大盤下方的獨立市場資料，不加入原本市場事件偵測範圍。
    taiex_night = fetch_taifex_night_summary() or {}
    for source_key, target_key in (("close", "taiex_night_close"),
                                   ("diff", "taiex_night_diff"),
                                   ("pct", "taiex_night_pct"),
                                   ("date", "taiex_night_date"),
                                   ("contract", "taiex_night_contract")):
        value = taiex_night.get(source_key)
        if value not in (None, ""):
            if source_key in ("date", "contract"):
                market[target_key] = str(value)
            else:
                try:
                    market[target_key] = float(value)
                except (TypeError, ValueError):
                    pass

    # MU 與 LITE 是盤前頁要直接看的個股，不改變原本的指數事件偵測範圍。
    symbols = ["^DJI", "^IXIC", "^GSPC", "^SOX", "MU", "LITE"]
    quotes = fetch_quotes_bulk(symbols) or {}
    for symbol in symbols:
        q = quotes.get(symbol)
        if isinstance(q, dict):
            close, pct, diff = q.get("close"), q.get("pct"), q.get("diff")
        elif isinstance(q, (tuple, list)) and len(q) >= 3:
            # fetch_quotes_bulk() 的既有格式是 (close, pct, diff)。
            close, pct, diff = q[0], q[1], q[2]
        else:
            close = pct = diff = None
        for suffix, value in (("close", close), ("pct", pct), ("diff", diff)):
            if value is None:
                continue
            try:
                market[f"{symbol}_{suffix}"] = float(value)
            except (TypeError, ValueError):
                pass
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
          SELECT code,total,position,close,support,resistance FROM watchlist_scores
          WHERE user_id=%s AND snapshot_date=%s
        """, (str(user_id), previous_date))
        old = {r[0]: {"total": r[1], "position": r[2], "close": r[3],
                      "support": r[4], "resistance": r[5]} for r in cur.fetchall()}
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
        # 支撐／壓力只使用既有即時報價中的實際近期高低點與均線參考；
        # 舊快照若尚未保存價位，不把缺值猜成 0，也不製造「由多少變多少」。
        stock = row.get("stock") or {}
        price = stock.get("close")
        old_price = before.get("close")
        old_pos, new_pos = before.get("position"), after.get("position")
        old_support, new_support = before.get("support"), stock.get("support")
        old_resistance, new_resistance = before.get("resistance"), stock.get("resistance")
        support_changed = _numeric_value_changed(old_support, new_support)
        resistance_changed = _numeric_value_changed(old_resistance, new_resistance)
        position_changed = (old_pos is not None and new_pos is not None and old_pos != new_pos)
        if price is not None and old_price is not None and (support_changed or resistance_changed):
            evidence = {"code": code,
                        "old": {"close": old_price, "position": old_pos,
                                "support": old_support, "resistance": old_resistance},
                        "new": {"close": price, "position": new_pos,
                                "support": new_support, "resistance": new_resistance}}
            events.append(_event("A", "watchlist_position", f"你的{name}支撐／壓力參考變化",
                                 _format_watchlist_level_change_detail(evidence),
                                 f"watch_position_{code}", evidence))
        elif price is not None and old_price is not None and position_changed:
            evidence = {"code": code,
                        "old": {"close": old_price, "position": old_pos},
                        "new": {"close": price, "position": new_pos}}
            events.append(_event("A", "watchlist_position", f"你的{name}位階評分變化",
                                 f"位階分數 {old_pos} → {new_pos}；前一交易日尚未保存完整支撐／壓力價位，暫不虛構價位變化。",
                                 f"watch_position_{code}", evidence))
    return events


def _sort_events(events):
    """依優先級排序，兼容舊快照缺少 event_key 或 severity 型別不完整。"""
    def sort_key(event):
        event = event if isinstance(event, dict) else {}
        severity = str(event.get("severity") or "C").strip().upper()
        return (-CHANGE_LEVEL.get(severity, CHANGE_LEVEL["C"]),
                str(event.get("category") or ""),
                str(event.get("event_key") or event.get("title") or ""))
    return sorted(events or [], key=sort_key)


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
                "blackhorse": _premarket_record_list(row[3]),
                "radar": _premarket_record_list(row[4]),
                "market": _premarket_json_value(row[5], "dict"),
                "news": _premarket_record_list(row[6]),
                "institutional": _premarket_record_map(row[7])}
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


@app.after_request
def _disable_web_page_cache(response):
    """行情、持股與盤前頁不可由瀏覽器／代理沿用上一個收盤時點的 HTML。"""
    if request.path.startswith("/web"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

# --- 繁體中文名稱對照表（僅作為個股直接查詢時的備用名稱） ---
STOCK_NAME_MAP = {
    "2330": "台積電", "2454": "聯發科", "3661": "世芯-KY", "6669": "緯穎",
    "3037": "欣興", "2382": "廣達", "3231": "緯創", "4931": "新日興",
    "3081": "聯亞", "6442": "光聖", "3529": "力旺", "3443": "創意",
    "6173": "信昌電", "1503": "士電",
    "0050": "元大台灣50", "00981A": "主動統一台股增長",
    "009816": "凱基台灣TOP50"
}

# ETF 商品屬性只放已由官方產品頁核實的資料；未知 ETF 不猜測配息政策。
# 官方產品頁與證交所 ETF 商品頁已核實的 ETF metadata；未知 ETF 不猜測配息政策。
# 0050、00981A、009816 的資料查閱日：2026-08-23。
ETF_PRODUCT_METADATA = {
    "0050": {
        "name": "元大台灣50",
        "category": "市值型",
        "management_style": "被動式",
        "distribution_policy": "distributing",
        "distribution_frequency": "半年配（依官方公告）",
        "listing_date": "2003-06-30",
        "inception_date": "2003-06-25",
        "benchmark": "FTSE TWSE Taiwan 50 Index（臺灣50指數）",
        "source_urls": [
            "https://www.yuantaetfs.com/product/detail/0050/Basic_information",
        ],
    },
    "00981A": {
        "name": "主動統一台股增長",
        "category": "主動式",
        "management_style": "主動式",
        "distribution_policy": "distributing",
        "distribution_frequency": "季配息（依官方公告）",
        "policy_note": "基金之配息來源可能為收益平準金",
        "listing_date": "2025-05-27",
        "inception_date": "2025-05-15",
        "benchmark": "臺灣證券交易所發行量加權股價報酬指數",
        "source_urls": [
            "https://www.twse.com.tw/zh/ETFortune/etfInfo/00981A",
            "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode=63YTW",
        ],
    },
    "009816": {
        "name": "凱基台灣TOP50",
        "category": "市值型",
        "management_style": "被動式",
        "distribution_policy": "non_distributing",
        "listing_date": "2026-02-03",
        "inception_date": "2026-01-22",
        "benchmark": "臺灣指數公司特選臺灣TOP50指數",
        "source_urls": [
            "https://www.kgifund.com.tw/Fund/Detail?fundID=J023",
            "https://www.twse.com.tw/zh/ETFortune-institute/etfInfo/009816",
        ],
    },
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
        # 持股操作日誌：獨立於目前庫存與已實現損益，保留每次加碼／減碼事件。
        # shares_delta 正數代表加碼，負數代表減碼；成交價與備註由使用者輸入或由既有交易流程帶入。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS position_change_logs (
                id BIGSERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                code TEXT NOT NULL,
                action TEXT NOT NULL,
                shares_delta INTEGER NOT NULL,
                trade_price REAL,
                trade_date DATE NOT NULL DEFAULT CURRENT_DATE,
                note TEXT,
                source TEXT NOT NULL DEFAULT 'web',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT position_change_logs_action_ck
                    CHECK (action IN ('add', 'reduce')),
                CONSTRAINT position_change_logs_shares_ck
                    CHECK (shares_delta <> 0)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_position_change_logs_user_date
            ON position_change_logs (user_id, trade_date DESC, id DESC)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_position_change_logs_user_code
            ON position_change_logs (user_id, code, trade_date DESC, id DESC)
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
                support REAL,
                resistance REAL,
                asset_type TEXT NOT NULL DEFAULT 'stock',
                PRIMARY KEY (user_id, code, snapshot_date)
            )
        ''')
        cursor.execute('''
            ALTER TABLE watchlist_scores
            ADD COLUMN IF NOT EXISTS asset_type TEXT NOT NULL DEFAULT 'stock'
        ''')
        cursor.execute('''
            ALTER TABLE watchlist_scores
            ADD COLUMN IF NOT EXISTS support REAL
        ''')
        cursor.execute('''
            ALTER TABLE watchlist_scores
            ADD COLUMN IF NOT EXISTS resistance REAL
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
        # 選股完整結果快照：pick_history 只保存前5名供成效追蹤；
        # 這張表保存 warmup 已完成的完整黑馬／雷達清單，供各 worker 快速讀取。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS screener_result_snapshots (
                mode TEXT PRIMARY KEY,
                snapshot_date DATE NOT NULL,
                computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                rows_json JSONB NOT NULL,
                skipped_liquidity INTEGER NOT NULL DEFAULT 0,
                momentum_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                source_meta JSONB NOT NULL DEFAULT '{}'::jsonb
            )
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
        # 共享資料快照：跨 Gunicorn worker／重啟保存非即時、可追溯的來源資料。
        # 網頁優先讀這裡的完整 JSON；資料日與 computed_at 一起保存，避免把舊資料冒充今天。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS shared_data_snapshots (
                snapshot_key TEXT PRIMARY KEY,
                data_date DATE,
                computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                payload JSONB NOT NULL,
                source_meta JSONB NOT NULL DEFAULT '{}'::jsonb
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_shared_data_snapshots_computed
            ON shared_data_snapshots (computed_at DESC)
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
    """驗證權杖並回傳 user_id；過期或不存在回傳 None。

    Render／Supabase pooler 偶爾會把閒置 TLS 連線切斷；遇到 bad record
    mac 時關閉該連線並只重試一次，避免把壞連線留給下一個請求。
    """
    if not token:
        return None
    for attempt in range(2):
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id FROM web_sessions WHERE token = %s AND expires_at > NOW()",
                (token,),
            )
            row = cursor.fetchone()
            cursor.close()
            release_db_connection(conn)
            conn = None
            return row[0] if row else None
        except Exception as e:
            print(f"❌ 驗證網頁權杖失敗（第 {attempt + 1} 次）: {e}")
            # SSL／連線例外後不要把壞連線放回 pool；下一次從 pool
            # 借用新連線再試一次。這裡不把例外內容回傳給使用者。
            if conn is not None:
                try:
                    connection_pool.putconn(conn, close=True)
                    conn = None
                except Exception:
                    pass
            if attempt == 0:
                continue
            return None
        finally:
            if conn is not None:
                release_db_connection(conn)
    return None


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
                "/web/workbench": "screener",
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
        cursor.execute(
            """
            INSERT INTO position_change_logs
                (user_id, code, action, shares_delta, trade_price, trade_date, note, source)
            VALUES (%s, %s, 'add', %s, %s, %s, %s, 'web')
            """,
            (str(user_id).strip(), str(code).strip(), int(shares), float(cost),
             bought_on or taiwan_today(), note or None),
        )
        conn.commit()
        cursor.close()
        clear_leaderboard_cache()
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
        if deleted > 0:
            clear_leaderboard_cache()
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
        cursor.execute(
            """
            INSERT INTO position_change_logs
                (user_id, code, action, shares_delta, trade_price, trade_date, note, source)
            VALUES (%s, %s, 'reduce', %s, %s, %s, %s, 'web')
            """,
            (uid, str(code).strip(), -int(sell_shares), float(sell_price),
             taiwan_today(), "賣出"),
        )
        conn.commit()
        cursor.close()
        clear_leaderboard_cache()
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
            """
            SELECT code FROM realized_trades WHERE user_id = %s
            UNION
            SELECT code FROM position_change_logs WHERE user_id = %s
            ORDER BY code
            """,
            (str(user_id).strip(), str(user_id).strip()))
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


def get_position_change_logs(user_id, limit=5000, code=None, trade_date=None):
    """讀取使用者的加碼／減碼日誌；缺少成交價時維持 None，不補猜價格。"""
    where, params = ["user_id = %s"], [str(user_id).strip()]
    if code:
        where.append("code = %s")
        params.append(str(code).strip())
    if trade_date:
        where.append("trade_date = %s")
        params.append(trade_date)
    params.append(int(limit))
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT id, code, action, shares_delta, trade_price,
                   trade_date, note, created_at
            FROM position_change_logs
            WHERE {' AND '.join(where)}
            ORDER BY trade_date DESC NULLS LAST, id DESC
            LIMIT %s
            """, tuple(params))
        rows = cur.fetchall()
        cur.close()
        return [{
            "id": r[0], "code": str(r[1]).strip(), "action": r[2],
            "shares_delta": int(r[3]), "trade_price": float(r[4]) if r[4] is not None else None,
            "trade_date": r[5], "note": r[6], "created_at": r[7],
        } for r in rows]
    except Exception as exc:
        print(f"❌ 讀取持股操作日誌失敗: {exc}")
        return []
    finally:
        release_db_connection(conn)


def _position_change_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10]) if text else None
    except ValueError:
        return None


def enrich_position_change_logs(logs, current_positions, price_map, total_value):
    """以目前庫存反推功能上線前的基準股數，再逐筆重建加減碼前後狀態。

    這樣既有使用者不會因為功能上線前沒有歷史日誌，而把第一筆新日誌誤標成從 0 股開始。
    """
    current_by_code = {
        str(p.get("code")).strip(): int(p.get("shares") or 0)
        for p in (current_positions or []) if p.get("code")
    }
    all_delta = {}
    for log in logs or []:
        code = str(log.get("code") or "").strip()
        all_delta[code] = all_delta.get(code, 0) + int(log.get("shares_delta") or 0)
    state = {code: current_by_code.get(code, 0) - all_delta.get(code, 0)
             for code in set(current_by_code) | set(all_delta)}
    chronological = sorted(
        logs or [], key=lambda item: (_position_change_date(item.get("trade_date")) or date.min,
                                      int(item.get("id") or 0)))
    enriched_by_id = {}
    for log in chronological:
        code = str(log.get("code") or "").strip()
        delta = int(log.get("shares_delta") or 0)
        before = int(state.get(code, 0))
        after = before + delta
        trade_price = log.get("trade_price")
        event_value = (abs(delta) * float(trade_price)
                       if trade_price is not None else None)
        current_price = (price_map.get(code) or {}).get("close") if price_map else None
        current_shares = current_by_code.get(code, 0)
        current_weight = ((float(current_price) * current_shares / total_value * 100)
                          if current_price is not None and total_value > 0 else None)
        event_weight = ((delta * float(trade_price) / total_value * 100)
                        if trade_price is not None and total_value > 0 else None)
        change_pct = (delta / before * 100) if before > 0 else None
        item = dict(log)
        item.update({
            "shares_before": before, "shares_after": after,
            "change_pct": change_pct, "event_value": event_value,
            "current_weight": current_weight, "event_weight": event_weight,
        })
        enriched_by_id[item.get("id")] = item
        state[code] = after
    return [enriched_by_id.get(log.get("id"), log) for log in logs or []]


def render_position_change_journal(user_id, current_positions=None, price_map=None,
                                  inst_data=None, logs=None, trade_date=None,
                                  display_limit=100, realized_trades=None):
    """呈現操作日報；總資產口徑是目前已登錄持股的最新可得市值，不含未輸入的現金／其他資產。"""
    all_logs = logs if logs is not None else get_position_change_logs(user_id, limit=5000)
    if not all_logs:
        return ""
    current_positions = (current_positions if current_positions is not None
                         else merge_positions(get_positions(user_id)))
    codes = sorted({str(p.get("code")).strip() for p in current_positions if p.get("code")} |
                   {str(log.get("code")).strip() for log in all_logs if log.get("code")})
    if price_map is None:
        price_map = get_realtime_stocks_bulk(codes, rng="1d") if codes else {}
    total_value = sum(
        float((price_map.get(p.get("code")) or {}).get("close") or 0) * int(p.get("shares") or 0)
        for p in current_positions if price_map.get(p.get("code"))
    )
    enriched = enrich_position_change_logs(all_logs, current_positions, price_map, total_value)
    realized_trades = (list(realized_trades) if realized_trades is not None
                       else get_realized_trades(user_id, limit=500))
    realized_by_key = {}
    for trade in realized_trades:
        sold_date = _position_change_date(trade.get("sold_on"))
        trade_code = str(trade.get("code") or "").strip()
        realized_pl = trade.get("realized_pl")
        if sold_date and trade_code and realized_pl is not None:
            key = (trade_code, sold_date)
            realized_by_key[key] = realized_by_key.get(key, 0.0) + float(realized_pl)
    selected_date = _position_change_date(trade_date) if trade_date else None
    if selected_date:
        enriched = [log for log in enriched
                    if _position_change_date(log.get("trade_date")) == selected_date]
    enriched = enriched[:max(1, int(display_limit))]
    if not enriched:
        label = selected_date.strftime("%Y/%m/%d") if selected_date else "這個範圍"
        return (f'<section class="position-journal"><div class="position-journal-head">'
                f'<h2>操作日報</h2><small>{html.escape(label)}<br>沒有操作紀錄</small></div>'
                f'<div class="position-journal-empty">這個日期範圍內沒有加碼／減碼紀錄。</div></section>')

    inst_data = inst_data or {}
    grouped = {}
    for log in enriched:
        grouped.setdefault(_position_change_date(log.get("trade_date")), []).append(log)

    day_sections = []
    for day, day_logs in grouped.items():
        day_text = day.strftime("%Y/%m/%d") if day else "日期待確認"
        row_parts = []
        attached_pnl_keys = set()
        for log in day_logs:
            code = str(log.get("code") or "")
            name = html.escape(str(stock_display_name(code, inst_data)))
            action = "加碼" if log.get("action") == "add" else "減碼"
            delta = int(log.get("shares_delta") or 0)
            before = int(log.get("shares_before") or 0)
            status_label = "新增" if action == "加碼" and before == 0 else action
            status_cls = "new" if status_label == "新增" else ("add" if action == "加碼" else "reduce")
            delta_text = f"{delta:+,} 股"
            delta_class = "up" if delta > 0 else "down"
            after = int(log.get("shares_after") or 0)
            change_pct = log.get("change_pct")
            change_text = ("100%" if status_label == "新增" else
                           (f"{change_pct:+.2f}%" if change_pct is not None else "待確認"))
            weight = log.get("current_weight")
            weight_text = f"{weight:.2f}%" if weight is not None else "待確認"
            event_weight = log.get("event_weight")
            event_weight_text = (f"{event_weight:+.2f}%"
                                 if event_weight is not None else "待確認")
            event_weight_class = ("up" if event_weight is not None and event_weight > 0 else
                                  "down" if event_weight is not None and event_weight < 0 else "flat")
            price = log.get("trade_price")
            price_text = f"成交／成本 {price:,.2f}" if price is not None else "成交價待確認"
            note = html.escape(str(log.get("note") or ""))
            note_html = f'<small>{note}</small>' if note else ''
            pnl_key = (code, day)
            realized_pl = realized_by_key.get(pnl_key)
            if pnl_key in attached_pnl_keys:
                pnl_html = ''
            elif action == "減碼" and realized_pl is not None:
                attached_pnl_keys.add(pnl_key)
                pnl_cls = "up" if realized_pl >= 0 else "down"
                pnl_html = (f'<small class="position-journal-pnl {pnl_cls}">'
                             f'已實現損益 {realized_pl:+,.0f}</small>')
            elif action == "減碼":
                pnl_html = '<small class="position-journal-pnl flat">已實現損益 待確認</small>'
            else:
                pnl_html = '<small class="position-journal-pnl flat">已實現損益 —</small>'
            row_parts.append(f'''<div class="position-journal-row">
  <div class="position-journal-name"><b>{name}</b><small>{html.escape(code)} · {price_text}</small></div>
  <div class="position-journal-status"><span class="position-journal-badge {status_cls}">{status_label}</span></div>
  <div class="position-journal-cell"><b class="{delta_class}">{delta_text}</b><small>{before:,} → {after:,} 股</small></div>
  <div class="position-journal-cell"><b>{html.escape(change_text)}</b><small>相對操作前</small></div>
  <div class="position-journal-cell"><b>{html.escape(weight_text)}</b><small class="{event_weight_class}">{html.escape(event_weight_text)}</small>{pnl_html}{note_html}</div>
</div>''')
        day_sections.append(f'<div class="position-journal-day">{day_text}</div>{"".join(row_parts)}')

    filter_text = (selected_date.strftime("%Y/%m/%d") if selected_date else "全部日期")
    return f'''<section class="position-journal">
  <div class="position-journal-head"><h2>操作日報</h2><small>{len(enriched)} 筆操作<br>{html.escape(filter_text)}</small></div>
  <div class="position-journal-note">記錄加碼／減碼後的持股變化。<b>目前權重</b>＝最新可得價格 × 目前持股 ÷ 目前持股總市值；未輸入的現金與其他資產不會被假設加入分母。</div>
  <div class="position-journal-table-head"><span>標的</span><span>狀態</span><span>持股變動</span><span>變動幅度</span><span>目前權重<br>變動 %</span></div>
  {"".join(day_sections)}
  <div class="position-journal-foot">本次影響是以成交／成本價 × 股數變動估算，占目前已登錄持股總市值；不是個人化買賣建議。價格若查無有效資料，相關欄位維持待確認。</div>
</section>'''


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
    # rank stage 是收盤資料完成後的明確更新點，不能沿用當天稍早的頁面快取。
    clear_leaderboard_cache()
    full_value = build_leaderboard(top_n=100, days=365)
    boards, (series_map, market) = full_value
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
        # build_leaderboard() 已在 fresh compute 時保存前100名完整 payload；
        # 若本次命中既有 payload，也不在這裡用前20名覆蓋它。
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


_FAST_RANK_SUMMARY_CACHE_SECONDS = 30
_fast_rank_summary_cache = {}
_fast_rank_summary_cache_lock = threading.Lock()


def get_fast_rank_summary(user_id):
    """首頁 fast 專用：只讀最近兩次已保存名次，不重算全體排行榜。"""
    uid = str(user_id).strip()
    now = time.time()
    with _fast_rank_summary_cache_lock:
        cached = _fast_rank_summary_cache.get(uid)
        if cached and now - cached.get("at", 0) < _FAST_RANK_SUMMARY_CACHE_SECONDS:
            return cached["value"]
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
    with _fast_rank_summary_cache_lock:
        _fast_rank_summary_cache[uid] = {"at": time.time(), "value": result}
        if len(_fast_rank_summary_cache) > 1000:
            cutoff = time.time() - _FAST_RANK_SUMMARY_CACHE_SECONDS * 2
            for cache_key, entry in list(_fast_rank_summary_cache.items()):
                if entry.get("at", 0) < cutoff:
                    _fast_rank_summary_cache.pop(cache_key, None)
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
    # 每日收盤快照完成後，首頁 fast 不能繼續拿舊的個人名次摘要。
    fast_cache_lock = globals().get("_fast_rank_summary_cache_lock")
    fast_cache = globals().get("_fast_rank_summary_cache")
    if fast_cache_lock is not None and fast_cache is not None:
        with fast_cache_lock:
            fast_cache.clear()
    # 成員暱稱、是否公開持股或參加狀態變更後，持久化頁面也必須失效。
    try:
        _delete_shared_data_snapshot("leaderboard_page")
    except Exception as exc:
        print(f"⚠️ 排行榜快照失效處理失敗: {exc}")


def _leaderboard_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _leaderboard_data_date(series_map, market):
    """從已計算的真實曲線找出頁面可標示的最新資料日。"""
    dates = []
    for item in (series_map or {}).values():
        for point in (item.get("curve") or []):
            if isinstance(point, (list, tuple)) and point:
                parsed = _leaderboard_date(point[0])
                if parsed:
                    dates.append(parsed)
    for point in market or []:
        if isinstance(point, (list, tuple)) and point:
            parsed = _leaderboard_date(point[0])
            if parsed:
                dates.append(parsed)
    return max(dates) if dates else None


def _leaderboard_snapshot_valid(snapshot):
    """快照仍須接近目前資料日；週末只顯示最近交易日的真實曲線。"""
    if not snapshot:
        return False
    data_date = _leaderboard_date(snapshot.get("data_date"))
    if not data_date:
        return False
    today = taiwan_today()
    return data_date <= today and (today - data_date).days <= 3


def _load_persisted_leaderboard_page():
    """讀取排行榜完整頁 payload；失敗或過期時回傳 None，讓呼叫端走原流程。"""
    shared = _load_shared_data_snapshot(
        "leaderboard_page",
        max_age_seconds=_SHARED_SNAPSHOT_MAX_AGE.get("leaderboard_page", 3 * 86400),
    )
    if not shared or not _leaderboard_snapshot_valid(shared):
        return None
    source_meta = shared.get("source_meta") or {}
    # ETF 持股統計加入後，舊版快照沒有 etf_holdings，首次讀取時強制重算一次。
    if source_meta.get("schema_version") != 2:
        return None
    payload = shared.get("payload") or {}
    boards = payload.get("boards") if isinstance(payload, dict) else None
    graph = payload.get("graph") if isinstance(payload, dict) else None
    if not isinstance(boards, dict) or not isinstance(graph, dict):
        return None
    raw_series_map = graph.get("series_map")
    raw_market = graph.get("market")
    if not isinstance(raw_series_map, dict) or not isinstance(raw_market, list):
        return None

    # JSONB 會把 date 與 tuple 還原成字串與 list；圖表與既有計算函式
    # 仍以 date／(date, value) 工作，因此在讀取邊界一次還原，不改 UI 邏輯。
    series_map = {}
    for key, item in raw_series_map.items():
        if not isinstance(item, dict):
            continue
        curve = []
        for point in item.get("curve") or []:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            parsed = _leaderboard_date(point[0])
            if not parsed:
                continue
            try:
                curve.append((parsed, float(point[1])))
            except (TypeError, ValueError):
                continue
        series_map[str(key)] = {"nickname": str(item.get("nickname") or ""),
                                "curve": curve}
    market = []
    for point in raw_market:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        parsed = _leaderboard_date(point[0])
        if not parsed:
            continue
        try:
            market.append((parsed, float(point[1])))
        except (TypeError, ValueError):
            continue
    return {
        "value": (boards, (series_map, market)),
        "data_date": shared.get("data_date"),
        "computed_at": shared.get("computed_at"),
        "source_meta": shared.get("source_meta") or {},
    }


def _save_persisted_leaderboard_page(value, data_date=None):
    """保存與網頁 top20／365 日口徑完全相同的排行榜 payload。"""
    if not value or not data_date:
        return False
    boards, graph = value
    series_map, market = graph
    return _save_shared_data_snapshot(
        "leaderboard_page",
        {"boards": boards,
         "graph": {"series_map": series_map, "market": market}},
        data_date=data_date,
        source_meta={"source": "leaderboard_build", "schema_version": 2,
                     "top_n": 100, "days": 365, "member_count": len(boards.get("waiting", [])) +
                     len(boards.get("long", []))},
    )


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
    build_started = time.monotonic()
    now = time.time()
    with _leaderboard_cache_lock:
        cached = _leaderboard_cache.get(cache_key)
        if cached and now - cached["at"] < LEADERBOARD_CACHE_SECONDS:
            print("⏱️ 排行榜計算：記憶體快取 %.0fms" %
                  ((time.monotonic() - build_started) * 1000))
            return cached["value"]

    # 排行榜的內容只依賴已保存的每日組合快照與成員公開設定；
    # Render 重啟或切換 worker 後，先讀完整頁 payload，避免重新抓所有公開持股的一年行情。
    if cache_key in ((20, 365), (100, 365)):
        persisted = _load_persisted_leaderboard_page()
        if persisted:
            stored_boards, stored_graph = persisted["value"]
            # 網頁顯示前 20 名，但保留前 100 名給 get_my_rank_summary()，
            # 與原本排行榜頁面「前20顯示、前100個人摘要」的功能完全一致。
            value = (
                {"long": (stored_boards.get("long") or [])[:int(top_n)],
                 "short": (stored_boards.get("short") or [])[:int(top_n)],
                 "waiting": stored_boards.get("waiting") or []},
                stored_graph,
            )
            with _leaderboard_cache_lock:
                _leaderboard_cache[cache_key] = {
                    "at": now, "value": value, "source": "persisted",
                    "data_date": persisted.get("data_date"),
                }
            print("⚡ 排行榜改讀 Supabase 完整快照（資料日 %s），目前取前 %s 名；耗時 %.0fms" %
                  (persisted.get("data_date") or "未標日期", top_n,
                   (time.monotonic() - build_started) * 1000))
            return value

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

        member_positions = positions_map.get(str(uid), [])
        etf_holdings = sum(1 for position in member_positions
                           if is_etf(position.get("code")))
        holds = (summarize_member_holdings(
            uid, prices, inst,
            positions=member_positions, ind_map=ind_map,
            joined_on=joined)
                 if show else None)
        base = {
            "user_id": str(uid),
            "nickname": nick,
            "holdings": len(member_positions),
            "etf_holdings": etf_holdings,
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
    long_all = sorted(scored, key=lambda r: r["ret"], reverse=True)
    short_all = sorted([r for r in scored if r["m30"] is not None],
                       key=lambda r: r["m30"], reverse=True)
    long_board = long_all[:top_n]
    short_board = short_all[:top_n]
    waiting = [r for r in rows if r["ret"] is None]
    value = ({"long": long_board, "short": short_board, "waiting": waiting},
             (series_map, market))
    data_date = _leaderboard_data_date(series_map, market)
    with _leaderboard_cache_lock:
        _leaderboard_cache[cache_key] = {
            "at": time.time(), "value": value, "source": "computed",
            "data_date": data_date,
        }
    if cache_key in ((20, 365), (100, 365)) and data_date:
        persisted_value = (
            {"long": long_all[:100], "short": short_all[:100],
             "waiting": waiting},
            (series_map, market),
        )
        saved_page = _save_persisted_leaderboard_page(
            persisted_value, data_date=data_date)
        print("⚡ 排行榜完整頁快照%s（資料日 %s）" %
              ("已保存" if saved_page else "保存失敗", data_date))
    print("⏱️ 排行榜計算：完整計算 %.0fms" %
          ((time.monotonic() - build_started) * 1000))
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


def get_missing_portfolio_snapshot_user_ids(snapshot_date=None):
    """找出有持股但指定資料日尚未保存 portfolio snapshot 的使用者。

    每日快照可能在某一位使用者處理後逾時或失敗，checkpoint 會讓下一次從
    中間續跑；若工作最後被標成完成，仍要能辨識漏掉的 user，不能把「完成」
    當成所有使用者都已保存。
    """
    snapshot_date = snapshot_date or taiwan_today()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT p.user_id
            FROM positions p
            LEFT JOIN portfolio_snapshots s
              ON s.user_id = p.user_id AND s.snapshot_date = %s
            WHERE s.user_id IS NULL
            ORDER BY p.user_id
        """, (snapshot_date,))
        ids = [r[0] for r in cursor.fetchall()]
        cursor.close()
        return ids
    except Exception as e:
        print(f"⚠️ 檢查缺少每日組合快照失敗: {e}")
        # 檢查失敗時不捏造「完整」；回傳 None 讓呼叫端保守維持既有狀態。
        return None
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
_TWSE_MIS_CACHE = {"day": None, "at": 0, "data": {}}
_TWSE_MIS_CACHE_SECONDS = 15
_TWSE_MIS_BATCH_SIZE = 80


def _taiwan_post_close(now=None):
    """13:30 後才可把官方 MIS 的最後成交價視為當日正式收盤口徑。"""
    current = now if now is not None else taiwan_now()
    return current.weekday() < 5 and (current.hour > 13 or
                                      (current.hour == 13 and current.minute >= 30))


def _twse_mis_quote_symbols(codes, market_suffix=None):
    symbols = []
    for code in codes:
        suffix = market_suffix.get(code) if isinstance(market_suffix, dict) else market_suffix
        suffix = str(suffix or "").upper()
        if suffix == ".TW":
            symbols.append(f"tse_{code}.tw")
        elif suffix == ".TWO":
            symbols.append(f"otc_{code}.tw")
        else:
            # 未知市場才查兩種；已知市場只查一種，避免端點與結果重複。
            symbols.extend((f"tse_{code}.tw", f"otc_{code}.tw"))
    return symbols


def _fetch_twse_mis_quotes(codes, market_suffix=None, force_refresh=False):
    """批次讀 TWSE MIS；13:30 後取 z/y，確保使用最後市撮／正式收盤價。

    回傳格式與 realtime quote 可合併，但只保留官方明確回傳且日期為台灣
    今日的項目。查不到時回空 dict，呼叫端繼續使用既有 Yahoo 降級路徑。
    """
    codes = list(dict.fromkeys(str(code).strip() for code in (codes or []) if code))
    if not codes or not _taiwan_post_close():
        return {}
    today = taiwan_today().isoformat()
    now = time.time()
    with _realtime_cache_lock:
        if (not force_refresh and _TWSE_MIS_CACHE.get("day") == today and
                now - _TWSE_MIS_CACHE.get("at", 0) < _TWSE_MIS_CACHE_SECONDS):
            cached = _TWSE_MIS_CACHE.get("data") or {}
            if all(code in cached for code in codes):
                return {code: cached[code] for code in codes}
            # 只要有任一代號尚未進入快取，就重新查詢這一批，
            # 不讓缺少的代號回退到 Yahoo 可能停在 13:30 前的舊價。

    symbols = _twse_mis_quote_symbols(codes, market_suffix)
    parsed = {}
    for start in range(0, len(symbols), _TWSE_MIS_BATCH_SIZE):
        batch = symbols[start:start + _TWSE_MIS_BATCH_SIZE]
        try:
            response = requests.get(
                "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                params={"ex_ch": "|".join(batch), "json": "1", "delay": "0",
                        "_": int(time.time() * 1000)},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            payload = response.json()
        except Exception as exc:
            print(f"⚠️ TWSE MIS 收盤快照失敗（{len(batch)} 檔）: {exc}")
            continue
        for item in payload.get("msgArray") or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("c") or "").strip()
            if code not in codes or str(item.get("d") or "") != today.replace("-", ""):
                continue
            def number(key):
                try:
                    value = str(item.get(key) or "").replace(",", "").strip()
                    return None if value in ("", "-", "--") else float(value)
                except (TypeError, ValueError):
                    return None
            close = number("z") or number("pz")
            previous = number("y")
            if close is None or close <= 0 or previous is None or previous <= 0:
                continue
            volume_lots = number("v")
            parsed[code] = {
                "close": close,
                "previous_close": previous,
                "pct": (close - previous) / previous * 100,
                "high": number("h") or close,
                "low": number("l") or close,
                "volume": int(volume_lots * 1000) if volume_lots is not None else 0,
                "updated_at": f"{item.get('d')} {item.get('t')}".strip(),
                "source": "TWSE MIS 最後成交／收盤集合競價",
                "close_is_final": True,
                "close_date": item.get("d"),
                "close_time": item.get("t"),
            }
    with _realtime_cache_lock:
        merged = dict(_TWSE_MIS_CACHE.get("data") or {})
        merged.update(parsed)
        _TWSE_MIS_CACHE.update({"day": today, "at": time.time(), "data": merged})
    return {code: merged[code] for code in codes if code in merged}


def get_realtime_stock(code, rng="3mo", market_suffix=None, force_refresh=False,
                       official_quote=None):
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
    cache_session = "postclose" if _taiwan_post_close() else "intraday"
    cache_key = f"{code}:{rng}:{cache_day}:{cache_session}"
    now = time.time()
    with _realtime_cache_lock:
        cached = _realtime_cache.get(cache_key)
        if (cached and not force_refresh and
                now - cached["at"] < REALTIME_CACHE_SECONDS):
            return cached["data"]

    # 已知後綴排前面試，未知就照原順序
    known = _suffix_cache.get(code)
    if market_suffix in (".TW", ".TWO"):
        suffixes = [market_suffix]
    else:
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

            # 13:30 後官方 MIS 的 z/y 優先於 Yahoo regularMarketPrice；
            # Yahoo 在收盤集合競價後可能仍停在盤中最後成交，不能拿來代表正式收盤。
            official_quote = official_quote or (
                _fetch_twse_mis_quotes([code], {code: suffix}).get(code)
                if _taiwan_post_close() else None)
            close = (official_quote.get("close") if official_quote else
                     meta.get('regularMarketPrice', 0.0))
            # 官方 MIS 偶爾短暫沒有回傳個別代號；收盤後若 Yahoo 日 K
            # 已經有今天的最後一根，使用該日 K close，不能退回盤中 meta 價。
            if (not official_quote and _taiwan_post_close() and bars and
                    bars[-1][0] == today_date):
                close = bars[-1][1]
            if not close or close == 0:
                close = bars[-1][1] if bars else 0.0

            # 判斷「日K序列」最後一筆到底是不是今天：
            # - 是今天 → 昨收 = 倒數第二筆
            # - 還停在昨天（Yahoo 資料還沒更新到今天）→ 倒數第一筆本身才是昨收，
            #   不能再往前抓倒數第二筆，不然會變成抓到前天，算出兩天以上的
            #   累積漲幅，誤標成「當日漲幅」。
            if official_quote:
                prev_close = official_quote.get("previous_close") or meta.get("chartPreviousClose", close)
                hist = bars[:-1] if bars and bars[-1][0] == today_date else bars
            elif bars and bars[-1][0] == today_date:
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
            high = ((official_quote.get("high") if official_quote else None) or
                    meta.get('regularMarketDayHigh', close) or close)
            low = ((official_quote.get("low") if official_quote else None) or
                   meta.get('regularMarketDayLow', close) or close)
            volume = ((official_quote.get("volume") if official_quote else None) or
                      meta.get('regularMarketVolume', 0) or 0)

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
                "source": (official_quote.get("source") if official_quote else
                            ("Yahoo Finance 今日最後日K（官方 MIS 暫缺）"
                             if _taiwan_post_close() and bars and bars[-1][0] == today_date
                             else "Yahoo Finance 日線行情")),
                "close_is_final": bool(official_quote),
                "close_date": (official_quote.get("close_date") if official_quote else
                               today_date.strftime("%Y%m%d")),
                "close_time": (official_quote.get("close_time") if official_quote else None),
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


def get_realtime_stocks_bulk(codes, workers=12, rng="3mo", market_suffix=None,
                             force_refresh=False):
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
    def suffix_for(code):
        if isinstance(market_suffix, dict):
            return market_suffix.get(code)
        return market_suffix

    # 收盤後先批次取得官方 MIS 最後成交／市撮價格；同一次頁面請求的
    # 所有檔案共用這份 map，避免首頁與持股頁各自落到不同的 Yahoo 時點。
    official_quotes = _fetch_twse_mis_quotes(
        codes, market_suffix=market_suffix, force_refresh=force_refresh)

    if len(codes) == 1:  # 只有一檔就不必付出開執行緒池的成本
        return {codes[0]: get_realtime_stock(
            codes[0], rng, market_suffix=suffix_for(codes[0]),
            force_refresh=force_refresh,
            official_quote=official_quotes.get(codes[0]))}

    def safe_fetch(c):
        # 單檔失敗不能拖垮整批，一律吞掉例外回 None，交由呼叫端顯示「查無行情」
        try:
            return get_realtime_stock(
                c, rng, market_suffix=suffix_for(c),
                force_refresh=force_refresh,
                official_quote=official_quotes.get(c))
        except Exception as e:
            print(f"⚠️ 並行抓取失敗 {c}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=min(workers, len(codes))) as ex:
        return dict(zip(codes, ex.map(safe_fetch, codes)))


_RADAR_SPARK_CACHE = {"at": 0, "day": None, "data": {}}
_RADAR_SPARK_CACHE_LOCK = threading.Lock()
RADAR_SPARK_CACHE_SECONDS = 15
# Yahoo spark 實測單次最多約 20 個 symbols；依 market map 選單一後綴，
# 只有未知市場代號才雙後綴，避免超過端點上限造成整批 400。
RADAR_SPARK_BATCH_SIZE = 20
RADAR_SPARK_MAX_WORKERS = 12
RADAR_DEEP_SCAN_LIMIT = 240
# 定時 producer 以全市場 spark 初篩為主；深度技術補抓只處理前 48 檔，
# 讓十分鐘快照週期有機會穩定完成，網頁仍可用手動完整分析上限 240。
RADAR_SCHEDULED_DEEP_SCAN_LIMIT = 48


def _fetch_yahoo_spark_bulk(codes, rng="3mo", force_refresh=False,
                            market_map=None):
    """用 Yahoo spark 分批取全市場輕量即時報價；完整技術欄位再由候選股補抓。

    market_map 來自 stock_info 的官方市場欄位；能判斷上市／上櫃時只送一個
    Yahoo 後綴，未知代號才送雙後綴，並把每個 HTTP 批次控制在端點可接受上限。
    """
    codes = list(dict.fromkeys(str(code).strip().upper() for code in codes
                              if re.fullmatch(r"\d{4}", str(code).strip())))
    if not codes:
        return {}
    cache_day = taiwan_now().date().isoformat()
    now = time.time()
    with _RADAR_SPARK_CACHE_LOCK:
        if (not force_refresh and _RADAR_SPARK_CACHE.get("day") == cache_day and
                now - _RADAR_SPARK_CACHE.get("at", 0) < RADAR_SPARK_CACHE_SECONDS):
            cached = _RADAR_SPARK_CACHE.get("data") or {}
            return {code: cached[code] for code in codes if code in cached}

    market_map = market_map or {}
    symbols = []
    for code in codes:
        market = str(market_map.get(code) or "").strip().lower()
        if market in ("上市", "twse", "listed", "tw"):
            symbols.append(f"{code}.TW")
        elif market in ("上櫃", "上柜", "tpex", "otc", "two"):
            symbols.append(f"{code}.TWO")
        else:
            # 舊 stock_info 或測試資料沒有 market 時保留正確性，但只讓這一檔
            # 佔兩個 symbols，不會把整批無條件加倍。
            symbols.extend((f"{code}.TW", f"{code}.TWO"))
    batches = [symbols[index:index + RADAR_SPARK_BATCH_SIZE]
               for index in range(0, len(symbols), RADAR_SPARK_BATCH_SIZE)]

    def fetch_batch(batch_symbols):
        symbols_param = ",".join(batch_symbols)
        try:
            response = requests.get(
                "https://query1.finance.yahoo.com/v7/finance/spark",
                params={"symbols": symbols_param, "range": rng, "interval": "1d"},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            response.raise_for_status()
            payload = response.json().get("spark", {}).get("result") or []
        except Exception as exc:
            print(f"⚠️ Yahoo spark 批次行情失敗（{len(batch_symbols)} 個 symbols）: {exc}")
            return {}
        parsed = {}
        for item in payload:
            response_list = item.get("response") or []
            if not response_list:
                continue
            raw = response_list[0] or {}
            symbol = str(item.get("symbol") or raw.get("meta", {}).get("symbol") or "")
            match = re.match(r"^(\d{4})\.(?:TW|TWO)$", symbol.upper())
            if not match:
                continue
            code = match.group(1)
            meta = raw.get("meta") or {}
            quote = ((raw.get("indicators") or {}).get("quote") or [{}])[0] or {}
            closes = [float(value) for value in (quote.get("close") or [])
                      if value is not None and float(value) > 0]
            close = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
            # chartPreviousClose 在部分台股商品會落到較早的調整基準，
            # 造成 1709／2605 等即時漲幅被放大，雷達便會錯誤排除或誤判。
            # 只要有最近兩個實際日線收盤，使用 closes[-2] 作為今日漲跌基準。
            previous = closes[-2] if len(closes) >= 2 else meta.get("chartPreviousClose")
            if close is None or previous in (None, 0):
                continue
            volume = meta.get("regularMarketVolume") or 0
            try:
                close = float(close)
                previous = float(previous)
                volume = int(float(volume or 0))
            except (TypeError, ValueError):
                continue
            # 若極少數代號同時回傳兩個市場後綴，優先採上市結果，
            # 不讓回應順序決定結果；一般股票只會命中其中一個。
            if code in parsed and symbol.upper().endswith(".TWO"):
                continue
            parsed[code] = {
                "close": close,
                "pct": (close - previous) / previous * 100,
                "volume": volume,
                "previous_close": previous,
                "updated_at": meta.get("regularMarketTime"),
                # spark 是盤中／輕量行情，不共享個股收盤 MIS 變數；
                # 這裡不能引用 get_realtime_stock 作用域內的 official_quote 或 today_date。
                "source": "Yahoo spark 批次行情",
                "close_is_final": False,
                "close_date": None,
                "close_time": None,
            }
        return parsed

    parsed = {}
    max_workers = int(globals().get("RADAR_SPARK_MAX_WORKERS", 8) or 8)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(batches))) as executor:
        futures = [executor.submit(fetch_batch, batch) for batch in batches]
        for future in futures:
            parsed.update(future.result())

    with _RADAR_SPARK_CACHE_LOCK:
        _RADAR_SPARK_CACHE.update({"at": time.time(), "day": cache_day, "data": parsed})
    return parsed


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
_market_cache = {"map": None}
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
            markets = payload.get("markets") or {}
            if names:
                _name_cache["map"] = names
            if industries:
                _industry_cache["map"] = industries
            if markets:
                _market_cache["map"] = markets
            _stock_info_file_loaded_mtime = file_mtime
        except FileNotFoundError:
            return
        except Exception as exc:
            print(f"⚠️ 讀取 stock_info 檔案快取失敗: {exc}")


def _write_stock_info_file_cache():
    """以原子替換寫入 stock_info 快照，避免 worker 讀到半份 JSON。"""
    payload = {"names": _name_cache.get("map") or {},
               "industries": _industry_cache.get("map") or {},
               "markets": _market_cache.get("map") or {},
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


_SHARED_SNAPSHOT_MAX_AGE = {
    "stock_info_map": 7 * 86400,
    "monthly_revenue": 3 * 86400,
    "valuation": 2 * 86400,
    "screener_blackhorse": 3 * 86400,
    "screener_radar": 3 * 86400,
    "leaderboard_page": 3 * 86400,
    "chips_superman": 3 * 86400,
    "etf_catalog": 86400,
    "etf_product_metrics": 3 * 86400,
    "etf_distribution_history": 3 * 86400,
    "turning_observation": 900,
    "etf_product_rankings": 3 * 86400,
}


def _save_shared_data_snapshot(snapshot_key, payload, data_date=None,
                               source_meta=None):
    """把可追溯的非即時資料保存到 Supabase；失敗不可阻塞原本流程。"""
    if not snapshot_key or payload is None:
        return False
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shared_data_snapshots
                (snapshot_key, data_date, computed_at, payload, source_meta)
            VALUES (%s, %s, NOW(), CAST(%s AS JSONB), CAST(%s AS JSONB))
            ON CONFLICT (snapshot_key) DO UPDATE SET
                data_date = EXCLUDED.data_date,
                computed_at = EXCLUDED.computed_at,
                payload = EXCLUDED.payload,
                source_meta = EXCLUDED.source_meta
            """,
            (str(snapshot_key), data_date,
             json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
             json.dumps(source_meta or {}, ensure_ascii=False,
                        separators=(",", ":"))),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as exc:
        conn.rollback()
        print(f"⚠️ 保存共享資料快照失敗 {snapshot_key}: {exc}")
        return False
    finally:
        release_db_connection(conn)


def _delete_shared_data_snapshot(snapshot_key):
    """刪除需要立即失效的共享快照；失敗只記錄，不阻塞原本寫入。"""
    if not snapshot_key:
        return False
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM shared_data_snapshots WHERE snapshot_key = %s",
                    (str(snapshot_key),))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        return deleted > 0
    except Exception as exc:
        conn.rollback()
        print(f"⚠️ 刪除共享資料快照失敗 {snapshot_key}: {exc}")
        return False
    finally:
        release_db_connection(conn)


def _load_shared_data_snapshot(snapshot_key, max_age_seconds=None):
    """讀取 Supabase 共享快照；過期、空資料或資料庫錯誤都回傳 None。"""
    max_age = (_SHARED_SNAPSHOT_MAX_AGE.get(str(snapshot_key), 86400)
               if max_age_seconds is None else float(max_age_seconds))
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT data_date, computed_at, payload, source_meta
            FROM shared_data_snapshots
            WHERE snapshot_key = %s
            LIMIT 1
            """,
            (str(snapshot_key),),
        )
        row = cur.fetchone()
        cur.close()
    except Exception as exc:
        print(f"⚠️ 讀取共享資料快照失敗 {snapshot_key}: {exc}")
        row = None
    finally:
        release_db_connection(conn)
    if not row:
        return None
    data_date, computed_at, payload, source_meta = row
    try:
        if computed_at:
            computed_at = (computed_at if computed_at.tzinfo
                           else computed_at.replace(tzinfo=timezone.utc))
            age = (datetime.now(timezone.utc) - computed_at).total_seconds()
            if age < 0 or age > max_age:
                return None
        if not payload:
            return None
        return {"data_date": data_date, "computed_at": computed_at,
                "payload": payload, "source_meta": source_meta or {}}
    except Exception as exc:
        print(f"⚠️ 解析共享資料快照失敗 {snapshot_key}: {exc}")
        return None


def _stock_info_from_shared_snapshot():
    snapshot = _load_shared_data_snapshot("stock_info_map")
    if not snapshot:
        return False
    payload = snapshot.get("payload") or {}
    names = payload.get("names") or {}
    industries = payload.get("industries") or {}
    markets = payload.get("markets") or {}
    if names:
        _name_cache["map"] = names
    if industries:
        _industry_cache["map"] = industries
    if markets:
        _market_cache["map"] = markets
    loaded = bool(names or industries or markets)
    if loaded:
        print(f"⚡ stock_info 改讀 Supabase 快照（{snapshot.get('data_date') or '未標日期'}）")
    return loaded


def get_name_map(force_reload=False):
    """
    代號→公司名稱。來自 stock_info（含上市、上櫃、興櫃），
    比程式裡那份只有十幾檔的寫死對照表完整得多。
    """
    _load_stock_info_file_cache()
    if _name_cache["map"] is not None and not force_reload:
        return _name_cache["map"]
    if not force_reload and _stock_info_from_shared_snapshot():
        if _name_cache.get("map") is not None:
            return _name_cache["map"]
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code, name FROM stock_info WHERE name IS NOT NULL AND name <> ''")
        _name_cache["map"] = {c: n for c, n in cursor.fetchall()}
        cursor.close()
        _write_stock_info_file_cache()
        _save_shared_data_snapshot(
            "stock_info_map",
            {"names": _name_cache.get("map") or {},
             "industries": _industry_cache.get("map") or {},
             "markets": _market_cache.get("map") or {}},
            data_date=taiwan_today(),
            source_meta={"source": "stock_info", "kind": "names"},
        )
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
    """回傳 {代號: 產業別}。讀一次就快取在記憶體，並跨 worker 重用快照。"""
    _load_stock_info_file_cache()
    if _industry_cache["map"] is not None and not force_reload:
        return _industry_cache["map"]
    if not force_reload and _stock_info_from_shared_snapshot():
        if _industry_cache.get("map") is not None:
            return _industry_cache["map"]
    conn = get_db_connection()

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code, industry FROM stock_info WHERE industry IS NOT NULL AND industry <> ''")
        rows = cursor.fetchall()
        cursor.close()
        _industry_cache["map"] = {code: ind for code, ind in rows}
        _write_stock_info_file_cache()
        _save_shared_data_snapshot(
            "stock_info_map",
            {"names": _name_cache.get("map") or {},
             "industries": _industry_cache.get("map") or {},
             "markets": _market_cache.get("map") or {}},
            data_date=taiwan_today(),
            source_meta={"source": "stock_info", "kind": "industries"},
        )
        return _industry_cache["map"]
    except Exception as e:
        print(f"❌ 讀取產業別失敗: {e}")
        return {}
    finally:
        release_db_connection(conn)


def get_market_map(force_reload=False):
    """回傳 {代號: 市場}，供即時行情選擇正確的 Yahoo 後綴。"""
    _load_stock_info_file_cache()
    if _market_cache["map"] is not None and not force_reload:
        return _market_cache["map"]
    if not force_reload and _stock_info_from_shared_snapshot():
        if _market_cache.get("map") is not None:
            return _market_cache["map"]
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code, market FROM stock_info WHERE market IS NOT NULL AND market <> ''")
        _market_cache["map"] = {str(code).strip(): str(market).strip()
                                 for code, market in cursor.fetchall()}
        cursor.close()
        _write_stock_info_file_cache()
        _save_shared_data_snapshot(
            "stock_info_map",
            {"names": _name_cache.get("map") or {},
             "industries": _industry_cache.get("map") or {},
             "markets": _market_cache.get("map") or {}},
            data_date=taiwan_today(),
            source_meta={"source": "stock_info", "kind": "markets"},
        )
        return _market_cache["map"]
    except Exception as e:
        print(f"❌ 讀取市場別失敗: {e}")
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
_valuation_cache = {"date": None, "data": {},
                    "source": "none", "source_date": None}
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
        _valuation_cache["source"] = "file"
        _valuation_cache["source_date"] = today
        print("⚡ 估值改讀同日檔案快照，共 %s 筆" % len(file_data))
        return file_data

    shared = _load_shared_data_snapshot("valuation")
    shared_data = (shared.get("payload") if shared else None) or {}
    if isinstance(shared_data, dict) and shared_data:
        _valuation_cache["date"] = today
        _valuation_cache["data"] = shared_data
        _valuation_cache["source"] = "shared"
        _valuation_cache["source_date"] = shared.get("data_date")
        print("⚡ 估值改讀 Supabase 快照（來源日 %s），共 %s 筆" %
              (shared.get("data_date") or "未標日期", len(shared_data)))
        return shared_data

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
        _valuation_cache["source"] = "external"
        _valuation_cache["source_date"] = taiwan_today()
        _write_valuation_file_cache(today, result)
        _save_shared_data_snapshot(
            "valuation", result, data_date=taiwan_today(),
            source_meta={"source": "TWSE+TPEx", "retrieved_on": today},
        )
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
        samples = []
        for sample_ret, sample_pick in buckets[label]:
            sample_base = taiex_by_date.get(sample_pick["date"])
            sample_market = None
            if sample_base and taiex_now:
                sample_market = (taiex_now - sample_base) / sample_base * 100
            samples.append({
                "date": sample_pick["date"],
                "code": sample_pick["code"],
                "name": sample_pick.get("name") or sample_pick["code"],
                "rank": sample_pick.get("rank"),
                "score": sample_pick.get("score"),
                "ret": sample_ret,
                "market": sample_market,
                "excess": (sample_ret - sample_market)
                          if sample_market is not None else None,
            })
        result[label] = {
            "n": n,
            "avg": sum(vals) / n,
            "median": median,
            "win_rate": len([v for v in vals if v > 0]) / n * 100,
            "best": max(buckets[label], key=lambda x: x[0]),
            "worst": min(buckets[label], key=lambda x: x[0]),
            "market": (sum(mk) / len(mk)) if mk else None,
            "samples": samples,
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


def get_institutional_shift_candidates(prior_days=5, top_n=12):
    """找出近幾日法人方向反轉或異常放大的標的；只使用已保存的 T86 歷史。"""
    try:
        prior_days = max(3, int(prior_days))
        top_n = max(1, int(top_n))
    except (TypeError, ValueError):
        prior_days, top_n = 5, 12
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT trade_date
            FROM inst_history
            ORDER BY trade_date DESC
            LIMIT %s
            """,
            (prior_days + 1,),
        )
        dates = [row[0] for row in cursor.fetchall()]
        if len(dates) < prior_days + 1:
            cursor.close()
            return []
        latest_date = dates[0]
        cursor.execute(
            """
            SELECT code, MAX(name) AS name, trade_date,
                   COALESCE(foreign_net_lots, 0),
                   COALESCE(trust_net_lots, 0),
                   COALESCE(dealer_net_lots, 0),
                   COALESCE(total_net_lots, 0)
            FROM inst_history
            WHERE trade_date = ANY(%s)
              AND length(code) = 4 AND code ~ '^[0-9]+$'
              AND code NOT LIKE '00%%'
            GROUP BY code, trade_date,
                     foreign_net_lots, trust_net_lots,
                     dealer_net_lots, total_net_lots
            ORDER BY code, trade_date DESC
            """,
            (dates,),
        )
        rows = cursor.fetchall()
        cursor.close()
    except Exception as exc:
        print(f"❌ 查詢法人方向突變失敗: {exc}")
        return []
    finally:
        release_db_connection(conn)

    by_code = {}
    for code, name, trade_date, foreign, trust, dealer, total in rows:
        item = by_code.setdefault(str(code), {"name": name or code, "rows": {}})
        item["rows"][trade_date] = {
            "foreign": int(foreign or 0),
            "trust": int(trust or 0),
            "dealer": int(dealer or 0),
            "total": int(total or 0),
        }

    investor_names = {"foreign": "外資", "trust": "投信", "dealer": "自營商"}
    candidates = []
    for code, item in by_code.items():
        latest = item["rows"].get(latest_date)
        prior = [item["rows"][d] for d in dates[1:] if d in item["rows"]]
        if not latest or len(prior) < prior_days:
            continue

        prior_avg = {
            key: sum(row[key] for row in prior) / len(prior)
            for key in ("foreign", "trust", "dealer", "total")
        }
        current_total = latest["total"]
        prior_total = prior_avg["total"]
        changed = []
        for key in ("foreign", "trust", "dealer"):
            current = latest[key]
            before = prior_avg[key]
            if current > 0 and before < 0:
                changed.append(f"{investor_names[key]}轉買")
            elif current < 0 and before > 0:
                changed.append(f"{investor_names[key]}轉賣")

        total_reversal = ((current_total > 0 and prior_total < 0) or
                          (current_total < 0 and prior_total > 0))
        avg_abs_total = sum(abs(row["total"]) for row in prior) / len(prior)
        magnitude_ratio = (abs(current_total) / avg_abs_total
                           if avg_abs_total > 0 else 0.0)
        magnitude_spike = (current_total != 0 and avg_abs_total > 0 and
                           magnitude_ratio >= 2.5)
        if not total_reversal and not changed and not magnitude_spike:
            continue

        current_signs = [latest[key] > 0 for key in ("foreign", "trust", "dealer")]
        current_neg_signs = [latest[key] < 0 for key in ("foreign", "trust", "dealer")]
        if all(current_signs):
            consensus = "三方同步買超"
        elif all(current_neg_signs):
            consensus = "三方同步賣超"
        elif ((latest["foreign"] > 0 and latest["trust"] > 0) or
              (latest["foreign"] < 0 and latest["trust"] < 0)):
            consensus = "外資、投信同向"
        else:
            consensus = "法人分歧"

        if total_reversal:
            event_type = "賣轉買" if current_total > 0 else "買轉賣"
            priority = 3
        elif changed:
            event_type = "單一法人轉向"
            priority = 2
        else:
            event_type = "買超放大" if current_total > 0 else "賣超放大"
            priority = 1
        priority += min(len(changed), 2)
        if magnitude_spike:
            priority += 1

        candidates.append({
            "code": code,
            "name": str(item["name"] or code),
            "event_type": event_type,
            "current_total_lots": current_total,
            "prior_avg_total_lots": round(prior_total, 1),
            "magnitude_ratio": round(magnitude_ratio, 2),
            "investor_changes": changed[:3],
            "consensus": consensus,
            "current": {key: latest[key] for key in ("foreign", "trust", "dealer", "total")},
            "prior_avg": {key: round(prior_avg[key], 1) for key in ("foreign", "trust", "dealer", "total")},
            "prior_days": len(prior),
            "data_date": str(latest_date),
            "priority": priority,
        })

    candidates.sort(key=lambda x: (x["priority"], abs(x["current_total_lots"]), x["code"]),
                    reverse=True)
    for item in candidates:
        item.pop("priority", None)
    return candidates[:top_n]


_TURNING_OBSERVATION_CACHE = {"at": 0, "data": None}
# 轉折頁需要比一般即時報價更穩定的批次快取；T86 與收盤資料更新後由 warmup 重建。
TURNING_OBSERVATION_CACHE_SECONDS = 900
TURNING_OBSERVATION_SHARED_MAX_AGE = 900
TURNING_OBSERVATION_SNAPSHOT_KEY = "turning_observation"
TURNING_OBSERVATION_SCHEMA_VERSION = 2


def _turning_reason_details(inst_item, stock, direction, cross_up, cross_down,
                            up_streak, down_streak, vol_ratio, broke_support,
                            prior_days=5):
    """把轉折判斷用到的原始數值轉成逐項事實；不使用泛化的方向變化文案。"""
    current = inst_item.get("current") or {}
    prior_avg = inst_item.get("prior_avg") or {}
    investor_names = {"foreign": "外資", "trust": "投信", "dealer": "自營商"}

    def fmt_lots(value):
        try:
            return f"{float(value):+,.0f} 張"
        except (TypeError, ValueError):
            return "資料不足"

    def fmt_abs_lots(value):
        try:
            return f"{abs(float(value)):,.0f} 張"
        except (TypeError, ValueError):
            return "資料不足"

    def side_text(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "方向不明"
        if value > 0:
            return "買超"
        if value < 0:
            return "賣超"
        return "接近中性"

    def fmt_price(value):
        try:
            return f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return "資料不足"

    details = []
    for key in ("foreign", "trust", "dealer"):
        current_value = current.get(key)
        prior_value = prior_avg.get(key)
        if current_value is None or prior_value is None:
            continue
        name = investor_names[key]
        try:
            current_number = float(current_value)
            prior_number = float(prior_value)
        except (TypeError, ValueError):
            continue
        if current_number > 0 and prior_number < 0:
            details.append(
                f"{name}由前{prior_days}日平均賣超 {abs(prior_number):,.0f} 張，"
                f"轉為今日買超 {current_number:,.0f} 張")
        elif current_number < 0 and prior_number > 0:
            details.append(
                f"{name}由前{prior_days}日平均買超 {prior_number:,.0f} 張，"
                f"轉為今日賣超 {abs(current_number):,.0f} 張")

    total = current.get("total", inst_item.get("current_total_lots"))
    prior_total = prior_avg.get("total", inst_item.get("prior_avg_total_lots"))
    if total is not None:
        total_text = (f"三大法人今日{side_text(total)} {fmt_abs_lots(total)}")
        if prior_total is not None:
            total_text += f"；前{prior_days}日平均{side_text(prior_total)} {fmt_abs_lots(prior_total)}"
        details.append(total_text)

    ratio = inst_item.get("magnitude_ratio")
    try:
        if ratio is not None and float(ratio) >= 2.5:
            details.append(f"今日合計絕對值約為前{prior_days}日平均的 {float(ratio):.1f} 倍")
    except (TypeError, ValueError):
        pass

    close = stock.get("close")
    ma20 = stock.get("ma20")
    if cross_up and close is not None and ma20 is not None:
        details.append(f"收盤 {fmt_price(close)} 站回20日均線 {fmt_price(ma20)}")
    elif cross_down and close is not None and ma20 is not None:
        details.append(f"收盤 {fmt_price(close)} 跌破20日均線 {fmt_price(ma20)}")
    elif close is not None and ma20 is not None:
        relation = "高於" if float(close) >= float(ma20) else "低於"
        details.append(f"收盤 {fmt_price(close)}，目前{relation}20日均線 {fmt_price(ma20)}，本日未形成均線穿越")
    else:
        details.append("20日均線資料不足，無法判定站回或跌破")

    if direction == "up":
        details.append(f"連續上漲 {up_streak} 天" if up_streak else "今日未形成連續上漲")
    elif direction == "down":
        details.append(f"連續下跌 {down_streak} 天" if down_streak else "今日未形成連續下跌")

    if vol_ratio > 0:
        details.append(f"成交量約為20日均量 {vol_ratio:.1f} 倍")
    else:
        details.append("20日均量資料不足")

    support = stock.get("support")
    resistance = stock.get("resistance")
    if support is not None:
        if broke_support:
            details.append(f"現價 {fmt_price(close)} 已跌破近期支撐參考 {fmt_price(support)}")
        else:
            details.append(f"現價 {fmt_price(close)} 尚在近期支撐 {fmt_price(support)} 上方")
    else:
        details.append("近期支撐資料不足")
    if resistance is not None:
        details.append(f"近期壓力參考 {fmt_price(resistance)}")
    return details[:10]


def build_turning_observation(limit=60, prior_days=5, force_refresh=False):
    """以真實法人轉向搭配價格、均線與量能，建立轉折觀察三狀態。"""
    try:
        limit = max(10, min(int(limit), 120))
        prior_days = max(3, min(int(prior_days), 10))
    except (TypeError, ValueError):
        limit, prior_days = 60, 5
    now = time.time()
    with _realtime_cache_lock:
        cached = _TURNING_OBSERVATION_CACHE.get("data")
        if (not force_refresh and cached is not None and
                now - _TURNING_OBSERVATION_CACHE.get("at", 0) < TURNING_OBSERVATION_CACHE_SECONDS):
            return cached

    # 多 worker／重啟後先讀共享快照；這一步避免每個使用者重新並行抓數十檔 3mo 行情。
    try:
        shared = (None if force_refresh else _load_shared_data_snapshot(
            TURNING_OBSERVATION_SNAPSHOT_KEY,
            max_age_seconds=TURNING_OBSERVATION_SHARED_MAX_AGE))
        shared_payload = (shared.get("payload") if shared else None) or {}
        if (isinstance(shared_payload, dict) and isinstance(shared_payload.get("items"), list)
                and int(shared_payload.get("schema_version") or 0) >= TURNING_OBSERVATION_SCHEMA_VERSION
                and all(isinstance(item, dict) and item.get("reason_details")
                        for item in shared_payload.get("items") or [])):
            with _realtime_cache_lock:
                _TURNING_OBSERVATION_CACHE.update({"at": now, "data": shared_payload})
            return shared_payload
    except Exception as exc:
        print(f"⚠️ 讀取轉折觀察共享快照失敗: {exc}")

    institutional = get_institutional_shift_candidates(prior_days=prior_days, top_n=limit)
    if not institutional:
        result = {"data_date": None, "prior_days": prior_days, "items": []}
        with _realtime_cache_lock:
            _TURNING_OBSERVATION_CACHE["at"] = time.time()
            _TURNING_OBSERVATION_CACHE["data"] = result
        try:
            _save_shared_data_snapshot(
                TURNING_OBSERVATION_SNAPSHOT_KEY, result,
                data_date=taiwan_today(),
                source_meta={"source": "turning_observation", "item_count": 0})
        except Exception as exc:
            print(f"⚠️ 保存轉折觀察空快照失敗: {exc}")
        return result

    codes = [item.get("code") for item in institutional if item.get("code")]
    # 1mo 已涵蓋 20 日均線與近期位階；只有少數新上市／資料缺口標的才回補 3mo。
    prices = get_realtime_stocks_bulk(codes, workers=16, rng="1mo")
    fallback_codes = [code for code in codes
                      if len((prices.get(code) or {}).get("closes") or []) < 20]
    if fallback_codes:
        prices.update(get_realtime_stocks_bulk(fallback_codes, workers=12, rng="3mo"))
    items = []
    state_order = {"confirmed": 3, "observing": 2, "invalid": 1}
    for inst_item in institutional:
        code = str(inst_item.get("code") or "")
        stock = prices.get(code) or {}
        close = stock.get("close")
        if close is None:
            continue
        series = [float(x) for x in (stock.get("closes") or []) if x not in (None, 0)]
        if len(series) < 3:
            continue
        prev_close = series[-2]
        ma20 = stock.get("ma20")
        prev_window = series[:-1][-20:]
        prev_ma20 = sum(prev_window) / len(prev_window) if prev_window else None
        cross_up = bool(prev_ma20 and ma20 and prev_close < prev_ma20 <= close)
        cross_down = bool(prev_ma20 and ma20 and prev_close > prev_ma20 >= close)
        up_streak = int(stock.get("up_streak") or 0)
        down_streak = int(stock.get("down_streak") or 0)
        vol_ratio = float(stock.get("vol_ratio") or 0)
        broke_support = bool(stock.get("broke_support"))
        current_total = int(inst_item.get("current_total_lots") or 0)
        changes = inst_item.get("investor_changes") or []
        inst_up = current_total > 0 and (
            inst_item.get("event_type") == "賣轉買" or any("轉買" in str(x) for x in changes))
        inst_down = current_total < 0 and (
            inst_item.get("event_type") == "買轉賣" or any("轉賣" in str(x) for x in changes))
        direction = "up" if current_total > 0 else "down" if current_total < 0 else "neutral"
        if direction == "neutral":
            continue

        reasons = []
        score = 0
        if inst_up or inst_down:
            reasons.append("法人方向反轉")
            score += 1
        if cross_up or cross_down:
            reasons.append("站回20日均線" if cross_up else "跌破20日均線")
            score += 1
        if (direction == "up" and up_streak >= 2) or (direction == "down" and down_streak >= 2):
            reasons.append(f"連續{'上漲' if direction == 'up' else '下跌'} {up_streak if direction == 'up' else down_streak} 天")
            score += 1
        if vol_ratio >= 1.3:
            reasons.append(f"量能約20日均量 {vol_ratio:.1f} 倍")
            score += 1
        if (direction == "up" and not broke_support and close > (stock.get("low_20d") or close)):
            reasons.append("價格未跌破近期支撐")
            score += 1
        if direction == "down" and broke_support:
            reasons.append("近期支撐已跌破")
            score += 1

        invalid_reasons = []
        if direction == "up" and broke_support:
            support_reference = (stock.get("low_20d") or stock.get("low_60d")
                                 or stock.get("support"))
            try:
                if support_reference is not None and float(support_reference) >= float(close):
                    invalid_reasons.append(
                        f"現價 {float(close):,.2f} 已跌破近期支撐參考 {float(support_reference):,.2f}")
                else:
                    invalid_reasons.append(
                        f"現價 {float(close):,.2f} 下方未找到有效的近期支撐，支撐條件已失效")
            except (TypeError, ValueError):
                invalid_reasons.append(
                    f"現價 {float(close):,.2f} 已跌破近期支撐，支撐數值資料不足")
        if direction == "up" and down_streak >= 3:
            invalid_reasons.append(f"法人仍偏買方，但價格已連續下跌 {down_streak} 天")
        if direction == "down" and cross_up and up_streak >= 3:
            invalid_reasons.append(
                f"原本偏空，但收盤 {float(close):,.2f} 已站回20日均線 {float(ma20):,.2f}，且連續上漲 {up_streak} 天")
        if direction == "down" and cross_up and up_streak < 3:
            invalid_reasons.append(
                f"原本偏空，但收盤 {float(close):,.2f} 站回20日均線 {float(ma20):,.2f}")
        invalid = bool(invalid_reasons)
        reason_details = _turning_reason_details(
            inst_item, stock, direction, cross_up, cross_down,
            up_streak, down_streak, vol_ratio, broke_support, prior_days)
        if invalid:
            state = "invalid"
            state_label = "已失效"
            direction_label = "狀態已失效"
            state_reason = "；".join(invalid_reasons)
        elif score >= 3:
            state = "confirmed"
            state_label = "已確認"
            direction_label = "轉強" if direction == "up" else "轉弱"
            state_reason = "；".join(reason_details[:3]) or "已符合轉折確認條件"
        else:
            state = "observing"
            state_label = "觀察中"
            direction_label = "轉強" if direction == "up" else "轉弱"
            state_reason = "；".join(reason_details[:3]) or "目前資料不足以形成完整判讀"
        if invalid_reasons:
            reason_details = invalid_reasons + reason_details

        # 把「法人方向反轉」轉成使用者一眼可辨識的買賣流程；
        # event_type 與各法人 investor_changes 都來自已保存的 T86，不自行推測。
        event_type = str(inst_item.get("event_type") or "")
        if event_type == "賣轉買" or any("轉買" in str(x) for x in changes):
            direction_flow, direction_flow_label = "sell_to_buy", "賣轉買"
        elif event_type == "買轉賣" or any("轉賣" in str(x) for x in changes):
            direction_flow, direction_flow_label = "buy_to_sell", "買轉賣"
        elif current_total > 0:
            direction_flow, direction_flow_label = "buying_strength", "買方增強"
        elif current_total < 0:
            direction_flow, direction_flow_label = "selling_strength", "賣方增強"
        else:
            direction_flow, direction_flow_label = "unknown", "方向不明"

        items.append({
            "code": code,
            "name": inst_item.get("name") or stock_display_name(code, fallback=code),
            "direction": direction,
            "direction_label": direction_label,
            "direction_flow": direction_flow,
            "direction_flow_label": direction_flow_label,
            "state": state,
            "state_label": state_label,
            "state_reason": state_reason,
            "invalid_reasons": invalid_reasons,
            "event_type": inst_item.get("event_type") or "法人方向變化",
            "consensus": inst_item.get("consensus") or "法人分歧",
            "current_total_lots": current_total,
            "magnitude_ratio": inst_item.get("magnitude_ratio"),
            "reasons": reason_details[:4] or ["轉折細節資料不足，請以原始行情與法人明細核對"],
            "reason_details": reason_details[:10],
            "close": close,
            "pct": stock.get("pct"),
            "support": stock.get("support"),
            "resistance": stock.get("resistance"),
            "vol_ratio": vol_ratio,
            "data_date": inst_item.get("data_date"),
            "score": score,
        })
    items.sort(key=lambda x: (state_order.get(x["state"], 0), x["score"], abs(x["current_total_lots"])), reverse=True)
    result = {"schema_version": TURNING_OBSERVATION_SCHEMA_VERSION,
              "data_date": next((x.get("data_date") for x in items if x.get("data_date")), None),
              "prior_days": prior_days, "items": items[:limit]}
    with _realtime_cache_lock:
        _TURNING_OBSERVATION_CACHE["at"] = time.time()
        _TURNING_OBSERVATION_CACHE["data"] = result
    try:
        _save_shared_data_snapshot(
            TURNING_OBSERVATION_SNAPSHOT_KEY, result,
            data_date=result.get("data_date") or taiwan_today(),
            source_meta={"source": "turning_observation", "schema_version": TURNING_OBSERVATION_SCHEMA_VERSION,
                         "item_count": len(items),
                         "limit": limit, "prior_days": prior_days})
    except Exception as exc:
        print(f"⚠️ 保存轉折觀察共享快照失敗: {exc}")
    return result


_TURNING_REFRESH_LOCK = threading.Lock()
_TURNING_REFRESH_RUNNING = False
TURNING_STALE_SNAPSHOT_MAX_AGE_SECONDS = 7 * 86400


def _turning_snapshot_status(payload):
    """回傳轉折快照與是否為近三日資料；過舊資料只作暫存畫面並明示日期。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return False
    data_date = _leaderboard_date(payload.get("data_date"))
    today = taiwan_today()
    return bool(data_date and data_date <= today and (today - data_date).days <= 3)


def _get_turning_web_snapshot():
    """網頁先取可用快照，不在 request 內執行完整轉折計算。"""
    now = time.time()
    with _realtime_cache_lock:
        cached = _TURNING_OBSERVATION_CACHE.get("data")
        cached_at = _TURNING_OBSERVATION_CACHE.get("at", 0)
        if cached is not None and now - cached_at < TURNING_OBSERVATION_CACHE_SECONDS:
            return cached, _turning_snapshot_status(cached), "記憶體快取"
    try:
        shared = _load_shared_data_snapshot(
            TURNING_OBSERVATION_SNAPSHOT_KEY,
            max_age_seconds=TURNING_STALE_SNAPSHOT_MAX_AGE_SECONDS)
        payload = (shared.get("payload") if shared else None) or {}
        if (isinstance(payload, dict) and isinstance(payload.get("items"), list)
                and int(payload.get("schema_version") or 0) >= TURNING_OBSERVATION_SCHEMA_VERSION
                and all(isinstance(item, dict) and item.get("reason_details")
                        for item in payload.get("items") or [])):
            with _realtime_cache_lock:
                _TURNING_OBSERVATION_CACHE.update({"at": now, "data": payload})
            return payload, _turning_snapshot_status(payload), "共享快照"
        if isinstance(payload, dict) and payload:
            return None, False, "舊版轉折快照"
    except Exception as exc:
        print(f"⚠️ 讀取轉折網頁快照失敗: {exc}")
    return None, False, "尚未建立快照"


def _start_turning_background_refresh():
    """每個 process 最多同時執行一個轉折刷新，避免多個使用者重複打外部行情。"""
    global _TURNING_REFRESH_RUNNING
    with _TURNING_REFRESH_LOCK:
        if _TURNING_REFRESH_RUNNING:
            return False
        _TURNING_REFRESH_RUNNING = True

    def worker():
        global _TURNING_REFRESH_RUNNING
        try:
            build_turning_observation(limit=60, prior_days=5, force_refresh=True)
        except Exception as exc:
            print(f"⚠️ 背景刷新轉折觀察失敗: {exc}")
        finally:
            with _TURNING_REFRESH_LOCK:
                _TURNING_REFRESH_RUNNING = False

    threading.Thread(target=worker, name="turning-refresh", daemon=True).start()
    return True


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



_CHIPS_CACHE_SECONDS = 300
_chips_cache = {"at": 0, "result": None}
_chips_cache_lock = threading.Lock()
_CHIPS_REFRESH_LOCK = threading.Lock()
_CHIPS_REFRESH_RUNNING = False


def _start_chips_background_refresh():
    """籌碼網頁沒有快照時，背景執行一次完整整理，不讓 HTTP request 卡住。"""
    global _CHIPS_REFRESH_RUNNING
    with _CHIPS_REFRESH_LOCK:
        if _CHIPS_REFRESH_RUNNING:
            return False
        _CHIPS_REFRESH_RUNNING = True

    def worker():
        global _CHIPS_REFRESH_RUNNING
        try:
            build_chips_payload(force_refresh=True, persist=True)
            print("✅ 籌碼超人背景刷新完成")
        except Exception as exc:
            print("⚠️ 籌碼超人背景刷新失敗: %s" % exc)
        finally:
            with _CHIPS_REFRESH_LOCK:
                _CHIPS_REFRESH_RUNNING = False

    threading.Thread(target=worker, name="chips-refresh", daemon=True).start()
    return True


def _chips_data_date(value):
    """把法人資料日統一成 ISO 日期字串；無法確認時回傳 None。"""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8])).isoformat()
        except ValueError:
            return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _chips_amount(price, lots):
    """沿用原本籌碼超人的換算：以整理當下股價將張數換算成億元。"""
    if not price or lots is None:
        return None
    try:
        return abs(float(lots)) * 1000 * float(price) / 100_000_000
    except (TypeError, ValueError):
        return None


def _chips_group_rows(rows, prices, both=False):
    """將法人 SQL 結果轉成可保存、可供 LINE／網頁共用的純 JSON 資料。"""
    scored = []
    for row in rows or []:
        if both:
            code, name, foreign_lots, trust_lots, total_lots = row
            lots = total_lots
            extra = {
                "foreign_lots": int(foreign_lots or 0),
                "trust_lots": int(trust_lots or 0),
            }
        else:
            code, name, lots, hit_days, total_days = row
            extra = {"hit_days": int(hit_days or 0),
                     "total_days": int(total_days or 0)}
        price = (prices.get(code) or {}).get("close")
        amount = _chips_amount(price, lots)
        if amount is None:
            continue
        item = {"code": str(code), "name": str(name or code),
                "lots": int(lots or 0), "amount_billion": round(amount, 4),
                **extra}
        scored.append(item)
    scored.sort(key=lambda x: (x["amount_billion"], x["code"]), reverse=True)
    return scored


def _chips_result_from_persisted():
    snapshot = _load_shared_data_snapshot("chips_superman")
    if not snapshot:
        return None
    payload = snapshot.get("payload")
    if not isinstance(payload, dict) or not payload.get("available"):
        return None
    groups = payload.get("groups")
    if not isinstance(groups, dict):
        return None
    # 新版快照需包含法人突變欄位；舊快照命中時強制重算一次，避免新功能不顯示。
    if "institutional_shifts" not in payload:
        return None
    return {"payload": payload, "source": "持久化快照",
            "data_date": _chips_data_date(snapshot.get("data_date") or payload.get("data_date")),
            "computed_at": snapshot.get("computed_at")}


def build_chips_payload(days=10, force_refresh=False, persist=True,
                        allow_compute=True):
    """建立籌碼超人共同 payload；快照命中時不重跑五組法人查詢。"""
    now = time.time()
    if not force_refresh:
        cached = _chips_cache.get("result")
        if cached and now - _chips_cache.get("at", 0) < _CHIPS_CACHE_SECONDS:
            return cached
        persisted = _chips_result_from_persisted()
        if persisted:
            with _chips_cache_lock:
                _chips_cache.update({"at": now, "result": persisted})
            print("⚡ 籌碼超人改讀 Supabase 快照（資料日 %s）" %
                  (persisted.get("data_date") or "未標日期"))
            return persisted

    if not allow_compute:
        return {"payload": {"available": False, "building": True,
                             "history_days": None, "groups": {},
                             "institutional_shifts": []},
                "source": "背景整理中", "data_date": None,
                "computed_at": None}

    with _chips_cache_lock:
        if not force_refresh:
            cached = _chips_cache.get("result")
            if cached and time.time() - _chips_cache.get("at", 0) < _CHIPS_CACHE_SECONDS:
                return cached
        hist_days = get_history_days_count()
        if hist_days < 3:
            result = {"payload": {"available": False, "history_days": hist_days,
                                  "groups": {}},
                      "source": "資料不足", "data_date": None,
                      "computed_at": None}
            _chips_cache.update({"at": time.time(), "result": result})
            return result

        inst = fetch_institutional_data() or {}
        actual = min(int(days), hist_days)
        raw = {
            "trust_buy": get_top_by_investor("trust", "buy", actual, 20, min_days=6),
            "foreign_buy": get_top_by_investor("foreign", "buy", actual, 20, min_days=6),
            "trust_sell": get_top_by_investor("trust", "sell", actual, 20, min_days=6),
        }
        both_buy = get_both_side_codes("buy", actual, 20, min_days=5)
        both_sell = get_both_side_codes("sell", actual, 20, min_days=5)
        institutional_shifts = get_institutional_shift_candidates(
            prior_days=min(5, max(3, actual - 1)), top_n=12)
        all_codes = {c for rows in raw.values() for c, *_ in rows}
        all_codes |= {c for c, *_ in both_buy} | {c for c, *_ in both_sell}
        prices = get_realtime_stocks_bulk(list(all_codes), workers=16) if all_codes else {}
        groups = {
            "trust_buy": _chips_group_rows(raw["trust_buy"], prices),
            "foreign_buy": _chips_group_rows(raw["foreign_buy"], prices),
            "both_buy": _chips_group_rows(both_buy, prices, both=True),
            "trust_sell": _chips_group_rows(raw["trust_sell"], prices),
            "both_sell": _chips_group_rows(both_sell, prices, both=True),
        }
        data_date = _chips_data_date(_t86_cache.get("data_date"))
        payload = {
            "available": True, "actual_days": actual,
            "history_days": hist_days,             "data_date": data_date,
            "groups": groups,
            "institutional_shifts": institutional_shifts,
            "shift_prior_days": min(5, max(3, actual - 1)),
            "amount_note": "金額以整理當下可取得的真實股價換算",

        }
        result = {"payload": payload, "source": "本次完整整理",
                  "data_date": data_date, "computed_at": taiwan_now().isoformat()}
        _chips_cache.update({"at": time.time(), "result": result})
        if persist and data_date:
            saved = _save_shared_data_snapshot(
                "chips_superman", payload, data_date=data_date,
                source_meta={"source": "chips_superman", "days": actual,
                             "group_count": len(groups),
                             "shift_count": len(institutional_shifts)},
            )
            print("💾 籌碼超人快照%s" % ("已保存" if saved else "保存失敗"))
        return result


def build_line_chips_message(user_id, base_url=None):
    """LINE 籌碼超人：只顯示三個重點區塊，完整五區資料由網頁查看。"""
    result = build_chips_payload()
    payload = result.get("payload") or {}
    if not payload.get("available"):
        return TextSendMessage(
            text="❌ 法人歷史資料還不夠，籌碼超人需要至少幾個交易日的累積；請稍後再試。")

    token = create_web_token(user_id)
    web_url = None
    if token:
        web_url = (f"{public_web_base_url(base_url)}/web/chips?t="
                   f"{quote(token, safe='')}")
    data_date = result.get("data_date") or payload.get("data_date") or "未標日期"
    actual = payload.get("actual_days") or 0
    contents = [
        {"type": "text", "text": "🦸 籌碼超人｜今日摘要", "weight": "bold",
         "size": "xl", "color": "#1B2027"},
        {"type": "text", "text": f"資料來源：{result.get('source')}・資料日：{data_date}・近 {actual} 日",
         "size": "xs", "color": "#767D85", "margin": "sm", "wrap": True},
    ]
    shift_items = (payload.get("institutional_shifts") or [])[:3]
    contents.append({"type": "separator", "margin": "lg", "color": "#E8EAE6"})
    contents.append({"type": "text", "text": "⚡ 法人籌碼突變", "weight": "bold",
                     "size": "md", "color": "#6E5228", "margin": "lg"})
    contents.append({"type": "text", "text": "比較最新 T86 與前幾個交易日方向，不代表即時法人資料。",
                     "size": "xs", "color": "#767D85", "margin": "xs", "wrap": True})
    if not shift_items:
        contents.append({"type": "text", "text": "目前沒有符合方向反轉或異常放大條件的標的。",
                         "size": "sm", "color": "#767D85", "margin": "sm", "wrap": True})
    for item in shift_items:
        changes = "、".join(item.get("investor_changes") or []) or "三大法人合計方向變化"
        current_total = int(item.get("current_total_lots") or 0)
        ratio = float(item.get("magnitude_ratio") or 0)
        ratio_text = f"・約前期平均 {ratio:.1f} 倍" if ratio >= 2.5 else ""
        contents.append({"type": "text", "text":
                         f"・{item.get('name')}（{item.get('code')}） {item.get('event_type')}｜"
                         f"{changes}｜今日 {current_total:+,} 張{ratio_text}｜{item.get('consensus')}",
                         "size": "sm", "color": "#454C55", "margin": "sm", "wrap": True})
    labels = [
        ("both_buy", "🔥 外資投信同買", "兩種資金同時站買方"),
        ("trust_buy", "🏦 投信認養", "至少 6／10 天持續同方向"),
        ("trust_sell", "📉 今日需留意調節", "投信近十日持續賣超"),
    ]
    for key, title, note in labels:
        contents.append({"type": "separator", "margin": "lg", "color": "#E8EAE6"})
        contents.append({"type": "text", "text": title, "weight": "bold",
                         "size": "md", "color": "#6E5228", "margin": "lg"})
        contents.append({"type": "text", "text": note, "size": "xs",
                         "color": "#767D85", "margin": "xs", "wrap": True})
        items = (payload.get("groups") or {}).get(key) or []
        if not items:
            contents.append({"type": "text", "text": "近期無符合標的", "size": "sm",
                             "color": "#767D85", "margin": "sm"})
        for item in items[:3]:
            days_text = (f"・{item.get('hit_days', 0)}/{item.get('total_days', 0)}天"
                         if key == "trust_buy" or key == "trust_sell" else "")
            contents.append({"type": "text", "text":
                             f"・{item.get('name')}（{item.get('code')}）"
                             f" {item.get('amount_billion', 0):,.0f}億{days_text}",
                             "size": "sm", "color": "#454C55", "margin": "sm", "wrap": True})
    if web_url:
        contents += [
            {"type": "separator", "margin": "lg", "color": "#E8EAE6"},
            {"type": "button", "style": "primary", "height": "sm",
             "color": "#6E5228", "margin": "lg",
             "action": {"type": "uri", "label": "查看完整籌碼分析", "uri": web_url}},
        ]
    bubble = {"type": "bubble",
              "body": {"type": "box", "layout": "vertical", "contents": contents,
                        "paddingAll": "18px", "backgroundColor": "#FFFFFF"},
              "styles": {"body": {"backgroundColor": "#FFFFFF"}}}
    alt = f"🦸 籌碼超人｜近 {actual} 日｜資料日 {data_date}"
    return FlexSendMessage(alt_text=alt, contents=bubble)


def render_chips_web_body(result):
    """網頁籌碼超人完整版：完整五區、資料日期與換算說明。"""
    payload = result.get("payload") or {}
    if payload.get("building"):
        return '''<section class="card"><div class="screener-fast-state"><span class="screener-fast-state-mark"></span><div>
<b>籌碼超人正在背景整理</b><div class="screener-fast-note">目前沒有可使用的完整快照，本頁已立即回應；法人資料整理完成後重新整理即可查看。</div>
<p><a class="btn" href="/web/chips">重新整理查看結果</a></p>
</div></div></section>'''
    if not payload.get("available"):
        return '<section class="card"><div class="empty">法人歷史資料還不夠，暫時無法建立近十日籌碼整理。</div></section>'
    esc = html.escape
    group_info = [
        ("trust_buy", "🏦 投信認養", "國內基金持續站在買方，至少 6／10 天才列入認養。"),
        ("foreign_buy", "🌐 外資認養", "外資近十日持續買超；外資也可能包含指數或 ETF 被動調整。"),
        ("both_buy", "🔥 外資投信同買", "外資與投信同時站買方，顯示兩類資金方向一致。"),
        ("trust_sell", "📉 投信調節", "投信近十日持續賣超，作為籌碼面的撤退訊號觀察。"),
        ("both_sell", "❄️ 外資投信同賣", "外資與投信同時站賣方，顯示兩類資金方向一致轉弱。"),
    ]
    sections = []
    shift_items = payload.get("institutional_shifts") or []
    shift_prior_days = int(payload.get("shift_prior_days") or 5)
    shift_rows = []
    for item in shift_items:
        changes = "、".join(item.get("investor_changes") or []) or "三大法人合計方向變化"
        current_total = int(item.get("current_total_lots") or 0)
        ratio = float(item.get("magnitude_ratio") or 0)
        ratio_text = f"；約前{shift_prior_days}日平均絕對值 {ratio:.1f} 倍" if ratio >= 2.5 else ""
        detail = (f"{item.get('event_type')}・{changes}・今日三大法人 {current_total:+,} 張"
                  f"・{item.get('consensus')}{ratio_text}")
        shift_rows.append(
            f'<div class="chips-row"><div><b>{esc(str(item.get("name") or item.get("code")))}</b>'
            f'<small>（{esc(str(item.get("code") or ""))}）・{esc(detail)}</small></div>'
            f'<strong>{esc(str(item.get("event_type") or "方向變化"))}</strong></div>')
    if not shift_rows:
        shift_rows.append('<div class="chips-empty">目前沒有符合「方向反轉或異常放大」條件的標的</div>')
    shift_section = (
        '<section class="chips-section chips-shift-section">'
        '<h2>⚡ 法人籌碼突變</h2>'
        f'<p>比較最新 T86 與前 {shift_prior_days} 個交易日平均方向；只列出方向反轉或異常放大的標的。</p>'
        f'{"".join(shift_rows)}</section>')
    for key, title, note in group_info:
        rows = []
        for item in ((payload.get("groups") or {}).get(key) or []):
            if key in ("trust_buy", "trust_sell"):
                detail = (f"投信 {int(item.get('lots') or 0):+,} 張・"
                          f"{item.get('hit_days', 0)}/{item.get('total_days', 0)} 天同方向")
            elif key == "foreign_buy":
                detail = f"外資 {int(item.get('lots') or 0):+,} 張"
            else:
                detail = (f"外資 {int(item.get('foreign_lots') or 0):+,} 張・"
                          f"投信 {int(item.get('trust_lots') or 0):+,} 張")
            rows.append(
                f'<div class="chips-row"><div><b>{esc(str(item.get("name") or item.get("code")))}</b>'
                f'<small>（{esc(str(item.get("code") or ""))}）・{esc(detail)}</small></div>'
                f'<strong>{float(item.get("amount_billion") or 0):,.0f} 億</strong></div>')
        if not rows:
            rows.append('<div class="chips-empty">近期無符合標的</div>')
        sections.append(
            f'<section class="chips-section"><h2>{esc(title)}</h2>'
            f'<p>{esc(note)}</p>{"".join(rows)}</section>')
    data_date = result.get("data_date") or payload.get("data_date") or "未標日期"
    source = result.get("source") or "未標來源"
    return f'''<div class="tabs">\n  <a href="/web/screener?mode=blackhorse&view=list">黑馬</a>\n  <a href="/web/screener?mode=radar&view=list">雷達</a>\n  <a href="/web/chips" class="on">籌碼超人</a>\n  <a href="/web/screener?mode=review">成效</a>\n  <a href="/web/screener?mode=turning">轉折觀察</a>\n  <a href="/web/etf">ETF 專區</a>\n</div>\n<div class="chips-meta">資料來源：<b>{esc(str(source))}</b>　資料日：<b>{esc(str(data_date))}</b>　近 <b>{int(payload.get("actual_days") or 0)}</b> 個交易日</div>
<div class="callout">億＝以整理當下可取得的真實股價，將法人近十日累計張數換算為億元；天數＝近十日站同方向的天數。這裡只看法人籌碼，不含基本面與估值。</div>
{shift_section}
{"".join(sections)}
<div class="callout">認養需至少 6／10 天持續同向；單日爆量隔天就跑的不算。法人買不代表便宜，法人賣不代表公司變壞。以上為公開資料整理，不構成投資建議。</div>'''


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
    """壓力為 None 代表目前沒有可用的上方壓力參考，直接標示無壓力位。"""
    return f"{r}" if r is not None else "無壓力位"


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
_revenue_cache = {"period": None, "data": {}, "checked_at": 0,
                  "source": "none", "source_date": None}
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

    # Render 多 worker 或重啟後，記憶體快取可能是空的；先讀 Supabase
    # 共享快照。來源月份另存在 source_meta，資料日期不拿來冒充月份。
    if not _revenue_cache["data"]:
        shared = _load_shared_data_snapshot("monthly_revenue")
        shared_data = (shared.get("payload") if shared else None) or {}
        shared_period = ((shared.get("source_meta") or {}).get("period")
                         if shared else None)
        if isinstance(shared_data, dict) and shared_data and shared_period:
            _revenue_cache["period"] = str(shared_period)
            _revenue_cache["data"] = shared_data
            _revenue_cache["checked_at"] = now
            _revenue_cache["source"] = "shared"
            _revenue_cache["source_date"] = shared.get("data_date")
            print("⚡ 月營收改讀 Supabase 快照（月份 %s，來源日 %s），共 %s 筆" %
                  (shared_period, shared.get("data_date") or "未標日期",
                   len(shared_data)))
            return shared_data

        # 沒有共享快照時，沿用原本已保存的最新月份資料庫 fallback。
        history_data, history_period = _load_latest_revenue_history()
        if history_data:
            _revenue_cache["period"] = history_period
            _revenue_cache["data"] = history_data
            _revenue_cache["checked_at"] = now
            _revenue_cache["source"] = "history"
            _revenue_cache["source_date"] = None
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
    _revenue_cache["source"] = "external"
    _revenue_cache["source_date"] = taiwan_today()
    print(f"✅ 月營收抓取成功（{period}），共 {len(result)} 筆（含上櫃、興櫃）")
    save_revenue_history(period, result)
    _save_shared_data_snapshot(
        "monthly_revenue", result, data_date=taiwan_today(),
        source_meta={"source": "TWSE+TPEx", "period": str(period)},
    )
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

# --- 大盤指數（Yahoo 日K，固定回傳最新一個交易日收盤資訊） ---
# 今日首頁與盤前摘要都會用到大盤；短時間內重整不需要重複打外部 API。
_taiex_cache = {"at": 0, "data": None}
TAIEX_CACHE_SECONDS = 60

_taifex_night_cache = {"at": 0, "data": None}
TAIFEX_NIGHT_CACHE_SECONDS = 300


TAIFEX_NIGHT_URL = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
TAIFEX_NIGHT_MAX_AGE_DAYS = 3


def _taifex_number(value):
    """把 TAIFEX HTML／JSON 的數字字串轉成 float；箭頭與缺值視為格式，不是數字。"""
    if value in (None, "", "-", "NULL", "null"):
        return None
    try:
        text = str(value).replace(",", "").replace("%", "").replace("−", "-")
        text = re.sub(r"[^0-9+\-.]", "", text).strip()
        if not text or text in ("-", "+", "."):
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _taifex_clean_html_text(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _taifex_expiration_date(year, month):
    """TAIFEX 月台指期的第三個星期三，用來排除資料日後仍殘留的到期月。"""
    first = date(year, month, 1)
    days_to_wednesday = (2 - first.weekday()) % 7
    return first + timedelta(days=days_to_wednesday + 14)


def _taifex_month_is_expired(month_text, data_date):
    if not re.fullmatch(r"\d{6}", str(month_text or "")):
        return True
    try:
        year, month = int(month_text[:4]), int(month_text[4:6])
        month_date = date(year, month, 1)
        data_month = date(data_date.year, data_date.month, 1)
        if month_date < data_month:
            return True
        if month_date == data_month and data_date >= _taifex_expiration_date(year, month):
            return True
        return False
    except (TypeError, ValueError):
        return True


def _parse_taifex_night_html(page_text):
    """解析官方 futDailyMarketReport 的 TX 盤後主表，回傳候選列與資料日。"""
    heading = re.search(
        r"(\d{4}/\d{1,2}/\d{1,2})(?:\s|&nbsp;|&#160;)+15:00~次日05:00\s*盤後交易時段行情表",
        page_text or "", flags=re.IGNORECASE)
    if not heading:
        return None
    try:
        data_date = date.fromisoformat(heading.group(1).replace("/", "-"))
    except ValueError:
        return None

    table_match = re.search(
        r"盤後交易時段行情表.*?<table\b[^>]*>(.*?)</table>",
        page_text or "", flags=re.IGNORECASE | re.DOTALL)
    if not table_match:
        return None
    rows = []
    for row_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_match.group(1),
                               flags=re.IGNORECASE | re.DOTALL):
        cells = [
            _taifex_clean_html_text(cell)
            for cell in re.findall(r"<td\b[^>]*>(.*?)</td>", row_html,
                                   flags=re.IGNORECASE | re.DOTALL)
        ]
        if len(cells) < 8:
            continue
        contract_month = cells[1].strip()
        # 只接受單一月份；202609/202610 等價差列不能冒充近月。
        if not re.fullmatch(r"\d{6}", contract_month):
            continue
        last = _taifex_number(cells[5])
        if last is None or _taifex_month_is_expired(contract_month, data_date):
            continue
        rows.append({
            "month": contract_month,
            "close": last,
            "diff": _taifex_number(cells[6]),
            "pct": _taifex_number(cells[7]),
        })
    if not rows:
        return None
    return {"data_date": data_date, "rows": rows}


def fetch_taifex_night_summary():
    """取得官方 TX 近月盤後資料；以夜盤歸屬日查詢，過舊或解析失敗不顯示舊值。"""
    now = time.time()
    with _realtime_cache_lock:
        cached = _taifex_night_cache.get("data")
        if cached is not None and now - _taifex_night_cache.get("at", 0) < TAIFEX_NIGHT_CACHE_SECONDS:
            return cached

    today = taiwan_today()
    # 夜盤 15:00 至次日 05:00 的資料，官方要求以次日歸屬日期查詢。
    query_dates = [today + timedelta(days=1)] + [today - timedelta(days=offset) for offset in range(0, 8)]
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-TW,zh;q=0.9"}
    request_errors = []
    for query_date in query_dates:
        try:
            params = {
                "queryDate": query_date.strftime("%Y/%m/%d"),
                "marketCode": "1",
                "MarketCode": "1",
                "commodity_id": "TX",
                "commodity_id2": "",
            }
            # 官方 futDailyMarketReport 的查詢表單是 POST；使用 GET 可能只回傳
            # 預設頁面，導致日期／夜盤資料沒有跟著 queryDate 更新。
            response = requests.post(TAIFEX_NIGHT_URL, data=params,
                                     headers=headers, timeout=15)
            response.raise_for_status()
            parsed = _parse_taifex_night_html(response.text)
            if not parsed:
                continue
            data_date = parsed["data_date"]
            if data_date > today or (today - data_date).days > TAIFEX_NIGHT_MAX_AGE_DAYS:
                continue
            row = min(parsed["rows"], key=lambda item: item["month"])
            result = {
                "close": row["close"],
                "diff": row.get("diff"),
                "pct": row.get("pct"),
                "date": data_date.strftime("%Y/%m/%d"),
                "contract": f"TX {row['month']}",
                "source": "TAIFEX futDailyMarketReport（官方盤後歸屬日）",
            }
            with _realtime_cache_lock:
                _taifex_night_cache["at"] = time.time()
                _taifex_night_cache["data"] = result
            return result
        except Exception as exc:
            request_errors.append(f"{query_date.isoformat()}: {exc}")
            continue
    if request_errors:
        print(f"⚠️ 抓取台指期夜盤官方資料部分日期失敗，已繼續回退：{request_errors[-1]}")
    # 不使用過時 OpenAPI 或舊快照填補，以免再次顯示錯價。
    with _realtime_cache_lock:
        _taifex_night_cache["at"] = time.time()
        _taifex_night_cache["data"] = None
    return None


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
        raw_closes = (result[0].get("indicators", {})
                      .get("quote", [{}])[0].get("close", []) or [])
        # 不能混用 meta.regularMarketPrice 與日K close：Yahoo 的 meta 行情有時仍
        # 停在前一筆，但日K已更新；混用會讓首頁在實際上漲日顯示前一日跌幅。
        bars = [(ts[idx] if idx < len(ts) else None, close)
                for idx, close in enumerate(raw_closes) if close is not None]
        if len(bars) < 2:
            return None
        tw_tz = timezone(timedelta(hours=8))
        last_ts, close = bars[-1]
        _previous_ts, prev = bars[-2]
        if not prev:
            return None
        bar_date = (datetime.fromtimestamp(last_ts, tw_tz).strftime("%Y%m%d")
                    if last_ts else None)
        diff = float(close) - float(prev)
        result = {
            "close": f"{float(close):,.2f}",
            "sign": "+" if diff > 0 else ("-" if diff < 0 else ""),
            "pts": f"{abs(diff):,.2f}",
            "pct": f"{diff / float(prev) * 100:+.2f}",
            "date": bar_date,
            "previous_close": f"{float(prev):,.2f}",
            "source": "Yahoo ^TWII 日K最後兩筆收盤",
        }
        with _realtime_cache_lock:
            _taiex_cache["at"] = time.time()
            _taiex_cache["data"] = result
        return result
    except Exception as e:
        print(f"❌ 抓取大盤指數錯誤: {e}")
        return None


def _market_date_matches(value, expected_date):
    """首頁只接受資料日等於顯示交易日的大盤數值，避免舊快照冒充今日。"""
    if not value or not expected_date:
        return False
    try:
        parsed = date.fromisoformat(str(value).replace("/", "-")[:10])
    except ValueError:
        digits = re.sub(r"\D", "", str(value))
        if len(digits) != 8:
            return False
        try:
            parsed = datetime.strptime(digits, "%Y%m%d").date()
        except ValueError:
            return False
    return parsed == expected_date


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
    位階分數（0-20）。股價相對近期高點與均線的位置。
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

        # 位階只反映價格結構，不讓接近高點本身過度推高健康分。
    return min(20, round(score * 20 / 25))

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


def _compute_stock_watchlist_scores(codes):
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
        pos_score = score_watchlist_position(stock)                       # 0-20

        cum_yoy = revenue_data.get(code, {}).get("cum_yoy_pct")
        rev_score = round(score_from_cum_revenue_growth(cum_yoy) * 30 / 40)  # 0-30

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


def compute_watchlist_scores(codes):
    """自選股評分總入口：個股與 ETF 分流，避免用錯商品模型。"""
    normalized = list(dict.fromkeys(
        str(code).strip().upper() for code in (codes or []) if str(code).strip()))
    if not normalized:
        return {}
    stock_codes = [code for code in normalized if not is_etf(code)]
    etf_codes = [code for code in normalized if is_etf(code)]
    result = {}
    if stock_codes:
        result.update(_compute_stock_watchlist_scores(stock_codes))
    if etf_codes:
        result.update(_compute_etf_watchlist_scores(etf_codes))
    return result


def save_watchlist_scores(user_id, scores):
    """存下今天的自選股分數。同一天重複寫入會覆蓋，cron 跑兩次也不會重複。"""
    if not scores:
        return
    rows = [(str(user_id).strip(), s["code"], s.get("total"), s.get("chip"),
             s.get("position"), s.get("revenue"), s.get("valuation"),
             (s.get("stock") or {}).get("close"),
             (s.get("stock") or {}).get("support"),
             (s.get("stock") or {}).get("resistance"),
             s.get("asset_type") or ("etf" if is_etf(s.get("code")) else "stock"))
            for s in scores.values()]
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        execute_values(
            cursor,
            """
            INSERT INTO watchlist_scores
                (user_id, code, snapshot_date, total, chip, position,
                 revenue, valuation, close, support, resistance, asset_type)
            VALUES %s
            ON CONFLICT (user_id, code, snapshot_date) DO UPDATE SET
                total = EXCLUDED.total, chip = EXCLUDED.chip,
                position = EXCLUDED.position, revenue = EXCLUDED.revenue,
                valuation = EXCLUDED.valuation, close = EXCLUDED.close,
                support = EXCLUDED.support, resistance = EXCLUDED.resistance,
                asset_type = EXCLUDED.asset_type
            """,
            rows,
            template="(%s, %s, CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                   code, snapshot_date, total, chip, position, revenue, valuation,
                   asset_type
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
                       "position": r[4], "revenue": r[5], "valuation": r[6],
                       "asset_type": r[7] or "stock"}
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
    if not prev or cur.get("total") is None or prev.get("total") is None:
        return None, None
    cur_type = cur.get("asset_type") or ("etf" if is_etf(cur.get("code")) else "stock")
    prev_type = prev.get("asset_type") or ("etf" if is_etf(cur.get("code")) else "stock")
    # 模型切換時不拿個股舊分項與 ETF 新分項硬比，等下一筆同模型快照。
    if cur_type != prev_type:
        return None, None
    diff = cur["total"] - prev["total"]
    if abs(diff) < 5:
        return None, None

    # 找出貢獻最多的面向，讓「為什麼變」有依據而不只是報數字。
    if cur_type == "etf":
        parts = [("同期超額報酬", (cur.get("chip") or 0) - (prev.get("chip") or 0)),
                 ("價格報酬", (cur.get("position") or 0) - (prev.get("position") or 0)),
                 ("配息殖利率", (cur.get("revenue") or 0) - (prev.get("revenue") or 0)),
                 ("風險控制", (cur.get("valuation") or 0) - (prev.get("valuation") or 0))]
    else:
        parts = [("籌碼", cur["chip"] - prev["chip"]),
                 ("位階", cur["position"] - prev["position"]),
                 ("營收", cur["revenue"] - prev["revenue"]),
                 ("估值", cur["valuation"] - prev["valuation"])]
    driver, dval = max(parts, key=lambda x: abs(x[1]))
    reason = f"，主要來自{driver}{dval:+d}" if abs(dval) >= 3 else ""
    arrow = "📈" if diff > 0 else "📉"
    return arrow, f"{prev['total']}→{cur['total']} 分（{diff:+d}）{reason}"


def _format_stock_detail_lines(code, name, stock, score=None, bd=None,
                               industry_label=None, score_change=None,
                               watchlist_status=None):
    """單檔與自選股共用的手機版股票詳情格式。

    所有會因長度造成 LINE 自動折行的欄位都拆成固定直向行；
    只改顯示層，不改任何真實資料、評分或判讀計算。
    """
    lines = [f"📊 {code} {name}"]
    if industry_label:
        lines.append(industry_label)
    lines.append("─" * 14)

    close = stock.get("close")
    pct = stock.get("pct")
    if close is not None and pct is not None:
        lines.append(f"💰 {close:.2f}（{pct:+.2f}%）")
    elif close is not None:
        lines.append(f"💰 {close:.2f}（漲跌資料不足）")
    else:
        lines.append("💰 股價資料不足")

    high, low = stock.get("high"), stock.get("low")
    if high is not None and low is not None:
        lines.append(f"高低 {high:.2f}/{low:.2f}")
    else:
        lines.append("高低資料不足")

    volume = stock.get("volume")
    if volume is not None:
        lines.append(f"📦 {int(volume / 1000):,} 張")
    else:
        lines.append("📦 成交量資料不足")
    lines.append(f"🛡️ 支撐 {fmt_support(stock)}")
    lines.append(f"🚧 壓力 {fmt_resistance(stock.get('resistance'))}")

    if score:
        total = score["total"]
        flag = "🟢" if total >= 70 else ("🟡" if total >= 45 else "🔴")
        lines += [
            "",
            f"{flag} 綜合評分：{total}／100",
            f"　籌碼{score['chip']}/30　位階{score['position']}/20",
            f"　營收{score['revenue']}/30　估值{score['valuation']}/20",
        ]
        if score_change:
            lines.append(score_change)
        cum_yoy, pe = score["cum_yoy"], score["pe"]
        lines.append(
            f"　營收年增 {cum_yoy:+.1f}%" if cum_yoy is not None
            else "　營收年增資料不足")
        lines.append(f"　PE {pe:.1f}" if pe else "　PE 無")

    lines += ["", "【法人籌碼】近10日"]
    desc = describe_investor_breakdown(bd)
    lines.append(desc if desc else "　尚無法人歷史資料")
    lines += ["", "【位階】", build_position_desc(stock)]

    if score:
        lines += [
            "",
            "【觀察】",
            build_watchlist_advice(
                score["total"], score["chip"], score["position"],
                score["revenue"], score["valuation"], score["cum_lots"],
                score["streak"], stock, score["cum_yoy"], score["pe"]),
        ]

    if watchlist_status:
        lines += ["", watchlist_status]
    return lines


def _flex_report_contents(text):
    """把報告逐行轉成 Flex 元件，讓重點字級與粗細一致。"""
    contents = []
    first = True
    for raw_line in str(text or "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if set(line) == {"─"}:
            contents.append({"type": "separator", "margin": "sm", "color": "#D9DDE2"})
            first = False
            continue

        component = {
            "type": "text",
            "text": line,
            "size": "sm",
            "color": "#454C55",
            "wrap": True,
            "margin": "none" if first else "xs",
        }
        if line.startswith("📊") or (line.startswith("【") and "📊" in line[:12]):
            component.update({"size": "md", "weight": "bold", "color": "#1B2027",
                              "margin": "none" if first else "sm"})
        elif line.startswith("💰"):
            component.update({"size": "md", "weight": "bold", "color": "#1B2027",
                              "margin": "md"})
        elif "綜合評分" in line:
            component.update({"size": "md", "weight": "bold", "color": "#1B2027",
                              "margin": "md"})
        elif line.startswith("【"):
            component.update({"weight": "bold", "color": "#1B2027", "margin": "md"})
        elif line.startswith("※"):
            component.update({"size": "xs", "color": "#6F7782", "margin": "md"})
        contents.append(component)
        first = False
    return contents


def _build_text_flex_message(text, alt_text=None):
    """把沒有可點擊元件的報告也放進與新聞版相同的 Flex 卡片。"""
    return FlexSendMessage(
        alt_text=(alt_text or text)[:400],
        contents={"type": "bubble",
                  "body": {"type": "box", "layout": "vertical",
                           "contents": _flex_report_contents(text),
                           "paddingAll": "18px",
                           "backgroundColor": "#FFFFFF"},
                  "styles": {"body": {"backgroundColor": "#FFFFFF"}}})


def build_single_stock_report(code, user_id=None):
    """
    單檔完整健檢。LINE 直接輸入代號就走這裡——
    單檔與多檔自選股共用同一套手機版欄位，避免相同功能出現兩種格式。
    """
    stock = get_realtime_stock(code)
    if not stock:
        return f"❌ 查無代號 {code} 的行情，請確認代號是否正確。"

    inst = fetch_institutional_data() or {}
    scores = compute_watchlist_scores([code])
    score = scores.get(code)
    ind_map = get_industry_map() or {}
    industry = ind_map.get(code)
    name = short_company_name(stock_display_name(code, inst, stock["name"]))

    score_change = None
    if score and user_id:
        prev = get_previous_scores(user_id, [code]).get(code)
        arrow, change_txt = describe_score_change(score, prev)
        if arrow:
            score_change = f"{arrow} {change_txt}"

    bd = get_investor_breakdown([code]).get(code)
    watchlist_status = None
    if user_id:
        in_wl = code in get_user_watchlist(user_id)
        watchlist_status = ("※ 已在自選清單" if in_wl
                            else f"※ 輸入「加 {code}」加入自選")

    lines = _format_stock_detail_lines(
        code, name, stock, score=score, bd=bd,
        industry_label=industry_name(industry) if industry else None,
        score_change=score_change, watchlist_status=watchlist_status)

    news = fetch_stock_news(
        name, max_items=2, within_hours=36,
        subject_name=name, known_names=_news_company_names([name]))
    core_report = "\n".join(lines)
    if not news:
        core_report += ("\n\n📰 相關新聞\n"
                        f"目前沒有抓到以{name}為主體的相關新聞。")
        return _build_text_flex_message(core_report)

    # 單檔查詢保留完整健檢文字，但新聞標題使用可點擊 Flex 元件，
    # 不把 Google News 的長網址直接顯示在聊天室。
    contents = _flex_report_contents(core_report)
    contents += [
        {"type": "separator", "margin": "lg", "color": "#E8EAE6"},
        {"type": "text", "text": "📰 相關新聞", "weight": "bold",
         "size": "sm", "color": "#1B2027", "margin": "lg"},
    ]
    for index, item in enumerate(news):
        src = f"（{item['source']}）" if item.get("source") else ""
        component = {"type": "text", "text": f"・{item['title']}{src}",
                     "size": "sm", "color": "#4A5F7A", "wrap": True,
                     "decoration": "underline",
                     "margin": "md" if index else "sm"}
        uri = _valid_news_uri(item.get("link"))
        if uri:
            component["action"] = {"type": "uri", "uri": uri}
        else:
            component.pop("decoration", None)
            component["color"] = "#454C55"
        contents.append(component)
    alt_text = core_report + "\n📰 相關新聞\n" + "；".join(
        item.get("title", "") for item in news)
    return FlexSendMessage(
        alt_text=alt_text[:400],
        contents={"type": "bubble",
                  "body": {"type": "box", "layout": "vertical",
                           "contents": contents, "paddingAll": "18px",
                           "backgroundColor": "#FFFFFF"},
                  "styles": {"body": {"backgroundColor": "#FFFFFF"}}})

def _etf_price_drawdown_summary(closes, close_dates):
    """用可取得的價格序列計算觀測期間最大回撤與是否回到前高。"""
    values = []
    for index, value in enumerate(closes or []):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        date_value = close_dates[index] if index < len(close_dates) else None
        values.append((number, date_value))
    if len(values) < 3:
        return None
    peak_value, peak_date = values[0]
    peak_index = 0
    max_drawdown = 0.0
    trough_index = 0
    trough_value = peak_value
    for index, (value, date_value) in enumerate(values):
        if value > peak_value:
            peak_value, peak_date = value, date_value
            peak_index = index
        drawdown = value / peak_value - 1.0 if peak_value else 0.0
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            trough_index = index
            trough_value = value
    if max_drawdown >= 0:
        return {"max_drawdown": 0.0, "peak_date": peak_date,
                "trough_date": peak_date, "recovery_date": peak_date,
                "recovery_days": 0, "latest_gap": 0.0}
    recovery_index = None
    for index in range(trough_index + 1, len(values)):
        if values[index][0] >= peak_value:
            recovery_index = index
            break
    latest_gap = values[-1][0] / peak_value - 1.0 if peak_value else None
    return {
        "max_drawdown": max_drawdown * 100,
        "peak_date": peak_date,
        "trough_date": values[trough_index][1],
        "recovery_date": values[recovery_index][1] if recovery_index is not None else None,
        "recovery_days": (recovery_index - trough_index) if recovery_index is not None else None,
        "latest_gap": latest_gap * 100 if latest_gap is not None else None,
    }


def build_single_etf_report(code, user_id=None):
    """ETF 單檔摘要；不套用個股營收、PE 或法人籌碼評分。"""
    code = str(code).strip()
    meta = get_etf_metadata(code)
    stock = get_realtime_stock(code, rng="5y")
    if not stock:
        return f"❌ 查無 ETF {code} 的行情，請確認代號是否正確。"

    name = short_company_name(meta.get("name") or stock_display_name(code, fallback=stock.get("name")))
    close = stock.get("close")
    pct = stock.get("pct")
    lines = [f"📦 {code} {name}",
             f"{meta.get('category', '待分類')} ETF｜{meta.get('management_style', '待確認')}",
             "─" * 14]
    if close is not None and pct is not None:
        lines.append(f"💰 最新價格 {close:,.2f}（{pct:+.2f}%）")
    elif close is not None:
        lines.append(f"💰 最新價格 {close:,.2f}（漲跌資料不足）")
    else:
        lines.append("💰 價格資料不足")
    if stock.get("volume") is not None:
        lines.append(f"📦 成交量 {int(stock['volume'] / 1000):,} 張")

    lines += ["", "【ETF 商品】",
              f"管理方式：{meta.get('management_style', '待確認')}",
              f"策略分類：{meta.get('category', '待分類')}",
              f"追蹤基準：{meta.get('benchmark') or '資料待確認'}"]
    if meta.get("policy_note"):
        # 配息來源備註屬於配息區塊，避免和商品基本資料混在一起。
        policy_note = f"配息備註：{meta['policy_note']}"
    else:
        policy_note = None

    distribution_records = meta.get("distribution_records") or []
    lines += ["", "【配息明細】",
              f"配息政策：{_etf_distribution_label(meta.get('distribution_policy'))}"]
    if meta.get("distribution_frequency"):
        lines.append(f"配息頻率：{meta['distribution_frequency']}")
    if policy_note:
        lines.append(policy_note)
    if meta.get("distribution_policy") == "non_distributing":
        lines.append("現金配息：不適用（不分配／累積型）")
    elif distribution_records:
        latest = _etf_recent_distribution_records(meta, limit=1)
        latest = latest[0] if latest else distribution_records[0]
        latest_amount = latest.get("amount")
        latest_amount_text = (f"每單位 {float(latest_amount):.4f} 元"
                              if latest_amount is not None else "金額待確認")
        lines.append(f"最近一次除息：{latest.get('ex_date') or '未標日期'}・{latest_amount_text}")
        lines.append(f"官方已核實配息：{len(distribution_records)} 筆")
    elif meta.get("distribution_policy") == "distributing":
        lines.append("現金配息：官方已發生金額待確認；空白不視為 0 元")
    else:
        lines.append("現金配息：官方配息政策或金額待確認")
    if distribution_records:
        recent_records = _etf_recent_distribution_records(meta, limit=4)
        lines.append(_format_recent_distribution_records(recent_records))

    # 單檔報告只讀取既有 ranking snapshot，不為了顯示對照而重掃全市場。
    ranking_row = None
    ranking_period_label = None
    try:
        ranking_payload, _ranking_fresh, _ranking_source = _load_etf_product_ranking_snapshot()
        for period_key in ("short", "long"):
            period_rows = ((ranking_payload or {}).get("categories") or {}) \
                          .get(period_key, {})
            found = []
            for rows in period_rows.values() if isinstance(period_rows, dict) else []:
                if isinstance(rows, list):
                    found.extend(rows)
            ranking_row = next((item for item in found
                                if str(item.get("code") or "").strip().upper() == code.upper()), None)
            if ranking_row:
                period_info = ((ranking_payload or {}).get("periods") or {}).get(period_key) or {}
                ranking_period_label = period_info.get("label") or ("短期" if period_key == "short" else "長期")
                break
    except Exception as exc:
        print(f"⚠️ 單檔 ETF 報酬對照快照待確認: {exc}")

    if ranking_row:
        performance = _etf_performance_comparison(ranking_row)
        lines += ["", f"【報酬對照｜{ranking_period_label}】",
                  "價格報酬不含配息",
                  f"ETF 價格報酬：{performance['return_text']}",
                  f"同期大盤：{performance['market_text']}",
                  f"超額報酬：{performance['excess_text']}（{performance['verdict_text']}）"]
    else:
        lines += ["", "【報酬對照】", "ETF／同期大盤／超額報酬：待排名快照確認"]

    closes = [float(x) for x in (stock.get("closes") or []) if x not in (None, 0)]
    close_dates = stock.get("close_dates") or []
    if distribution_records and close is not None:
        lines += ["", "【配息統計】"]
        end_date = _parse_history_date(close_dates[-1]) if close_dates else taiwan_today()
        trailing = _etf_trailing_distribution_metrics(meta, end_date, close)
        stability = _etf_distribution_stability_metrics(meta, end_date, close)
        if trailing.get("status") == "verified":
            lines.append(f"近12個月官方現金配息：{float(trailing['amount']):.2f} 元")
            lines.append(f"原始近12個月參考殖利率：{float(trailing['yield_pct']):.2f}%（以期末價格估算，非含息總報酬）")
            if stability.get("stability_status") == "verified_four_records":
                lines.append(f"評分用穩定殖利率：{float(stability['score_yield_pct']):.2f}%（近4次中位數／平均值調整，避免單次高配息直接灌高分）")
            else:
                lines.append("評分用穩定殖利率：待近4次官方配息資料完整")
        elif trailing.get("status") == "partial":
            observed = trailing.get("observed_yield_pct")
            observed_text = (f"；觀察期率 {float(observed):.2f}%"
                             if observed is not None else "")
            lines.append(
                f"已發生現金配息：{float(trailing['amount']):.2f} 元（{int(trailing.get('count') or 0)} 次）"
                f"{observed_text}；官方紀錄覆蓋 {int(trailing.get('coverage_days') or 0)} 日，未滿 12 個月，暫不年化")
    if len(closes) >= 2 and closes[0] > 0:
        return_pct = (closes[-1] / closes[0] - 1) * 100
        observed = f"{close_dates[0]} 至 {close_dates[-1]}" if close_dates else "可取得價格期間"
        lines += ["", "【目前可計算】",
                  f"可取得期間價格變化：{return_pct:+.2f}%",
                  f"觀測期間：{observed}",
                  "※ 第一版先顯示可追溯的價格序列，不把價格變化冒充完整含息總報酬。"]
    else:
        lines += ["", "【目前可計算】", "價格歷史資料不足，暫不計算報酬"]

    drawdown = _etf_price_drawdown_summary(closes, close_dates)
    if drawdown:
        dd = float(drawdown.get("max_drawdown") or 0)
        lines += ["", "【風險觀察】", f"可取得價格期間最大回撤：{dd:+.1f}%"]
        if dd == 0:
            lines.append("恢復狀態：觀測期間尚無明顯回撤")
        elif drawdown.get("recovery_date"):
            lines.append(f"恢復狀態：已於 {drawdown['recovery_date']} 回到前高（{drawdown['recovery_days']} 個交易日）")
        else:
            latest_gap = drawdown.get("latest_gap")
            gap_text = f"；目前仍低於前高 {abs(float(latest_gap)):.1f}%" if latest_gap is not None and latest_gap < 0 else ""
            lines.append(f"恢復狀態：尚未回到前高，截至 {close_dates[-1] if close_dates else '資料日'}{gap_text}")
        lines.append("※ 目前使用價格序列計算，未把配息調整成含息總報酬回撤。")

    lines += ["", "【資料成熟度】",
              _etf_maturity_label(meta.get("listing_date")),
              f"成立日：{meta.get('inception_date') or '待確認'}",
              f"掛牌日：{meta.get('listing_date') or '待確認'}",
              "", "※ ETF 不套用個股營收、PE、法人籌碼評分；資料不足會明確標示。"]
    if user_id:
        in_wl = code in get_user_watchlist(user_id)
        lines.append("※ 已在自選清單" if in_wl else f"※ 輸入「加 {code}」加入自選")
    return _build_text_flex_message("\n".join(lines), alt_text=f"{code} {name} ETF 分析")


def build_healthcheck_report(user_id):
    """LINE 多檔自選健檢；每檔與單檔查詢共用同一套手機版面。"""
    codes = get_user_watchlist(user_id)
    if not codes:
        return "📂 自選股清單是空的\n輸入「加 3081」新增自選"

    scores = compute_watchlist_scores(codes)
    prev_scores = get_previous_scores(user_id, codes)
    tags = get_watchlist_tags(user_id)
    stock_codes = [code for code in codes if not is_etf(code)]
    breakdowns = get_investor_breakdown(stock_codes) if stock_codes else {}
    industry_map = get_industry_map() or {}

    rows = []
    for code in codes:
        tag = tags.get(code)
        score = scores.get(code)
        if not score:
            missing_label = (f"⚪ {code} ETF 資料待確認，暫不套用個股評分"
                             if is_etf(code) else f"⚪ {code} 查無行情")
            rows.append((tag, -1, missing_label))
            continue

        arrow, change_txt = describe_score_change(score, prev_scores.get(code))
        score_change = f"{arrow} {change_txt}" if arrow else None
        if score.get("asset_type") == "etf":
            lines = _format_etf_watchlist_lines(
                code, score, score_change=score_change,
                watchlist_status="※ 已在自選清單")
            sort_score = score.get("total") if score.get("total") is not None else -1
            rows.append((tag, sort_score, "\n".join(lines)))
            continue

        stock = score["stock"]
        name = score["name"]
        industry = industry_map.get(code)
        lines = _format_stock_detail_lines(
            code, name, stock, score=score, bd=breakdowns.get(code),
            industry_label=industry_name(industry) if industry else None,
            score_change=score_change,
            watchlist_status="※ 已在自選清單")
        rows.append((tag, score["total"], "\n".join(lines)))

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
        numbered = [f"【{index}】 {text}"
                    for index, (_, text) in enumerate(items, start=1)]
        item_block = "\n\n────────────\n\n".join(numbered)
        if has_tags:
            icon = TAG_ICONS.get(tag, "📌")
            label = tag or "未分類"
            blocks.append(f"{icon}　{label}（{len(items)}）\n\n" + item_block)
        else:
            blocks.append(item_block)

    body = "\n\n".join(blocks)
    tag_hint = ("" if has_tags else
                "\n分類：輸入「加 2330 長線」或「分類 2330 短線」")
    has_stock = any(not is_etf(code) for code in codes)
    has_etf = any(is_etf(code) for code in codes)
    formula_lines = []
    if has_stock:
        formula_lines.append("評分＝籌碼30＋位階20＋營收30＋估值20")
    if has_etf:
        formula_lines.append("ETF評分＝依類別權重計算超額報酬、價格報酬、配息殖利率、回撤、波動與資料完整度")
    formula_text = "\n".join(formula_lines)
    report = (
        f"📋 自選股健檢（{len(codes)}檔）\n\n{body}\n\n"
        f"{formula_text}\n"
        f"🟢70+ 🟡45-69 🔴<45{tag_hint}\n"
        f"※ ETF 與個股使用不同模型；觀察為數據歸納，非投資建議，請自行判斷"
    )
    if len(report) > 4800:
        report = report[:4750] + "\n\n…（清單過長，已截斷）"
    return report

def render_watchlist_web_body(user_id):
    """保留的內部舊版 renderer；目前不由 LINE 或網頁導覽使用，自選健檢留在 LINE。"""
    codes = get_user_watchlist(user_id)
    if not codes:
        return ('<div class="tabs">'
                '<a href="/web/screener?mode=blackhorse&view=list">黑馬</a>'
                '<a href="/web/screener?mode=radar&view=list">雷達</a>'
                '<a href="/web/chips">籌碼超人</a>'
                '<a href="/web/screener?mode=review">成效</a></div>'
                '<section class="card"><div class="empty">自選股清單是空的。<br><br>'
                '請回 LINE 輸入「加 2330」新增自選股。</div></section>')

    fetch_institutional_data()
    scores = compute_watchlist_scores(codes)
    prev_scores = get_previous_scores(user_id, codes)
    tags = get_watchlist_tags(user_id)
    breakdowns = get_investor_breakdown(codes)

    def data_date_text():
        value = _t86_cache.get("data_date")
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return str(value or "未標日期")

    grouped = {tag: [] for tag in WATCHLIST_TAGS}
    grouped[None] = []
    for code in codes:
        tag = tags.get(code)
        s = scores.get(code)
        if not s:
            grouped.setdefault(tag, []).append(
                '<div class="watchlist-card watchlist-muted"><h3>⚪ '
                f'{html.escape(str(code))}</h3><p>查無目前行情，暫不計算健檢分數。</p></div>')
            continue

        if s.get("asset_type") == "etf":
            arrow, change_txt = describe_score_change(s, prev_scores.get(code))
            score_change = f"{arrow} {change_txt}" if arrow else None
            etf_lines = _format_etf_watchlist_lines(
                code, s, score_change=score_change,
                watchlist_status="※ 已在自選清單")
            total = s.get("total")
            flag = ("🟢" if total is not None and total >= 70
                    else "🟡" if total is not None and total >= 45 else "⚪")
            score_class = ("watchlist-good" if total is not None and total >= 70
                           else "watchlist-mid" if total is not None and total >= 45
                           else "watchlist-muted")
            card = f'''<article class="watchlist-card {score_class} watchlist-etf-card">
  <div class="watchlist-card-head"><div><h3>{flag} {html.escape(str(s.get("name") or code))}</h3>
    <span class="watchlist-code">{html.escape(str(code))}　ETF 專用健檢</span></div>
    <strong class="watchlist-score">{html.escape(str(total)) if total is not None else "待確認"}<small>{"分" if total is not None else ""}</small></strong></div>
  <div class="watchlist-etf-lines">{"<br>".join(html.escape(str(line)) for line in etf_lines)}</div>
</article>'''
            grouped.setdefault(tag, []).append(card)
            continue

        stock = s["stock"]
        name = s["name"]
        cum_lots, buy_days, streak = s["cum_lots"], s["buy_days"], s["streak"]
        chip_score, pos_score = s["chip"], s["position"]
        rev_score, val_score = s["revenue"], s["valuation"]
        cum_yoy, pe = s["cum_yoy"], s["pe"]
        total = s["total"]
        flag = "🟢" if total >= 70 else ("🟡" if total >= 45 else "🔴")
        score_class = "watchlist-good" if total >= 70 else ("watchlist-mid" if total >= 45 else "watchlist-low")

        bd = breakdowns.get(code)
        trust = bd["trust"] if bd else None
        if trust and trust["streak"] >= 3:
            note = f"投信連 {trust['streak']} 日買超（累計 {trust['cum']:+,} 張）"
        elif cum_lots < 0:
            note = f"近10日賣超 {abs(cum_lots):,} 張"
        elif streak >= 3:
            note = f"連續買超 {streak} 天"
        elif cum_lots > 0:
            note = f"近10日買超 {cum_lots:,} 張（{buy_days} 天）"
        else:
            note = "近期無明顯動作"

        pos_txt = (f"距高點 {stock['pos_vs_60d_high']:+.1f}%"
                   if stock.get("pos_vs_60d_high") is not None else "位階資料不足")
        rev_txt = f"營收年增 {cum_yoy:+.1f}%" if cum_yoy is not None else "營收無資料"
        pe_txt = f"PE {pe:.1f}" if pe else "PE 無"
        advice = build_watchlist_advice(total, chip_score, pos_score, rev_score,
                                        val_score, cum_lots, streak, stock, cum_yoy, pe)
        arrow, change_txt = describe_score_change(s, prev_scores.get(code))
        bd_desc = describe_investor_breakdown(bd)
        breakdown_html = (f'<p class="watchlist-breakdown">{html.escape(bd_desc)}</p>'
                          if bd_desc else "")
        change_html = (f'<span class="watchlist-change">{html.escape(arrow)} '
                       f'{html.escape(change_txt)}</span>' if arrow else "")
        stock_name = html.escape(str(name or code))
        card = f'''<article class="watchlist-card {score_class}">
  <div class="watchlist-card-head"><div><h3>{flag} {stock_name}</h3>
    <span class="watchlist-code">{html.escape(str(code))}　{change_html}</span></div>
    <strong class="watchlist-score">{int(total)}<small>分</small></strong></div>
  <div class="watchlist-metrics"><span>現價 <b>{float(stock.get("close") or 0):,.2f}</b></span>
    <span>今日 <b class="{'up' if (stock.get('pct') or 0) > 0 else ('down' if (stock.get('pct') or 0) < 0 else 'flat')}">{float(stock.get("pct") or 0):+.2f}%</b></span>
    <span>{html.escape(pos_txt)}</span></div>
  <div class="watchlist-facts"><span>{html.escape(note)}</span>
    <span>{html.escape(rev_txt)}　{html.escape(pe_txt)}</span>
    <span>🛡️{html.escape(fmt_support(stock))}　🚧{html.escape(fmt_resistance(stock.get("resistance")))}</span></div>
  {breakdown_html}
  <div class="watchlist-advice">{html.escape(advice).replace(chr(10), "<br>")}</div>
</article>'''
        grouped.setdefault(tag, []).append(card)

    formula_lines = []
    if any(not is_etf(code) for code in codes):
        formula_lines.append("個股：籌碼 30＋位階 20＋營收 30＋估值 20")
    if any(is_etf(code) for code in codes):
        formula_lines.append("ETF：依類別權重計算超額報酬、價格報酬、配息殖利率、回撤、波動與資料完整度")
    formula_text = "<br>".join(html.escape(text) for text in formula_lines)

    tag_order = [tag for tag in WATCHLIST_TAGS if grouped.get(tag)]
    if grouped.get(None):
        tag_order.append(None)
    sections = []
    for tag in tag_order:
        label = tag or "未分類"
        icon = TAG_ICONS.get(tag, "📌")
        cards = "".join(grouped[tag])
        sections.append(f'<section class="watchlist-group"><h2>{icon} {html.escape(label)}'
                        f'<small>（{len(grouped[tag])}）</small></h2>{cards}</section>')

    return f'''<style>
.watchlist-meta{{padding:13px 15px;margin:0 0 14px;border-left:3px solid var(--brass);background:#F7F4EC;color:var(--ink-soft);font-size:12px;line-height:1.6}}
.watchlist-intro{{color:var(--ink-soft);font-size:13px;line-height:1.7;margin:0 0 15px}}
.watchlist-group{{margin:20px 0 0}}.watchlist-group>h2{{font-size:19px;margin:0 0 10px;padding-bottom:8px;border-bottom:1px solid var(--rule)}}.watchlist-group>h2 small{{font-size:12px;color:var(--ink-faint);font-weight:400;margin-left:5px}}
.watchlist-card{{background:#FFF;border:1px solid #E3E2DC;border-left:4px solid #B8B5AB;border-radius:12px;padding:15px;margin:10px 0;box-shadow:0 3px 13px rgba(35,39,35,.05)}}.watchlist-card.watchlist-good{{border-left-color:#087A4B}}.watchlist-card.watchlist-mid{{border-left-color:#B18A39}}.watchlist-card.watchlist-low{{border-left-color:#B52F2F}}.watchlist-card-head{{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}}.watchlist-card h3{{font-size:18px;margin:0 0 3px;line-height:1.35}}.watchlist-code{{color:var(--ink-soft);font-size:12px}}.watchlist-change{{color:var(--brass);margin-left:6px}}.watchlist-score{{font-size:25px;line-height:1;color:var(--ink);white-space:nowrap}}.watchlist-score small{{font-size:12px;font-weight:400;margin-left:2px}}.watchlist-metrics{{display:flex;flex-wrap:wrap;gap:8px;margin:13px 0 9px;padding:9px 0;border-top:1px solid #ECEDE8;border-bottom:1px solid #ECEDE8;color:var(--ink-soft);font-size:12px}}.watchlist-metrics span{{padding-right:10px;border-right:1px solid #E2E1DB}}.watchlist-metrics span:last-child{{border-right:0}}.watchlist-metrics b{{color:var(--ink);margin-left:3px}}.watchlist-facts{{display:grid;gap:4px;color:var(--ink-soft);font-size:12px;line-height:1.55}}.watchlist-breakdown{{margin:10px 0 0;padding-top:9px;border-top:1px solid #ECEDE8;color:var(--ink-soft);font-size:12px;line-height:1.55}}.watchlist-advice{{margin-top:10px;padding:10px 11px;background:#F7F7F3;border-radius:8px;color:var(--ink-soft);font-size:12px;line-height:1.65}}.watchlist-muted{{color:var(--ink-soft)}}.watchlist-muted h3{{color:var(--ink)}}
@media(max-width:640px){{.watchlist-metrics{{gap:6px}}.watchlist-metrics span{{padding-right:6px}}.watchlist-card h3{{font-size:17px}}}}
</style>
<div class="tabs"><a href="/web/screener?mode=blackhorse&view=list">黑馬</a><a href="/web/screener?mode=radar&view=list">雷達</a><a href="/web/chips">籌碼超人</a><a href="/web/screener?mode=review">成效</a></div>
<div class="watchlist-meta">資料來源：即時健檢計算　法人資料日：<b>{html.escape(data_date_text())}</b>　共 <b>{len(codes)}</b> 檔</div>
    <p class="watchlist-intro">{formula_text}。這裡是公開資料整理，提供檢視線索，不構成買賣建議。</p>
{"".join(sections)}'''


def build_line_watchlist_message(user_id, base_url=None):
    """LINE 自選股健檢：完整內容留在 LINE，避免與網頁版實際庫存混淆。"""
    report = build_healthcheck_report(user_id)
    if not report:
        return TextSendMessage(text="📂 自選股清單是空的\n輸入「加 3081」新增自選")
    return _build_text_flex_message(report, alt_text=report)

# --- 個股新聞（Google News RSS，免費、可帶關鍵字查詢） ---
def _news_company_names(extra=None):
    """整理現有 stock_info 名稱，供新聞主體判斷使用；不建立人工公司清單。"""
    names = set()
    try:
        names.update((get_name_map() or {}).values())
    except Exception as exc:
        print(f"⚠️ 讀取新聞公司名稱對照失敗：{exc}")
    names.update((extra or []))
    names.update((STOCK_NAME_MAP or {}).values())
    return tuple(sorted({short_company_name(name).strip() for name in names
                         if str(name or "").strip() and len(short_company_name(name).strip()) >= 2}))


def _news_subject_relevant(title, subject_name, known_names=None):
    """用可解釋的規則判斷標題主體，避免只因提到自選股就收進來。"""
    title = str(title or "").strip()
    subject = short_company_name(subject_name or "").strip()
    if not title or not subject or subject.casefold() not in title.casefold():
        return False

    # 呼叫端未提供公司清單時，仍使用現有 stock_info 的全市場名稱；
    # 這能辨識「群創法說……台積電」這類另一家公司事件。
    known = {short_company_name(name).strip() for name in
             (known_names if known_names is not None else _news_company_names())
             if str(name or "").strip() and len(short_company_name(name).strip()) >= 2}
    others = [name for name in known
              if len(name) >= 2 and name.casefold() != subject.casefold()
              and name.casefold() in title.casefold()]
    if not others:
        return True

    # 「台積電｜群創法說……台積電合作未回應」這類標題即使後段
    # 再次提到台積電，事件主體仍是群創；只要其他公司緊接財經事件詞，排除。
    match = re.search(r"[|｜:：／/—–-]", title)
    if match:
        head, tail = title[:match.start()], title[match.end():]
        if subject.casefold() in head.casefold():
            event_terms = ("法說", "財報", "營收", "股價", "獲利", "展望", "股東會",
                           "公告", "接單", "產能", "配息", "除息", "合作")
            tail_lower = tail.casefold()
            for other in others:
                pos = tail_lower.find(other.casefold())
                if pos >= 0 and any(term in tail[pos:pos + 40] for term in event_terms):
                    return False

    # 另一家公司才是主詞，而目標股只出現在「未回應／提及／供應／影響」等
    # 關係語句後面時，視為延伸提及，不列入該自選股新聞。
    relation_terms = ("未回應", "未提及", "提及", "提到", "談及", "受惠", "供應",
                      "供應鏈", "客戶", "影響", "連帶", "旗下", "比較")
    title_lower = title.casefold()
    subject_lower = subject.casefold()
    start = 0
    while True:
        pos = title_lower.find(subject_lower, start)
        if pos < 0:
            break
        before = title[max(0, pos - 14):pos]
        if any(term.casefold() in before for term in relation_terms):
            return False
        # 目標名稱若只在標題後段出現，且後面緊接合作／供應／受惠等
        # 關係詞，通常是被提及的公司，不是新聞事件主體。
        after = title[pos + len(subject):pos + len(subject) + 24]
        mention_after_terms = ("合作", "效益", "供應", "供應鏈", "客戶", "受惠",
                               "帶動", "影響", "相關", "夥伴", "聯手", "攜手")
        if pos >= max(8, int(len(title) * 0.35)) and any(
                term in after for term in mention_after_terms):
            return False
        start = pos + len(subject_lower)
    return True


def fetch_stock_news(keyword, max_items=2, within_hours=30, subject_name=None,
                     known_names=None):
    """
    抓取最新新聞，只保留標題、來源、連結與發布時間。

    這裡不抓新聞內文、不自行解讀，也不把網址直接塞進 LINE 文字；
    呼叫端可以把標題做成可點擊的 URI。查詢同時加入股票／財經主題詞，
    並對標題做關鍵字、財經語境與重複過濾，降低無關結果。
    """
    import xml.etree.ElementTree as ET
    from urllib.parse import quote

    keyword_text = str(keyword or "").strip()
    query_terms = [part.strip() for part in keyword_text.split(" OR ")
                   if part.strip()]
    finance_terms = (
        "股價", "營收", "法人", "財報", "法說", "業績", "獲利", "展望",
        "訂單", "產能", "供應鏈", "股利", "配息", "除息", "投資", "併購",
        "增資", "減資", "公告", "買超", "賣超", "漲停", "跌停", "新高", "新低",
    )
    if len(query_terms) > 1:
        query_text = (f"({' OR '.join(query_terms)}) "
                      f"({' OR '.join(finance_terms[:16])})")
    else:
        one_term = query_terms[0] if query_terms else keyword_text
        query_text = f'"{one_term}" ({" OR ".join(finance_terms[:16])})'
    query = quote(query_text)
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
    seen_titles = set()
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or "").strip()
        pub = item.findtext("pubDate")
        if not title:
            continue

        # Google News 常把媒體名放在標題末端；只在 source 欄位空白時切出來。
        if " - " in title and not source:
            title, source = title.rsplit(" - ", 1)
        title = title.strip()
        title_key = title.casefold()
        if title_key in seen_titles:
            continue

        # 股票查詢至少要在標題中出現股票名稱；OR 查詢則至少命中一個主題詞。
        title_lower = title.casefold()
        if query_terms and not any(term.casefold() in title_lower
                                   for term in query_terms):
            continue
        if len(query_terms) == 1 and not any(term in title for term in finance_terms):
            continue
        if subject_name and not _news_subject_relevant(title, subject_name, known_names):
            continue

        if pub:
            try:
                pub_dt = datetime.strptime(
                    pub, "%a, %d %b %Y %H:%M:%S %Z"
                ).replace(tzinfo=timezone.utc)
                age_seconds = (now - pub_dt).total_seconds()
                if age_seconds < -7200 or age_seconds > within_hours * 3600:
                    continue
            except ValueError:
                pass

        seen_titles.add(title_key)
        items.append({"title": title, "source": source.strip(), "link": link})
        if len(items) >= max_items:
            break
    return items


def build_news_digest(user_id):
    """
    自選股新聞摘要：LINE 只顯示相關新聞標題，標題本身可點擊查看原文。
    不在聊天室顯示完整網址，也不把新聞內文或 AI 解讀帶進來。
    """
    codes = get_user_watchlist(user_id)
    if not codes:
        return None

    # 名稱不必先重新抓整份法人行情，避免新聞指令被不必要的外部查詢拖慢。
    names = {code: stock_display_name(code) for code in codes}
    all_company_names = _news_company_names(names.values())

    def fetch_one(code):
        try:
            return fetch_stock_news(
                names[code], max_items=2, within_hours=36,
                subject_name=names[code], known_names=all_company_names)
        except Exception as e:
            print(f"⚠️ 並行抓新聞失敗 {code}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=min(4, len(codes))) as ex:
        news_map = dict(zip(codes, ex.map(fetch_one, codes)))

    records = []
    seen = set()
    for code in codes:
        for item in (news_map.get(code) or []):
            title = str(item.get("title") or "").strip()
            if not title or title.casefold() in seen:
                continue
            seen.add(title.casefold())
            records.append({"code": code, "name": names[code], **item})

    if not records:
        return TextSendMessage(text="📰 自選股新聞\n\n今日沒有抓到符合自選股與財經主題的相關新聞。")

    # LINE 只做入口，最多顯示 8 個標題，避免又變成長篇報告。
    records = records[:8]
    token = create_web_token(user_id)
    contents = [
        {"type": "text", "text": f"📰 自選股新聞｜{taiwan_now().strftime('%m/%d')}",
         "weight": "bold", "size": "xl", "color": "#1B2027"},
        {"type": "text", "text": "只顯示相關新聞標題；點擊標題即可查看原文。",
         "size": "xs", "color": "#767D85", "margin": "sm", "wrap": True},
    ]
    for index, item in enumerate(records):
        uri = _valid_news_uri(item.get("link"))
        component = {
            "type": "text",
            "text": f"・{item['name']}｜{item['title']}",
            "size": "sm", "color": "#4A5F7A", "wrap": True,
            "maxLines": 3, "margin": "md" if index else "lg",
        }
        if uri:
            component["decoration"] = "underline"
            component["action"] = {"type": "uri", "uri": uri}
        contents.append(component)

    alt_text = "📰 自選股新聞｜" + "；".join(item["title"] for item in records[:3])
    return FlexSendMessage(
        alt_text=alt_text[:400],
        contents={
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical",
                     "contents": contents, "paddingAll": "18px",
                     "backgroundColor": "#FFFFFF"},
            "styles": {"body": {"backgroundColor": "#FFFFFF"}},
        },
    )


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


def build_push_request_message(user_id, base_url=None):
    """推播申請回覆：確認申請已收到，並提供盤前總經頁的直接入口。"""
    plain_text = (
        "📮 已收到每日推播的申請\n\n"
        "每日盤前推播為名額制，需由管理者開通。\n"
        "已收到你的申請，開通後隔天早上就會自動收到。\n\n"
        "在此之前，請按下「盤前」查看總體經濟面與今日盤前內容。"
    )
    token = create_web_token(user_id)
    if not token:
        return TextSendMessage(text=plain_text)
    web_url = (f"{public_web_base_url(base_url)}/web/premarket?t="
               f"{quote(token, safe='')}")
    bubble = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": "📮 已收到每日推播的申請",
             "weight": "bold", "size": "xl", "color": "#1B2027"},
            {"type": "text", "text":
             "每日盤前推播為名額制，需由管理者開通。\n"
             "已收到你的申請，開通後隔天早上就會自動收到。\n\n"
             "在此之前，請按下「盤前」查看總體經濟面與今日盤前內容。",
             "size": "sm", "color": "#454C55", "wrap": True, "margin": "md"},
            {"type": "separator", "margin": "lg", "color": "#E8EAE6"},
            {"type": "button", "style": "primary", "height": "sm",
             "color": "#6E5228", "margin": "lg",
             "action": {"type": "uri", "label": "☀️ 開啟盤前",
                        "uri": web_url}},
        ], "paddingAll": "18px", "backgroundColor": "#FFFFFF"},
        "styles": {"body": {"backgroundColor": "#FFFFFF"}},
    }
    return FlexSendMessage(alt_text=plain_text, contents=bubble)


def build_turning_observation_line_message(user_id, base_url=None):
    """LINE 轉折摘要只讀完成快照；沒有快照時背景刷新，不阻塞 webhook。"""
    result, fresh, snapshot_source = _get_turning_web_snapshot()
    status_note = None
    if result is None:
        _start_turning_background_refresh()
        result = {"items": [], "data_date": None, "prior_days": 5}
        status_note = "轉折快照正在背景整理，這則訊息不等待完整計算。"
    elif not fresh:
        _start_turning_background_refresh()
        status_note = (f"先顯示{snapshot_source}（資料日 {result.get('data_date') or '未標日期'}）；"
                       "最新資料正在背景更新。")
    items = result.get("items") or []
    data_date = result.get("data_date") or "未標日期"
    prior_days = int(result.get("prior_days") or 5)
    token = create_web_token(user_id)
    web_url = None
    if token:
        web_url = (f"{public_web_base_url(base_url)}/web/screener?mode=turning"
                   f"&t={quote(token, safe='')}")
    contents = [
        {"type": "text", "text": "🔄 轉折觀察", "weight": "bold",
         "size": "xl", "color": "#1B2027"},
        {"type": "text", "text": f"資料來源：{snapshot_source}・法人資料日：{data_date}",
         "size": "xs", "color": "#767D85", "margin": "sm", "wrap": True},
    ]
    if status_note:
        contents.append({"type": "text", "text": status_note,
                         "size": "xs", "color": "#8A6A32", "margin": "sm", "wrap": True})
    state_labels = (("confirmed", "✅ 已確認"), ("observing", "👀 觀察中"),
                    ("invalid", "⚠️ 已失效"))
    shown = 0
    for state, state_label in state_labels:
        group = [item for item in items if item.get("state") == state]
        if not group:
            continue
        contents.append({"type": "text", "text": state_label, "weight": "bold",
                         "size": "sm", "color": "#6E5228", "margin": "lg"})
        for item in group[:3]:
            state = str(item.get("state") or state)
            state_text = "已確認" if state == "confirmed" else "觀察中" if state == "observing" else "已失效"
            flow_label = str(item.get("direction_flow_label") or "")
            if flow_label not in {"賣轉買", "買轉賣", "買方增強", "賣方增強"}:
                legacy_label = str(item.get("direction_label") or "")
                event_type_fallback = str(item.get("event_type") or "")
                if legacy_label == "賣轉買" or event_type_fallback == "賣轉買" or "轉買" in event_type_fallback:
                    flow_label = "賣轉買"
                elif legacy_label == "買轉賣" or event_type_fallback == "買轉賣" or "轉賣" in event_type_fallback:
                    flow_label = "買轉賣"
                elif item.get("direction") == "up":
                    flow_label = "買方增強"
                elif item.get("direction") == "down":
                    flow_label = "賣方增強"
                else:
                    flow_label = "方向不明"
            details = [str(value) for value in
                       (item.get("reason_details") or item.get("reasons") or [])
                       if str(value).strip()]

            def pick_fact(prefixes, fallback):
                for value in details:
                    if any(value.startswith(prefix) for prefix in prefixes):
                        return value
                return fallback

            current = item.get("current_total_lots")
            institutional = pick_fact(("三大法人今日", "外資由", "投信由", "自營由"),
                                      f"法人今日{('買超' if item.get('direction') == 'up' else '賣超')} "
                                      f"{int(current or 0):+,} 張")
            price_fact = pick_fact(("收盤",), "價格／20日均線資料不足")
            volume_fact = pick_fact(("成交量",), "成交量資料不足")
            streak_fact = pick_fact(("連續上漲", "連續下跌", "今日為近期", "今日翻黑"), "")
            facts = [institutional, price_fact, volume_fact]
            if streak_fact:
                facts.append(streak_fact)
            if state == "invalid":
                conclusion = str(item.get("state_reason") or "失效原因資料不足")
            else:
                score = int(item.get("score") or 0)
                conclusion = (f"{item.get('event_type') or '法人方向變化'}；"
                              f"符合 {score}/5 個條件，"
                              f"{'已達確認門檻' if state == 'confirmed' else '尚未達確認門檻'}")
            contents.append({"type": "text",
                             "text": f"・{item.get('name') or item.get('code')}（{item.get('code')}）｜{flow_label}｜{state_text}\n"
                                      f"  判讀：{conclusion}\n"
                                      f"  " + "\n  ".join(facts),
                              "size": "sm", "color": "#454C55", "wrap": True,
                              "margin": "sm"})
            shown += 1
    if not shown:
        contents.append({"type": "text", "text": "目前沒有足夠的法人與行情資料建立轉折觀察。",
                         "size": "sm", "color": "#767D85", "margin": "lg", "wrap": True})
    contents.append({"type": "separator", "margin": "lg", "color": "#E8EAE6"})
    contents.append({"type": "text", "text": "※ 轉折觀察是規則式資料整理，不代表確定反轉或買賣建議。",
                     "size": "xs", "color": "#767D85", "wrap": True, "margin": "md"})
    if web_url:
        contents.append({"type": "button", "style": "link", "height": "sm",
                         "color": "#6E5228", "margin": "md",
                         "action": {"type": "uri", "label": "查看完整轉折觀察",
                                    "uri": web_url}})
    return FlexSendMessage(alt_text="🔄 轉折觀察", contents={
        "type": "bubble", "body": {"type": "box", "layout": "vertical",
        "contents": contents, "paddingAll": "18px", "backgroundColor": "#FFFFFF"},
        "styles": {"body": {"backgroundColor": "#FFFFFF"}}})


def _radar_live_cache_is_fresh(snapshot, max_age_seconds=None):
    """檢查記憶體內盤中快取的完成時間，避免把過期即時資料當新資料。"""
    if not isinstance(snapshot, dict) or not snapshot.get("radar_live"):
        return True
    finished = snapshot.get("scan_finished_at")
    if not finished:
        return False
    try:
        text = str(finished).replace("Z", "+00:00")
        computed_at = datetime.fromisoformat(text)
        if computed_at.tzinfo is None:
            computed_at = computed_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - computed_at).total_seconds()
        limit = float(max_age_seconds or RADAR_LIVE_SNAPSHOT_MAX_AGE_SECONDS)
        return 0 <= age <= limit
    except (TypeError, ValueError, OverflowError):
        return False


def _line_screener_snapshot(mode, intraday=False):
    """LINE 只讀已完成結果；盤中優先使用最近的 radar_live 快照。"""
    now = time.time()
    if intraday and mode == "radar" and "_load_recent_live_radar_snapshot" in globals():
        live = _load_recent_live_radar_snapshot()
        if live and isinstance(live.get("rows"), list):
            live_meta = live.get("source_meta") or {}
            live_cache = {
                "at": now, "rows": live.get("rows") or [],
                "radar_live": True,
                "skipped": live.get("skipped", 0),
                "momentum": live.get("momentum") or {},
                "source_date": live.get("source_date"),
                "scan_universe_count": live.get("scan_universe_count") or live_meta.get("scan_universe_count"),
                "scan_finished_at": live.get("scan_finished_at") or live_meta.get("scan_finished_at"),
                "radar_diagnostics": live.get("radar_diagnostics") or live_meta.get("radar_diagnostics") or {},
            }
            _screener_cache[mode] = live_cache
            return live_cache, "盤中跨 worker 即時快照", True
    cached = _screener_cache.get(mode)
    if cached and isinstance(cached.get("rows"), list):
        age = now - float(cached.get("at") or 0)
        if (intraday and age <= SCREENER_CACHE_SECONDS and
                _radar_live_cache_is_fresh(cached)):
            return cached, "盤中記憶體快取（5分鐘內）", True
        if not intraday and _screener_snapshot_is_recent(cached, 3):
            return cached, "最近完整快照", False

    persisted = _load_persisted_screener_snapshot(mode)
    if persisted and _screener_snapshot_is_recent(persisted, 3):
        if not intraday:
            return persisted, "共享完整快照", False
        # 盤中可先顯示最近收盤結果，但不可把它稱為即時行情。
        return persisted, "最近收盤快照（盤中更新中）", False
    return None, "尚無可用快照", False


def _build_line_radar_pending_message(user_id, base_url=None,
                                      already_running=False):
    """LINE 盤中雷達的立即回覆；真正掃描在背景完成後另行推送。"""
    token = create_web_token(user_id)
    web_url = None
    if token:
        web_url = (f"{public_web_base_url(base_url)}/web/screener?mode=radar"
                   f"&view=list&t={quote(token, safe='')}")
    state = ("已有另一個即時全市場掃描正在處理，完成後請重新輸入「雷達」查看。"
             if already_running else
             "已啟動即時全市場行情掃描；完成後會再推送本次真實前三名。")
    contents = [
        {"type": "text", "text": "🚨 雷達｜即時掃描已啟動",
         "weight": "bold", "size": "xl", "color": "#1B2027"},
        {"type": "text", "text": state, "size": "sm", "color": "#454C55",
         "margin": "md", "wrap": True},
        {"type": "text", "text": "LINE 不再卡住等待；掃描期間不展示舊報酬率，也不以舊快照冒充即時結果。",
         "size": "xs", "color": "#767D85", "margin": "sm", "wrap": True},
    ]
    if web_url:
        contents += [
            {"type": "separator", "margin": "lg", "color": "#E8EAE6"},
            {"type": "button", "style": "primary", "height": "sm",
             "color": "#6E5228", "margin": "lg",
             "action": {"type": "uri", "label": "查看雷達網頁入口",
                        "uri": web_url}},
        ]
    return FlexSendMessage(
        alt_text="🚨 雷達即時全市場掃描已啟動",
        contents={"type": "bubble",
                  "body": {"type": "box", "layout": "vertical",
                           "contents": contents, "paddingAll": "18px",
                           "backgroundColor": "#FFFFFF"},
                  "styles": {"body": {"backgroundColor": "#FFFFFF"}}})


def build_line_screener_message(user_id, mode, base_url=None,
                                _completed_live_scan=False):
    """LINE 黑馬／雷達只回前 3 檔；盤中雷達先立即回覆，完成後再推送結果。"""
    mode = "radar" if str(mode).strip() == "radar" else "blackhorse"
    label = "雷達" if mode == "radar" else "黑馬"
    icon = "🚨" if mode == "radar" else "🐎"

    intraday = mode == "radar" and _is_taiwan_intraday_window()
    if intraday and not _completed_live_scan:
        # 正常點擊只讀最近 10 分鐘定時快照；不因同一使用者再次輸入而重掃。
        live_snapshot = None
        if "_load_recent_live_radar_snapshot" in globals():
            live_snapshot = _load_recent_live_radar_snapshot(
                max_age_seconds=globals().get("RADAR_LIVE_SNAPSHOT_MAX_AGE_SECONDS", 10 * 60))
        if live_snapshot and isinstance(live_snapshot.get("rows"), list):
            return build_line_screener_message(
                user_id, "radar", base_url, _completed_live_scan=True)
        # 尚未有定時快照時只允許一次冷啟動；之後由排程接手每 10 分鐘更新。
        started = _start_screener_background_refresh(
            "radar", intraday=True, radar_deep_limit=48,
            on_complete=lambda: line_bot_api.push_message(
                user_id, build_line_screener_message(
                    user_id, "radar", base_url, _completed_live_scan=True)),
            on_failure=lambda _exc: line_bot_api.push_message(
                user_id, TextSendMessage(
                    text="❌ 雷達即時掃描未完成，公開行情資料源暫時無法回應；請稍後再輸入「雷達」重試。")))
        return _build_line_radar_pending_message(
            user_id, base_url, already_running=not started)

    snapshot, snapshot_source, is_realtime_memory = _line_screener_snapshot(
        mode, intraday=intraday)
    is_realtime_scan = bool(_completed_live_scan and mode == "radar" and snapshot)
    live_scan_error = None
    background_started = False
    if is_realtime_scan:
        scan_count = int((snapshot or {}).get("scan_universe_count") or
                         (_screener_cache.get("radar") or {}).get("scan_universe_count") or 0)
        snapshot_source = f"盤中即時全市場掃描（{scan_count or '全市場'}檔）"
        is_realtime_memory = False
    if snapshot is not None:
        rows = list(snapshot.get("rows") or [])
        source_date = snapshot.get("source_date")
        # 非今日的快照只作明示日期的暫時結果；即時掃描成功時不啟動背景重算。
        source_date_obj = source_date
        if isinstance(source_date_obj, str):
            try:
                source_date_obj = date.fromisoformat(source_date_obj[:10])
            except ValueError:
                source_date_obj = None
        if ((intraday and not (is_realtime_memory or is_realtime_scan)) or
                (not intraday and
                 (not source_date_obj or source_date_obj != taiwan_today()))):
            background_started = _start_screener_background_refresh(
                mode, intraday=intraday)
    else:
        rows = []
        source_date = None
        background_started = _start_screener_background_refresh(
            mode, intraday=intraday)

    if mode == "blackhorse":
        rows = [r for r in rows if r.get("score") is not None]
        rows.sort(key=lambda r: r.get("score", -1), reverse=True)
    else:
        def line_radar_key(r):
            breakout = (2 if r.get("breakout") == "季線新高"
                        else (1 if r.get("breakout") else 0))
            fatigue = -1 if (r.get("up_streak") or 0) >= 5 else 0
            return (breakout + fatigue, r.get("vol_ratio") or 0,
                    r.get("streak", 0), r.get("pct", 0))
        rows.sort(key=line_radar_key, reverse=True)

    token = create_web_token(user_id)
    web_url = None
    if token:
        web_url = (f"{public_web_base_url(base_url)}/web/screener?mode={mode}"
                   f"&view=list&t={quote(token, safe='')}")

    date_text = (source_date.isoformat() if hasattr(source_date, "isoformat")
                 else str(source_date or "未標日期"))
    if intraday and is_realtime_scan:
        source_text = snapshot_source or "盤中即時全市場掃描"
        finished_at = snapshot.get("scan_finished_at") if snapshot else None
        if finished_at:
            date_text = f"掃描完成 {str(finished_at)[:16].replace('T', ' ')}・法人資料日 {date_text}"
        else:
            date_text = f"掃描完成・法人資料日 {date_text}"
    elif intraday and is_realtime_memory:
        source_text = "盤中記憶體快取（5分鐘內）"
        date_text = (f"行情快取時間 {taiwan_now().strftime('%Y-%m-%d %H:%M')}・"
                     f"法人資料日 {date_text}")
    elif intraday:
        source_text = "最近收盤快照（盤中更新中）"
        date_text = f"收盤資料日 {date_text}"
    elif snapshot is not None:
        source_text = snapshot_source
    else:
        source_text = "背景整理中"
        date_text = "尚無可用資料日"
    contents = [
        {"type": "text", "text": f"{icon} {label}｜前 3 名",
         "weight": "bold", "size": "xl", "color": "#1B2027"},
        {"type": "text", "text": f"資料來源：{source_text}・資料日：{date_text}",
         "size": "xs", "color": "#767D85", "margin": "sm", "wrap": True},
    ]

    if not rows:
        if mode == "radar" and snapshot is not None:
            diagnostics = (snapshot.get("radar_diagnostics") or
                           (_screener_cache.get("radar") or {}).get("radar_diagnostics") or {})
            empty_text = "目前這次即時掃描沒有可列入雷達的標的。\n" + _radar_empty_summary(
                diagnostics, skipped_liquidity=snapshot.get("skipped", 0))
        else:
            empty_text = (f"目前沒有符合條件的{label}標的。" if snapshot is not None else
                          f"{label}完整快照正在背景整理，這則訊息不等待全市場計算；請稍後重新輸入或點下方網頁入口。")
        contents.append({"type": "text", "text": empty_text,
                         "size": "sm", "color": "#767D85", "margin": "lg", "wrap": True})
    else:
        for rank, row in enumerate(rows[:3], 1):
            name = str(row.get("name") or row.get("code") or "未命名")
            code = str(row.get("code") or "")
            if mode == "blackhorse":
                score = row.get("score")
                score_text = f"黑馬指數 {score}／100" if score is not None else "黑馬指數尚無資料"
                growth = (f"・累計年增 {row['cum_yoy']:+.1f}%"
                          if row.get("cum_yoy") is not None else "")
                detail = f"{score_text}{growth}・法人連買 {row.get('streak', 0)} 日"
            else:
                pct = row.get("pct")
                pct_text = f"{pct:+.2f}%" if pct is not None else "漲跌尚無資料"
                state = row.get("radar_state") or row.get("breakout") or "雷達訊號"
                detail = f"{pct_text}・{state}・法人連買 {row.get('streak', 0)} 日"
            contents.append({
                "type": "box", "layout": "vertical", "margin": "lg",
                "contents": [
                    {"type": "text", "text": f"#{rank} {name}（{code}）",
                     "weight": "bold", "size": "md", "color": "#1B2027", "wrap": True},
                    {"type": "text", "text": detail, "size": "sm",
                     "color": "#454C55", "margin": "xs", "wrap": True},
                ],
            })

    if web_url:
        contents += [
            {"type": "separator", "margin": "lg", "color": "#E8EAE6"},
            {"type": "button", "style": "primary", "height": "sm",
             "color": "#6E5228", "margin": "lg",
             "action": {"type": "uri", "label": f"查看完整{label}分析",
                        "uri": web_url}},
        ]
    else:
        contents.append({"type": "text", "text": "網頁入口暫時無法建立，請稍後再試。",
                         "size": "xs", "color": "#767D85", "margin": "lg", "wrap": True})

    alt_lines = [f"{icon} {label}｜前 3 名｜資料日 {date_text}"]
    for rank, row in enumerate(rows[:3], 1):
        alt_lines.append(f"#{rank} {row.get('name') or row.get('code')}")
    plain_text = "\n".join(alt_lines)
    bubble = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "contents": contents,
                  "paddingAll": "18px", "backgroundColor": "#FFFFFF"},
        "styles": {"body": {"backgroundColor": "#FFFFFF"}},
    }
    return FlexSendMessage(alt_text=plain_text[:400], contents=bubble)


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
        missing_users = get_missing_portfolio_snapshot_user_ids(taiwan_today())
        if missing_users:
            # 之前可能在 portfolio index=1 後逾時，卻仍完成了後續 rank；
            # 不能直接回報完成，改由同一個 job 在下一次重跑補齊所有使用者。
            print(f"⚠️ 每日快照完成完整性檢查：缺少 {len(missing_users)} 位使用者，重新補跑 portfolio stage")
            progress = {"stage": "portfolio", "index": 0,
                        "total": len(get_all_position_user_ids())}
            current_stage = "portfolio"
        elif missing_users == []:
            return f"{taiwan_today()} 的每日快照已完成，避免重複抓取。"
        else:
            return f"{taiwan_today()} 的每日快照已完成，但無法確認所有使用者快照，請稍後重試。"

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
    pick_failures = []
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
                # 選股資料失敗不能阻止收盤後的持股、自選與排行榜快照；
                # 記錄失敗並前進 checkpoint，避免每次重跑都卡在同一個 mode。
                print(f"❌ {mode} 選股名單快照失敗，繼續後續階段: {e}")
                pick_failures.append(mode)
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
        # 所有使用者的每日組合快照完成後才失效一次，避免在迴圈內每人重複操作資料庫。
        # 下一個 rank stage 會依最新每日快照重新計算 TWR，再保存排行榜完整頁 payload。
        clear_leaderboard_cache()
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

    missing_after = get_missing_portfolio_snapshot_user_ids(taiwan_today())
    if missing_after is None:
        portfolio_status = "；持股快照完整性待確認"
    elif missing_after:
        portfolio_status = (f"；持股快照尚缺 {len(missing_after)} 位，下一次會自動補跑："
                            f"{','.join(str(uid) for uid in missing_after[:5])}")
    else:
        portfolio_status = f"；持股快照完整 {len(user_ids)}/{len(user_ids)}"
    pick_status = (f"；選股失敗但未阻斷後續：{','.join(pick_failures)}"
                   if pick_failures else "")
    return (f"組合本次續跑處理 {saved}（略過 {skipped}，共 {len(user_ids)}）、"
            f"自選本次 {wl_saved}/{len(wl_users)}、產業 {ind_saved}、"
            f"選股名單 {picks_saved}、排行榜名次 {rank_saved}、大盤 {taiex_close}"
            f"{portfolio_status}{pick_status}")


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
    """預熱目前持股主頁需要的 1d 真實行情，直接填入既有90秒快取。"""
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
    # 主頁的即時價格、今日損益與權重只需要 1d；較重的一年走勢改成
    # 使用者展開個別持股時才載入，避免每次打開頁面都下載不必要的歷史資料。
    prices = get_realtime_stocks_bulk(sorted(codes), workers=12, rng="1d")
    valid = sum(1 for value in prices.values() if value)
    return len(user_ids), len(codes), valid, "完成"


def _do_warmup():
    done = []
    shared_data = {}
    for label, fn in [
        ("法人", fetch_institutional_data),
        ("月營收", fetch_monthly_revenue),
        ("估值", fetch_valuation),
        ("產業別", get_industry_map),
        ("名稱對照", get_name_map),
    ]:
        try:
            data = fn()
            shared_data[label] = data or {}
            done.append(f"{label} {len(data) if data else 0}")
        except Exception as e:
            print(f"❌ 預熱 {label} 失敗: {e}")
            shared_data[label] = {}
            done.append(f"{label} 失敗")

    # warmup 明確把共享資料保存一次，跨 Render worker／重啟可直接讀取。
    # 各 loader 本身也有保存與資料庫 fallback；這裡的再次保存只在有完整資料時執行，
    # 失敗不得讓既有 warmup 或網站功能中斷。
    snapshot_jobs = [
        ("monthly_revenue", shared_data.get("月營收"),
         _revenue_cache.get("source"), _revenue_cache.get("source_date"),
         {"period": str(_revenue_cache.get("period") or "")}),
        ("valuation", shared_data.get("估值"),
         _valuation_cache.get("source"), _valuation_cache.get("source_date"),
         {}),
    ]
    for snapshot_key, payload, source_kind, source_date, meta in snapshot_jobs:
        if not isinstance(payload, dict) or not payload:
            done.append(f"{snapshot_key} 快照略過")
            continue
        # shared/history 是既有資料的 fallback，不刷新 computed_at，避免來源中斷時
        # 每次 warmup 都讓舊資料永久延命；external/file 才代表本次可確認的新資料。
        if source_kind not in ("external", "file"):
            done.append(f"{snapshot_key} 快照保留原來源（{source_kind or '未知'}）")
            continue
        try:
            saved = _save_shared_data_snapshot(
                snapshot_key, payload, data_date=source_date or taiwan_today(),
                source_meta={"source": f"warmup:{source_kind}", **{
                    k: v for k, v in meta.items() if v}},
            )
            done.append(f"{snapshot_key} 快照{'已保存' if saved else '未保存'}")
        except Exception as e:
            print(f"⚠️ 預熱保存共享快照 {snapshot_key} 失敗: {e}")
            done.append(f"{snapshot_key} 快照失敗")

    names = shared_data.get("名稱對照") or {}
    industries = shared_data.get("產業別") or {}
    markets = shared_data.get("市場") or {}
    if names or industries or markets:
        try:
            saved = _save_shared_data_snapshot(
                "stock_info_map", {"names": names, "industries": industries,
                                    "markets": markets},
                data_date=taiwan_today(),
                source_meta={"source": "warmup", "name_count": len(names),
                             "industry_count": len(industries),
                             "market_count": len(markets)},
            )
            done.append(f"stock_info 快照{'已保存' if saved else '未保存'}")
        except Exception as e:
            print(f"⚠️ 預熱保存 stock_info 快照失敗: {e}")
            done.append("stock_info 快照失敗")
    else:
        done.append("stock_info 快照略過")

    # 籌碼超人與 LINE／網頁共用同一份近十日法人整理。17:00／20:00
    # 收盤後 warmup 強制刷新；其他時段只讀有效快照，避免重複做五組分類查詢。
    try:
        chips_result = build_chips_payload(
            force_refresh=taiwan_now().hour >= 16, persist=True)
        chips_payload = chips_result.get("payload") or {}
        chips_state = ("資料日 " + str(chips_result.get("data_date"))
                       if chips_result.get("data_date") else "資料不足")
        done.append(f"籌碼超人 {chips_state}（{chips_result.get('source') or '未知來源'}）")
    except Exception as e:
        print(f"❌ 預熱籌碼超人失敗: {e}")
        done.append("籌碼超人 失敗")

    # ETF 商品指標先批次抓取並跨 worker 保存；不可讓每張商品卡片各自打 TWSE。
    # 這些欄位包含官方資產規模、官方收盤價、年初至今均成交與受益人次。
    try:
        metrics_payload = fetch_twse_etf_product_metrics(
            force_reload=taiwan_now().hour >= 16)
        done.append("ETF官方商品指標 %s 檔" % len(metrics_payload or {}))
    except Exception as e:
        print(f"❌ 預熱 ETF 官方商品指標失敗: {e}")
        done.append("ETF官方商品指標 失敗")

    # ETF 配息資料先批次抓取並跨 worker 保存；不可讓每張商品卡片各自打 TWSE。
    # 若官方頁暫時不可用，保留既有快取／unknown，不把失敗當作零配息。
    try:
        distribution_payload = fetch_twse_etf_distribution_history(
            force_reload=taiwan_now().hour >= 16)
        done.append("ETF官方配息 %s 檔有已發生紀錄" % len(distribution_payload or {}))
    except Exception as e:
        print(f"❌ 預熱 ETF 官方配息失敗: {e}")
        done.append("ETF官方配息 失敗")

    # ETF 商品排名使用真實日收盤序列，在固定 warmup 建立，避免進入 ETF 專區時
    # 才同步抓取全市場商品；排名快照另存共享資料，跨 Render worker 可直接使用。
    if taiwan_now().hour >= 16:
        try:
            _existing_ranking, ranking_fresh, _ranking_source = _load_etf_product_ranking_snapshot()
            ranking_payload = build_etf_product_rankings(
                force_refresh=not ranking_fresh)
            done.append("ETF排名 %s（資料日 %s）" % (
                "已整合" if ranking_payload.get("categories") else "資料不足",
                ranking_payload.get("market_data_date") or "未標日期"))
        except Exception as e:
            print(f"❌ 預熱 ETF 商品排名失敗: {e}")
            done.append("ETF排名 失敗")

    # 轉折觀察與籌碼超人共用 T86 與行情來源；在 warmup 先建立共享結果，
    # 避免第一位使用者進頁面時才並行抓取數十檔 3mo 行情。
    try:
        turning_result = build_turning_observation(
            limit=60, prior_days=5, force_refresh=taiwan_now().hour >= 16)
        turning_date = turning_result.get("data_date") or "資料不足"
        done.append(f"轉折觀察 {len(turning_result.get('items') or [])} 檔（資料日 {turning_date}）")
    except Exception as e:
        print(f"❌ 預熱轉折觀察失敗: {e}")
        done.append("轉折觀察 失敗")

    # 今日完整首頁最慢的外部資料是持股即時行情；交易日先預熱到既有
    # 90 秒記憶體快取，使用者開頁時直接命中。週末不把最新收盤誤當成今日行情。
    try:
        user_count, code_count, valid_count, state = _warm_current_position_quotes()
        done.append(f"持股行情 {valid_count}/{code_count} 檔（{user_count} 人，{state}）")
    except Exception as e:
        print(f"❌ 預熱持股行情失敗: {e}")
        done.append("持股行情 失敗")

    # 順便把選股台的候選池也算好，使用者進來就是快取命中；
    # compute_screener_rows 會在完整計算後保存 rows，若已命中持久化快照則不重掃 Yahoo。
    for mode in ("blackhorse", "radar"):
        try:
            rows, _s, _m = compute_screener_rows(mode)
            persisted = _load_persisted_screener_snapshot(mode)
            if persisted and _screener_snapshot_valid_for_today(persisted):
                done.append(f"{mode} {len(rows)} 檔（快照 {persisted.get('source_date') or '未標日期'}）")
            else:
                done.append(f"{mode} {len(rows)} 檔（快照未命中）")
        except Exception as e:
            print(f"❌ 預熱 {mode} 失敗: {e}")
            done.append(f"{mode} 失敗")

    # 排行榜只在收盤後／週末晚間整合，沿用現有 17:00／20:00 warmup；
    # 早上 07:00／中午 12:00 不為了排行榜額外重算一年行情。
    if taiwan_now().hour >= 16:
        try:
            # 若持久化頁面的曲線最新日還不是今天，收盤 warmup 必須重建一次；
            # 不能因快照仍在有效期限內就把前一交易日排名當成今日排名。
            persisted_rank = _load_persisted_leaderboard_page()
            persisted_date = (_leaderboard_date(persisted_rank.get("data_date"))
                              if persisted_rank else None)
            if taiwan_today().weekday() < 5 and persisted_date != taiwan_today():
                clear_leaderboard_cache()
            build_leaderboard(top_n=100, days=365)
            with _leaderboard_cache_lock:
                rank_meta = dict(_leaderboard_cache.get((100, 365)) or {})
            done.append("排行榜 %s（資料日 %s）" % (
                "快照已整合" if rank_meta.get("data_date") else "資料日不足",
                rank_meta.get("data_date") or "未標日期"))
        except Exception as e:
            print(f"❌ 預熱排行榜失敗: {e}")
            done.append("排行榜 失敗")
    else:
        done.append("排行榜 快照略過（等待收盤）")
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
    "籌碼", "籌碼超人", "認養",
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
/* 視覺方向：明亮霧藍資料工作台；深墨文字、台股紅漲綠跌、低裝飾高可讀性。 */
:root{
  --paper:#F6F8FB; --paper-2:#ECF1F6; --ink:#1D2939;
  --ink-soft:#475467; --ink-faint:#7B8794; --rule:#D4DEE8;
  --up:#C0443C; --down:#197653; --brass:#355B7B; --brass-2:#6E8BA8;
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
  .position-journal{margin:22px 0;border:1px solid #D9D5C9;border-radius:12px;background:#FFFDF8;overflow:hidden}
  .position-journal-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;padding:15px 15px 11px;border-bottom:1px solid #E8E3D8}
  .position-journal-head h2{margin:0;font-size:18px}
  .position-journal-head small{color:var(--ink-faint);font-size:11.5px;line-height:1.5;text-align:right}
  .position-journal-note{padding:9px 15px;background:#F7F4EC;color:var(--ink-soft);font-size:11.5px;line-height:1.6}
  .position-journal-day{padding:10px 15px 4px;color:var(--brass);font-size:12px;font-weight:700;letter-spacing:.04em}
  .position-journal-table-head{display:grid;grid-template-columns:minmax(0,1.25fr) .72fr minmax(0,1fr) minmax(0,1fr) minmax(0,1.05fr);gap:9px;align-items:end;padding:9px 15px 7px;background:#F8F7F2;color:var(--ink-soft);font-size:11px;font-weight:700;line-height:1.25;border-bottom:1px solid #E8E3D8}
  .position-journal-table-head span:not(:first-child){text-align:right}
  .position-journal-row{display:grid;grid-template-columns:minmax(0,1.25fr) .72fr minmax(0,1fr) minmax(0,1fr) minmax(0,1.05fr);gap:9px;align-items:center;padding:12px 15px;border-top:1px solid #EEEAE1}
  .position-journal-name,.position-journal-status{min-width:0}
  .position-journal-name b{display:block;font-size:15px;overflow-wrap:anywhere}
  .position-journal-name small{display:block;color:var(--ink-soft);font-size:11px;margin-top:2px;overflow-wrap:anywhere}
  .position-journal-status{text-align:right}
  .position-journal-cell{min-width:0;color:var(--ink-soft);font-size:11px;line-height:1.45;text-align:right}
  .position-journal-cell b{display:block;color:var(--ink);font-size:14px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
  .position-journal-cell small{display:block;color:var(--ink-faint);font-size:10.5px;overflow-wrap:anywhere}
  .position-journal-pnl{margin-top:2px;font-weight:700}
  .position-journal-badge{display:inline-block;padding:3px 7px;border-radius:999px;font-size:11px;font-weight:700;line-height:1.25}
  .position-journal-badge.new{background:#F6F0D7;color:#927A12}
  .position-journal-badge.add{background:#FCE9E6;color:var(--up)}
  .position-journal-badge.reduce{background:#E8F2EA;color:var(--down)}
  .position-journal-foot{padding:10px 15px 13px;color:var(--ink-faint);font-size:10.5px;line-height:1.55}
  .position-journal-empty{padding:14px 15px;color:var(--ink-soft);font-size:12.5px}
  @media(max-width:640px){
    .position-journal-head{padding:13px 12px 10px}
    .position-journal-head h2{font-size:17px}
    .position-journal-head small{text-align:right;font-size:10.5px}
    .position-journal-table-head{grid-template-columns:minmax(0,1.25fr) .72fr minmax(0,1fr) minmax(0,1fr) minmax(0,1.05fr);gap:5px;padding:8px 8px 6px;font-size:9.5px}
    .position-journal-row{grid-template-columns:minmax(0,1.25fr) .72fr minmax(0,1fr) minmax(0,1fr) minmax(0,1.05fr);gap:6px 5px;padding:10px 8px}
    .position-journal-name b{font-size:13px}
    .position-journal-name small{font-size:9.5px}
    .position-journal-cell{font-size:9.5px}
    .position-journal-cell b{font-size:12px}
    .position-journal-cell small{font-size:9px}
    .position-journal-status{font-size:9px}
    .position-journal-badge{padding:3px 5px;font-size:9.5px}
    .position-journal-note,.position-journal-day,.position-journal-foot{padding-left:8px;padding-right:8px}
  }

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
.chips-meta{margin:16px 0 12px;padding:11px 13px;border-left:3px solid var(--brass);background:#F7F6F1;color:var(--ink-soft);font-size:12px;line-height:1.65}
.chips-section{background:#FFF;border:1px solid #E1E3DE;border-radius:14px;padding:16px 15px;margin:14px 0;box-shadow:0 3px 14px rgba(35,39,35,.045)}
.chips-section h2{font-size:17px;color:var(--ink);margin-bottom:4px}
.chips-section>p{color:var(--ink-soft);font-size:12px;line-height:1.65;margin-bottom:8px}
.chips-row{display:flex;justify-content:space-between;align-items:center;gap:12px;border-top:1px solid #ECEDE8;padding:11px 0}
.chips-row>div{min-width:0}.chips-row b{display:block;font-size:14px;overflow-wrap:anywhere}.chips-row small{display:block;color:var(--ink-soft);font-size:11px;margin-top:2px;line-height:1.5}.chips-row strong{font-size:15px;white-space:nowrap;color:var(--ink)}
.chips-empty{padding:9px 0;color:var(--ink-faint);font-size:12px}
.callout{padding:12px 14px;margin:14px 0;background:#F5F5F1;color:var(--ink-soft);font-size:11.5px;line-height:1.7;border-radius:9px}
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
.review-details{margin-top:14px;border:1px solid #E1E3DE;border-radius:10px;background:#FFF;overflow:hidden}
.review-details>summary{cursor:pointer;padding:12px 14px;color:var(--brass);font-size:13px;font-weight:600;background:#F7F6F1}
.review-samples{border-top:1px solid #ECEDE8}
.review-sample{padding:11px 14px;border-bottom:1px solid #ECEDE8}
.review-sample:last-child{border-bottom:0}
.review-sample-title{display:flex;align-items:baseline;justify-content:space-between;gap:10px;font-size:13px}
.review-sample-title b{font-weight:600;color:var(--ink)}
.review-sample-title small{font-size:11px;color:var(--ink-faint);white-space:nowrap}
.review-sample-metrics{display:flex;flex-wrap:wrap;gap:5px 14px;margin-top:5px;color:var(--ink-soft);font-size:12px;line-height:1.55}
.review-sample-metrics span{white-space:nowrap}
.review-sample-metrics b{font-weight:600}
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
 .rank-source-note{font-size:11px;color:var(--ink-faint);margin:0 0 9px;text-align:right}
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
	html{background:#F6F8FB;touch-action:manipulation}
	body{background:#F6F8FB;overflow-x:hidden;padding-bottom:calc(76px + env(safe-area-inset-bottom));-webkit-tap-highlight-color:transparent}
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
	background:rgba(246,248,251,.98);backdrop-filter:blur(14px);padding:12px 16px 10px;border-bottom:1px solid rgba(212,222,232,.95);
	box-shadow:0 3px 12px rgba(29,41,57,.06)}
	@media(max-width:699px){.app-header{backdrop-filter:none;background:#F6F8FB}.app-bottom-nav{backdrop-filter:none;background:rgba(255,255,255,.98)}}
.app-header .eyebrow{margin-bottom:2px;font-size:10px;letter-spacing:.18em}
.app-header h1{font-size:21px;letter-spacing:.01em}
.app-header .dateline{font-size:11px;margin-top:2px}
.top-nav{display:none;gap:6px;overflow-x:auto;white-space:nowrap;padding:10px 0;margin:0 -2px 4px;border:0;scrollbar-width:none}
.top-nav::-webkit-scrollbar{display:none}
	.top-nav a{padding:7px 11px;border-radius:999px;background:#EAF0F6;color:var(--ink-soft);font-size:12.5px;text-decoration:none}
	.top-nav a.on{background:#355B7B;color:#FFF;border:0;padding-bottom:7px}
	.app-bottom-nav{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;justify-content:center;background:rgba(255,255,255,.96);backdrop-filter:blur(16px);border-top:1px solid #D4DEE8;padding:8px 8px calc(8px + env(safe-area-inset-bottom));box-shadow:0 -5px 18px rgba(29,41,57,.07)}
.app-bottom-nav .bottom-inner{width:min(760px,100%);display:grid;grid-template-columns:repeat(5,1fr);gap:3px}
.app-bottom-nav a{display:flex;flex-direction:column;align-items:center;gap:2px;color:var(--ink-faint);font-size:11px;text-decoration:none;padding:3px 0;border-radius:10px}
.app-bottom-nav a b{font-size:17px;font-weight:500;line-height:1}
	.app-bottom-nav a.on{color:#355B7B;background:#EAF1F7;font-weight:700}
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
	/* 全站共用的明亮資料介面層：讓舊 renderer 也有一致卡片、表格與表單層級。 */
	.daily-card,.more-group,.rank-spotlight,.chips-section,.position-journal,.rank-situation,.review-details{border-color:#D9E2EC!important;background:#FFF!important;box-shadow:0 5px 18px rgba(29,41,57,.045)}
	.daily-card,.rank-spotlight,.more-group{border-radius:14px}
	.app-page-content{min-width:0}.app-page-loading{opacity:.72;transition:opacity .16s ease}.app-fragment-status{display:flex;align-items:center;gap:10px;margin:0 0 10px;padding:10px 11px;border:1px solid #cfddea;border-radius:8px;background:#f7fbff;color:#526b84;font-size:12px}.app-fragment-status b,.app-fragment-status small{display:block}.app-fragment-status small{margin-top:2px;color:#6c8095}.app-sync-spinner{width:15px;height:15px;border:2px solid #c9d8e5;border-top-color:#3f6f91;border-radius:50%;flex:none;animation:app-sync-spin .72s linear infinite}@keyframes app-sync-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.app-sync-spinner{animation:none;border-top-color:#c9d8e5}}.app-fragment-error{border-color:#e5c5bf;background:#fff8f6;color:#a33b2e}
	.section-head{margin-top:26px}.section-head h2{font-size:18px;font-weight:800;color:var(--ink)}
	.callout,.chips-meta,.hint,.msg,.dist{background:#EEF5FB;border-left-color:#527A9B;color:var(--ink-soft)}
	input,select{background:#FFF;border-color:#CAD7E5;border-radius:7px}input:focus,select:focus{outline-color:#6E8BA8}
	button{background:#355B7B;color:#FFF;border-radius:7px}button:hover{background:#274865}
	.form.add,form.add,.sellpanel{background:#EEF3F8;border-radius:12px;border:1px solid #D9E2EC}
	.rows,.row,.rank-card,.chips-row,.more-item{border-color:#E5EBF1}
	.rank-tabs{background:#E8EFF6}.rank-tabs a.on{background:#FFF;color:#274865}.rank-mine{background:#EEF5FB;border-left-color:#527A9B}
	.profile-grid{background:#D9E2EC;border-color:#D9E2EC}.pf{background:#F9FBFD}
	.cmp th.rk{background:#F6F8FB}.cmp td.best{background:#EEF5FB}
	@media(max-width:640px){.wrap{padding-left:12px;padding-right:12px}.app-header{padding-left:12px;padding-right:12px}.section-head h2{font-size:17px}.daily-card,.rank-spotlight,.more-group{border-radius:12px}}
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
        refresh = ' data-fresh-nav="1"' if href == "/web/portfolio" else ""
        return f'<a href="{href}"{on}{refresh} data-app-nav="1">{label}</a>'

    def bottom_tab(href, icon, label, key):
        on = " on" if key == active_nav else ""
        refresh = ' data-fresh-nav="1"' if href == "/web/portfolio" else ""
        return f'<a href="{href}" class="{on.strip()}"{refresh} data-app-nav="1"><b>{icon}</b>{label}</a>'

    nav = ""
    if nav_active:
        nav = ("<nav class=\"top-nav\">"
               + tab("/web/portfolio", "今日", "portfolio")
               + tab("/web/leaderboard", "排行榜", "leaderboard")
               + tab("/web/positions", "持股", "positions")
               + tab("/web/trades", "紀錄", "trades")
               + tab("/web/workbench", "選股", "screener")
               + tab("/web/compare", "比較", "compare")
               + tab("/web/settings", "設定", "settings")
               + "</nav>")

    more_on = active_nav in {"settings", "trades", "compare", "more"}
    bottom_nav = ("<div class=\"app-bottom-nav\"><div class=\"bottom-inner\">"
                  + bottom_tab("/web/portfolio", "⌂", "今日", "portfolio")
                  + bottom_tab("/web/positions", "▣", "持股", "positions")
                  + bottom_tab("/web/workbench", "⌁", "選股", "screener")
                  + bottom_tab("/web/leaderboard", "≡", "排行", "leaderboard")
                  + f'<a href="/web/more" class="{"on" if more_on else ""}" data-app-nav="1"><b>⋯</b>更多</a>'
                  + "</div></div>")

    # LINE WebView 偶爾不保留 cookie；導覽列也必須帶著有效 token，不能只有內容區帶。
    nav = preserve_web_token(nav)
    bottom_nav = preserve_web_token(bottom_nav)
    body = inject_csrf_inputs(body)
    body = preserve_web_token(body)
    page_back = ""
    if nav_active and nav_active != "portfolio":
        page_back = preserve_web_token(
            '<div class="page-back"><a href="/web/portfolio" data-fresh-nav="1" data-app-nav="1">‹ 回首頁</a></div>')
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
<main id="app-page-content" class="app-page-content">{body}</main>
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

  // 主導覽改為只替換內容 fragment：LINE WebView 不必每次切今日、持股、選股、排行
  // 都重建整張頁面。資料仍由每個既有 route／權杖 decorator 提供，未更改任何計算流程。
  var appContent = document.getElementById('app-page-content');
  var appRouteKeys = {{
    '/web/portfolio':'portfolio','/web/positions':'positions','/web/workbench':'screener',
    '/web/leaderboard':'leaderboard','/web/trades':'trades','/web/compare':'compare',
    '/web/settings':'settings','/web/more':'more','/web/chips':'screener','/web/etf':'more'
  }};
  var appTitles = {{
    '/web/portfolio':'今日','/web/positions':'持股','/web/workbench':'選股工作台',
    '/web/leaderboard':'排行榜','/web/trades':'紀錄','/web/compare':'比較',
    '/web/settings':'設定','/web/more':'更多功能','/web/chips':'籌碼超人','/web/etf':'ETF 專區'
  }};
  function executeFragmentScripts(container) {{
    container.querySelectorAll('script').forEach(function(oldScript) {{
      var replacement = document.createElement('script');
      Array.prototype.slice.call(oldScript.attributes).forEach(function(attr) {{
        replacement.setAttribute(attr.name, attr.value);
      }});
      replacement.text = oldScript.text || oldScript.textContent || '';
      oldScript.parentNode.replaceChild(replacement, oldScript);
    }});
  }}
  function setAppNavState(path) {{
    var key = appRouteKeys[path] || '';
    document.querySelectorAll('[data-app-nav="1"]').forEach(function(link) {{
      var linkPath = new URL(link.href, window.location.href).pathname;
      var linkKey = appRouteKeys[linkPath] || '';
      link.classList.toggle('on', !!key && linkKey === key);
    }});
    var h1 = document.querySelector('.app-header h1');
    if (h1 && appTitles[path]) h1.textContent = appTitles[path];
    if (appTitles[path]) document.title = appTitles[path] + '｜台股 BOT';
  }}
  function switchAppPage(rawHref, pushState) {{
    if (!appContent) {{ window.location.assign(rawHref); return; }}
    var target = new URL(rawHref, window.location.href);
    if (!appRouteKeys[target.pathname]) {{ window.location.assign(target.pathname + target.search); return; }}
    target.searchParams.set('fragment', '1');
    target.searchParams.delete('fast');
    if (target.pathname === '/web/portfolio') target.searchParams.set('_nav', String(Date.now()));
    var requestUrl = target.pathname + '?' + target.searchParams.toString();
    var navNotice = document.createElement('div');
    navNotice.className = 'app-fragment-status';
    navNotice.setAttribute('role', 'status');
    navNotice.innerHTML = '<span class="app-sync-spinner" aria-hidden="true"></span><span><b>正在同步頁面資料</b><small>保留目前內容，完成後會自動更新</small></span>';
    if (appContent.parentNode) appContent.parentNode.insertBefore(navNotice, appContent);
    appContent.setAttribute('aria-busy', 'true');
    appContent.classList.add('app-page-loading');
    fetch(requestUrl, {{credentials:'same-origin', cache:'no-store'}})
      .then(function(response) {{
        if (response.status === 401) {{ window.location.assign(target.pathname + target.search.replace(/([?&])fragment=1&?/, '$1').replace(/[?&]$/, '')); return null; }}
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.text();
      }})
      .then(function(fragment) {{
        if (fragment === null) return;
        if (fragment.indexOf('AUTH_EXPIRED') >= 0) {{ window.location.assign(target.pathname + target.search); return; }}
        document.dispatchEvent(new CustomEvent('stockbot:pageleaving'));
        appContent.innerHTML = fragment;
        executeFragmentScripts(appContent);
        if (navNotice.parentNode) navNotice.parentNode.removeChild(navNotice);
        appContent.removeAttribute('aria-busy');
        appContent.classList.remove('app-page-loading');
        target.searchParams.delete('fragment');
        if (pushState) history.pushState({{stockbotApp:true}}, '', target.pathname + target.search);
        setAppNavState(target.pathname);
        window.scrollTo({{top:0, behavior: prefersReducedMotion() ? 'auto' : 'smooth'}});
      }})
      .catch(function(error) {{
        appContent.removeAttribute('aria-busy');
        appContent.classList.remove('app-page-loading');
        if (navNotice.parentNode) navNotice.parentNode.removeChild(navNotice);
        var retryNotice = document.createElement('div');
        retryNotice.className = 'app-fragment-status app-fragment-error';
        retryNotice.textContent = '頁面暫時無法切換，已保留原內容；請稍後再試。';
        if (appContent.parentNode) {{
          appContent.parentNode.insertBefore(retryNotice, appContent);
          window.setTimeout(function() {{ if (retryNotice.parentNode) retryNotice.parentNode.removeChild(retryNotice); }}, 4200);
        }}
        console.error(error);
      }});
  }}
  window.stockBotSwitchPage = switchAppPage;
  document.addEventListener('click', function(e) {{
    var appLink = e.target.closest ? e.target.closest('a[data-app-nav="1"]') : null;
    if (!appLink || appLink.target === '_blank' || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    switchAppPage(appLink.href, true);
  }}, true);
  window.addEventListener('popstate', function() {{
    if (appRouteKeys[window.location.pathname]) switchAppPage(window.location.href, false);
  }});

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

  // 持股一年損益走勢只在使用者展開該檔時載入，避免主頁為所有圖表先抓歷史行情。
  window.loadPositionTrend = function (details) {{
    if (!details || details.dataset.loaded === '1' || !details.open) return;
    var code = details.getAttribute('data-code') || '';
    var target = details.querySelector('.trend-body');
    if (!code || !target) return;
    details.dataset.loaded = '1';
    details.setAttribute('aria-busy', 'true');
    target.textContent = '正在載入一年損益走勢…';
    var query = new URLSearchParams(window.location.search);
    var token = query.get('t');
    var url = '/web/position-trend?code=' + encodeURIComponent(code) + '&fragment=1';
    if (token) url += '&t=' + encodeURIComponent(token);
    fetch(url, {{ credentials: 'same-origin' }})
      .then(function (r) {{
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      }})
      .then(function (body) {{
        target.innerHTML = body;
        details.removeAttribute('aria-busy');
      }})
      .catch(function (e) {{
        target.textContent = '一年走勢暫時載入失敗，請稍後再試。';
        details.dataset.loaded = '0';
        details.removeAttribute('aria-busy');
        console.error(e);
      }});
  }};

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
  <div class="load-stage">
    <span id="loadstage">正在準備…</span>
  </div>
  {render_quote_block()}
  <div class="load-note">{note}</div>
</div>
<div id="content" class="loading-content" aria-live="polite"><div class="loading-preview"><b>正在讀取最近有效資料…</b><span>先顯示頁面結構，不會重新掃描全市場</span><i></i><i></i><i></i></div></div>
<style>.loading-content{{min-height:190px}}.loading .load-stage{{display:flex;align-items:center;gap:10px}}.loading .load-stage:before{{content:"";width:17px;height:17px;border:2px solid #c9d8e5;border-top-color:#3f6f91;border-radius:50%;animation:app-sync-spin .72s linear infinite;flex:none}}@media(prefers-reduced-motion:reduce){{.loading .load-stage:before{{animation:none;border-top-color:#c9d8e5}}}}.loading-preview{{margin:14px 0;padding:18px 16px;border:1px solid #d7e0ea;border-radius:12px;background:#fff;color:#526b84}}.loading-preview b,.loading-preview span{{display:block}}.loading-preview b{{font-size:15px;color:#1d2939}}.loading-preview span{{margin-top:5px;font-size:12px}}.loading-preview i{{display:block;height:13px;margin-top:13px;border-radius:5px;background:linear-gradient(90deg,#eef3f7 20%,#dfeaf2 45%,#eef3f7 70%);background-size:220% 100%;animation:loading-scan 1.1s linear infinite}}.loading-preview i:nth-of-type(2){{width:82%}}.loading-preview i:nth-of-type(3){{width:64%}}@keyframes loading-scan{{to{{background-position:-120% 0}}}}</style>
{detail_status_html}
<script>
(function () {{
  var stages = [{stages_js}];
  var stageEl = document.getElementById('loadstage');
  var done = false, elapsed = 0, stageIndex = 0;

  // 只輪換真實的處理階段，不顯示沒有後端回報依據的預估百分比。
  var timer = setInterval(function () {{
    if (done) return;
    elapsed += 2.6;
    stageIndex = Math.min(stages.length - 1, stageIndex + 1);
    var label = stages[stageIndex] || '正在處理…';
    if (elapsed > 45) label = '資料量較大，仍在處理中…';
    else if (elapsed > 25) label = stages[stages.length - 1] + '（仍在處理）';
    else if (elapsed > 10) label = '仍在等待資料回應，先保留目前畫面…';
    stageEl.textContent = label;
  }}, 2600);

  function finish(html) {{
    done = true;
    clearInterval(timer);
    setTimeout(function () {{
      var content = document.getElementById('content');
      content.innerHTML = html;
      document.getElementById('loading').style.display = 'none';
      if ({staged_literal}) {{
        var status = document.getElementById('detail-status');
        if (status) status.style.display = 'block';
        var detailParams = new URLSearchParams(window.location.search);
        detailParams.set('fragment', '1');
        detailParams.set('detail', '1');
        // detail 請求不能沿用 fast=1，否則後端會再次回傳預覽殼，
        // 永遠不會進入完整內容／背景完成判斷。
        detailParams.delete('fast');
        var detailUrl = window.location.pathname + '?' + detailParams.toString();
        var detailAttempt = 0;
        function loadDetail() {{
          detailAttempt += 1;
          fetchWithTimeout(detailUrl, 15000)
            .then(function (r) {{
              if (r.status === 401) throw new Error('登入狀態已失效');
              if (!r.ok) throw new Error('HTTP ' + r.status);
              return r.text();
            }})
            .then(function (detailHtml) {{
              content.innerHTML = detailHtml;
              var pending = content.querySelector('[data-screener-pending="1"]');
              if (pending && detailAttempt < 24) {{
                if (status) {{
                  status.style.display = 'block';
                  status.textContent = '即時資料仍在背景整理，頁面會自動再次檢查（第 ' + detailAttempt + ' 次）…';
                }}
                window.setTimeout(loadDetail, 5000);
              }} else if (status) {{
                status.style.display = pending ? 'block' : 'none';
              }}
            }})
            .catch(function (e) {{
              // 首屏快照已經可用；背景結果尚未完成時繼續輪詢，
              // 不把使用者卡在一次逾時或短暫 5xx。
              if (detailAttempt < 24) {{
                if (status) {{
                  status.style.display = 'block';
                  status.textContent = '完整分析暫時未回應，5 秒後自動重試（第 ' + detailAttempt + ' 次）…';
                }}
                window.setTimeout(loadDetail, 5000);
              }} else if (status) {{
                status.style.display = 'block';
                status.textContent = '完整分析仍未完成，請稍後重新整理查看最新狀態。';
              }}
              console.error(e);
            }});
        }}
        loadDetail();
      }}
    }}, 70);
  }}

  function fetchWithTimeout(url, timeoutMs) {{
    return Promise.race([
      fetch(url, {{ credentials: 'same-origin', cache: 'no-store' }}),
      new Promise(function (_, reject) {{
        window.setTimeout(function () {{ reject(new Error('載入逾時，請重新整理查看最新狀態')); }}, timeoutMs);
      }})
    ]);
  }}
  var fragmentParams = new URLSearchParams(window.location.search);
  fragmentParams.set('fragment', '1');
  if ({staged_literal}) fragmentParams.set('fast', '1');
  var url = window.location.pathname + '?' + fragmentParams.toString();

  fetchWithTimeout(url, 30000)
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
    try:
        data = build_today_change_web_data(uid)
    except Exception as exc:
        # 完整變化頁不能因單一使用者快照、舊資料庫欄位或外部資料
        # 暫時失敗而回 500；保留可讀狀態頁，並把真正例外留在 Render Logs。
        print(f"❌ 盤前完整變化頁載入失敗（uid={uid}）：{exc}")
        requested_date = taiwan_today()
        data = {
            "date": requested_date.isoformat(),
            "requested_date": requested_date.isoformat(),
            "is_weekend": requested_date.weekday() >= 5,
            "snapshot": None,
            "events": [],
            "state": {
                "title": "盤前資料暫時無法載入",
                "detail": "完整資料整理遇到暫時性問題，請稍後重新整理；目前不顯示推測訊號。",
            },
            "load_error": True,
        }
    if not isinstance(data, dict):
        print(f"❌ 盤前完整變化頁資料型別異常（uid={uid}）：{type(data).__name__}")
        requested_date = taiwan_today()
        data = {
            "date": requested_date.isoformat(),
            "requested_date": requested_date.isoformat(),
            "is_weekend": requested_date.weekday() >= 5,
            "snapshot": None,
            "events": [],
            "state": {"title": "盤前資料暫時無法載入",
                      "detail": "完整資料格式暫時無法辨識，請稍後重新整理；目前不顯示推測訊號。"},
            "load_error": True,
        }
    snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else None
    if snapshot:
        snapshot = dict(snapshot)
        snapshot["blackhorse"] = _premarket_record_list(snapshot.get("blackhorse"))
        snapshot["radar"] = _premarket_record_list(snapshot.get("radar"))
        snapshot["market"] = _premarket_json_value(snapshot.get("market"), "dict")
        snapshot["news"] = _premarket_record_list(snapshot.get("news"))
        snapshot["institutional"] = _premarket_record_map(snapshot.get("institutional"))
    state = data.get("state") if isinstance(data.get("state"), dict) else {}
    event_source = data.get("events")
    events = [event for event in event_source
              if isinstance(event, dict)] if isinstance(event_source, (list, tuple)) else []
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

    all_rows = []
    market_cards = []
    market_items = []
    for event in events:
        severity = str(event.get("severity") or "C").strip().upper()
        severity = severity if severity in CHANGE_LEVEL else "C"
        severity_label = LEVEL_LABEL.get(severity, "一般變化")
        category = category_labels.get(event.get("category"), event.get("category") or "其他")
        evidence_text = _format_premarket_event_evidence(event)
        all_rows.append(
            f'<article class="premarket-event-card severity-{severity}">'
            f'<div class="premarket-event-head">'
            f'<span class="premarket-event-severity">{text(severity)}・{esc(severity_label)}</span>'
            f'<span class="premarket-event-category">{text(category)}</span></div>'
            f'<h3>{text(event.get("title"))}</h3>'
            f'<p>{text(event.get("detail"))}</p>'
            f'<details><summary>查看比較依據</summary>'
            f'<div class="premarket-evidence">{text(evidence_text)}</div></details>'
            f'</article>'
        )
    if not all_rows:
        empty_events = ("目前尚未建立盤前事件資料。" if not snapshot else
                        (state.get("title") or "今日沒有符合條件的新事件。"))
        all_rows.append(f'<div class="premarket-events-empty">{text(empty_events)}</div>')
    rows = all_rows[:3]
    extra_rows = all_rows[3:]
    event_list_html = ''.join(rows)
    if extra_rows:
        event_list_html += (
            f'<details class="premarket-more-events">'
            f'<summary>查看其餘 {len(extra_rows)} 個變化</summary>'
            f'<div class="premarket-event-list premarket-event-list-extra">'
            f'{"".join(extra_rows)}</div></details>'
        )

    display_date = data.get("date")
    requested_date = data.get("requested_date")
    state_title = state.get("title") or ("盤前資料已載入" if snapshot else "盤前資料尚未建立")
    state_detail = state.get("detail") or ("以下內容來自最近可用的真實盤前快照。" if snapshot else
                                             "完成盤後資料更新與變化偵測後，這裡會顯示完整內容。")
    if data.get("is_weekend") and snapshot:
        state_detail = f"目前是週末，以下顯示最近可用的盤前批次；{state_detail}"
    status_color = "#9A3A30" if data.get("load_error") else ("#6E5228" if snapshot else "#767D85")

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
        market_definitions = [
            ("taiex", "台股大盤", "TAIEX", "taiex_close", "taiex_diff", "taiex_pct", "收盤"),
            ("taiex_night", "台指期夜盤", "TX 近月", "taiex_night_close", "taiex_night_diff", "taiex_night_pct", "盤後"),
            ("^DJI", "道瓊", "^DJI", "^DJI_close", "^DJI_diff", "^DJI_pct", "收盤"),
            ("^IXIC", "那斯達克", "^IXIC", "^IXIC_close", "^IXIC_diff", "^IXIC_pct", "收盤"),
            ("^GSPC", "S&P 500", "^GSPC", "^GSPC_close", "^GSPC_diff", "^GSPC_pct", "收盤"),
            ("^SOX", "費城半導體", "^SOX", "^SOX_close", "^SOX_diff", "^SOX_pct", "收盤"),
            ("MU", "美光 MU", "MU", "MU_close", "MU_diff", "MU_pct", "收盤"),
            ("LITE", "Lumentum LITE", "LITE", "LITE_close", "LITE_diff", "LITE_pct", "收盤"),
        ]
        market_data = snapshot.get("market") or {}
        night_date_text = market_data.get("taiex_night_date")
        night_fresh = False
        if night_date_text:
            try:
                night_date = date.fromisoformat(str(night_date_text).replace("/", "-")[:10])
                today = taiwan_today()
                night_fresh = night_date <= today and (today - night_date).days <= TAIFEX_NIGHT_MAX_AGE_DAYS
            except (TypeError, ValueError):
                night_fresh = False
        for _key, label, symbol, close_key, diff_key, pct_key, period_label in market_definitions:
            close = market_data.get(close_key)
            diff = market_data.get(diff_key)
            pct = market_data.get(pct_key)
            if _key == "taiex_night" and not night_fresh:
                close = diff = pct = None
            # 夜盤是使用者指定的固定市場欄位；即使官方資料暫時未更新，
            # 也要保留卡片並明確顯示資料不足，不能讓使用者誤以為功能消失。
            if close is None and diff is None and pct is None and _key != "taiex_night":
                continue
            try:
                close_text = f"{float(close):,.2f}" if close is not None else "收盤點位尚無資料"
            except (TypeError, ValueError):
                close_text = esc(str(close)) if close not in (None, "") else "收盤點位尚無資料"
            if diff is not None:
                try:
                    diff_num = float(diff)
                    diff_color = "#B52F2F" if diff_num > 0 else ("#087A4B" if diff_num < 0 else "#767D85")
                    diff_text = f'<span style="color:{diff_color};font-weight:700">{diff_num:+,.2f}</span>'
                except (TypeError, ValueError):
                    diff_text = esc(str(diff))
            else:
                diff_text = "—"
            pct_html = pct_text(pct) if pct is not None else '<span style="color:#767D85">尚無漲跌幅</span>'
            movement = f'{diff_text}　{pct_html}'
            display_symbol = symbol
            display_period_label = period_label
            if _key == "taiex_night":
                if night_fresh:
                    display_symbol = str(market_data.get("taiex_night_contract") or "TX 近月")
                    display_period_label = f"盤後資料日 {night_date_text}"
                else:
                    display_symbol = "TX 近月"
                    display_period_label = "官方資料暫缺"
            market_cards.append(
                f'<div class="premarket-market-quote"><div class="premarket-market-label">'
                f'<b>{esc(label)}</b><small>{esc(display_symbol)}　{esc(display_period_label)}</small></div>'
                f'<div class="premarket-market-value"><strong>{close_text}</strong>'
                f'<span>{movement}</span></div></div>')
            market_items.append(
                f'<div class="premarket-metric"><span>{esc(label)}<small>（{esc(display_symbol)} {esc(display_period_label)}）</small></span>'
                f'<b>{close_text}<br>{movement}</b></div>')
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
                suffix.append(              f"法人合計 {net:+,} 張")

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
            section("📰 相關新聞", "".join(news_items), "目前快照沒有新聞資料") +
            section("🏦 法人資料", "".join(inst_items), "目前快照沒有法人資料")
        )
        raw_snapshot = ("<details class=\"premarket-raw\"><summary>查看原始快照資料</summary>"
                        f"<pre>{esc(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))}</pre></details>")
    else:
        market_cards = []
        market_items = []
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
      .premarket-event-list {{ display:grid; gap:12px; margin-top:12px; }}
      .premarket-event-card {{ padding:14px 15px; border:1px solid #E4E4DE; border-left:4px solid #9A9A91; border-radius:12px; background:#FFF; overflow-wrap:anywhere; }}
      .premarket-event-card.severity-S {{ border-left-color:#9A3A30; background:#FFF9F7; }}
      .premarket-event-card.severity-A {{ border-left-color:#A6782C; background:#FFFCF5; }}
      .premarket-event-card.severity-B {{ border-left-color:#58735F; }}
      .premarket-event-card.severity-C {{ border-left-color:#9A9A91; }}
      .premarket-event-head {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:7px; flex-wrap:wrap; }}
      .premarket-event-severity {{ font-weight:800; color:#6E5228; }}
      .premarket-event-category {{ color:#767D85; font-size:.9rem; }}
      .premarket-event-card h3 {{ margin:0; font-size:1.05rem; line-height:1.45; color:#252A2F; }}
      .premarket-event-card p {{ margin:7px 0 0; color:#545B61; line-height:1.65; }}
      .premarket-event-card details {{ margin-top:10px; border-top:1px solid #ECEBE6; padding-top:8px; }}
      .premarket-event-card summary {{ color:#6E5228; cursor:pointer; font-weight:700; }}
      .premarket-evidence {{ margin-top:8px; padding:9px 10px; border-radius:8px; background:#F7F7F3; color:#4F565C; line-height:1.65; white-space:pre-line; }}
      .premarket-events-empty {{ padding:15px; color:#767D85; line-height:1.7; background:#F7F7F3; border-radius:10px; }}
      .premarket-more-events {{ margin-top:12px; border-top:1px solid #E6E4DE; padding-top:10px; }}
      .premarket-more-events > summary {{ color:#6E5228; cursor:pointer; font-weight:800; padding:5px 0; }}
      .premarket-event-list-extra {{ margin-top:10px; }}
      .premarket-metric small {{ color:#8A8F94; font-weight:400; font-size:.78em; }}
      .premarket-row small {{ color:#8A8F94; font-weight:400; }}
      .premarket-market-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px; margin-top:10px; }}
      .premarket-market-quote {{ display:flex; flex-direction:column; justify-content:space-between; gap:8px; padding:12px 11px; background:#FAFAF7; border:1px solid #E8E6DF; border-radius:10px; min-width:0; }}
      .premarket-market-label b {{ display:block; color:#30353A; font-size:.98rem; }}
      .premarket-market-label small {{ display:block; color:#8A8F94; font-size:.75rem; margin-top:2px; }}
      .premarket-market-value strong {{ display:block; color:#20252A; font-size:1.15rem; letter-spacing:.01em; white-space:nowrap; }}
      .premarket-market-value span {{ display:block; margin-top:4px; font-size:.82rem; white-space:nowrap; }}
      .premarket-market-empty {{ padding:12px 0; color:#767D85; line-height:1.7; }}
      @media(max-width:420px) {{ .premarket-market-grid {{ grid-template-columns:1fr; }} }}
      .premarket-news {{ padding:8px 0; border-top:1px solid #ECEBE6; line-height:1.6; }}
      .premarket-news:first-of-type {{ border-top:0; }}
      .premarket-news a {{ color:#4A5F7A; text-decoration:underline; }}
      .premarket-empty, .premarket-empty-state {{ color:#767D85; line-height:1.7; }}
      .premarket-market-note {{ color:#767D85; font-size:.84rem; line-height:1.6; margin:4px 0 0; }}
      .premarket-empty-state {{ padding:18px; background:#F7F7F3; border-radius:11px; }}
      .premarket-empty-state b {{ color:#6E5228; font-size:1.05rem; }}
      .premarket-empty-state p {{ margin:.55rem 0 0; }}
      .premarket-raw {{ margin-top:14px; }}
    </style>
    <section class="card premarket-market-card">
      <h2>📈 大盤／美股收盤</h2>
      <p class="premarket-market-note">先看最近可取得的真實收盤數字；紅色代表上漲，綠色代表下跌。</p>
      <div class="premarket-market-grid">{"".join(market_cards) if market_cards else '<div class="premarket-market-empty">目前沒有可顯示的大盤或美股收盤資料。</div>'}</div>
    </section>
    <section class="card">
      <h1>🔥 今日值得注意</h1>
      {meta}
      <div class="premarket-status"><b>{text(state_title)}</b><p>{text(state_detail)}</p></div>
      <div class="premarket-event-list">{event_list_html}</div>
    </section>
    <section class="card"><h2>其他盤前快照</h2>
      {snapshot_sections}
      {raw_snapshot}
    </section>
    """
    try:
        return render_page("盤前變化", body, nav_active="premarket")
    except Exception as exc:
        # route 的資料 fallback 之後仍可能遇到極舊快照或模板資料型別；
        # 不讓使用者看到 Flask 原生 500，並保留不含 token／資料內容的錯誤類型供 Logs 定位。
        print(f"❌ 盤前完整變化頁渲染失敗（uid={uid}，{type(exc).__name__}）：{exc}")
        if request.args.get("fragment") == "1":
            return make_response("PREMARKET_TEMPORARILY_UNAVAILABLE", 503,
                                 {"X-StockBot-Error": "premarket-render"})
        safe_body = ('<section class="card"><h1>盤前資料暫時無法顯示</h1>'
                     '<p>頁面整理遇到暫時性問題，資料沒有被推測或補造；請稍後重新整理。</p>'
                     '</section>')
        try:
            return render_page("盤前變化", safe_body, nav_active="premarket"), 503
        except Exception as fallback_exc:
            print(f"❌ 盤前安全頁渲染失敗（{type(fallback_exc).__name__}）：{fallback_exc}")
            return make_response(
                '<!doctype html><meta charset="utf-8"><title>盤前資料暫時無法顯示</title>'
                "<main><h1>盤前資料暫時無法顯示</h1>"
                "<p>請稍後重新整理；系統沒有自行補造市場資料。</p></main>", 503)



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
    positions_page_started = time.monotonic()
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
                note = (request.form.get("position_note") or "").strip()[:200]
                ok = add_position(uid, code, shares, cost,
                                  request.form.get("bought_on") or None,
                                  note=note or None)
                if not ok:
                    msg = "新增失敗，資料沒有成功寫入，請稍後再試。"
                else:
                    msg = (f"已新增 {code}（含手續費，每股成本 {cost:,.2f}）。"
                           if bf > 0 else f"已新增 {code}。")

    positions_data_started = time.monotonic()
    positions = merge_positions(get_positions(uid))
    positions_data_done = time.monotonic()
    inst_started = time.monotonic()
    inst = fetch_institutional_data() or {}
    inst_done = time.monotonic()

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
    # 主頁先抓 1d 即時資料；較重的一年損益走勢在使用者展開個別明細時才抓取，
    # 所以不會為尚未查看的圖表付出外部請求成本。
    quote_started = time.monotonic()
    price_map = get_realtime_stocks_bulk(
        [p["code"] for p in positions], rng="1d")
    quote_done = time.monotonic()
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
    <details class="disclosure trend" style="margin-top:2px"
             data-code="{html.escape(str(p['code']), quote=True)}"
             ontoggle="window.loadPositionTrend(this)">
      <summary>損益走勢</summary>
      <div class="trend-body"><div class="sub">展開後載入一年損益走勢…</div></div>
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

    # 使用者目前所在的「持股」頁也要直接看得到兩張圖；不能只把圖放在
    # 另一個完整分析 route。配置圖只用有有效現價的市值，走勢圖沿用
    # get_portfolio_snapshots/render_trend_chart 的既有組合對加權指數口徑。
    chart_holdings = []
    for p, price, value, _cost_total in enriched:
        if not price or not value or not math.isfinite(float(value)):
            continue
        chart_holdings.append({
            "code": p["code"],
            "name": stock_display_name(p["code"], inst, price.get("name")),
            "value": value,
        })
    allocation_html_positions = render_portfolio_allocation_chart(chart_holdings)
    try:
        position_snapshots = get_portfolio_snapshots(uid, days=120)
        position_trend_html = render_trend_chart(position_snapshots)
    except Exception as exc:
        print(f"⚠️ 持股頁組合走勢載入失敗: {exc}")
        position_trend_html = '<div class="empty">組合走勢暫時無法載入，請稍後再試。</div>'
    portfolio_trend_html = f"""
<section class="portfolio-chart-card portfolio-trend-card" id="portfolio-trend">
  <div class="section-head"><h2>組合 vs 加權指數</h2>
    <span class="section-note">相對起始日漲跌幅</span></div>
  <p class="portfolio-chart-note">同一張圖比較你的組合與台灣加權指數；若快照不足兩天，會明確顯示資料累積狀態。</p>
  <div class="portfolio-trend-body">{position_trend_html}</div>
</section>"""

    body = f"""
{f'<div class="msg">{msg}</div>' if msg else ''}
{totals}
{allocation_html_positions}
{portfolio_trend_html}
<div class="section-head"><h2>持股明細</h2>
  <span class="section-note">依市值排序　·　<a href="/web/trades" style="color:var(--brass)">操作日報 →</a></span></div>
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
    <div><label>操作備註（可略）</label>
      <input name="position_note" maxlength="200" placeholder="例如：調整權重"></div>
  </div>
  <button type="submit">新增並記錄加碼</button>
  <div class="sell-hint">
    直接抄券商庫存頁的「成本價」就好，那個數字已含買進手續費，手續費欄留空即可。<br>
    新增後會同步記入操作日報；備註只保存你自己輸入的內容。若填的是純成交價，在手續費欄填實際金額，會自動攤進每股成本。
  </div>
</form>"""
    print("⏱️ 持股頁：持股資料 %.0fms、法人 %.0fms、1y行情 %.0fms、HTML %.0fms、合計 %.0fms" % (
        (positions_data_done - positions_data_started) * 1000,
        (inst_done - inst_started) * 1000,
        (quote_done - quote_started) * 1000,
        (time.monotonic() - quote_done) * 1000,
        (time.monotonic() - positions_page_started) * 1000))
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


ETF_CATALOG_URL = "https://www.twse.com.tw/rwd/zh/ETF/list"
ETF_CATALOG_CACHE_SECONDS = 86400
_etf_catalog_cache = {"at": 0, "data": None}

# TWSE ETF 投資篩選器提供逐檔官方資產規模與市場欄位；
# 只在快照過期時抓一次，不讓每個 ETF 卡片各自打官方端點。
ETF_PRODUCT_METRICS_URL = "https://www.twse.com.tw/zh/ETFortune/ajaxProductsResult"
ETF_PRODUCT_METRICS_CACHE_SECONDS = 86400
ETF_PRODUCT_METRICS_SHARED_MAX_AGE = 3 * 86400
ETF_PRODUCT_METRICS_SNAPSHOT_KEY = "etf_product_metrics"
_etf_product_metrics_cache = {"at": 0, "data": None}


def _split_twse_etf_cell(value):
    """拆開證交所 ETF 清單中的 <br> 多商品儲存格，避免多幣別代號錯配。"""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    pieces = []
    for chunk in re.split(r"[\r\n]+", text):
        chunk = re.sub(r"\s+", " ", chunk).strip()
        if chunk:
            pieces.append(chunk)
    return pieces


def _parse_twse_etf_date(value):
    """將 TWSE 上市日期（YYYY.MM.DD／YYYY-MM-DD）轉成 ISO 日期。"""
    match = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", str(value or ""))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def _etf_catalog_classification(code, name, benchmark, override=None):
    """用透明規則作第一版策略分組；這不是基金公司或證交所的策略認證。"""
    override = override or {}
    verified_category = override.get("category")
    if verified_category in ("主動式", "高股息", "市值型", "主題型"):
        return verified_category, "已核實 ETF metadata 覆寫"

    text = f"{name or ''} {benchmark or ''}"
    # 「主動式」是管理方式，不代表投資標的是股票；債券／固定收益
    # 必須先排到其他，否則 00982D／00983D／00984D 會錯進主動式股票榜。
    fixed_income_terms = (
        "債", "債券", "公司債", "公債", "非投", "高收益債", "投資級債",
        "固定收益", "信用債", "利率債", "收益債"
    )
    blocked_terms = (
        "槓桿", "反向", "正2", "正二", "反1", "反二", "期貨",
        "多資產", "多重資產", "平衡", "貨幣", "原物料", "黃金", "白銀", "石油"
    )
    if any(term in text for term in fixed_income_terms + blocked_terms):
        return "其他", "名稱／標的關鍵字：非第一版四大股票策略"
    if "主動" in text or str(code).upper().endswith("A"):
        return "主動式", "官方名稱／代號尾碼 A 規則候選"
    if any(term in text for term in ("高股息", "高息", "優息", "股利", "收益")):
        return "高股息", "名稱／標的關鍵字規則候選；不代表配息政策"
    if any(term in text for term in ("50", "大型", "龍頭", "市值", "加權", "TOP")):
        return "市值型", "名稱／標的關鍵字規則候選"
    return "主題型", "其餘一般股票型／產業主題規則候選"


def _fallback_etf_catalog():
    """官方清單暫時不可用時，只回傳已核實覆寫，且不把它宣稱成完整商品池。"""
    fallback = {}
    for code, override in ETF_PRODUCT_METADATA.items():
        item = dict(override)
        item.setdefault("source", "已核實 ETF metadata（官方清單暫時不可用）")
        item.setdefault("classification_basis", "已核實 ETF metadata 覆寫")
        fallback[code] = item
    return fallback


def fetch_twse_etf_catalog(force_reload=False):
    """取得 TWSE 官方上市 ETF 清單，按上市日過濾並跨 worker 快取。"""
    now = time.time()
    with _realtime_cache_lock:
        cached = _etf_catalog_cache.get("data")
        if (cached and not force_reload and
                now - _etf_catalog_cache.get("at", 0) < ETF_CATALOG_CACHE_SECONDS):
            return cached

    if not force_reload:
        try:
            shared = _load_shared_data_snapshot(
                "etf_catalog", max_age_seconds=ETF_CATALOG_CACHE_SECONDS)
            payload = (shared.get("payload") if shared else None) or {}
            if isinstance(payload, dict) and payload:
                with _realtime_cache_lock:
                    _etf_catalog_cache.update({"at": now, "data": payload})
                return payload
        except Exception as exc:
            print(f"⚠️ 讀取 ETF 清單共享快照失敗: {exc}")

    try:
        response = requests.get(
            ETF_CATALOG_URL, timeout=15,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        response.raise_for_status()
        raw = response.json()
        fields = raw.get("fields") if isinstance(raw, dict) else None
        rows = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(fields, list) or not isinstance(rows, list):
            raise ValueError("TWSE ETF 清單格式不完整")
        field_index = {str(field).strip(): index for index, field in enumerate(fields)}
        required = ("上市日期", "證券代號", "證券簡稱", "發行人", "標的指數")
        if any(field not in field_index for field in required):
            raise ValueError("TWSE ETF 清單缺少必要欄位")

        today = taiwan_today()
        catalog = {}
        for row in rows:
            if not isinstance(row, list):
                continue
            def cell(field):
                index = field_index[field]
                return row[index] if index < len(row) else ""

            code_parts = _split_twse_etf_cell(cell("證券代號"))
            date_parts = _split_twse_etf_cell(cell("上市日期"))
            name_parts = _split_twse_etf_cell(cell("證券簡稱"))
            issuer = re.sub(r"\s+", " ", html.unescape(str(cell("發行人") or ""))).strip()
            benchmark = re.sub(r"\s+", " ", html.unescape(str(cell("標的指數") or ""))).strip()
            if not code_parts:
                continue
            for index, raw_code in enumerate(code_parts):
                code_match = re.search(r"(?<!\d)(\d{4,6}[A-Za-z]?)(?!\d)", raw_code)
                if not code_match:
                    continue
                code = code_match.group(1).upper()
                if not code.startswith("00"):
                    continue
                listing_date = _parse_twse_etf_date(
                    date_parts[index] if index < len(date_parts) else (date_parts[0] if date_parts else ""))
                if not listing_date:
                    continue
                try:
                    if date.fromisoformat(listing_date) > today:
                        continue
                except ValueError:
                    continue
                name = (name_parts[index] if index < len(name_parts)
                        else (name_parts[0] if name_parts else code))
                category, basis = _etf_catalog_classification(
                    code, name, benchmark, ETF_PRODUCT_METADATA.get(code))
                catalog[code] = {
                    "name": name,
                    "listing_date": listing_date,
                    "issuer": issuer or "待確認",
                    "benchmark": benchmark or "待確認",
                    "category": category,
                    "asset_class": ("固定收益／債券" if any(term in f"{name} {benchmark}" for term in (
                                        "債", "債券", "非投", "固定收益", "收益債"))
                                    else "股票／股權" if category in ("主動式", "高股息", "市值型", "主題型")
                                    else "其他資產／待確認"),
                    "classification_basis": basis,
                    "distribution_policy": "unknown",
                    "source": "TWSE ETF 上市清單",
                    "source_url": ETF_CATALOG_URL,
                    "catalog_retrieved_date": today.isoformat(),
                }
        if not catalog:
            raise ValueError("TWSE ETF 清單沒有可用商品")

        with _realtime_cache_lock:
            _etf_catalog_cache.update({"at": time.time(), "data": catalog})
        _save_shared_data_snapshot(
            "etf_catalog", catalog, data_date=today,
            source_meta={"source": "TWSE ETF 上市清單", "count": len(catalog),
                         "url": ETF_CATALOG_URL})
        return catalog
    except Exception as exc:
        print(f"⚠️ 抓取 TWSE ETF 上市清單失敗: {exc}")
        fallback = _fallback_etf_catalog()
        with _realtime_cache_lock:
            _etf_catalog_cache.update({"at": time.time(), "data": fallback})
        return fallback


def _parse_etf_metric_number(value, integer=False):
    """解析 TWSE 商品指標；破折號、空白與非數字維持 None，不轉成 0。"""
    text = html.unescape(str(value or "")).replace(",", "").strip()
    if text in ("", "-", "—", "N/A", "NA"):
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if integer else number


def fetch_twse_etf_product_metrics(force_reload=False):
    """取得 TWSE ETF 篩選器的逐檔官方市場欄位並跨 worker 快取。"""
    now = time.time()
    with _realtime_cache_lock:
        cached = _etf_product_metrics_cache.get("data")
        if (isinstance(cached, dict) and not force_reload and
                now - _etf_product_metrics_cache.get("at", 0) < ETF_PRODUCT_METRICS_CACHE_SECONDS):
            return cached

    if not force_reload:
        try:
            shared = _load_shared_data_snapshot(
                ETF_PRODUCT_METRICS_SNAPSHOT_KEY,
                max_age_seconds=ETF_PRODUCT_METRICS_SHARED_MAX_AGE)
            payload = (shared.get("payload") if shared else None) or {}
            if isinstance(payload, dict) and payload:
                with _realtime_cache_lock:
                    _etf_product_metrics_cache.update({"at": now, "data": payload})
                return payload
        except Exception as exc:
            print(f"⚠️ 讀取 ETF 官方指標共享快照失敗: {exc}")

    try:
        response = requests.post(
            ETF_PRODUCT_METRICS_URL, data={}, timeout=8,
            headers={"User-Agent": "Mozilla/5.0",
                     "X-Requested-With": "XMLHttpRequest",
                     "Accept-Language": "zh-TW,zh;q=0.9"})
        response.raise_for_status()
        raw = response.json()
        rows = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(rows, list) or raw.get("status") != "success":
            raise ValueError("TWSE ETF 商品指標格式不完整")
        retrieved_date = taiwan_today().isoformat()
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            code_match = re.search(r"(?<!\d)(\d{4,6}[A-Za-z]?)(?!\d)",
                                   str(row.get("stockNo") or ""))
            if not code_match:
                continue
            code = code_match.group(1).upper()
            if not code.startswith("00"):
                continue
            result[code] = {
                "asset_size_billion": _parse_etf_metric_number(row.get("totalAv")),
                "official_close": _parse_etf_metric_number(row.get("close1")),
                "ytd_avg_turnover_million": _parse_etf_metric_number(row.get("valueYTD")),
                "ytd_volume_shares": _parse_etf_metric_number(row.get("volumeYTD"), integer=True),
                "holders": _parse_etf_metric_number(
                    str(row.get("holders") or "").replace(",", ""), integer=True),
                "retrieved_date": retrieved_date,
                "source": "TWSE ETF 投資篩選器",
                "source_url": "https://www.twse.com.tw/zh/ETFortune/products",
            }
        if not result:
            raise ValueError("TWSE ETF 商品指標沒有可用資料")
        with _realtime_cache_lock:
            _etf_product_metrics_cache.update({"at": time.time(), "data": result})
        _save_shared_data_snapshot(
            ETF_PRODUCT_METRICS_SNAPSHOT_KEY, result, data_date=taiwan_today(),
            source_meta={"source": "TWSE ETF 投資篩選器",
                         "url": "https://www.twse.com.tw/zh/ETFortune/products",
                         "count": len(result),
                         "data_date_basis": "官方動態篩選 endpoint 回應日／擷取日"})
        return result
    except Exception as exc:
        print(f"⚠️ 抓取 TWSE ETF 官方商品指標失敗: {exc}")
        with _realtime_cache_lock:
            cached = _etf_product_metrics_cache.get("data")
        return cached if isinstance(cached, dict) else {}


def get_etf_metadata(code, distribution_map=None):
    """合併官方上市清單、核實覆寫與官方已發生配息紀錄。"""
    code = str(code).strip().upper()
    catalog = fetch_twse_etf_catalog()
    meta = dict(catalog.get(code) or {})
    override = dict(ETF_PRODUCT_METADATA.get(code) or {})
    if override:
        meta.update(override)
        meta["classification_basis"] = "已核實 ETF metadata 覆寫"
    meta.setdefault("name", STOCK_NAME_MAP.get(code, code))
    meta.setdefault("category", "待分類")
    meta.setdefault("management_style", "待確認")
    meta.setdefault("distribution_policy", "unknown")
    if meta.get("category") == "主動式" and not override.get("management_style"):
        meta["management_style"] = "主動式"
    # 既有共享 catalog 可能是在固定收益排除規則加入前建立；
    # 每次合併 metadata 時再校正一次，避免舊快取讓債券 ETF 留在主動式股票榜。
    if not override:
        corrected_category, corrected_basis = _etf_catalog_classification(
            code, meta.get("name"), meta.get("benchmark"), {})
        if corrected_category != meta.get("category"):
            meta["category"] = corrected_category
            meta["classification_basis"] = corrected_basis
    if meta.get("category") in ("主動式", "高股息", "市值型", "主題型"):
        meta["asset_class"] = "股票／股權"
    elif any(term in f'{meta.get("name") or ""} {meta.get("benchmark") or ""}'
             for term in ("債", "債券", "非投", "固定收益", "收益債")):
        meta["asset_class"] = "固定收益／債券"
    else:
        meta.setdefault("asset_class", "其他資產／待確認")

    # 官方清單沒有配息金額；以證交所配息頁已發生且金額有效的公告補入。
    # 沒有紀錄不代表不配息，只有明確核實的 override 才能標示不分配。
    if distribution_map is None:
        distribution_map = fetch_twse_etf_distribution_history()
    distribution = (distribution_map or {}).get(code) or {}
    records = distribution.get("records") if isinstance(distribution, dict) else None
    if isinstance(records, list) and records:
        meta["distribution_records"] = records
        meta["distribution_record_count"] = len(records)
        meta["latest_distribution_amount"] = distribution.get("latest_amount")
        meta["latest_distribution_ex_date"] = distribution.get("latest_ex_date")
        meta["distribution_source"] = distribution.get("source") or "TWSE ETF 配息清單"
        meta["distribution_source_url"] = distribution.get("source_url") or ETF_DISTRIBUTION_URL
        if meta.get("distribution_policy") == "unknown":
            meta["distribution_policy"] = "distributing"
    return meta


def get_etf_catalog_products():
    """回傳網頁 ETF 專區使用的完整官方商品池，逐檔套用核實覆寫。"""
    catalog = fetch_twse_etf_catalog()
    distribution_map = fetch_twse_etf_distribution_history()
    metrics_map = fetch_twse_etf_product_metrics()
    products = {}
    for code in catalog.keys():
        meta = get_etf_metadata(code, distribution_map=distribution_map)
        official_metrics = metrics_map.get(code) if isinstance(metrics_map, dict) else None
        if isinstance(official_metrics, dict):
            meta["official_metrics"] = official_metrics
        products[code] = meta
    return products


# 證交所 ETF 配息清單是官方公告資料；只採已發生且有每單位金額的紀錄。
# 未查到紀錄不等於不配息，仍保留 unknown，避免把資料缺口當成 0%。
ETF_DISTRIBUTION_URL = "https://www.twse.com.tw/zh/ETFortune/dividendList"
ETF_DISTRIBUTION_CACHE_SECONDS = 86400
ETF_DISTRIBUTION_SHARED_MAX_AGE = 3 * 86400
ETF_DISTRIBUTION_SNAPSHOT_KEY = "etf_distribution_history"
_etf_distribution_cache = {"at": 0, "data": None}


def _parse_twse_roc_date(value):
    """解析證交所民國日期；也接受西元日期，並拒絕不存在日期。"""
    text = html.unescape(str(value or "")).replace(" ", "")
    match = re.search(r"(?:民國)?(\d{2,3})年(\d{1,2})月(\d{1,2})日", text)
    if match:
        try:
            return date(int(match.group(1)) + 1911, int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return _parse_twse_etf_date(text)


def _clean_twse_html_cell(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_twse_etf_distribution_history(force_reload=False):
    """批次抓證交所官方 ETF 配息清單，按代號保存已發生的現金配息紀錄。"""
    now = time.time()
    with _realtime_cache_lock:
        cached = _etf_distribution_cache.get("data")
        if (cached is not None and not force_reload and
                now - _etf_distribution_cache.get("at", 0) < ETF_DISTRIBUTION_CACHE_SECONDS):
            return cached

    if not force_reload:
        try:
            shared = _load_shared_data_snapshot(
                ETF_DISTRIBUTION_SNAPSHOT_KEY,
                max_age_seconds=ETF_DISTRIBUTION_SHARED_MAX_AGE)
            payload = (shared.get("payload") if shared else None) or {}
            if isinstance(payload, dict) and payload:
                with _realtime_cache_lock:
                    _etf_distribution_cache.update({"at": now, "data": payload})
                return payload
        except Exception as exc:
            print(f"⚠️ 讀取 ETF 配息共享快照失敗: {exc}")

    today = taiwan_today()
    start_year = max(2010, today.year - 3)
    params = {"startDate": str(start_year), "endDate": str(today.year)}
    try:
        response = requests.get(
            ETF_DISTRIBUTION_URL, params=params, timeout=6,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-TW,zh;q=0.9"})
        response.raise_for_status()
        # TWSE 的 onclick 屬性通常是雙引號包住、內部 document.location
        # 再用單引號包 URL；不能用「排除單引號」的舊 regex，否則整張表會零筆。
        row_html_list = re.findall(
            r"<tr\b[^>]*etfInfo/[^>]*>(.*?)</tr>",
            response.text, flags=re.IGNORECASE | re.DOTALL)
        result = {}
        for row_html in row_html_list:
            cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row_html,
                               flags=re.IGNORECASE | re.DOTALL)
            if len(cells) < 6:
                continue
            code_match = re.search(r"(?<!\d)(\d{4,6}[A-Za-z]?)(?!\d)",
                                   _clean_twse_html_cell(cells[0]))
            if not code_match:
                continue
            code = code_match.group(1).upper()
            if not code.startswith("00"):
                continue
            ex_date = _parse_twse_roc_date(_clean_twse_html_cell(cells[2]))
            if not ex_date or ex_date > today:
                continue
            amount_text = _clean_twse_html_cell(cells[5]).replace(",", "")
            try:
                amount = float(amount_text)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            payment_date = _parse_twse_roc_date(_clean_twse_html_cell(cells[4]))
            base_date = _parse_twse_roc_date(_clean_twse_html_cell(cells[3]))
            record = {
                "ex_date": ex_date.isoformat(),
                "base_date": base_date.isoformat() if base_date else None,
                "payment_date": payment_date.isoformat() if payment_date else None,
                "amount": round(amount, 6),
                "source": "TWSE ETF 配息清單",
                "source_url": (f"{ETF_DISTRIBUTION_URL}?stkNo={code}&"
                               f"startDate={start_year}&endDate={today.year}"),
            }
            result.setdefault(code, []).append(record)

        for code, records in result.items():
            records.sort(key=lambda item: item["ex_date"], reverse=True)
            result[code] = {
                "records": records,
                "record_count": len(records),
                "latest_amount": records[0]["amount"],
                "latest_ex_date": records[0]["ex_date"],
                "source": "TWSE ETF 配息清單",
                "source_url": ETF_DISTRIBUTION_URL,
                "retrieved_date": today.isoformat(),
            }
        payload = result
        with _realtime_cache_lock:
            _etf_distribution_cache.update({"at": time.time(), "data": payload})
        _save_shared_data_snapshot(
            ETF_DISTRIBUTION_SNAPSHOT_KEY, payload, data_date=today,
            source_meta={"source": "TWSE ETF 配息清單", "url": ETF_DISTRIBUTION_URL,
                         "recorded_codes": len(payload), "start_year": start_year,
                         "end_year": today.year})
        return payload
    except Exception as exc:
        print(f"⚠️ 抓取 TWSE ETF 配息清單失敗: {exc}")
        with _realtime_cache_lock:
            cached = _etf_distribution_cache.get("data")
        return cached if isinstance(cached, dict) else {}


def _etf_distribution_records_for_window(meta, start_date, end_date):
    records = []
    for raw in (meta or {}).get("distribution_records") or []:
        try:
            ex_date = date.fromisoformat(str(raw.get("ex_date") or "")[:10])
            amount = float(raw.get("amount"))
        except (TypeError, ValueError):
            continue
        if amount > 0 and start_date <= ex_date <= end_date:
            records.append((ex_date, amount))
    return records


def _etf_distribution_metrics(meta, start_date, end_date, start_close):
    """以除息交易日落在觀測窗內的實際現金配息計算期間配息率。"""
    policy = str((meta or {}).get("distribution_policy") or "unknown")
    if policy == "non_distributing":
        return {"amount": None, "yield_pct": None, "count": 0, "status": "non_distributing"}
    if not start_close or start_close <= 0:
        return {"amount": None, "yield_pct": None, "count": 0, "status": "unknown"}
    records = _etf_distribution_records_for_window(meta, start_date, end_date)
    if not records:
        return {"amount": None, "yield_pct": None, "count": 0, "status": "unknown"}
    amount = sum(value for _day, value in records)
    return {"amount": round(amount, 6),
            "yield_pct": round(amount / float(start_close) * 100, 4),
            "count": len(records), "status": "verified"}


def _etf_recent_distribution_records(meta, limit=4):
    """回傳最近幾次已核實現金配息；沿用 TWSE 官方紀錄，不自行預估下次配息。"""
    records = []
    for raw in (meta or {}).get("distribution_records") or []:
        if not isinstance(raw, dict):
            continue
        try:
            ex_date = date.fromisoformat(str(raw.get("ex_date") or "")[:10])
            amount = float(raw.get("amount"))
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        records.append({
            "ex_date": ex_date.isoformat(),
            "amount": round(amount, 6),
            "base_date": raw.get("base_date"),
            "payment_date": raw.get("payment_date"),
        })
    records.sort(key=lambda item: item["ex_date"], reverse=True)
    return records[:max(0, int(limit or 0))]


def _etf_distribution_stability_metrics(meta, end_date, end_close):
    """把原始年化參考殖利率與近四次配息穩定性分開，避免單次高配息直接灌高分。"""
    trailing = _etf_trailing_distribution_metrics(meta, end_date, end_close)
    recent = _etf_recent_distribution_records(meta, limit=4)
    raw_yield = trailing.get("yield_pct")
    status = trailing.get("status", "unknown")
    result = {
        "raw_yield_pct": raw_yield,
        "score_yield_pct": None,
        "stability_status": status,
        "recent_records": recent,
        "recent_count": len(recent),
        "recent_mean_amount": None,
        "recent_median_amount": None,
    }
    if status == "non_distributing":
        return result
    if raw_yield is None:
        result["stability_status"] = "partial_or_unknown"
        return result
    if len(recent) < 4:
        # 不足四次時不把單筆／少數筆配息拿來給殖利率分數；原始參考值仍照實顯示。
        result["stability_status"] = "insufficient_recent_records"
        return result
    amounts = sorted(float(item["amount"]) for item in recent)
    mean_amount = sum(amounts) / len(amounts)
    middle = len(amounts) // 2
    median_amount = (amounts[middle - 1] + amounts[middle]) / 2
    result["recent_mean_amount"] = round(mean_amount, 6)
    result["recent_median_amount"] = round(median_amount, 6)
    if mean_amount <= 0:
        result["stability_status"] = "insufficient_recent_records"
        return result
    # 以近四次中位數／平均值作穩定性折減；四次接近時不改變，單次異常高時會降低。
    adjusted = float(raw_yield) * (median_amount / mean_amount)
    result["score_yield_pct"] = round(adjusted, 4)
    result["stability_status"] = "verified_four_records"
    return result


def _format_recent_distribution_records(records, prefix="近4次官方配息", multiline=True):
    """將官方最近四次除息日／每單位金額整理成 LINE 可讀的逐列文字。"""
    if not records:
        return f"{prefix}：待確認"
    records = list(records[:4])
    pieces = []
    for record in records:
        ex_date = str(record.get("ex_date") or "未標日期")
        amount = record.get("amount")
        amount_text = f"每單位 {float(amount):.4f} 元" if amount is not None else "金額待確認"
        pieces.append(f"・{ex_date}　{amount_text}")
    label = prefix if len(records) >= 4 else f"{prefix}（目前 {len(records)} 筆）"
    if multiline:
        return f"{label}：\n" + "\n".join(pieces)
    return f"{label}：" + "、".join(piece.lstrip("・").strip() for piece in pieces)


def _etf_trailing_distribution_metrics(meta, end_date, end_close, days=365):
    """用官方除息紀錄計算近12月現金配息；資料未滿一整年不硬算年化殖利率。"""
    policy = str((meta or {}).get("distribution_policy") or "unknown")
    if policy == "non_distributing":
        return {"amount": None, "yield_pct": None, "observed_yield_pct": None,
                "count": 0, "status": "non_distributing", "coverage_days": 0}
    if not end_date or not end_close or end_close <= 0:
        return {"amount": None, "yield_pct": None, "observed_yield_pct": None,
                "count": 0, "status": "unknown", "coverage_days": 0}
    start_date = end_date - timedelta(days=max(1, int(days)))
    records = _etf_distribution_records_for_window(meta, start_date, end_date)
    if not records:
        return {"amount": None, "yield_pct": None, "observed_yield_pct": None,
                "count": 0, "status": "unknown", "coverage_days": 0}
    amount = sum(value for _day, value in records)
    all_records = []
    for raw in (meta or {}).get("distribution_records") or []:
        try:
            all_records.append(date.fromisoformat(str(raw.get("ex_date") or "")[:10]))
        except (TypeError, ValueError):
            continue
    earliest = min(all_records) if all_records else records[-1][0]
    coverage_days = max(1, (end_date - earliest).days + 1)
    observed_yield = round(amount / float(end_close) * 100, 4)
    # 只有官方紀錄至少覆蓋約 11 個月，才把最近12月金額標成可比較的年化窗口。
    annualized = coverage_days >= 330
    return {"amount": round(amount, 6),
            "yield_pct": observed_yield if annualized else None,
            "observed_yield_pct": observed_yield,
            "count": len(records),
            "status": "verified" if annualized else "partial",
            "coverage_days": coverage_days}


def _etf_distribution_label(policy):
    return {
        "distributing": "配息型",
        "non_distributing": "不分配／累積型",
        "unknown": "配息政策待確認",
    }.get(str(policy or "unknown"), "配息政策待確認")


def _etf_maturity_label(listing_date):
    """只根據實際上市日標示資料成熟度，不用不存在的歷史資料補值。"""
    if not listing_date:
        return "資料成熟度：上市日待確認"
    try:
        listed = date.fromisoformat(str(listing_date)[:10])
        today = taiwan_today()
        months = (today.year - listed.year) * 12 + today.month - listed.month
        if today.day < listed.day:
            months -= 1
        if months < 6:
            return "資料成熟度：未滿 6 個月，暫不排名"
        if months < 12:
            return "資料成熟度：6 個月至未滿 1 年，短期觀察"
        if months < 36:
            return "資料成熟度：1 年至未滿 3 年，歷史較短"
        return "資料成熟度：滿 3 年，可做完整長期比較"
    except (TypeError, ValueError):
        return "資料成熟度：上市日格式待確認"


# ── ETF 商品績效排名 ──
# 商品排名和會員績效排行榜是兩件事：這裡比較 ETF 本身，會員排行榜則比較使用者持股 TWR。
# 先採價格報酬，因為完整配息紀錄尚未對所有上市 ETF 核實；不把價格報酬冒充含息總報酬。
ETF_PRODUCT_RANKING_SNAPSHOT_KEY = "etf_product_rankings"
ETF_PRODUCT_RANKING_CACHE_SECONDS = 900
ETF_PRODUCT_RANKING_SHARED_MAX_AGE = 3 * 86400
ETF_PRODUCT_RANKING_SCHEMA_VERSION = 5
ETF_PRODUCT_RANKING_PERIODS = {
    "short": {"label": "短期（近 40 個交易日，約 2 個月）", "days": 40},
    "long": {"label": "長期（近 250 個交易日，約 1 年）", "days": 250},
}
# 四類仍各自排名；權重依商品使用目的調整，分數不可跨類別直接比較。
ETF_CATEGORY_SCORE_WEIGHTS = {
    "主動式": {"同期超額報酬": 30, "絕對價格報酬": 20, "配息殖利率": 15,
              "回撤控制": 15, "波動控制": 10, "資料完整度": 10},
    "高股息": {"配息殖利率": 40, "同期超額報酬": 20, "絕對價格報酬": 15,
              "回撤控制": 10, "波動控制": 5, "資料完整度": 10},
    "市值型": {"同期超額報酬": 40, "絕對價格報酬": 25, "回撤控制": 15,
              "波動控制": 10, "資料完整度": 10},
    "主題型": {"同期超額報酬": 25, "絕對價格報酬": 25, "回撤控制": 20,
              "波動控制": 20, "資料完整度": 10},
}
_etf_product_ranking_cache = {"at": 0, "data": None}
_ETF_RANKING_REFRESH_LOCK = threading.Lock()
_ETF_RANKING_REFRESH_RUNNING = False


def _parse_history_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return None


def _history_from_quote(quote):
    """從既有行情結果整理有效日期／收盤價，排除重複日期與非正價格。"""
    dates = quote.get("close_dates") or [] if isinstance(quote, dict) else []
    closes = quote.get("closes") or [] if isinstance(quote, dict) else []
    pairs = []
    seen = set()
    for raw_date, raw_close in zip(dates, closes):
        parsed = _parse_history_date(raw_date)
        try:
            close = float(raw_close)
        except (TypeError, ValueError):
            continue
        if not parsed or close <= 0 or parsed in seen:
            continue
        seen.add(parsed)
        pairs.append((parsed, close))
    pairs.sort(key=lambda item: item[0])
    return pairs


def _fetch_taiex_history(rng="2y"):
    """取得與 ETF 價格同口徑的加權指數日收盤序列；失敗就回傳空清單。"""
    try:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
               f"?range={quote(str(rng), safe='')}&interval=1d")
        response = requests.get(url, timeout=10,
                                headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        result = (response.json().get("chart", {}).get("result") or [None])[0]
        if not result:
            return []
        timestamps = result.get("timestamp") or []
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        tw_tz = timezone(timedelta(hours=8))
        pairs, seen = [], set()
        for timestamp, raw_close in zip(timestamps, closes):
            try:
                parsed = datetime.fromtimestamp(timestamp, tw_tz).date()
                close = float(raw_close)
            except (TypeError, ValueError, OSError):
                continue
            if close <= 0 or parsed in seen:
                continue
            seen.add(parsed)
            pairs.append((parsed, close))
        return sorted(pairs, key=lambda item: item[0])
    except Exception as exc:
        print(f"⚠️ 取得 ETF 排名大盤序列失敗: {exc}")
        return []


def _market_return_for_window(market_history, start_date, end_date):
    """用 ETF 自己的觀測起訖日找同期大盤，避免不同上市日直接硬對陣列。"""
    if not market_history or not start_date or not end_date:
        return None
    start = next(((d, value) for d, value in market_history if d >= start_date), None)
    end_candidates = [(d, value) for d, value in market_history if d <= end_date]
    end = end_candidates[-1] if end_candidates else None
    if not start or not end or start[1] <= 0:
        return None
    return (end[1] / start[1] - 1) * 100


def _etf_ranking_comment(category, period_key, item, market_return):
    """四種類別各自使用不同的事實型評論；不把報酬結果寫成預測。"""
    excess = item.get("excess_pct")
    if excess is None:
        comparison = "同期大盤資料不足"
    elif excess >= 3:
        comparison = f"跑贏同期大盤 {excess:+.1f} 個百分點"
    elif excess <= -3:
        comparison = f"落後同期大盤 {abs(excess):.1f} 個百分點"
    else:
        comparison = f"與同期大盤接近（{excess:+.1f} 個百分點）"

    distribution_status = item.get("distribution_status")
    distribution_yield = item.get("distribution_yield_pct")
    distribution_amount = item.get("distribution_amount")
    distribution_count = int(item.get("distribution_count") or 0)
    if distribution_status == "verified" and distribution_yield is not None:
        distribution_note = (f"近12個月現金配息 {float(distribution_amount):.2f} 元、"
                             f"年化參考殖利率 {float(distribution_yield):.2f}%（{distribution_count} 次）")
    elif distribution_status == "partial" and distribution_amount is not None:
        coverage = int(item.get("distribution_coverage_days") or 0)
        distribution_note = (f"已發生現金配息 {float(distribution_amount):.2f} 元（{distribution_count} 次），"
                             f"官方資料覆蓋 {coverage} 日，未滿 12 個月，年化配息排名待資料完整")
    elif distribution_status == "non_distributing":
        distribution_note = "不分配／累積型，現金配息不適用"
    else:
        distribution_note = "官方配息金額尚待確認，配息項目未給分"

    if category == "主動式":
        return (f"主動式｜{period_key}價格報酬{comparison}；{distribution_note}；"
                "結果不等於經理人長期能力。")
    if category == "高股息":
        return (f"高股息｜{period_key}價格報酬{comparison}；{distribution_note}；"
                "年化配息排名與績效排名分開看，綜合分數再搭配風險與大盤比較。")
    if category == "市值型":
        return (f"市值型｜{period_key}相對加權指數{comparison}；用來觀察跟隨核心市場的程度。")
    return (f"主題型｜{period_key}價格報酬{comparison}；主題集中與波動風險需一併觀察。")


def _etf_window_risk_stats(window):
    """從同一觀測窗計算最大回撤與日報酬波動；不足資料時回傳 None。"""
    values = []
    for _day, raw_value in window or []:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.append(value)
    if len(values) < 3:
        return {"max_drawdown_pct": None, "volatility_pct": None}
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = min(max_drawdown, (value / peak - 1.0) * 100)
    returns = [(values[index] / values[index - 1] - 1.0) * 100
               for index in range(1, len(values)) if values[index - 1] > 0]
    if len(returns) < 2:
        volatility = None
    else:
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / len(returns)
        volatility = math.sqrt(variance)
    return {"max_drawdown_pct": round(max_drawdown, 2),
            "volatility_pct": round(volatility, 2) if volatility is not None else None}


def _etf_percentile_score(value, values, higher_is_better=True):
    """同類別橫截面百分位，單一商品給中性 50 分，不製造虛假優勢。"""
    valid = []
    for raw in values or []:
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            valid.append(number)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not valid or not math.isfinite(number):
        return None
    if len(valid) == 1 or max(valid) == min(valid):
        return 50.0
    better = sum(1 for candidate in valid if candidate < number)
    equal = sum(1 for candidate in valid if candidate == number)
    percentile = (better + 0.5 * equal) / len(valid)
    if not higher_is_better:
        percentile = 1.0 - percentile
    return round(max(0.0, min(100.0, percentile * 100)), 1)


def _apply_etf_period_scores(rows, required_days):
    """在單一類別／期間內依類別專屬權重計算 100 分；名次以分數，另保留報酬名次。"""
    if not rows:
        return
    category = str(rows[0].get("category") or "")
    weights = ETF_CATEGORY_SCORE_WEIGHTS.get(category, ETF_CATEGORY_SCORE_WEIGHTS["主題型"])
    excess_values = [item.get("excess_pct") for item in rows]
    return_values = [item.get("return_pct") for item in rows]
    # 配息排名仍只採「年化窗口」；綜合評分的配息因子更嚴格，
    # 只採近四次官方紀錄的穩定性調整值；不足四次不給配息因子分數，
    # 不把單次高配息直接當成下一期可持續收益。
    dividend_values = [item.get("distribution_score_yield_pct") for item in rows]
    drawdown_values = [item.get("max_drawdown_pct") for item in rows]
    volatility_values = [item.get("volatility_pct") for item in rows]
    for item in rows:
        score_values = {
            "同期超額報酬": _etf_percentile_score(item.get("excess_pct"), excess_values, True),
            "絕對價格報酬": _etf_percentile_score(item.get("return_pct"), return_values, True),
            "配息殖利率": _etf_percentile_score(
                item.get("distribution_score_yield_pct"), dividend_values, True),
            "回撤控制": _etf_percentile_score(item.get("max_drawdown_pct"), drawdown_values, True),
            "波動控制": _etf_percentile_score(item.get("volatility_pct"), volatility_values, False),
        }
        components = {}
        for label, weight in weights.items():
            if label == "資料完整度":
                components[label] = 10.0 if item.get("observations", 0) >= required_days else 0.0
            else:
                # 配息資料未知時不當成 0%：該項只是不給分，並在畫面明示資料待確認。
                raw_score = score_values.get(label)
                components[label] = round((raw_score or 0.0) * float(weight) / 100.0, 1)
        item["score"] = round(sum(components.values()), 1)
        item["score_breakdown"] = components

    return_ranked = sorted(rows, key=lambda item: (item.get("return_pct", -999999), item.get("code", "")), reverse=True)
    for rank, item in enumerate(return_ranked, 1):
        item["return_rank"] = rank
    dividend_ranked = sorted(
        [item for item in rows if item.get("distribution_annualized_yield_pct") is not None],
        key=lambda item: (item.get("distribution_annualized_yield_pct", -999999), item.get("code", "")),
        reverse=True)
    for rank, item in enumerate(dividend_ranked, 1):
        item["distribution_rank"] = rank
    rows.sort(key=lambda item: (item.get("score", -999999), item.get("return_pct", -999999), item.get("code", "")), reverse=True)
    for rank, item in enumerate(rows, 1):
        item["rank"] = rank


def _etf_watchlist_rank_rows(codes):
    """從既有 ETF 商品排名快照取自選 ETF 的短／長期資料，不重掃全市場。"""
    wanted = {str(code).strip().upper() for code in (codes or [])}
    if not wanted:
        return {}, "無自選 ETF"
    try:
        payload, _fresh, source = _load_etf_product_ranking_snapshot()
    except Exception as exc:
        print(f"⚠️ 讀取自選 ETF 排名快照失敗: {exc}")
        return {}, "ETF 排名快照待確認"
    categories = (payload or {}).get("categories") if isinstance(payload, dict) else None
    if not isinstance(categories, dict):
        return {}, source or "ETF 排名快照待確認"
    found = {code: {} for code in wanted}
    for period_key in ETF_PRODUCT_RANKING_PERIODS:
        period_rows = categories.get(period_key) or {}
        if not isinstance(period_rows, dict):
            continue
        for rows in period_rows.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                code = str(row.get("code") or "").strip().upper()
                if code in wanted:
                    found[code][period_key] = dict(row)
    return found, source or "ETF 排名快照"


def _etf_component_point(breakdown, labels):
    """把 ETF 排名的事實型分項貢獻轉成快照欄位；完全缺資料仍回 None。"""
    values = []
    for label in labels:
        raw = (breakdown or {}).get(label)
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return int(round(sum(values)))


def _compute_etf_watchlist_scores(codes):
    """ETF 自選健檢：只使用 ETF 商品排名模型，不套用個股法人／營收／PE。"""
    codes = list(dict.fromkeys(str(code).strip().upper() for code in (codes or [])
                               if str(code).strip()))
    if not codes:
        return {}
    rank_map, rank_source = _etf_watchlist_rank_rows(codes)
    try:
        price_map = get_realtime_stocks_bulk(
            codes, workers=min(12, max(1, len(codes))), rng="3mo", market_suffix=".TW")
    except Exception as exc:
        print(f"⚠️ 取得自選 ETF 行情失敗: {exc}")
        price_map = {}
    try:
        distribution_map = fetch_twse_etf_distribution_history()
    except Exception as exc:
        print(f"⚠️ 取得自選 ETF 配息資料失敗: {exc}")
        distribution_map = {}

    result = {}
    for code in codes:
        periods = rank_map.get(code) or {}
        selected_period = "long" if periods.get("long") else ("short" if periods.get("short") else None)
        selected = periods.get(selected_period) if selected_period else None
        selected = dict(selected) if isinstance(selected, dict) else None
        try:
            meta = get_etf_metadata(code, distribution_map=distribution_map)
        except Exception as exc:
            print(f"⚠️ 讀取自選 ETF metadata 失敗 {code}: {exc}")
            meta = {}
        quote = price_map.get(code)
        category = str((selected or {}).get("category") or meta.get("category") or "待分類")
        name = str(meta.get("name") or (selected or {}).get("name") or
                    stock_display_name(code, fallback=code))
        breakdown = (selected or {}).get("score_breakdown") or {}
        total_raw = (selected or {}).get("score") if selected else None
        try:
            total = int(round(float(total_raw))) if total_raw is not None else None
        except (TypeError, ValueError):
            total = None
        result[code] = {
            "code": code,
            "name": name,
            "asset_type": "etf",
            "stock": quote or {"close": None, "pct": None},
            "total": total,
            # 舊 watchlist_scores schema 的四個欄位改存 ETF 分項貢獻，
            # 顯示與變化說明會依 asset_type 使用 ETF 名稱，不再誤稱法人／營收。
            "chip": _etf_component_point(breakdown, ["同期超額報酬"]),
            "position": _etf_component_point(breakdown, ["絕對價格報酬"]),
            "revenue": _etf_component_point(breakdown, ["配息殖利率"]),
            "valuation": _etf_component_point(
                breakdown, ["回撤控制", "波動控制", "資料完整度"]),
            "cum_lots": None, "buy_days": None, "streak": None,
            "cum_yoy": None, "pe": None,
            "etf_category": category,
            "etf_period": selected_period,
            "etf_selected": selected,
            "etf_periods": periods,
            "etf_score_status": ("ranked" if selected else "pending"),
            "etf_rank_source": rank_source,
            "etf_data_date": ((selected or {}).get("end_date") or
                              (selected or {}).get("data_date")),
        }
    return result


def _etf_watchlist_advice(score):
    """將 ETF 已計算出的事實欄位整理成簡短判讀，不做價格預測。"""
    row = score.get("etf_selected") or {}
    if not row:
        return "⏳ ETF 同類排名快照尚未建立，暫不把資料不足誤標成低分"
    notes = []
    excess = row.get("excess_pct")
    if excess is not None:
        try:
            excess = float(excess)
            if excess >= 3:
                notes.append(f"同期超額報酬 {excess:+.2f} 個百分點")
            elif excess <= -3:
                notes.append(f"同期落後大盤 {abs(excess):.2f} 個百分點")
            else:
                notes.append(f"同期超額報酬 {excess:+.2f} 個百分點，與大盤接近")
        except (TypeError, ValueError):
            pass
    drawdown = row.get("max_drawdown_pct")
    if drawdown is not None:
        try:
            if float(drawdown) <= -20:
                notes.append(f"觀測期間最大回撤 {float(drawdown):+.2f}%，波動需留意")
            else:
                notes.append(f"觀測期間最大回撤 {float(drawdown):+.2f}%")
        except (TypeError, ValueError):
            pass
    distribution_status = row.get("distribution_status")
    yield_pct = row.get("distribution_annualized_yield_pct")
    if distribution_status == "verified" and yield_pct is not None:
        notes.append(f"官方近12月年化參考殖利率 {float(yield_pct):.2f}%")
    elif distribution_status == "partial":
        notes.append("官方配息紀錄未滿約12個月，暫不年化")
    elif distribution_status == "non_distributing":
        notes.append("不分配／累積型，現金殖利率不適用")
    return "；".join(notes) if notes else "目前可取得 ETF 指標沒有明顯方向，持續觀察"


def _format_etf_watchlist_lines(code, score, score_change=None,
                                watchlist_status="※ 已在自選清單"):
    """ETF 自選健檢的 LINE 版面；與個股格式分開，缺值不補零。"""
    row = score.get("etf_selected") or {}
    name = score.get("name") or code
    category = score.get("etf_category") or "待分類"
    period = "長期" if score.get("etf_period") == "long" else "短期" if score.get("etf_period") == "short" else "待排名"
    lines = [f"📦 {code} {name}", f"{category} ETF｜ETF 專用健檢（{period}）", "─" * 14]
    quote = score.get("stock") or {}
    close, pct = quote.get("close"), quote.get("pct")
    if close is not None and pct is not None:
        lines.append(f"💰 最新價格 {float(close):,.2f}（{float(pct):+.2f}%）")
    elif close is not None:
        lines.append(f"💰 最新價格 {float(close):,.2f}（漲跌資料待確認）")
    else:
        lines.append("💰 最新價格待確認")

    total = score.get("total")
    if total is None:
        lines.append("🟡 ETF 綜合評分：待建立同類排名（不是低分）")
    else:
        flag = "🟢" if total >= 70 else ("🟡" if total >= 45 else "🔴")
        lines.append(f"{flag} ETF 綜合評分：{int(total)}／100")
        breakdown = (row.get("score_breakdown") or {})
        metric_available = {
            "同期超額報酬": row.get("excess_pct") is not None,
            "絕對價格報酬": row.get("return_pct") is not None,
            "配息殖利率": row.get("distribution_annualized_yield_pct") is not None,
            "回撤控制": row.get("max_drawdown_pct") is not None,
            "波動控制": row.get("volatility_pct") is not None,
            "資料完整度": row.get("observations") is not None,
        }
        parts = []
        for label in ("同期超額報酬", "絕對價格報酬", "配息殖利率", "回撤控制", "波動控制", "資料完整度"):
            if label not in breakdown:
                continue
            if not metric_available.get(label):
                parts.append(f"{label}待確認")
                continue
            try:
                parts.append(f"{label}{float(breakdown[label]):.1f}")
            except (TypeError, ValueError):
                parts.append(f"{label}待確認")
        if parts:
            lines.append("　分項貢獻　" + "　".join(parts))
    performance = _etf_performance_comparison(row)
    lines += ["", "【績效對照】", "價格報酬不含配息",
              f"ETF 價格報酬：{performance['return_text']}",
              f"同期大盤：{performance['market_text']}",
              f"超額報酬：{performance['excess_text']}（{performance['verdict_text']}）"]
    distribution_status = row.get("distribution_status")
    yield_pct = row.get("distribution_annualized_yield_pct")
    if distribution_status == "verified" and yield_pct is not None:
        lines.append(f"💰 年化參考殖利率 {float(yield_pct):.2f}%")
    elif distribution_status == "partial":
        observed = row.get("distribution_observed_yield_pct")
        observed_text = f"；觀察期率 {float(observed):.2f}%" if observed is not None else ""
        lines.append(f"💰 配息年化待確認{observed_text}（官方資料未滿約12個月）")
    elif distribution_status == "non_distributing":
        lines.append("💰 殖利率不適用（不分配／累積型）")
    else:
        lines.append("💰 年化參考殖利率待確認")
    lines.append(_format_recent_distribution_records(
        row.get("distribution_recent_records") or []))
    if row.get("distribution_stability_status") == "verified_four_records" and row.get("distribution_score_yield_pct") is not None:
        lines.append(f"評分用穩定殖利率 {float(row['distribution_score_yield_pct']):.2f}%（近4次中位數／平均值調整）")
    elif distribution_status != "non_distributing":
        lines.append("評分用穩定殖利率待近4次官方配息資料完整")
    if row.get("max_drawdown_pct") is not None or row.get("volatility_pct") is not None:
        dd = (f"最大回撤 {float(row['max_drawdown_pct']):+.2f}%"
              if row.get("max_drawdown_pct") is not None else "最大回撤待確認")
        vol = (f"波動 {float(row['volatility_pct']):.2f}%"
               if row.get("volatility_pct") is not None else "波動待確認")
        lines.append(f"📉 {dd}　{vol}")
    if score_change:
        lines.append(f"分數變化　{score_change}")
    lines += [f"判讀　{_etf_watchlist_advice(score)}"]
    if watchlist_status:
        lines.append(watchlist_status)
    lines.append("※ ETF 不套用個股法人、營收、PE、支撐／壓力評分；配息與報酬採官方／既有價格資料。")
    return lines


def _etf_ranking_snapshot_is_current(payload):
    if not isinstance(payload, dict):
        return False
    if int(payload.get("schema_version") or 0) < ETF_PRODUCT_RANKING_SCHEMA_VERSION:
        return False
    data_date = _parse_history_date(payload.get("market_data_date") or payload.get("data_date"))
    today = taiwan_today()
    if not data_date or data_date > today:
        return False
    # 交易日要等到當日資料完成；週末／假日沿用最近交易日快照即可。
    return data_date == today if today.weekday() < 5 else (today - data_date).days <= 3


def _load_etf_product_ranking_snapshot():
    now = time.time()
    with _realtime_cache_lock:
        cached = _etf_product_ranking_cache.get("data")
        if cached is not None and now - _etf_product_ranking_cache.get("at", 0) < ETF_PRODUCT_RANKING_CACHE_SECONDS:
            return cached, _etf_ranking_snapshot_is_current(cached), "記憶體快取"
    try:
        shared = _load_shared_data_snapshot(
            ETF_PRODUCT_RANKING_SNAPSHOT_KEY,
            max_age_seconds=ETF_PRODUCT_RANKING_SHARED_MAX_AGE)
        payload = (shared.get("payload") if shared else None) or {}
        if (isinstance(payload, dict) and isinstance(payload.get("categories"), dict)
                and int(payload.get("schema_version") or 0) >= ETF_PRODUCT_RANKING_SCHEMA_VERSION):
            with _realtime_cache_lock:
                _etf_product_ranking_cache.update({"at": now, "data": payload})
            return payload, _etf_ranking_snapshot_is_current(payload), "共享快照"
        if isinstance(payload, dict) and payload:
            return None, False, "舊版排名快照"
    except Exception as exc:
        print(f"⚠️ 讀取 ETF 商品排名快照失敗: {exc}")
    return None, False, "尚未建立快照"


def build_etf_product_rankings(force_refresh=False):
    """建立四類 ETF 的短／長期價格報酬排名與同期大盤對照。"""
    now = time.time()
    with _realtime_cache_lock:
        cached = _etf_product_ranking_cache.get("data")
        if (not force_refresh and cached is not None and
                now - _etf_product_ranking_cache.get("at", 0) < ETF_PRODUCT_RANKING_CACHE_SECONDS):
            return cached
    if not force_refresh:
        shared, _fresh, _source = _load_etf_product_ranking_snapshot()
        if shared:
            return shared

    catalog = get_etf_catalog_products()
    # 先用一年日線同時滿足大多數短期榜與成熟 ETF 的長期榜，
    # 不再對全市場每一檔直接抓 2 年；只有長期候選且一年資料不足者才回補。
    market_history = _fetch_taiex_history("1y")
    codes = [code for code, meta in catalog.items()
             if meta.get("category") in ("主動式", "高股息", "市值型", "主題型")]
    quotes = get_realtime_stocks_bulk(
        codes, workers=24, rng="1y", market_suffix=".TW") if codes else {}
    cutoff = taiwan_today() - timedelta(days=320)
    long_candidates = []
    for code in codes:
        listing = _parse_history_date((catalog.get(code) or {}).get("listing_date"))
        if listing and listing <= cutoff:
            long_candidates.append(code)
    long_fallback_codes = [code for code in long_candidates
                           if len(_history_from_quote(quotes.get(code) or {})) < 250]
    if long_fallback_codes:
        quotes.update(get_realtime_stocks_bulk(
            long_fallback_codes, workers=16, rng="2y", market_suffix=".TW"))
    period_rows = {key: {"active": [], "dividend": [], "market": [], "theme": []}
                   for key in ETF_PRODUCT_RANKING_PERIODS}
    category_key = {"主動式": "active", "高股息": "dividend",
                    "市值型": "market", "主題型": "theme"}

    for code, meta in catalog.items():
        category = meta.get("category")
        key = category_key.get(category)
        if not key:
            continue
        history = _history_from_quote(quotes.get(code) or {})
        if not history:
            continue
        for period_key, period in ETF_PRODUCT_RANKING_PERIODS.items():
            required = int(period["days"])
            # 短期至少要有完整 40 日、長期至少要有完整 250 日，
            # 不把剛上市商品用較短的歷史硬塞進不同期間排名。
            if len(history) < required:
                continue
            window = history[-required:]
            start_date, start_close = window[0]
            end_date, end_close = window[-1]
            if start_close <= 0:
                continue
            return_pct = (end_close / start_close - 1) * 100
            market_return = _market_return_for_window(
                market_history, start_date, end_date)
            excess = (return_pct - market_return
                       if market_return is not None else None)
            risk_stats = _etf_window_risk_stats(window)
            distribution = _etf_trailing_distribution_metrics(meta, end_date, end_close)
            window_distribution = _etf_distribution_metrics(
                meta, start_date, end_date, start_close)
            distribution_stability = _etf_distribution_stability_metrics(
                meta, end_date, end_close)
            official_metrics = meta.get("official_metrics") or {}
            item = {
                "code": code,
                "name": str(meta.get("name") or code),
                "category": category,
                "asset_size_billion": official_metrics.get("asset_size_billion"),
                "official_close": official_metrics.get("official_close"),
                "ytd_avg_turnover_million": official_metrics.get("ytd_avg_turnover_million"),
                "ytd_volume_shares": official_metrics.get("ytd_volume_shares"),
                "holders": official_metrics.get("holders"),
                "official_metrics_retrieved_date": official_metrics.get("retrieved_date"),
                "return_pct": round(return_pct, 2),
                "market_return_pct": (round(market_return, 2)
                                       if market_return is not None else None),
                "excess_pct": round(excess, 2) if excess is not None else None,
                "max_drawdown_pct": risk_stats.get("max_drawdown_pct"),
                "volatility_pct": risk_stats.get("volatility_pct"),
                "distribution_amount": distribution.get("amount"),
                "distribution_yield_pct": distribution.get("yield_pct"),
                "distribution_annualized_yield_pct": distribution.get("yield_pct"),
                "distribution_observed_yield_pct": distribution.get("observed_yield_pct"),
                "distribution_score_yield_pct": distribution_stability.get("score_yield_pct"),
                "distribution_stability_status": distribution_stability.get("stability_status", "unknown"),
                "distribution_recent_records": distribution_stability.get("recent_records") or [],
                "distribution_recent_count": distribution_stability.get("recent_count", 0),
                "distribution_recent_mean_amount": distribution_stability.get("recent_mean_amount"),
                "distribution_recent_median_amount": distribution_stability.get("recent_median_amount"),
                "distribution_coverage_days": distribution.get("coverage_days", 0),
                "distribution_count": distribution.get("count", 0),
                "distribution_status": distribution.get("status", "unknown"),
                "window_distribution_amount": window_distribution.get("amount"),
                "window_distribution_yield_pct": window_distribution.get("yield_pct"),
                "window_distribution_count": window_distribution.get("count", 0),
                "observations": len(window),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            }
            item["comment"] = _etf_ranking_comment(
                category,
                "短期" if period_key == "short" else "長期",
                item, market_return)
            period_rows[period_key][key].append(item)

    for period_key, period in ETF_PRODUCT_RANKING_PERIODS.items():
        required_days = int(period.get("days") or 0)
        for key, rows in period_rows[period_key].items():
            _apply_etf_period_scores(rows, required_days)

    market_data_date = market_history[-1][0].isoformat() if market_history else None
    result = {
        "schema_version": ETF_PRODUCT_RANKING_SCHEMA_VERSION,
        "data_date": market_data_date,
        "market_data_date": market_data_date,
        "periods": ETF_PRODUCT_RANKING_PERIODS,
        "categories": period_rows,
        "source": "Yahoo Finance 日收盤序列／TWSE ETF 官方上市清單／TWSE ETF 配息清單",
        "source_note": "價格報酬取 Yahoo 日收盤且不含配息；現金配息取 TWSE 官方已發生且金額有效紀錄。原始近12月參考殖利率＝已發配息加總／期末價格；年化配息排名只採官方紀錄覆蓋滿約 12 個月者，綜合評分的配息因子只採最近四次官方配息的中位數／平均值穩定性調整值，不足四次不給配息因子分數；大盤為同期間台灣加權指數價格報酬。",
    }
    with _realtime_cache_lock:
        _etf_product_ranking_cache.update({"at": time.time(), "data": result})
    try:
        _save_shared_data_snapshot(
            ETF_PRODUCT_RANKING_SNAPSHOT_KEY, result,
            data_date=market_data_date or taiwan_today(),
            source_meta={"source": "ETF product ranking", "schema_version": ETF_PRODUCT_RANKING_SCHEMA_VERSION,
                         "short_days": 40, "long_days": 250,
                         "category_counts": {key: len(period_rows["short"].get(key) or [])
                                             for key in period_rows["short"]}})
    except Exception as exc:
        print(f"⚠️ 保存 ETF 商品排名快照失敗: {exc}")
    return result


def _start_etf_product_ranking_refresh():
    """排名缺快照時只啟動一個背景刷新，頁面本身不等待全市場行情。"""
    global _ETF_RANKING_REFRESH_RUNNING
    with _ETF_RANKING_REFRESH_LOCK:
        if _ETF_RANKING_REFRESH_RUNNING:
            return False
        _ETF_RANKING_REFRESH_RUNNING = True

    def worker():
        global _ETF_RANKING_REFRESH_RUNNING
        try:
            build_etf_product_rankings(force_refresh=True)
        except Exception as exc:
            print(f"⚠️ 背景建立 ETF 商品排名失敗: {exc}")
        finally:
            with _ETF_RANKING_REFRESH_LOCK:
                _ETF_RANKING_REFRESH_RUNNING = False

    threading.Thread(target=worker, name="etf-ranking-refresh", daemon=True).start()
    return True


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



@app.route("/web/position-trend")
@web_login_required
def web_position_trend(uid):
    """只為使用者展開的單一持股載入一年走勢，避免主頁一次抓完所有歷史行情。"""
    code = normalize_code(request.args.get("code", ""))
    if not code:
        return respond_page("持股走勢", '<div class="sub">股票代號不正確。</div>', "positions")

    positions = merge_positions(get_positions(uid))
    position = next((p for p in positions if str(p.get("code")) == code), None)
    if not position:
        return respond_page("持股走勢", '<div class="sub">找不到這檔持股，可能已經被修改。</div>', "positions")

    price = get_realtime_stock(code, rng="1y")
    if not price:
        body = (f'<div class="sub">{html.escape(code)} 暫時查無一年走勢資料，'
                '目前持股頁其他資料不受影響。</div>')
    else:
        body = render_stock_sparkline(
            price, position.get("cost"), position.get("shares"), position.get("lots"))
    return respond_page("持股走勢", body, "positions")


def render_realized_summary(user_id, inst_data, summary_label="已實現損益",
                            trades=None):
    """
    已實現損益摘要＋最近交易明細。沒有任何賣出紀錄時回傳空字串，
    組合分析頁就不會多出一個空蕩蕩的區塊。
    """
    trades = (list(trades) if trades is not None
              else get_realized_trades(user_id, limit=100))
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
  <summary><span>{html.escape(summary_label)}</span><small>共 {len(trades)} 筆交易・點開查看</small></summary>
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
def render_portfolio_allocation_chart(holdings):
    """以目前有效市值畫輕量 SVG 配置甜甜圈；不把未知資料或未輸入現金補進分母。"""
    valid = []
    for holding in holdings or []:
        try:
            value = float(holding.get("value"))
        except (TypeError, ValueError):
            continue
        if value > 0 and math.isfinite(value):
            valid.append((holding, value))
    if not valid:
        return ('<section class="portfolio-chart-card">'
                '<div class="section-head"><h2>組合配置</h2>'
                '<span class="section-note">依目前市值</span></div>'
                '<div class="empty">目前沒有足夠的有效價格資料，暫時無法繪製配置圖。</div></section>')

    valid.sort(key=lambda item: item[1], reverse=True)
    total_value = sum(value for _holding, value in valid)
    top_items = valid[:5]
    if len(valid) > 5:
        top_items.append(({"name": "其他持股", "code": ""},
                          sum(value for _holding, value in valid[5:])))

    colors = ["#6E5228", "#8A6A3B", "#A98A5C", "#C3AC85", "#DCCFB4", "#EAEBE7"]
    radius, circumference = 43, 2 * math.pi * 43
    circles, legend = [], []
    offset = 0.0
    for idx, (holding, value) in enumerate(top_items):
        pct = value / total_value * 100 if total_value else 0
        dash = circumference * pct / 100
        color = colors[min(idx, len(colors) - 1)]
        circles.append(
            f'<circle cx="60" cy="60" r="{radius}" fill="none" stroke="{color}" '
            f'stroke-width="18" stroke-dasharray="{dash:.2f} {circumference - dash:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 60 60)"/>')
        offset += dash
        name = html.escape(str(holding.get("name") or holding.get("code") or "其他持股"))
        code = html.escape(str(holding.get("code") or ""))
        label = f"{name} {code}".strip()
        legend.append(
            f'<div class="portfolio-allocation-item"><i style="background:{color}"></i>'
            f'<div><b>{label}</b><span>{pct:.1f}%　市值 {value:,.0f}</span></div></div>')

    return f'''<style>
.portfolio-chart-card{{background:#fff;border:1px solid #e3e2dc;border-radius:12px;padding:18px;margin:14px 0;box-shadow:0 3px 14px rgba(35,39,35,.05)}}.portfolio-chart-card .section-head{{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px}}.portfolio-chart-card h2{{margin:0;font-size:20px}}.portfolio-chart-note{{color:var(--ink-soft);font-size:12px;line-height:1.6;margin:0 0 12px}}.portfolio-allocation-layout{{display:flex;align-items:center;gap:15px}}.portfolio-allocation-svg{{width:136px;height:136px;flex:none}}.portfolio-allocation-hole{{fill:#fff}}.portfolio-allocation-label{{font-size:8px;fill:var(--ink-soft);text-anchor:middle}}.portfolio-allocation-legend{{display:grid;gap:9px;min-width:0;flex:1}}.portfolio-allocation-item{{display:flex;align-items:center;gap:7px;min-width:0}}.portfolio-allocation-item i{{width:10px;height:10px;border-radius:50%;flex:none}}.portfolio-allocation-item b{{display:block;font-size:12px;line-height:1.3;overflow-wrap:anywhere}}.portfolio-allocation-item span{{display:block;color:var(--ink-soft);font-size:10px;line-height:1.35}}.portfolio-chart-footnote{{margin-top:12px;padding:10px 11px;border-left:3px solid var(--brass);border-radius:8px;background:#faf9f5;color:var(--ink-soft);font-size:11px;line-height:1.55}}@media(max-width:640px){{.portfolio-allocation-layout{{gap:11px}}.portfolio-allocation-svg{{width:128px;height:128px}}.portfolio-allocation-item b{{font-size:11px}}.portfolio-allocation-item span{{font-size:9.5px}}}}
</style><section class="portfolio-chart-card" id="portfolio-allocation">
  <div class="section-head"><h2>組合配置</h2><span class="section-note">依目前有效市值</span></div>
  <p class="portfolio-chart-note">只納入已登錄且有有效價格的持股；未輸入現金與其他資產不納入分母。</p>
  <div class="portfolio-allocation-layout"><svg class="portfolio-allocation-svg" viewBox="0 0 120 120" role="img" aria-label="組合配置甜甜圈圖">
    <circle cx="60" cy="60" r="43" fill="none" stroke="#E7E5DD" stroke-width="18"/>{''.join(circles)}
    <circle cx="60" cy="60" r="31" class="portfolio-allocation-hole"/>
    <text x="60" y="57" class="portfolio-allocation-label">組合</text><text x="60" y="68" class="portfolio-allocation-label">配置</text>
  </svg><div class="portfolio-allocation-legend">{''.join(legend)}</div></div>
  <div class="portfolio-chart-footnote">配置圖回答「目前資金放在哪裡」，不取代持股明細，也不把未知資料或現金假設成 0。</div>
</section>'''


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
        stroke="#D7D6D0" stroke-width="1" stroke-dasharray="2,3"/>
  {f'<path d="{taiex_path}" fill="none" stroke="#9AA19C" stroke-width="1.5" stroke-dasharray="4,3"/>' if taiex_path else ''}
  <path d="{port_path}" fill="none" stroke="#8B6934" stroke-width="2"/>
</svg>"""

    legend = f"""
<div class="legend" style="margin-top:6px">
  <span><i style="background:#8B6934"></i>組合 {port_last:+.1f}%</span>
  {f'<span><i style="background:#9AA19C"></i>加權指數 {taiex_last:+.1f}%</span>' if taiex_last is not None else ''}
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
    journal_date = request.args.get("journal_date", "")
    months, codes = get_trade_filters(uid)
    trades = get_realized_trades(uid, limit=500,
                                 code=code or None, month=month or None)
    journal_logs = get_position_change_logs(uid, limit=5000, code=code or None)
    inst = fetch_institutional_data() or {}
    current_positions = merge_positions(get_positions(uid))
    journal_codes = (sorted({str(p.get("code")).strip() for p in current_positions if p.get("code")} |
                            {str(log.get("code")).strip() for log in journal_logs if log.get("code")})
                     if journal_logs else [])
    journal_prices = (get_realtime_stocks_bulk(journal_codes, rng="1d")
                      if journal_codes else {})
    journal_html = render_position_change_journal(
        uid, current_positions=current_positions, price_map=journal_prices,
        inst_data=inst, logs=journal_logs, trade_date=journal_date or None)

    if not trades and not months and not journal_logs:
        return respond_page("交易紀錄", """
<div class="empty">還沒有任何交易或操作日誌。<br><br>
<span style="font-size:12.5px">在持股頁新增持股會記錄加碼；按「賣出」並填入賣價後，
會同時記錄減碼與已實現損益。</span><br><br>
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
    <div><label>加減碼日期</label>
      <input type="date" name="journal_date" value="{html.escape(journal_date)}" onchange="this.form.submit()"></div>
  </div>
</form>"""

    if not st:
        body = controls + journal_html + '<div class="empty">這個範圍內沒有已實現損益紀錄；若有加碼／減碼，請查看上方操作日報。</div>'
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
{journal_html}
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
    page_started = time.monotonic()
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
                note="報酬率以時間加權計算，加碼與贖回不影響結果；個股與 ETF 持股都會納入。")

    me = get_leaderboard_member(uid)
    board_started = time.monotonic()
    all_boards, (series_map, market) = build_leaderboard(top_n=100, days=365)
    board_done = time.monotonic()
    boards = {
        "long": (all_boards.get("long") or [])[:20],
        "short": (all_boards.get("short") or [])[:20],
        "waiting": all_boards.get("waiting") or [],
    }
    with _leaderboard_cache_lock:
        leaderboard_meta = dict(_leaderboard_cache.get((100, 365)) or {})
    leaderboard_source = ("Supabase 持久化快照"
                          if leaderboard_meta.get("source") == "persisted"
                          else "本次完整計算")
    leaderboard_data_date = leaderboard_meta.get("data_date") or _leaderboard_data_date(
        series_map, market)
    leaderboard_data_date = (leaderboard_data_date.isoformat()
                             if isinstance(leaderboard_data_date, (date, datetime))
                             else str(leaderboard_data_date or "未標日期"))
    rank_inputs = []
    for board_name in ("short", "long"):
        for current_rank, row in enumerate(all_boards.get(board_name, []), 1):
            rank_inputs.append((board_name, row.get("user_id"), current_rank))
    rank_status_map = get_rank_status_map(rank_inputs)
    rank_status_done = time.monotonic()
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
    # 已有本頁 top20 與排名狀態，直接重用；避免 get_my_rank_summary()
    # 預設再呼叫 build_leaderboard(top_n=100) 造成第二次重型計算。
    my_rank = get_my_rank_summary(uid, boards=all_boards,
                                  rank_status_map=rank_status_map)

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
            holdings_text = f'{r["holdings"]} 檔'
            if r.get("etf_holdings"):
                holdings_text += f'（含 ETF {r["etf_holdings"]} 檔）'
            supporting.append(f'<span><em>持股</em> {holdings_text}</span>')

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
<div class="rank-source-note">資料來源：{leaderboard_source}・資料日：{html.escape(leaderboard_data_date)}</div>
<div class="mode-note">個股與 ETF 持股都納入會員整體績效；ETF 只計入實際價格／市值變化，不套用個股營收、PE 或法人評分。</div>
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
  ・個股與 ETF 都可納入持股市值與時間加權報酬；ETF 不套用個股營收、PE 或法人評分，避免兩種商品口徑混在一起。<br>
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
    print("⏱️ 排行榜頁：榜單 %.0fms、排名狀態 %.0fms、HTML %.0fms、合計 %.0fms" % (
        (board_done - board_started) * 1000,
        (rank_status_done - board_done) * 1000,
        (time.monotonic() - rank_status_done) * 1000,
        (time.monotonic() - page_started) * 1000))
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


@app.route("/web/chips")
@web_login_required
def web_chips(uid):
    """籌碼超人完整網頁版；LINE 按鈕與選股模式列都進這裡。"""
    if request.args.get("legacy") != "1":
        token = str(request.args.get("t") or "").strip()
        target = "/web/workbench?tab=籌碼"
        if token:
            target += "&t=" + quote(token, safe="")
        return redirect(target)
    if not wants_fragment():
        return render_loading_shell(
            "籌碼超人", "screener",
            ["正在讀取法人資料…", "正在整理近十日資金方向…", "正在組裝完整籌碼分析…"],
            note="LINE 顯示快速摘要；網頁版提供完整五區法人資料與資料日期。",
            staged=False)
    result = build_chips_payload(allow_compute=False)
    if (result.get("payload") or {}).get("building"):
        _start_chips_background_refresh()
    # 籌碼超人是選股分析區的一員；從 LINE 進入也應亮「選股」，不亮「更多」。
    return respond_page("籌碼超人", render_chips_web_body(result), "screener")


def _etf_score_method_text(category_label):
    """回傳目前類別實際採用的評分權重，避免頁面把不同類別說成同一公式。"""
    weights = ETF_CATEGORY_SCORE_WEIGHTS.get(category_label) or {}
    parts = []
    for label, weight in weights.items():
        parts.append(f"{label} {weight} 分")
    return "・".join(parts)


def _etf_performance_comparison(row):
    """統一整理 ETF 價格報酬、同期大盤與超額報酬；缺值不補零。"""
    row = row or {}

    def finite_number(value):
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    return_pct = finite_number(row.get("return_pct"))
    market_pct = finite_number(row.get("market_return_pct"))
    excess_pct = finite_number(row.get("excess_pct"))
    result = {
        "return_pct": return_pct,
        "market_return_pct": market_pct,
        "excess_pct": excess_pct,
        "return_text": f"{return_pct:+.2f}%" if return_pct is not None else "待確認",
        "market_text": f"{market_pct:+.2f}%" if market_pct is not None else "待確認",
        "excess_text": (f"{excess_pct:+.2f} 個百分點"
                        if excess_pct is not None else "待確認"),
        "direction": "unknown",
        "verdict_text": "同期大盤或超額報酬待確認",
    }
    if excess_pct is not None:
        if excess_pct > 0:
            result["direction"] = "better"
            result["verdict_text"] = f"勝過同期大盤 {excess_pct:.2f} 個百分點"
        elif excess_pct < 0:
            result["direction"] = "worse"
            result["verdict_text"] = f"落後同期大盤 {abs(excess_pct):.2f} 個百分點"
        else:
            result["direction"] = "same"
            result["verdict_text"] = "與同期大盤相同"
    return result


def _etf_distribution_display_text(row):
    status = row.get("distribution_status")
    amount = row.get("distribution_amount")
    yield_pct = row.get("distribution_annualized_yield_pct")
    observed = row.get("distribution_observed_yield_pct")
    count = int(row.get("distribution_count") or 0)
    coverage = int(row.get("distribution_coverage_days") or 0)
    if status == "verified" and amount is not None and yield_pct is not None:
        base = (f"原始年化參考 {float(yield_pct):.2f}%・近12月已發 {float(amount):.2f} 元"
                f"（{count} 次）")
    elif status == "partial" and amount is not None:
        observed_text = f"・觀察期率 {float(observed):.2f}%" if observed is not None else ""
        base = (f"已發配息 {float(amount):.2f} 元（{count} 次）{observed_text}；"
                f"資料覆蓋 {coverage} 日，年化待滿 12 個月")
    elif status == "non_distributing":
        base = "不分配／累積型・現金配息不適用"
    else:
        base = "官方配息資料待確認・年化配息不列名次"

    recent_text = _format_recent_distribution_records(
        row.get("distribution_recent_records") or [], multiline=False)
    stability_status = row.get("distribution_stability_status")
    score_yield = row.get("distribution_score_yield_pct")
    if stability_status == "verified_four_records" and score_yield is not None:
        stability_text = f"評分用穩定殖利率 {float(score_yield):.2f}%（近4次中位數／平均值調整）"
    elif status == "non_distributing":
        stability_text = "評分配息因子不適用"
    elif row.get("distribution_recent_count", 0) < 4:
        stability_text = "評分用穩定殖利率待近4次資料完整"
    else:
        stability_text = "評分用殖利率待確認"
    return f"{base}；{recent_text}；{stability_text}"


def render_etf_product_ranking_html(payload, category_key, category_label,
                                     sort_key="score", sort_order="desc"):
    """渲染 ETF 短／長期排名；預設依綜合分數降冪，可切換四種指標。"""
    esc = html.escape
    sort_options = {
        "score": ("綜合分數", "score"),
        "return": ("價格報酬率", "return_pct"),
        "excess": ("超額報酬", "excess_pct"),
        "yield": ("殖利率／年化配息率", "distribution_annualized_yield_pct"),
    }
    selected_sort = sort_key if sort_key in sort_options else "score"
    selected_order = sort_order if sort_order in ("asc", "desc") else "desc"
    selected_sort_label, selected_field = sort_options[selected_sort]
    selected_arrow = "↓" if selected_order == "desc" else "↑"
    selected_direction = "高到低" if selected_order == "desc" else "低到高"

    def sort_link(key, label):
        active = key == selected_sort
        next_order = ("asc" if selected_order == "desc" else "desc") if active else "desc"
        arrow = selected_arrow if active else "↓"
        active_class = " active" if active else ""
        href = (f"/web/etf?category={esc(str(category_key), quote=True)}"
                f"&sort={key}&order={next_order}")
        aria = f"目前{label}{selected_direction}，按一下切換" if active else f"依{label}由高到低排序"
        return (f'<a class="etf-sort-link{active_class}" href="{href}" '
                f'aria-label="{esc(aria, quote=True)}"><span>{esc(label)}</span>'
                f'<b aria-hidden="true">{arrow}</b></a>')

    if not isinstance(payload, dict) or not isinstance(payload.get("categories"), dict):
        return '''<section class="etf-ranking-card etf-ranking-empty">
  <h2>ETF 商品排名</h2><p>排名資料尚在背景整理；稍後重新整理即可看到具體名次。</p></section>'''

    market_date = payload.get("market_data_date") or payload.get("data_date") or "未標日期"
    source_note = payload.get("source_note") or "價格報酬未含配息；官方現金配息另列；大盤為同期價格報酬。"
    sections = []
    for period_key in ("short", "long"):
        period = (payload.get("periods") or {}).get(period_key) or {}
        label = period.get("label") or ("短期" if period_key == "short" else "長期")
        rows = (((payload.get("categories") or {}).get(period_key) or {})
                .get(category_key) or [])
        if not rows:
            sections.append(
                f'<div class="etf-ranking-period"><h3>{esc(label)}</h3>'
                '<div class="etf-ranking-empty-line">資料不足，未把不完整期間硬列入排名。</div></div>')
            continue

        # 排序只使用已存在且可核實的數值；缺值永遠排在有數值之後，
        # 不把待確認／不適用轉成 0，也不會把不同期間混在一起。
        sortable_rows, missing_rows = [], []
        for row in rows:
            raw_value = row.get(selected_field)
            try:
                numeric_value = float(raw_value)
                if not math.isfinite(numeric_value):
                    raise ValueError
            except (TypeError, ValueError):
                missing_rows.append(row)
            else:
                sortable_rows.append((row, numeric_value))
        if selected_order == "desc":
            sortable_rows.sort(key=lambda item: (-item[1], str(item[0].get("code") or "")))
        else:
            sortable_rows.sort(key=lambda item: (item[1], str(item[0].get("code") or "")))
        ordered_items = ([(row, True) for row, _value in sortable_rows] +
                         [(row, False) for row in missing_rows])

        rendered = []
        for display_rank, (row, sort_value_available) in enumerate(ordered_items, 1):
            rank = display_rank
            display_rank_text = f'#{rank}' if sort_value_available else '—'
            performance = _etf_performance_comparison(row)
            return_pct = performance.get("return_pct")
            market_pct = performance.get("market_return_pct")
            excess = performance.get("excess_pct")
            ret_text = performance.get("return_text") or "待確認"
            market_text = performance.get("market_text") or "待確認"
            excess_text = performance.get("excess_text") or "待確認"
            ret_cls = "up" if return_pct is not None and return_pct >= 0 else "down"
            performance_direction = performance.get("direction") or "unknown"
            performance_verdict = performance.get("verdict_text") or "同期大盤或超額報酬待確認"
            score = row.get("score")
            score_text = f'綜合 {float(score):.0f} 分' if score is not None else "綜合評分資料不足"
            return_rank = row.get("return_rank")
            distribution_rank = row.get("distribution_rank")
            if return_rank:
                performance_rank_text = f'績效排名 #{int(return_rank)}'
            else:
                performance_rank_text = '績效排名 待確認'
            if distribution_rank:
                dividend_rank_text = f'年化配息排名 #{int(distribution_rank)}'
            elif row.get("distribution_status") == "non_distributing":
                dividend_rank_text = '年化配息 不適用'
            elif row.get("distribution_status") == "partial":
                dividend_rank_text = '年化配息 待滿12個月'
            else:
                dividend_rank_text = '年化配息 待確認'
            breakdown = row.get("score_breakdown") or {}
            weights = ETF_CATEGORY_SCORE_WEIGHTS.get(category_label) or {}
            breakdown_parts = []
            metric_available = {
                "同期超額報酬": row.get("excess_pct") is not None,
                "絕對價格報酬": row.get("return_pct") is not None,
                "配息殖利率": row.get("distribution_score_yield_pct") is not None,
                "回撤控制": row.get("max_drawdown_pct") is not None,
                "波動控制": row.get("volatility_pct") is not None,
                "資料完整度": row.get("observations") is not None,
            }
            for score_label, weight in weights.items():
                if not metric_available.get(score_label, True):
                    breakdown_parts.append(f'{score_label}待確認/{int(weight)}')
                else:
                    try:
                        point = float(breakdown.get(score_label))
                        breakdown_parts.append(f'{score_label} {point:.1f}/{int(weight)}')
                    except (TypeError, ValueError):
                        breakdown_parts.append(f'{score_label}待確認/{int(weight)}')
            breakdown_text = '評分拆解：' + '・'.join(breakdown_parts)
            distribution_text = _etf_distribution_display_text(row)
            max_dd = row.get("max_drawdown_pct")
            vol = row.get("volatility_pct")
            risk_text = (f'最大回撤 {float(max_dd):.2f}%' if max_dd is not None else '最大回撤資料不足')
            if vol is not None:
                risk_text += f'・日波動 {float(vol):.2f}%'
            asset_size = row.get("asset_size_billion")
            asset_text = (f'資產規模 {float(asset_size):,.0f} 億'
                          if asset_size is not None else '資產規模待確認')
            holders = row.get("holders")
            holders_text = (f'受益人次 {int(float(holders)):,}'
                            if holders is not None else '受益人次待確認')
            turnover = row.get("ytd_avg_turnover_million")
            turnover_text = (f'年初至今均成交 {float(turnover):,.3f} 百萬元'
                             if turnover is not None else '年初至今均成交待確認')
            metric_date = row.get("official_metrics_retrieved_date")
            metric_date_text = (f'官方欄位擷取日 {metric_date}' if metric_date else '官方欄位擷取日待確認')
            observations = row.get("observations")
            observation_text = (f'{int(float(observations))} 個交易日・'
                                f'{row.get("start_date") or "未標起日"}～{row.get("end_date") or "未標迄日"}'
                                if observations is not None else '價格觀察日數待確認')
            score_rank = row.get("rank")
            score_rank_text = (f'綜合排名 #{int(score_rank)}'
                               if score_rank is not None else '綜合排名 待確認')
            current_rank_text = ('綜合排名' if selected_sort == 'score'
                                 else f'{selected_sort_label}排名')
            if sort_value_available:
                rank_badges = [f'<span>{esc(current_rank_text)} #{rank}</span>']
            else:
                rank_badges = [f'<span>{esc(selected_sort_label)} 待確認／不適用</span>']
            if selected_sort != 'score' and score_rank is not None:
                rank_badges.append(f'<span>{esc(score_rank_text)}</span>')
            rank_badges.extend([
                f'<span>{esc(performance_rank_text)}</span>',
                f'<span>{esc(dividend_rank_text)}</span>',
            ])
            rendered.append(f'''<div class="etf-ranking-row">
  <div class="etf-ranking-rank">{display_rank_text}</div>
  <div class="etf-ranking-main"><div class="etf-ranking-name"><b>{esc(str(row.get("name") or row.get("code")))}</b><span>{esc(str(row.get("code") or ""))}</span></div><strong class="etf-ranking-score {ret_cls}">{score_text}</strong></div>
  <div class="etf-ranking-ranks">{"".join(rank_badges)}</div>
  <div class="etf-performance-compare">
    <div class="etf-performance-head"><b>績效對照（本期間）</b><span>價格報酬不含配息</span></div>
    <div class="etf-performance-grid">
      <div class="etf-performance-item"><span>ETF 價格報酬</span><strong class="{ret_cls}">{esc(ret_text)}</strong></div>
      <div class="etf-performance-item"><span>同期大盤</span><strong>{esc(market_text)}</strong></div>
      <div class="etf-performance-item"><span>超額報酬</span><strong class="etf-excess-{performance_direction}">{esc(excess_text)}</strong></div>
    </div>
    <div class="etf-performance-verdict etf-excess-{performance_direction}">{esc(performance_verdict)}</div>
  </div>
  <div class="etf-ranking-distribution"><b>配息資料</b><span>{esc(distribution_text)}</span></div>
  <div class="etf-ranking-support"><span><em>風險</em><b>{esc(risk_text)}</b></span><span><em>資產規模</em><b>{esc(asset_text)}</b></span><span><em>受益人次</em><b>{esc(holders_text)}</b></span><span><em>年初至今均成交</em><b>{esc(turnover_text)}</b></span></div>
  <div class="etf-ranking-comment">{esc(str(row.get("comment") or "資料整理完成，請搭配觀測期間判讀。"))}<small>{esc(breakdown_text)}</small></div>
  <div class="etf-ranking-provenance"><span>{esc(metric_date_text)}</span><span>{esc(observation_text)}</span></div>
</div>''')
        visible_html = "".join(rendered[:3])
        hidden_html = "".join(rendered[3:])
        ordered_count = len(ordered_items)
        missing_count = len(missing_rows)
        more_html = (f'<details class="etf-ranking-more">'
                     f'<summary>查看其餘 {ordered_count - 3} 檔（預設收合）</summary>'
                     f'<div class="etf-ranking-more-body">{hidden_html}</div></details>'
                     if hidden_html else '')
        missing_note = (f'另有 {missing_count} 檔因該排序欄位待確認／不適用而置後。'
                        if missing_count else '')
        extra_note = (f'<div class="etf-ranking-more-note">本期間共 {ordered_count} 檔符合資料門檻；'
                      f'目前依{esc(selected_sort_label)}{esc(selected_arrow)}排序，預設顯示前 3 名，其餘 {ordered_count - 3} 檔收合。'
                      f'{esc(missing_note)}</div>'
                      if ordered_count > 3 else '')
        sections.append(f'<div class="etf-ranking-period"><div class="etf-ranking-period-head"><h3>{esc(label)}</h3><span>依{esc(selected_sort_label)}{esc(selected_arrow)}・{esc(selected_direction)}</span></div>{visible_html}{more_html}{extra_note}</div>')

    return f'''<section class="etf-ranking-card">
  <div class="etf-ranking-head"><h2>{esc(category_label)}商品排名</h2><span>大盤資料日 {esc(str(market_date))}</span></div>
  <p class="etf-ranking-note">{esc(source_note)}　名次只在「{esc(category_label)}」類別內比較，分數不可跨類別解讀；上市未滿期間者顯示資料不足，不和其他期間混比。</p>
  <div class="etf-ranking-rank-guide"><b>本頁可依四種指標排序：</b>綜合分數、價格報酬率、超額報酬、殖利率／年化配息率。預設綜合分數由高到低；殖利率只比較官方資料完整者，待確認／不適用會排在最後。</div>
  <div class="etf-ranking-sortbar"><span class="etf-ranking-sort-title">排序</span>{sort_link("score", "綜合分數")}{sort_link("return", "價格報酬率")}{sort_link("excess", "超額報酬")}{sort_link("yield", "殖利率／年化配息率")}<small>目前：{esc(selected_sort_label)} {selected_arrow}（{esc(selected_direction)}）</small></div>
  <details class="etf-score-method"><summary>查看 {esc(category_label)} 評分方式（預設收合）</summary>
    <div>{esc(_etf_score_method_text(category_label))}。各項先在同類別、同期間內做百分位比較；官方配息金額空白或未核實時，配息數值維持待確認，不會假設為 0%。高股息的配息因子使用截至資料日近 12 個月已發生現金配息總額／期末價格，並非含息總報酬。</div>
  </details>
  {"".join(sections)}
</section>'''


def render_etf_inline_detail(code, meta, quote=None, ranking_row=None):
    """在 ETF 商品卡片下方呈現預設收合詳情，並可帶入既有排名快照的同期大盤對照。"""
    esc = html.escape
    code = str(code or "").strip().upper()
    name = str(meta.get("name") or code)
    policy = _etf_distribution_label(meta.get("distribution_policy"))
    lines = [
        f'<div class="etf-inline-grid">'
        f'<div><span>管理方式</span><b>{esc(str(meta.get("management_style") or "待確認"))}</b></div>'
        f'<div><span>策略分類</span><b>{esc(str(meta.get("category") or "待分類"))}</b></div>'
        f'<div><span>資產類別</span><b>{esc(str(meta.get("asset_class") or "待確認"))}</b></div>'
        f'<div><span>配息政策</span><b>{esc(policy)}</b></div>'
        f'<div><span>上市日</span><b>{esc(str(meta.get("listing_date") or "待確認"))}</b></div>'
        f'<div><span>發行人</span><b>{esc(str(meta.get("issuer") or "待確認"))}</b></div>'
        f'<div><span>追蹤基準</span><b>{esc(str(meta.get("benchmark") or "待確認"))}</b></div>'
        f'</div>'
    ]
    if meta.get("distribution_frequency"):
        lines.append(f'<div class="etf-inline-note">配息頻率：{esc(str(meta["distribution_frequency"]))}</div>')
    if meta.get("policy_note"):
        lines.append(f'<div class="etf-inline-note">配息備註：{esc(str(meta["policy_note"]))}</div>')
    if meta.get("classification_basis"):
        lines.append(f'<div class="etf-inline-note">歸類依據：{esc(str(meta["classification_basis"]))}</div>')

    distribution_records = meta.get("distribution_records") or []
    if distribution_records:
        latest = distribution_records[0]
        latest_amount = latest.get("amount")
        latest_date = latest.get("ex_date") or "未標日期"
        latest_text = (f"最近一次除息日 {latest_date}・每單位 {float(latest_amount):.4f} 元"
                       if latest_amount is not None else f"最近一次除息日 {latest_date}・金額待確認")
        lines.append(f'<div class="etf-inline-note">官方配息：{esc(latest_text)}・已核實 {len(distribution_records)} 筆</div>')
        recent_text = _format_recent_distribution_records(
            _etf_recent_distribution_records(meta, limit=4), multiline=False)
        lines.append(f'<div class="etf-inline-note">{esc(recent_text)}</div>')
    elif meta.get("distribution_policy") == "non_distributing":
        lines.append('<div class="etf-inline-note">官方商品資料標示為不分配／累積型；現金配息不適用。</div>')
    else:
        lines.append('<div class="etf-inline-note">官方已發生配息金額：待確認；空白不視為 0 元。</div>')

    if quote:
        close = quote.get("close")
        pct = quote.get("pct")
        if close is not None:
            close_text = f"{float(close):,.2f}"
            change_text = f"（{float(pct):+.2f}%）" if pct is not None else "（漲跌資料不足）"
            lines.append(f'<div class="etf-inline-quote">最新價格：<b>{close_text}</b> {esc(change_text)}</div>')
        if quote.get("volume") is not None:
            lines.append(f'<div class="etf-inline-note">成交量：{int(float(quote["volume"]) / 1000):,} 張</div>')
        closes = [float(value) for value in (quote.get("closes") or []) if value not in (None, 0)]
        close_dates = quote.get("close_dates") or []
        if distribution_records and close is not None:
            end_date = _parse_history_date(close_dates[-1]) if close_dates else taiwan_today()
            trailing = _etf_trailing_distribution_metrics(meta, end_date, close)
            if trailing.get("status") == "verified":
                lines.append(f'<div class="etf-inline-note">近12個月官方現金配息：{float(trailing["amount"]):.2f} 元・參考殖利率 {float(trailing["yield_pct"]):.2f}%（以期末價格估算；非含息總報酬）</div>')
            elif trailing.get("status") == "partial":
                observed = trailing.get("observed_yield_pct")
                observed_text = (f'・觀察期率 {float(observed):.2f}%'
                                 if observed is not None else '')
                lines.append(
                    f'<div class="etf-inline-note">已發生現金配息：{float(trailing["amount"]):.2f} 元・'
                    f'官方紀錄覆蓋 {int(trailing.get("coverage_days") or 0)} 日{esc(observed_text)}；'
                    f'未滿 12 個月，暫不年化、不列入年化配息排名</div>')
        if distribution_records:
            stability = _etf_distribution_stability_metrics(meta, end_date, close)
            if stability.get("stability_status") == "verified_four_records":
                lines.append(
                    f'<div class="etf-inline-note">評分用穩定殖利率：{float(stability["score_yield_pct"]):.2f}%・'
                    f'近4次中位數／平均值調整；原始年化值僅作參考</div>')
            else:
                lines.append('<div class="etf-inline-note">評分用穩定殖利率：待近4次官方配息資料完整</div>')
        if ranking_row:
            performance = _etf_performance_comparison(ranking_row)
            period_label = ranking_row.get("period_label") or "排名觀測期"
            lines.append(
                f'<div class="etf-inline-performance">'
                f'<div class="etf-inline-performance-head"><b>報酬對照｜{esc(str(period_label))}</b>'
                f'<span>價格報酬不含配息</span></div>'
                f'<div class="etf-inline-performance-grid">'
                f'<div><span>ETF 價格報酬</span><b class="{performance["direction"]}">{esc(performance["return_text"])}</b></div>'
                f'<div><span>同期大盤</span><b>{esc(performance["market_text"])}</b></div>'
                f'<div><span>超額報酬</span><b class="{performance["direction"]}">{esc(performance["excess_text"])}</b></div>'
                f'</div><strong class="etf-inline-verdict {performance["direction"]}">{esc(performance["verdict_text"])}</strong></div>')
        if len(closes) >= 2 and closes[0] > 0:
            return_pct = (closes[-1] / closes[0] - 1) * 100
            period = f"{close_dates[0]} 至 {close_dates[-1]}" if close_dates else "可取得價格期間"
            lines.append(f'<div class="etf-inline-note">可取得期間價格變化：{return_pct:+.2f}%・觀測期間：{esc(str(period))}</div>')
        drawdown = _etf_price_drawdown_summary(closes, close_dates)
        if drawdown:
            dd = float(drawdown.get("max_drawdown") or 0)
            recovery_date = drawdown.get("recovery_date")
            if recovery_date:
                recovery = f"已於 {recovery_date} 回到前高"
            elif dd:
                recovery = "截至目前尚未回到前高"
            else:
                recovery = "觀測期間尚無明顯回撤"
            lines.append(f'<div class="etf-inline-note">可取得價格期間最大回撤：{dd:+.1f}%・{esc(recovery)}</div>')
    else:
        lines.append('<div class="etf-inline-note">目前只先顯示官方商品資料；完整行情尚在載入或暫時無法取得。</div>')

    return (
        f'<details class="etf-inline-detail">'
        f'<summary>查看 {esc(code)} {esc(name)} 詳情</summary>'
        f'<div class="etf-inline-detail-body">{"".join(lines)}</div>'
        f'</details>'
    )


def _etf_route_guard(func):
    """ETF fragment 的最後防線：單一來源異常不得把整頁變成 HTTP 500。"""
    def guarded(uid):
        try:
            return func(uid)
        except Exception as exc:
            print(f"❌ ETF 專區路由失敗: {type(exc).__name__}: {exc}")
            body = '''<section class="etf-ranking-card etf-ranking-empty">
  <h2>ETF 專區暫時無法完成</h2>
  <p>官方商品、配息或行情資料來源目前未完整回應；本頁沒有用虛構數字補值。請稍後重新整理，若仍失敗請查看 Render Logs。</p>
</section>'''
            return respond_page("ETF 專區", body, "screener")
    guarded.__name__ = getattr(func, "__name__", "web_etf")
    guarded.__doc__ = getattr(func, "__doc__", None)
    return guarded


@app.route("/web/etf")
@web_login_required
@_etf_route_guard
def web_etf(uid):
    """ETF 專區第一版：四類入口與已核實商品清單，不混入個股選股排名。"""
    if not wants_fragment():
        return render_loading_shell(
            "ETF 專區", "screener",
            ["正在讀取 ETF 商品資料…", "正在取得最新價格…", "正在整理配息政策與資料成熟度…"],
            note="ETF 依商品策略分組；資料尚未核實的產品不會被硬分類。",
            staged=False)

    categories = [("active", "主動式 ETF", "主動式"),
                  ("dividend", "高股息 ETF", "高股息"),
                  ("market", "市值型 ETF", "市值型"),
                  ("theme", "主題型 ETF", "主題型"),
                  ("other", "其他／待分類", "其他")]
    selected = request.args.get("category", "market")
    valid_keys = {item[0] for item in categories}
    selected = selected if selected in valid_keys else "market"
    selected_label, selected_category = next(
        (label, category) for key, label, category in categories if key == selected)
    tabs = "".join(
        f'<a class="{"on" if key == selected else ""}" href="/web/etf?category={key}">{html.escape(label)}</a>'
        for key, label, _category in categories)

    catalog = get_etf_catalog_products()
    products = sorted(
        [(code, meta) for code, meta in catalog.items()
         if meta.get("category") == selected_category],
        key=lambda pair: (str(pair[1].get("listing_date") or ""), pair[0]),
        reverse=True)
    ranking_keys = {"active", "dividend", "market", "theme"}
    ranking_payload = None
    ranking_fresh = False
    ranking_source = ""
    if selected in ranking_keys:
        ranking_payload, ranking_fresh, ranking_source = _load_etf_product_ranking_snapshot()
        if ranking_payload is None or not ranking_fresh:
            _start_etf_product_ranking_refresh()
        sort_key = request.args.get("sort", "score").strip().lower()
        sort_order = request.args.get("order", "desc").strip().lower()
        if sort_key not in {"score", "return", "excess", "yield"}:
            sort_key = "score"
        if sort_order not in {"asc", "desc"}:
            sort_order = "desc"
        ranking_html = render_etf_product_ranking_html(
            ranking_payload, selected, selected_label.replace(" ETF", ""),
            sort_key=sort_key, sort_order=sort_order)
        if ranking_payload and not ranking_fresh:
            ranking_html = (f'<div class="etf-ranking-status">目前先顯示{html.escape(ranking_source)}的最近排名；'
                            '最新行情正在背景更新。</div>' + ranking_html)
        elif ranking_payload is None:
            ranking_html = ('<div class="etf-ranking-status">排名資料正在背景建立；'
                            '本頁先列出官方商品，稍後重新整理即可看到具體名次。</div>' + ranking_html)
    else:
        ranking_html = '''<section class="etf-ranking-card etf-ranking-empty">
  <h2>其他／待分類</h2><p>第一版不把債券、槓桿／反向、期貨與多資產商品混入四類股票型 ETF 排名。</p></section>'''

    # 排名 payload 與商品卡共用同一份類別資料；短期列在前，
    # 沒有短期排名時才用長期資料做卡片摘要，不跨類別混用。
    ranking_lookup = {}
    if isinstance(ranking_payload, dict):
        for period_key in ("short", "long"):
            period_rows = ((ranking_payload.get("categories") or {}).get(period_key) or {}).get(selected) or []
            period_label = ((ranking_payload.get("periods") or {}).get(period_key) or {}).get("label") or period_key
            for ranking_row in period_rows:
                code = str(ranking_row.get("code") or "").upper()
                if code and code not in ranking_lookup:
                    ranking_item = dict(ranking_row)
                    ranking_item["period_label"] = period_label
                    ranking_lookup[code] = ranking_item

    # CMoney 風格的上方摘要：每個數字都來自本類商品與官方／排名快照，
    # 只有存在真實數值才顯示，不以 0 代替未知。
    def overview_value(label, value, sub=""):
        return (f'<div class="etf-overview-item"><span>{html.escape(label)}</span>'
                f'<b>{html.escape(str(value))}</b>'
                f'{f"<small>{html.escape(str(sub))}</small>" if sub else ""}</div>')

    size_rows = [(code, meta) for code, meta in products
                 if (meta.get("official_metrics") or {}).get("asset_size_billion") is not None]
    size_leader = max(size_rows,
                      key=lambda pair: float((pair[1].get("official_metrics") or {}).get("asset_size_billion")),
                      default=None)
    short_rows = (((ranking_payload or {}).get("categories") or {}).get("short") or {}).get(selected) or []
    performance_leader = next((row for row in short_rows if row.get("return_rank") == 1), None)
    dividend_leader = next((row for row in short_rows if row.get("distribution_rank") == 1), None)
    overview = [overview_value("本類商品", f"{len(products)} 檔", selected_label)]
    if size_leader:
        size_code, size_meta = size_leader
        size_value = (size_meta.get("official_metrics") or {}).get("asset_size_billion")
        overview.append(overview_value("最大資產規模", f"{float(size_value):,.0f} 億",
                                       f"{size_meta.get('name') or size_code}（{size_code}）"))
    else:
        overview.append(overview_value("最大資產規模", "待確認", "TWSE 官方欄位尚未取得"))
    if performance_leader:
        perf_value = performance_leader.get("return_pct")
        perf_text = f"{performance_leader.get('name') or performance_leader.get('code')}"
        overview.append(overview_value("績效排名 #1",
                                       f"{float(perf_value):+.2f}%" if perf_value is not None else "待確認",
                                       f"{perf_text}（價格報酬）"))
    else:
        overview.append(overview_value("績效排名 #1", "待確認", "完整期間資料尚在整理"))
    if dividend_leader:
        div_value = dividend_leader.get("distribution_annualized_yield_pct")
        div_text = f"{dividend_leader.get('name') or dividend_leader.get('code')}"
        overview.append(overview_value("年化配息排名 #1",
                                       f"{float(div_value):.2f}%" if div_value is not None else "待確認",
                                       f"{div_text}（官方紀錄滿約 12 個月）"))
    else:
        overview.append(overview_value("年化配息排名 #1", "待確認", "未滿約 12 個月者不列名次"))
    overview_html = (f'<div class="etf-overview-grid">{"".join(overview)}</div>'
                     '<div class="etf-overview-note">上方摘要只在目前選定類別內比較；每檔卡片另列資產規模、價格報酬、同期大盤、配息與風險。官方商品欄位採擷取日標示，缺值維持待確認。</div>')

    # 商品池完整列出；即時價格只抓前 20 檔，並行批次取得，避免 20 檔
    # 逐檔等待 Yahoo timeout 累積成數分鐘。價格缺失只影響該卡，不影響整頁。
    price_codes = {code for code, _meta in products[:20]}
    price_codes.update(code for code in ("0050", "00981A", "009816")
                       if any(product_code == code for product_code, _meta in products))
    try:
        price_quotes = get_realtime_stocks_bulk(
            sorted(price_codes), workers=16, rng="1y", market_suffix=".TW") if price_codes else {}
    except Exception as exc:
        print(f"⚠️ ETF 商品批次行情失敗: {exc}")
        price_quotes = {}
    cards = []
    for code, meta in products:
        quote = price_quotes.get(code) if code in price_codes else None
        policy = _etf_distribution_label(meta.get("distribution_policy"))
        maturity = _etf_maturity_label(meta.get("listing_date")).replace("資料成熟度：", "")
        if quote:
            close = quote.get("close")
            pct = quote.get("pct")
            close_txt = f"{close:,.2f}" if close is not None else "—"
            pct_txt = f"{pct:+.2f}%" if pct is not None else "漲跌資料不足"
            price_html = f'<div class="etf-price">{close_txt} <span>{pct_txt}</span></div>'
        else:
            price_html = '<div class="etf-price etf-price-muted">商品資料已列出；完整行情暫缺</div>'
        ranking_row = dict(ranking_lookup.get(code) or {})
        inline_detail = render_etf_inline_detail(code, meta, quote, ranking_row)
        official_metrics = meta.get("official_metrics") or {}
        asset_size = official_metrics.get("asset_size_billion")
        asset_text = f"{float(asset_size):,.0f} 億" if asset_size is not None else "待確認"
        performance_value = ranking_row.get("return_pct")
        performance_text = (f"{float(performance_value):+.2f}%" if performance_value is not None else "待確認")
        distribution_value = ranking_row.get("distribution_annualized_yield_pct")
        distribution_observed = ranking_row.get("distribution_observed_yield_pct")
        distribution_status = ranking_row.get("distribution_status")
        if distribution_value is not None:
            distribution_text = f"{float(distribution_value):.2f}%"
        elif distribution_status == "partial" and distribution_observed is not None:
            distribution_text = f"觀察 {float(distribution_observed):.2f}%"
        elif distribution_status == "non_distributing":
            distribution_text = "不適用"
        else:
            distribution_text = "待確認"
        holders_value = official_metrics.get("holders")
        holders_text = f"{int(float(holders_value)):,}" if holders_value is not None else "待確認"
        cards.append(f'''<div class="etf-row">
  <div><b>{html.escape(str(meta.get("name") or code))}</b><span class="etf-code">{html.escape(code)}</span></div>
  <div class="etf-key-metrics"><span><em>資產規模</em><b>{html.escape(asset_text)}</b></span><span><em>績效</em><b>{html.escape(performance_text)}</b></span><span><em>{'年化殖利率' if distribution_value is not None else '殖利率'}</em><b>{html.escape(distribution_text)}</b></span><span><em>受益人次</em><b>{html.escape(holders_text)}</b></span></div>
  {price_html}
  <div class="etf-meta">上市日：{html.escape(str(meta.get("listing_date") or "待確認"))}・發行人：{html.escape(str(meta.get("issuer") or "待確認"))}</div>
  <div class="etf-meta">{html.escape(policy)}・{html.escape(maturity)}・{html.escape(str(meta.get("asset_class") or "待確認"))}</div>
  <div class="etf-meta">基準：{html.escape(str(meta.get("benchmark") or "待確認"))}</div>
  <div class="etf-meta">歸類依據：{html.escape(str(meta.get("classification_basis") or "待確認"))}</div>
  {inline_detail}
</div>''')
    if not cards:
        cards.append('<div class="etf-empty">官方清單目前沒有屬於此分類的上市商品。</div>')

    body = f'''<style>
.etf-hero{{padding:4px 0 14px}}
.etf-hero h1{{margin:0 0 6px;font-size:26px}}
.etf-note{{color:var(--ink-soft);font-size:13px;line-height:1.7}}
.etf-tabs{{display:flex;gap:8px;overflow-x:auto;padding:4px 0 12px}}
.etf-tabs a{{white-space:nowrap;padding:9px 12px;border:1px solid #dedbd2;border-radius:999px;color:var(--ink-soft);text-decoration:none;background:#fff}}
.etf-tabs a.on{{background:var(--ink);color:#fff;border-color:var(--ink);font-weight:700}}
.etf-card,.etf-detail{{background:#fff;border:1px solid #e4e1d8;border-radius:14px;padding:16px;margin:12px 0;box-shadow:0 3px 14px rgba(35,39,35,.05)}}
.etf-card h2,.etf-detail h2{{margin:0 0 10px;font-size:20px}}
.etf-row{{padding:14px 0;border-top:1px solid #eee;line-height:1.65}}
.etf-row:first-child{{border-top:0}}
.etf-row b{{font-size:17px;color:var(--ink)}}
.etf-code{{margin-left:8px;color:var(--ink-faint);font-size:13px}}
.etf-row small{{display:block;color:var(--ink-soft);font-size:12px}}
.etf-price{{margin-top:5px;font-size:20px;font-weight:700;color:var(--ink)}}
.etf-price span{{margin-left:5px;color:var(--red);font-size:15px}}
.etf-meta{{color:var(--ink-soft);font-size:12.5px;margin-top:4px}}
.etf-links{{margin-top:7px;font-size:13px}}
.etf-links a{{color:var(--brass);font-weight:700}}
.etf-empty{{color:var(--ink-soft);line-height:1.7;padding:8px 0}}
.etf-overview-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:12px 0 8px}}
.etf-overview-item{{min-width:0;background:#fff;border:1px solid #e4e1d8;border-radius:12px;padding:11px 10px;box-shadow:0 3px 14px rgba(35,39,35,.04)}}
.etf-overview-item span{{display:block;color:var(--ink-soft);font-size:11.5px;line-height:1.45}}
.etf-overview-item b{{display:block;margin-top:4px;color:var(--ink);font-size:18px;line-height:1.25;overflow-wrap:anywhere}}
.etf-overview-item small{{display:block;margin-top:4px;color:var(--ink-faint);font-size:10.5px;line-height:1.45;overflow-wrap:anywhere}}
.etf-overview-note{{color:var(--ink-faint);font-size:11.5px;line-height:1.55;margin:0 2px 10px}}
.etf-key-metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin:10px 0 4px;padding:9px 0;border-top:1px solid #eee;border-bottom:1px solid #eee}}
.etf-key-metrics span{{min-width:0;display:flex;flex-direction:column;gap:2px}}
.etf-key-metrics em{{font-style:normal;color:var(--ink-soft);font-size:11px;line-height:1.35}}
.etf-key-metrics b{{font-size:14px;line-height:1.35;overflow-wrap:anywhere}}
.etf-ranking-card{{background:#fff;border:1px solid #e4e1d8;border-radius:14px;padding:16px;margin:12px 0;box-shadow:0 3px 14px rgba(35,39,35,.05);max-width:100%;overflow:hidden;box-sizing:border-box}}
.etf-ranking-head{{display:flex;justify-content:space-between;gap:8px;align-items:baseline;flex-wrap:wrap;min-width:0}}
.etf-ranking-head h2{{margin:0;font-size:20px;line-height:1.35}}
.etf-ranking-head span{{font-size:11px;color:var(--ink-faint);white-space:normal;overflow-wrap:anywhere}}
.etf-ranking-note,.etf-ranking-status{{color:var(--ink-soft);font-size:12.5px;line-height:1.65}}
.etf-ranking-rank-guide{{margin:8px 0 10px;padding:8px 10px;background:#FAFAF7;border:1px solid #eee9dd;border-radius:8px;color:var(--ink-soft);font-size:11.5px;line-height:1.55}}
.etf-ranking-rank-guide b{{color:var(--brass)}}
.etf-ranking-status{{padding:10px 12px;background:#FAF5E9;border-left:3px solid var(--brass);border-radius:8px;margin:12px 0}}
.etf-ranking-period{{margin-top:14px}}
.etf-ranking-period-head{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:5px}}
.etf-ranking-period-head h3{{margin:0;font-size:16px;color:var(--ink)}}
.etf-ranking-period-head span{{color:var(--ink-faint);font-size:10.5px;line-height:1.4;overflow-wrap:anywhere}}
.etf-ranking-sortbar{{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:10px 0 12px;padding:9px 10px;background:#fbfaf6;border:1px solid #eee9dd;border-radius:10px}}
.etf-ranking-sort-title{{color:var(--ink-soft);font-size:11.5px;font-weight:700;margin-right:2px}}
.etf-sort-link{{display:inline-flex;align-items:center;gap:4px;padding:5px 7px;border:1px solid #e4dfd2;border-radius:999px;background:#fff;color:var(--ink-soft);font-size:11px;line-height:1.25;text-decoration:none;white-space:normal}}
.etf-sort-link b{{color:var(--brass);font-size:13px;line-height:1}}
.etf-sort-link.active{{background:#f5efe3;border-color:#d9c39b;color:var(--ink);font-weight:700}}
.etf-ranking-sortbar small{{flex-basis:100%;color:var(--ink-faint);font-size:10.5px;line-height:1.4}}
.etf-ranking-period h3{{margin:0 0 5px;font-size:16px;color:var(--ink)}}
.etf-ranking-row{{display:grid;grid-template-columns:30px minmax(0,1fr);gap:0 9px;padding:13px 0;border-top:1px solid #eee;align-items:start}}
.etf-ranking-rank{{font-size:16px;font-weight:800;color:var(--brass);padding-top:3px}}
.etf-ranking-main{{min-width:0;display:flex;justify-content:space-between;align-items:flex-start;gap:8px}}
.etf-ranking-name{{min-width:0;display:flex;align-items:baseline;gap:7px;flex-wrap:wrap}}
.etf-ranking-name b{{font-size:19px;line-height:1.35;color:var(--ink);font-weight:800;word-break:keep-all;overflow-wrap:break-word}}
.etf-ranking-name span{{color:var(--ink-soft);font-size:13px;font-weight:700;white-space:nowrap}}
.etf-ranking-score{{flex:none;font-size:20px;line-height:1.3;font-weight:800;text-align:right;white-space:nowrap}}
.etf-ranking-score.up{{color:var(--red)}}
.etf-ranking-score.down{{color:var(--green)}}
.etf-ranking-ranks{{grid-column:2;display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;min-width:0}}
.etf-ranking-ranks span{{display:inline-block;padding:3px 7px;border:1px solid #e6dfd1;border-radius:999px;background:#fcfaf4;color:var(--brass);font-size:11px;line-height:1.35;white-space:normal}}
.etf-ranking-numbers{{grid-column:2;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px 12px;margin-top:6px;min-width:0}}
.etf-ranking-numbers span{{display:block;color:var(--ink-soft);font-size:11.5px;line-height:1.5;white-space:normal;overflow-wrap:anywhere}}
.etf-performance-compare{{grid-column:2;margin-top:9px;padding:10px 11px;border:1px solid #e5ddcf;border-radius:10px;background:#fbfaf6;min-width:0}}
.etf-performance-head{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:7px}}
.etf-performance-head b{{font-size:14px;line-height:1.4;color:var(--ink)}}
.etf-performance-head span{{font-size:10.5px;line-height:1.4;color:var(--ink-faint)}}
.etf-performance-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px}}
.etf-performance-item{{min-width:0;padding:6px 7px;border-left:3px solid #d9d2c4;background:#fff}}
.etf-performance-item span{{display:block;font-size:10.5px;line-height:1.4;color:var(--ink-soft)}}
.etf-performance-item strong{{display:block;margin-top:2px;font-size:16px;line-height:1.3;font-weight:800;overflow-wrap:anywhere}}
.etf-performance-verdict{{margin-top:7px;font-size:12px;line-height:1.45;font-weight:700;overflow-wrap:anywhere}}
.etf-excess-better{{color:var(--red)}}
.etf-excess-worse{{color:var(--green)}}
.etf-excess-same,.etf-excess-unknown{{color:var(--ink-soft)}}
.etf-ranking-distribution{{grid-column:2;margin-top:8px;padding:8px 10px;border-left:3px solid #d4b16c;background:#fffdf8;min-width:0}}
.etf-ranking-distribution b{{display:block;margin-bottom:3px;color:var(--ink);font-size:13.5px;line-height:1.4}}
.etf-ranking-distribution span{{display:block;color:var(--ink-soft);font-size:12px;line-height:1.6;overflow-wrap:anywhere}}
.etf-ranking-support{{grid-column:2;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-top:8px;min-width:0}}
.etf-ranking-support span{{min-width:0;display:flex;flex-direction:column;gap:2px}}
.etf-ranking-support em{{font-style:normal;color:var(--ink-faint);font-size:10.5px;line-height:1.35}}
.etf-ranking-support b{{color:var(--ink-soft);font-size:11.5px;line-height:1.45;overflow-wrap:anywhere}}
.etf-ranking-comment{{grid-column:2;color:var(--ink-soft);font-size:12.5px;line-height:1.55;padding-top:7px;overflow-wrap:anywhere}}
.etf-ranking-comment small{{display:block;color:var(--ink-faint);font-size:10.5px;line-height:1.45;margin-top:4px;overflow-wrap:anywhere}}
.etf-ranking-provenance{{grid-column:2;display:flex;gap:8px;flex-wrap:wrap;color:var(--ink-faint);font-size:10.5px;line-height:1.45;margin-top:5px;overflow-wrap:anywhere}}
.etf-ranking-provenance span{{min-width:0;overflow-wrap:anywhere}}
.etf-ranking-more-note,.etf-ranking-empty-line{{color:var(--ink-soft);font-size:12px;line-height:1.6;padding:7px 0}}
.etf-ranking-more{{margin:4px 0 0;border-top:1px solid #eee9dd}}
.etf-ranking-more summary{{cursor:pointer;padding:10px 2px 7px;color:var(--brass);font-size:12px;font-weight:700;line-height:1.45;list-style-position:inside}}
.etf-ranking-more summary::marker{{color:var(--brass)}}
.etf-ranking-more-body{{padding:0 8px;border-left:2px solid #f0e6d2;background:#fdfcf9}}
.etf-ranking-more-body .etf-ranking-row:first-child{{border-top:0}}
.etf-ranking-more-note{{font-size:10.5px;color:var(--ink-faint);padding-top:4px}}
.etf-score-method{{margin:8px 0 12px;border:1px solid #eee9dd;border-radius:9px;padding:7px 10px;color:var(--ink-soft);font-size:11.5px;line-height:1.55;background:#fcfbf8}}
.etf-score-method summary{{cursor:pointer;color:var(--brass);font-weight:700}}
@media (max-width:480px){{.etf-overview-grid{{grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}}.etf-overview-item{{padding:9px 8px}}.etf-overview-item b{{font-size:16px}}.etf-key-metrics{{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 10px}}.etf-ranking-sortbar{{align-items:flex-start;gap:5px;padding:8px}}.etf-sort-link{{font-size:10.5px;padding:5px 6px}}.etf-ranking-period-head span{{font-size:10px}}.etf-ranking-row{{grid-template-columns:27px minmax(0,1fr);gap:0 7px}}.etf-ranking-main{{gap:5px}}.etf-ranking-name b{{font-size:18px}}.etf-ranking-name span{{font-size:12px}}.etf-ranking-score{{font-size:19px}}.etf-ranking-ranks{{gap:4px;margin-top:5px}}.etf-ranking-ranks span{{font-size:10.5px;padding:3px 6px}}.etf-performance-compare{{padding:9px 9px}}.etf-performance-head b{{font-size:13.5px}}.etf-performance-grid{{grid-template-columns:1fr;gap:5px}}.etf-performance-item{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;padding:6px 7px}}.etf-performance-item span{{font-size:11px}}.etf-performance-item strong{{font-size:17px;text-align:right}}.etf-performance-verdict{{font-size:12.5px}}.etf-ranking-distribution{{padding:8px 9px}}.etf-ranking-distribution span{{font-size:12.5px}}.etf-ranking-support{{grid-template-columns:repeat(2,minmax(0,1fr));gap:5px 8px}}.etf-ranking-numbers{{grid-template-columns:1fr;gap:1px;margin-top:5px}}.etf-ranking-comment{{font-size:12px}}.etf-ranking-provenance{{display:block}}.etf-ranking-provenance span{{display:block;margin-top:2px}}.etf-ranking-more-body{{padding:0 5px}}.etf-inline-performance{{padding:9px}}.etf-inline-performance-head b{{font-size:13.5px}}.etf-inline-performance-grid{{grid-template-columns:1fr;gap:5px}}.etf-inline-performance-grid div{{flex-direction:row;justify-content:space-between;align-items:baseline;gap:8px}}.etf-inline-performance-grid b{{font-size:17px;text-align:right}}}}
.etf-inline-detail{{margin-top:10px;border-top:1px solid #eee;padding-top:8px}}
.etf-inline-detail summary{{cursor:pointer;color:var(--brass);font-size:13px;font-weight:700;padding:4px 0}}
.etf-inline-detail-body{{margin-top:8px;padding:10px 11px;background:#faf9f5;border:1px solid #eee9dd;border-radius:10px}}
.etf-inline-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px}}
.etf-inline-grid div{{display:flex;flex-direction:column;gap:2px}}
.etf-inline-grid span,.etf-inline-note{{color:var(--ink-soft);font-size:12px;line-height:1.6}}
.etf-inline-grid b{{font-size:13px;color:var(--ink);line-height:1.55}}
.etf-inline-quote{{margin-top:8px;padding-top:8px;border-top:1px solid #e7e2d6;font-size:13px;color:var(--ink)}}
.etf-inline-performance{{margin-top:10px;padding:10px;border:1px solid #e5ddcf;border-radius:9px;background:#fffdf8}}
.etf-inline-performance-head{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:6px}}
.etf-inline-performance-head b{{font-size:14px;line-height:1.4;color:var(--ink)}}
.etf-inline-performance-head span{{font-size:10.5px;color:var(--ink-faint)}}
.etf-inline-performance-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}}
.etf-inline-performance-grid div{{min-width:0;display:flex;flex-direction:column;gap:2px;padding:5px 6px;background:#fff;border-left:3px solid #d9d2c4}}
.etf-inline-performance-grid span{{font-size:10.5px;color:var(--ink-soft);line-height:1.35}}
.etf-inline-performance-grid b{{font-size:16px;line-height:1.3;font-weight:800;overflow-wrap:anywhere}}
.etf-inline-verdict{{display:block;margin-top:6px;font-size:12px;line-height:1.45;font-weight:700;overflow-wrap:anywhere}}
.etf-inline-performance .better,.etf-inline-verdict.better{{color:var(--red)}}
.etf-inline-performance .worse,.etf-inline-verdict.worse{{color:var(--green)}}
.etf-inline-performance .same,.etf-inline-performance .unknown,.etf-inline-verdict.same,.etf-inline-verdict.unknown{{color:var(--ink-soft)}}
</style>
<div class="tabs">
  <a href="/web/screener?mode=blackhorse">黑馬</a>
  <a href="/web/screener?mode=radar">雷達</a>
  <a href="/web/chips">籌碼超人</a>
  <a href="/web/screener?mode=review">成效</a>
  <a href="/web/screener?mode=turning">轉折觀察</a>
  <a href="/web/etf" class="on">ETF 專區</a>
</div>
<div class="etf-hero"><div class="eyebrow">台股 BOT</div><h1>ETF 專區</h1><p class="etf-note">官方 TWSE 上市清單目前載入 {len(catalog)} 檔；依名稱／標的關鍵字作第一版策略分組。ETF 不套用個股黑馬、雷達或籌碼超人邏輯，未知配息政策顯示為待確認。</p></div>
<div class="etf-tabs">{tabs}</div>
{overview_html}
{ranking_html}
<section class="etf-card"><h2>{html.escape(selected_label)}（{len(products)} 檔）</h2><div class="etf-note">完整列出官方已上市商品；每檔詳情都直接放在卡片下方，預設收合，點擊「查看 ETF 詳情」即可展開。分類是可解釋規則候選，不代表官方策略認證。</div>{"".join(cards)}</section>'''
    return respond_page("ETF 專區", body, "screener")


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
  <a class="more-item" href="/web/etf"><span class="more-icon">▦</span><span><b>ETF 專區</b><small>主動式、高股息、市值型、主題型</small></span><strong>›</strong></a>
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
    # 首頁先行摘要不得把無日期／非當日的大盤快照當成今天漲跌。
    taiex_pct = (market.get("taiex_pct")
                 if _market_date_matches(market.get("taiex_date"), snapshot_date) else None)
    market_items = [("大盤", taiex_pct),
                    ("道瓊", market.get("^DJI_pct")),
                    ("那斯達克", market.get("^IXIC_pct")),
                    ("費城半導體", market.get("^SOX_pct"))]
    market_html = "".join(
        f'<span>{label}<b>{fmt_pct(value) if value is not None else "資料尚未更新"}</b></span>'
        for label, value in market_items
    )

    fast_quote_text = html.escape(_homepage_quote_for(snapshot_date))
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
.daily-fast-sync{{display:flex;gap:11px;align-items:flex-start;background:#F5F0E5;border:1px solid #D9C9A7;border-left:4px solid var(--brass);border-radius:12px;padding:14px 15px;margin:-4px 0 14px;box-shadow:0 3px 12px rgba(35,39,35,.05)}}.daily-fast-sync-dot{{width:10px;height:10px;margin-top:5px;border-radius:50%;background:var(--brass);box-shadow:0 0 0 4px rgba(139,105,52,.12);flex:none}}.daily-fast-sync b{{display:block;color:var(--ink);font-size:15px;line-height:1.35}}.daily-fast-sync-copy span{{display:block;margin-top:4px;color:var(--ink-soft);font-size:12px;line-height:1.65}}.daily-fast-hero{{background:linear-gradient(135deg,#f4f0e7,#e7ece8);padding:22px 18px 18px;margin:-8px -2px 14px;border-bottom:1px solid #d7d4ca}}.daily-fast-hero .eyebrow{{letter-spacing:.14em;color:var(--brass);font-size:11px}}.daily-fast-hero h1{{font-size:26px;line-height:1.25;margin:8px 0 14px}}.daily-fast-market{{display:flex;gap:8px;flex-wrap:wrap}}.daily-fast-market span{{background:rgba(255,255,255,.72);padding:8px 10px;border-radius:8px;font-size:12px}}.daily-fast-market b{{display:block;font-size:16px;margin-top:3px}}.daily-fast-card{{background:#fff;border:1px solid #e3e2dc;border-radius:12px;padding:15px;margin:12px 0;box-shadow:0 3px 14px rgba(35,39,35,.05)}}.daily-fast-title{{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:7px}}.daily-fast-title h2{{margin:0;font-size:19px}}.daily-fast-event{{display:flex;gap:10px;padding:11px 0;border-top:1px solid #eee}}.daily-fast-number{{background:var(--brass);color:#fff;border-radius:50%;width:22px;height:22px;text-align:center;line-height:22px;flex:none;font-size:12px}}.daily-fast-detail{{font-size:12.5px;color:var(--ink-soft);margin-top:3px}}.daily-fast-empty{{padding:11px 0;color:var(--ink-soft);font-size:13px}}.daily-fast-empty span{{font-size:12px}}.daily-fast-ranks{{display:grid;grid-template-columns:1fr;gap:0}}.daily-fast-rank{{background:transparent;border-bottom:1px solid #e3e2dc;border-radius:0;padding:9px 0}}.daily-fast-rank:last-child{{border-bottom:0}}.daily-fast-rank small,.daily-fast-rank>span{{display:block;color:var(--ink-soft);font-size:11px}}.daily-fast-rank b{{display:block;font-size:18px;margin:4px 0}}.daily-fast-panels{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:11px}}.daily-fast-panel{{min-width:0;padding:11px 10px;border:1px solid rgba(139,105,52,.24);border-radius:9px;background:rgba(255,255,255,.58)}}.daily-fast-panel-title{{display:flex;justify-content:space-between;align-items:center;gap:4px;padding-bottom:7px;border-bottom:1px solid rgba(139,105,52,.16)}}.daily-fast-panel-title b{{font-size:12px}}.daily-fast-panel-title a,.daily-fast-panel-title span{{font-size:8px;color:var(--brass);white-space:nowrap}}.daily-fast-index-row{{display:flex;justify-content:space-between;align-items:center;gap:4px;padding:8px 0;border-bottom:1px solid #e3e2dc}}.daily-fast-index-row:last-child{{border-bottom:0}}.daily-fast-index-row b{{font-size:10px}}.daily-fast-index-row span{{font-size:9px;text-align:right;white-space:nowrap}}.daily-fast-index-row strong{{display:block;font-size:10px;color:var(--ink)}}.daily-fast-index-row em{{font-style:normal;font-size:9px}}@media (max-width:640px){{.daily-fast-panels{{grid-template-columns:1fr}}}}.daily-fast-summary-stack{{display:block;margin-top:11px}}.daily-fast-rank-panel{{padding:10px 13px}}.daily-fast-rank-panel .daily-fast-panel-title{{padding-bottom:6px}}.daily-fast-rank-panel .daily-fast-ranks{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.daily-fast-rank-panel .daily-fast-rank{{border:0;padding:8px 0}}.daily-fast-rank-panel .daily-fast-rank b{{font-size:16px;margin:3px 0}}.daily-fast-quote{{display:flex;align-items:center;justify-content:center;gap:12px;margin-top:8px;padding:12px 13px;border:1px solid rgba(139,105,52,.22);border-radius:9px;background:#F8F5ED}}.daily-fast-quote strong{{color:var(--ink);font-family:"Kaiti TC","BiauKai","DFKai-SB","STKaiti","Noto Serif CJK TC","Noto Serif TC",serif;font-size:16px;font-weight:700;letter-spacing:.12em;line-height:1.5;text-align:center;white-space:nowrap}}@media (max-width:640px){{.daily-fast-rank-panel .daily-fast-ranks{{gap:6px}}.daily-fast-quote{{align-items:center;display:flex}}.daily-fast-quote strong{{display:block;margin-top:0;text-align:center;font-size:17px}}}}
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
<div class="daily-fast-summary-stack"><section class="daily-fast-panel daily-fast-rank-panel"><div class="daily-fast-panel-title"><b>🏆 我的排名</b><a href="/web/leaderboard">查看完整榜單 →</a></div><div class="daily-fast-ranks">{"".join(rank_html)}</div></section><section class="daily-fast-quote"><strong>{fast_quote_text}</strong></section></div>'''


def render_daily_home_top(uid, holdings, total_value, total_cost, price_map, pl_total,
                           taiex=None, position_journal_html="", daily_context=None,
                           rank_status=None):


    # 新版首頁上半部：先講今天，再提供完整分析入口。
    calendar_today = taiwan_today()
    display_date = _premarket_display_date(calendar_today)
    has_context = (isinstance(daily_context, dict)
                   and ("snapshot" in daily_context or "timeline" in daily_context))
    context = (daily_context if has_context
               else _get_daily_home_context(uid, display_date)) or {}
    display_snapshot = context.get("snapshot") or {}
    timeline = context.get("timeline") or {}
    signal_state = _daily_signal_state(display_snapshot, timeline)
    events = (timeline.get("new", []) + timeline.get("ongoing", []))[:3]
    taiex = (fetch_taiex_summary() if taiex is None else taiex) or {}
    # 大盤日K必須是首頁顯示交易日，否則寧可顯示資料尚未更新，也不能用昨天數字。
    market_is_current = _market_date_matches(taiex.get("date"), display_date)
    market_pct = None
    if market_is_current:
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
    # 使用最近兩次已保存的收盤快照；若外層已平行取得就直接重用。
    if rank_status is None:
        rank_started = time.monotonic()
        rank_status = get_fast_rank_summary(uid)
        print("⏱️ 今日完整頁：排名摘要 %.0fms" % ((time.monotonic() - rank_started) * 1000))

    def timeline_rows(items, status_label, status_class, start=1):
        rows = []
        for idx, raw_event in enumerate(items or [], start):
            event = raw_event if isinstance(raw_event, dict) else {}
            level = html.escape(str(event.get("severity") or "B"))
            title = html.escape(str(event.get("title") or ""))
            detail = html.escape(str(event.get("detail") or ""))
            rows.append(f'''<div class="daily-event timeline-{status_class} level-{level}">
              <span class="event-status">{status_label}</span>
              <div><b>{title}</b>
              <div class="event-detail">{detail}</div></div>
            </div>''')
        return "".join(rows)

    # 首頁與盤前完整頁採同一資訊層級：先給最高優先級 3 項，
    # 其餘資料不刪除，放進未預設開啟的 details，避免首頁被 20～30 項事件淹沒。
    new_events = [event for event in (timeline.get("new") or [])
                  if isinstance(event, dict)]
    ongoing_events = [event for event in (timeline.get("ongoing") or [])
                      if isinstance(event, dict)]
    resolved_events = [event for event in (timeline.get("resolved") or [])
                       if isinstance(event, dict)]
    current_events = new_events + ongoing_events
    visible_events = current_events[:3]
    visible_new_count = min(len(new_events), len(visible_events))
    timeline_html = timeline_rows(visible_events[:visible_new_count], "新", "new")
    timeline_html += timeline_rows(visible_events[visible_new_count:], "續", "ongoing",
                                   visible_new_count + 1)

    visible_ongoing_count = max(0, len(visible_events) - visible_new_count)
    extra_new_events = new_events[visible_new_count:]
    extra_ongoing_events = ongoing_events[visible_ongoing_count:]
    extra_count = len(extra_new_events) + len(extra_ongoing_events) + len(resolved_events)
    extra_html = timeline_rows(extra_new_events, "新", "new", visible_new_count + 1)
    extra_html += timeline_rows(extra_ongoing_events, "續", "ongoing", visible_ongoing_count + 1)
    if resolved_events:
        if extra_html:
            extra_html += '<div class="timeline-divider">✓ 昨日事件已解除</div>'
        else:
            extra_html = '<div class="timeline-divider">✓ 昨日事件已解除</div>'
        extra_html += timeline_rows(resolved_events, "解", "resolved")
    if extra_html:
        events_html = timeline_html
        if events_html:
            events_html += (f'<details class="home-more-events">'
                            f'<summary>查看其餘 {extra_count} 個變化</summary>'
                            f'{extra_html}</details>')
        else:
            events_html = (f'<div class="daily-empty"><b>今日沒有新的重大提醒</b></div>'
                           f'<details class="home-more-events">'
                           f'<summary>查看其餘 {extra_count} 個變化</summary>'
                           f'{extra_html}</details>')
    elif timeline_html:
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
    market_text = fmt_pct(market_pct) if market_pct is not None else "資料尚未更新"
    market_status_text = (f"大盤已同步 {taiex.get('date')} 日K"
                          if market_pct is not None else "大盤資料日未確認，未使用舊快照")
    portfolio_text = fmt_pct(portfolio_pct) if portfolio_pct is not None else fmt_pct(pl_total)
    relative_text = fmt_pct(relative) if relative is not None else "—"

    quote_text = _homepage_quote_for(display_date)

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
    quote_rows = [h.get("price") for h in holdings
                  if isinstance(h.get("price"), dict)]
    final_quote_rows = [p for p in quote_rows if p.get("close_is_final")]
    if _taiwan_post_close() and quote_rows:
        if len(final_quote_rows) == len(quote_rows):
            close_sync_note = (f"收盤後已同步官方最後成交／市撮價：{len(final_quote_rows)} 檔，"
                               "資料時間以各檔回傳的 13:30 收盤撮合為準。")
        elif final_quote_rows:
            close_sync_note = (f"收盤資料部分同步：官方最後成交／市撮價 {len(final_quote_rows)}／"
                               f"{len(quote_rows)} 檔，其餘資料待官方回傳，未把盤中價標成正式收盤。")
        else:
            close_sync_note = ("官方收盤資料尚未回傳；目前不把盤中報價冒充正式收盤，"
                               "請稍後重新整理。")
    else:
        close_sync_note = "盤中行情會隨市場更新；收盤後切換至官方最後成交／市撮價。"

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
  <details class="impact-details" open><summary>查看正貢獻明細（{positive_count} 檔）</summary>
    {contribution_detail_rows(positive_entries, 'up')}
  </details>
  <details class="impact-details" open><summary>查看負貢獻明細（{negative_count} 檔）</summary>
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
.daily-complete-sync{{display:flex;gap:11px;align-items:flex-start;background:#F5F0E5;border:1px solid #D9C9A7;border-left:4px solid var(--brass);border-radius:12px;padding:13px 15px;margin:-4px 0 14px;box-shadow:0 3px 12px rgba(35,39,35,.05)}}.daily-complete-sync-dot{{width:10px;height:10px;margin-top:5px;border-radius:50%;background:#087A4B;box-shadow:0 0 0 4px rgba(8,122,75,.12);flex:none}}.daily-complete-sync b{{display:block;color:var(--ink);font-size:15px;line-height:1.35}}.daily-complete-sync span{{display:block;margin-top:4px;color:var(--ink-soft);font-size:12px;line-height:1.6}}.daily-hero{{background:#f7fbff;padding:26px 24px 22px;margin:-8px -2px 18px;border:1px solid #d7e0ea;border-radius:14px;margin-top:0;box-shadow:0 3px 14px rgba(35,39,35,.04)}}.daily-hero .eyebrow{{letter-spacing:.16em;color:var(--brass);font-size:12px}}.daily-hero h1{{font-size:30px;line-height:1.2;margin:10px 0 18px}}.market-strip{{display:flex;gap:12px;flex-wrap:wrap}}.market-strip span{{background:rgba(255,255,255,.7);padding:9px 11px;border-radius:8px;font-size:13px}}.market-strip b{{display:block;font-size:18px;margin-top:3px}}.market-freshness{{display:block;margin-top:4px;color:var(--ink-soft);font-size:9px;line-height:1.35}}.daily-card{{background:#fff;border:1px solid #e3e2dc;border-radius:12px;padding:18px;margin:14px 0;box-shadow:0 3px 14px rgba(35,39,35,.05)}}.attention-card{{border-left:4px solid var(--brass)}}.daily-section-title{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}}.daily-section-title h2{{margin:0;font-size:20px}}.daily-section-title a{{font-size:13px;color:var(--brass)}}.daily-event{{display:flex;gap:11px;padding:12px 0;border-top:1px solid #eee}}.home-more-events{{margin-top:10px;border-top:1px solid #eee;padding-top:9px}}.home-more-events>summary{{cursor:pointer;color:var(--brass);font-size:13px;font-weight:700;padding:5px 0}}.event-number{{background:var(--brass);color:#fff;border-radius:50%;width:24px;height:24px;text-align:center;line-height:24px;flex:none}}.event-status{{min-width:28px;height:22px;padding:2px 5px;border-radius:7px;text-align:center;font-size:11px;font-weight:700;line-height:18px;flex:none;background:#eee;color:var(--ink-soft)}}.timeline-new .event-status{{background:#FCE9E6;color:var(--up)}}.timeline-ongoing .event-status{{background:#F3EEE1;color:var(--brass)}}.timeline-resolved .event-status{{background:#E8F2EA;color:var(--down)}}.timeline-divider{{margin:14px 0 0;padding-top:12px;border-top:1px solid #eee;color:var(--ink-soft);font-size:12px;font-weight:600}}.event-detail{{font-size:13px;color:var(--ink-soft);margin-top:4px}}.daily-empty{{padding:16px 0;color:var(--ink-soft)}}.daily-empty span{{font-size:13px}}.portfolio-highlights{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}.portfolio-highlights>div{{background:#f5f5f1;padding:12px;border-radius:8px}}.portfolio-highlights small{{display:block;color:var(--ink-soft);font-size:12px}}.portfolio-highlights b{{display:block;margin-top:6px;font-size:17px}}.positive,.up{{color:var(--up)}}.negative,.down{{color:var(--down)}}.flat{{color:var(--ink-soft)}}.rank-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}.rank-mini{{background:#f5f5f1;border-radius:8px;padding:11px;min-width:0}}.rank-mini span,.rank-mini small{{display:block;color:var(--ink-soft);font-size:11px}}.rank-mini b{{display:block;font-size:18px;margin:5px 0}}.rank-mini em{{font-style:normal;font-size:12px}}.hero-summary-panels{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:14px}}.hero-summary-panel{{min-width:0;padding:13px;border:1px solid rgba(139,105,52,.25);border-radius:11px;background:rgba(255,255,255,.58);box-shadow:0 2px 8px rgba(35,39,35,.04)}}.hero-summary-panel-head{{display:flex;justify-content:space-between;align-items:center;gap:6px;min-height:27px;padding-bottom:8px;border-bottom:1px solid rgba(139,105,52,.18)}}.hero-summary-panel-head b{{font-size:14px}}.hero-summary-panel-head a{{font-size:10px;color:var(--brass);white-space:nowrap}}.hero-summary-panel-head span{{font-size:10px;color:var(--ink-soft);white-space:nowrap}}.hero-summary-stack{{display:block;margin-top:14px}}.hero-summary-stack .hero-summary-panel{{padding:10px 13px}}.hero-rank-panel .rank-grid{{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.hero-rank-panel .rank-mini{{background:transparent;border:0;border-right:1px solid rgba(227,226,220,.88);border-radius:0;padding:8px 10px 8px 0}}.hero-rank-panel .rank-mini:last-child{{border-right:0;padding-right:0;padding-left:10px}}.hero-summary-stack .hero-summary-panel-head{{min-height:0;padding-bottom:6px}}.hero-summary-stack .hero-summary-panel-head b{{font-size:13px}}.hero-quote-panel{{margin-top:8px;border-color:rgba(139,105,52,.22);background:#F8F5ED}}.hero-quote-text{{margin:8px 0 1px;color:var(--ink);font-family:"Kaiti TC","BiauKai","DFKai-SB","STKaiti","Noto Serif CJK TC","Noto Serif TC",serif;font-size:19px;font-weight:700;line-height:1.5;text-align:center;letter-spacing:.12em;white-space:nowrap}}.risk-collapse{{margin:16px 0}}.risk-collapse>summary{{cursor:pointer;color:var(--brass);font-weight:600;padding:8px 0}}.risk-collapse .card{{margin-top:10px}}.home-judgement-card{{padding:16px 15px}}.judgement-row{{display:flex;justify-content:space-between;gap:12px;padding:9px 0;border-top:1px solid #ECEDE8;font-size:13px}}.judgement-row:first-of-type{{border-top:0}}.judgement-row span{{color:var(--ink-soft)}}.judgement-row b{{text-align:right}}.home-judgement-copy{{margin:10px 0 0;padding-top:10px;border-top:1px solid #ECEDE8;color:var(--ink-soft);font-size:12px;line-height:1.65}}.home-detail-collapse{{margin:14px 0;border:1px solid #E3E2DC;border-radius:12px;background:#fff;box-shadow:0 3px 14px rgba(35,39,35,.04)}}.home-detail-collapse>summary{{cursor:pointer;padding:14px 16px;color:var(--brass);font-size:13px;font-weight:700}}.home-detail-collapse .contribution-card{{margin:0;border:0;border-top:1px solid #ECEDE8;border-radius:0;box-shadow:none}}@media(max-width:640px){{.portfolio-highlights{{grid-template-columns:1fr 1fr}}.portfolio-highlights>div:last-child{{grid-column:span 2}}.impact-leads{{grid-template-columns:1fr}}.rank-grid{{grid-template-columns:1fr}}.hero-summary-stack{{gap:7px}}.hero-summary-panel{{padding:10px 11px}}.hero-summary-panel-head b{{font-size:12px}}.hero-summary-panel-head a,.hero-summary-panel-head span{{font-size:8px}}.rank-mini span,.rank-mini small{{font-size:10px}}.rank-mini b{{font-size:16px}}.hero-quote-text{{font-size:18px;text-align:center}}.daily-hero h1{{font-size:26px}}}}
.daily-interpretation{{padding:11px 14px;margin:-4px 0 14px;border-left:3px solid var(--brass);background:rgba(255,255,255,.6);color:var(--ink-soft);font-size:13px;line-height:1.65}}.daily-interpretation-label{{font-size:11px;color:var(--brass);font-weight:700;letter-spacing:.08em;margin-bottom:3px}}.contribution-card{{padding:16px 15px}}.contribution-card .daily-section-title span{{font-size:11px;color:var(--ink-faint)}}.impact-sentence{{margin:0 0 12px;color:var(--ink-soft);font-size:13px;line-height:1.6}}.impact-leads{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.impact-lead{{padding:13px;border-radius:11px;background:#F7F7F3;border:1px solid #ECEDE8}}.impact-lead small{{display:block;font-size:11px;font-weight:700;color:var(--ink-soft)}}.impact-lead h3{{font-size:20px;line-height:1.3;margin:7px 0 4px;overflow-wrap:anywhere}}.impact-lead p{{margin:0 0 8px;color:var(--ink-soft);font-size:11px}}.impact-lead strong{{font-size:12px}}.impact-up{{border-color:#EDC7C2;background:#FFF7F5}}.impact-up strong{{color:var(--up)}}.impact-down{{border-color:#C9DFD0;background:#F4FBF5}}.impact-down strong{{color:var(--down)}}.impact-muted{{color:var(--ink-faint)}}.impact-details{{margin-top:10px;border-top:1px solid #ECEDE8}}.impact-details summary{{padding:11px 2px 4px;cursor:pointer;color:var(--brass);font-size:12px;font-weight:600}}.impact-detail-row{{display:flex;align-items:center;gap:8px;padding:9px 2px;border-top:1px solid #F0F0EC}}.impact-rank{{width:20px;height:20px;border-radius:50%;background:#F0EEE8;color:var(--ink-soft);font-size:11px;text-align:center;line-height:20px;flex:none}}.impact-detail-name{{min-width:0;flex:1}}.impact-detail-name b{{display:block;font-size:14px;overflow-wrap:anywhere}}.impact-detail-name small{{display:block;color:var(--ink-soft);font-size:10.5px;margin-top:2px}}.impact-detail-row strong{{font-size:12px;white-space:nowrap}}.impact-empty{{padding:9px 2px;color:var(--ink-faint);font-size:11px}}.contribution-footnote{{margin-top:10px;color:var(--ink-faint);font-size:10.5px;line-height:1.55}} </style><div class="daily-complete-sync" aria-live="polite">
  <span class="daily-complete-sync-dot" aria-hidden="true"></span>
  <div><b>今日資料已整合完成</b><span>即時行情、今日事件與排名摘要已載入；詳細貢獻明細可往下展開查看。</span></div>
</div><section class="daily-hero">
  <div class="eyebrow">{hero_eyebrow}</div>
  <h1>今天你的投資發生了什麼？</h1>
  <div class="market-strip"><span>大盤 <b>{market_text}</b><small class="market-freshness">{html.escape(market_status_text)}</small></span><span>你的組合 <b>{portfolio_text}</b></span><span>相對大盤 <b>{relative_text}</b></span></div>
  <div class="hero-summary-stack"><section class="hero-summary-panel hero-rank-panel"><div class="hero-summary-panel-head"><b>🏆 我的排名</b><a href="/web/leaderboard">查看完整榜單 →</a></div><div class="rank-grid">{rank_line('short')}{rank_line('long')}</div></section><section class="hero-quote-panel"><p class="hero-quote-text">{html.escape(quote_text)}</p></section></div>
</section>
{position_journal_html}
<section class="daily-card daily-close-status" data-close-sync="official-mis-v20260825"><div class="daily-section-title"><h2>收盤資料</h2><span>與持股頁同口徑</span></div><p>{html.escape(close_sync_note)}</p></section>
<section class="daily-card attention-card" data-home-events="top3-collapsed-v20260825">
<div class="daily-section-title"><h2>🔥 今日值得注意</h2><a href="/web/premarket">查看完整變化 →</a></div>{events_html}</section>
<section class="daily-card"><div class="daily-section-title"><h2>我的組合今天怎麼了？</h2><span>即時報價</span></div><div class="portfolio-highlights"><div><small>最大貢獻</small><b class="positive">{gain_html}</b></div><div><small>最大拖累</small><b class="negative">{loss_html}</b></div><div><small>總市值</small><b>{total_value:,.0f}</b></div></div></section>
{home_judgement_html}
<details class="home-detail-collapse" id="home-contribution" open><summary>查看完整正／負貢獻明細</summary>{contribution_html}</details>'''


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
        trend_html_empty = render_trend_chart(get_portfolio_snapshots(uid, days=120))
        body = risk_card + f"""
<div class="empty">還沒有持股紀錄。<br><br>
<a href="/web/positions" style="color:var(--brass)">先去新增持股 →</a></div>"""
        if trend_html_empty:
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
        # 今日事件上下文與五份共享資料互相獨立；併行取得後，
        # render_daily_home_top 不必在完整頁尾端再次查詢同一份快照。
        ("今日事件", lambda: _get_daily_home_context(uid, taiwan_today())),
    ]
    with ThreadPoolExecutor(max_workers=len(shared_loaders)) as ex:
        shared_values = list(ex.map(
            lambda item: safe_shared_loader(item[0], item[1]), shared_loaders))
    inst, revenue, valuation, ind_map, taiex, daily_context = shared_values
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

    # 走勢、操作日誌與首頁排名摘要彼此獨立，尾端同時查詢，
    # 避免三個約 0.6 秒的 I/O 依序堆疊在完整頁回應時間上。
    aux_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=3) as aux_executor:
        trend_future = aux_executor.submit(
            lambda: render_trend_chart(get_portfolio_snapshots(uid, days=120)))
        journal_future = aux_executor.submit(
            get_position_change_logs, uid, 5000)
        rank_future = aux_executor.submit(get_fast_rank_summary, uid)
        trend_html = trend_future.result()
        journal_logs = journal_future.result()
        page_rank_status = rank_future.result()
    aux_done = time.monotonic()
    trend_done = aux_done
    journal_dates = [_position_change_date(log.get("trade_date")) for log in journal_logs]
    latest_journal_date = max((d for d in journal_dates if d), default=None)
    journal_html = render_position_change_journal(
        uid, current_positions=positions, price_map=price_map, inst_data=inst,
        logs=journal_logs, trade_date=latest_journal_date, display_limit=20)

    daily_top_started = time.monotonic()
    daily_top = render_daily_home_top(uid, holdings, total_value, total_cost,
                                      price_map, pl_total, taiex=taiex,
                                      position_journal_html=journal_html,
                                      daily_context=daily_context,
                                      rank_status=page_rank_status)
    daily_top_done = time.monotonic()
    print("⏱️ 今日完整頁：共享 %.0fms、持股行情 %.0fms、組合計算 %.0fms、走勢／日誌／排名並行 %.0fms、首頁判讀 %.0fms、合計 %.0fms" % (
        (shared_done - full_started) * 1000,
        (price_done - shared_done) * 1000,
        (calc_done - price_done) * 1000,
        (aux_done - calc_done) * 1000,
        (daily_top_done - daily_top_started) * 1000,
        (daily_top_done - full_started) * 1000))
    allocation_html = render_portfolio_allocation_chart(holdings)
    body = f"""
{daily_top}
<details class="risk-collapse"><summary>查看我的風險輪廓</summary>
{risk_card}
</details>
<div class="section-head"><h2>完整組合分析</h2><span class="section-note">往下查看詳細資料</span></div>
<div class="totals"><div><div class="total-label">總市值</div><div class="total-value num">{total_value:,.0f}</div><div class="total-sub">{fmt_pct(pl_total)}</div></div><div><div class="total-label">持股檔數</div><div class="total-value num">{len(holdings)}</div><div class="total-sub">{len(by_industry)} 個產業</div></div><div><div class="total-label">最大單一持股</div><div class="total-value num">{top['weight']:.1f}%</div><div class="total-sub">{top['name']}</div></div>{alert_card}</div>
{allocation_html}
<div class="section-head"><h2>組合走勢</h2><span class="section-note">相對起始日漲跌幅</span></div><div class="callout" style="padding:14px 15px 4px">{trend_html}</div>
<div class="section-head"><h2>產業集中度</h2><span class="section-note">寬度即權重</span></div><div class="band">{''.join(band)}</div><div class="legend">{''.join(legend)}</div><div class="callout">{corr_txt}</div>
<div class="section-head"><h2>持股權重</h2><span class="section-note">依權重排序</span></div><div class="rows">{''.join(f'''<div class="row"><div><span class="name">{h['name']}</span><span class="code">{h['code']}</span></div><div class="price num">{h['weight']:.1f}%</div><div class="meta"><span><em>產業</em> {h['industry']}</span><span><em>損益</em> {fmt_pct(h['pl'])}</span><span><em>營收年增</em> {f"{h['cum_yoy']:+.1f}%" if h['cum_yoy'] is not None else '—'}</span><span><em>PE</em> {f"{h['pe']:.1f}" if h['pe'] else '—'}</span></div><div class="chg">{fmt_pct(h['price']['pct'])}</div><div class="bar"><div style="width:{h['weight']:.1f}%"></div></div></div>''' for h in sorted(holdings, key=lambda x: x['weight'], reverse=True))}</div>
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
def _load_persisted_screener_snapshot(mode):
    """讀取專用選股完整快照；過期、格式不符或資料庫錯誤就回傳 None。"""
    mode = str(mode).strip()
    if mode not in ("blackhorse", "radar", "radar_live"):
        return None
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT snapshot_date, computed_at, rows_json, skipped_liquidity,
                   momentum_json, source_meta
            FROM screener_result_snapshots
            WHERE mode = %s
            LIMIT 1
            """,
            (mode,),
        )
        row = cur.fetchone()
        cur.close()
    except Exception as exc:
        print(f"⚠️ 讀取選股持久化快照失敗 {mode}: {exc}")
        row = None
    finally:
        release_db_connection(conn)
    if not row:
        return None

    snapshot_date, computed_at, rows, skipped, momentum, source_meta = row
    try:
        if computed_at:
            computed_at = (computed_at if computed_at.tzinfo
                           else computed_at.replace(tzinfo=timezone.utc))
            age = (datetime.now(timezone.utc) - computed_at).total_seconds()
            if age < 0 or age > _SHARED_SNAPSHOT_MAX_AGE.get(
                    "screener_" + mode, 3 * 86400):
                return None
        if not isinstance(rows, list):
            return None

        return {
            "rows": rows,
            "skipped": int(skipped or 0),
            "momentum": momentum if isinstance(momentum, dict) else {},
            "source_date": snapshot_date,
            "computed_at": computed_at,
            "source_meta": source_meta or {},
        }
    except Exception as exc:
        print(f"⚠️ 解析選股持久化快照失敗 {mode}: {exc}")
        return None


RADAR_LIVE_SNAPSHOT_INTERVAL_SECONDS = 10 * 60
RADAR_LIVE_SNAPSHOT_MAX_AGE_SECONDS = RADAR_LIVE_SNAPSHOT_INTERVAL_SECONDS


def _load_recent_live_radar_snapshot(max_age_seconds=RADAR_LIVE_SNAPSHOT_MAX_AGE_SECONDS):
    """讀取獨立盤中雷達快照；不把它混進收盤 radar 快照。"""
    snapshot = _load_persisted_screener_snapshot("radar_live")
    if not snapshot:
        return None
    computed_at = snapshot.get("computed_at")
    if computed_at:
        try:
            if computed_at.tzinfo is None:
                computed_at = computed_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - computed_at).total_seconds()
            if age < 0 or age > float(max_age_seconds):
                return None
        except (AttributeError, TypeError, ValueError):
            return None
    meta = snapshot.get("source_meta") or {}
    if isinstance(meta, dict):
        snapshot["scan_universe_count"] = meta.get("scan_universe_count")
        snapshot["scan_finished_at"] = meta.get("scan_finished_at")
        snapshot["radar_diagnostics"] = meta.get("radar_diagnostics") or {}
    return snapshot


def _save_persisted_screener_snapshot(mode, rows, skipped, momentum,
                                      source_date=None, source_meta=None):
    """保存 warmup 的完整選股結果；失敗不阻塞既有記憶體快取。"""
    mode = str(mode).strip()
    if mode not in ("blackhorse", "radar", "radar_live") or rows is None:
        return False
    effective_date = source_date or _screener_source_date()
    if not effective_date:
        print(f"⚠️ 選股快照 {mode} 缺少可確認的資料日，略過保存")
        return False
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO screener_result_snapshots
                (mode, snapshot_date, computed_at, rows_json,
                 skipped_liquidity, momentum_json, source_meta)
            VALUES (%s, %s, NOW(), CAST(%s AS JSONB), %s,
                    CAST(%s AS JSONB), CAST(%s AS JSONB))
            ON CONFLICT (mode) DO UPDATE SET
                snapshot_date = EXCLUDED.snapshot_date,
                computed_at = EXCLUDED.computed_at,
                rows_json = EXCLUDED.rows_json,
                skipped_liquidity = EXCLUDED.skipped_liquidity,
                momentum_json = EXCLUDED.momentum_json,
                source_meta = EXCLUDED.source_meta
            """,
            (mode, effective_date,
             json.dumps(_jsonable(rows), ensure_ascii=False,
                        separators=(",", ":")), int(skipped or 0),
             json.dumps(_jsonable(momentum if isinstance(momentum, dict) else {}),
                        ensure_ascii=False, separators=(",", ":")),
            json.dumps({"source": "warmup_or_screener", "mode": mode,
                         "row_count": len(rows), **(source_meta or {})},
                       ensure_ascii=False, separators=(",", ":"))),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as exc:
        conn.rollback()
        print(f"⚠️ 保存選股持久化快照失敗 {mode}: {exc}")
        return False
    finally:
        release_db_connection(conn)


# 選股結果快取。每個 mode 一份，存的是「還沒套使用者篩選條件」的完整清單。
# 這一頁真正花時間的是抓上百檔報價與評分，而那份結果對所有使用者、
# 所有篩選條件都是同一份——排序、筆數、產業、類股全是在既有清單上做取捨。
# 沒有快取的話，使用者每動一次下拉選單就要重跑一次全部流程（數十秒），
# 那才是最勸退的地方：第一次慢還能接受，每調一個條件都慢就不會有人用了。
_screener_cache = {}
SCREENER_CACHE_SECONDS = 300   # 盤中五分鐘內的報價差異對選股結論沒有影響
_screener_compute_lock = threading.Lock()


def _screener_source_date():
    """以法人實際資料日作為選股快照來源日；無法判斷時才退回台灣今日。"""
    raw = _t86_cache.get("data_date") if isinstance(_t86_cache, dict) else None
    if isinstance(raw, date):
        return raw
    text = str(raw or "").strip()
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            pass
    return None


def _is_taiwan_intraday_window(now=None):
    """判斷台股平日一般盤中時段；週末與盤前／盤後走收盤快照。"""
    now = now or taiwan_now()
    minutes = now.hour * 60 + now.minute
    return (now.weekday() < 5 and 9 * 60 <= minutes <= 13 * 60 + 30)


def _screener_snapshot_valid_for_today(snapshot):
    """只在來源日符合今日或最近週末交易日時使用，避免舊行情冒充今日。"""
    source_date = snapshot.get("source_date") if snapshot else None
    if isinstance(source_date, datetime):
        source_date = source_date.date()
    if isinstance(source_date, str):
        try:
            source_date = date.fromisoformat(source_date[:10])
        except ValueError:
            return False
    if not isinstance(source_date, date):
        return False
    today = taiwan_today()
    if source_date == today:
        return True
    return today.weekday() >= 5 and source_date <= today and (today - source_date).days <= 3


def compute_screener_rows(mode, inst=None, revenue=None, valuation=None,
                          ind_map=None, persist=True, force_refresh=False,
                          radar_deep_limit=None, persist_live=False):
    """
    算出某個模式的完整候選清單。回傳 (rows, 因流動性被排除的檔數, 產業動能)。
    結果快取 5 分鐘，讓調整篩選條件變成瞬間反應。

    路由若已經先取過共享資料，可以直接傳入，避免同一個請求
    在 route 與計算函式之間重複呼叫法人／產業資料。
    """
    now = time.time()
    cache_ttl = 60 if force_refresh else SCREENER_CACHE_SECONDS
    hit = _screener_cache.get(mode)
    if hit and not force_refresh and now - hit["at"] < cache_ttl:
        return hit["rows"], hit["skipped"], hit["momentum"]

    # Render 重啟或切到另一個 worker 時，先讀 warmup 的持久化完整結果。
    # 只有呼叫端沒有明確帶入共享資料時才命中，避免傳入最新法人資料卻被舊快照攔截。
    can_use_persisted = (not force_refresh and all(value is None for value in
                            (inst, revenue, valuation, ind_map)))
    persisted = (_load_persisted_screener_snapshot(mode)
                 if can_use_persisted else None)
    if persisted is not None and _screener_snapshot_valid_for_today(persisted):
        _screener_cache[mode] = {
            "at": now, "rows": persisted["rows"],
            "skipped": persisted["skipped"], "momentum": persisted["momentum"],
            "source_date": persisted.get("source_date"),
            "radar_diagnostics": (persisted.get("source_meta") or {}).get(
                "radar_diagnostics", {}),
        }
        print("⚡ %s 改讀 Supabase 完整快照（來源日 %s），共 %s 檔" %
              (mode, persisted.get("source_date") or "未標日期",
               len(persisted["rows"])))
        return persisted["rows"], persisted["skipped"], persisted["momentum"]

    # 快取失效時只允許一個 worker 進行全量選股；其他請求等候後重新命中快取，
    # 避免朋友同時開啟選股台時重複打 Yahoo／資料庫並放大延遲。
    with _screener_compute_lock:
        now = time.time()
        hit = _screener_cache.get(mode)
        if hit and not force_refresh and now - hit["at"] < cache_ttl:
            return hit["rows"], hit["skipped"], hit["momentum"]

        # 另一個請求可能在等待鎖期間剛好保存了持久化快照，再檢查一次。
        persisted = (_load_persisted_screener_snapshot(mode)
                     if can_use_persisted else None)
        if persisted is not None and _screener_snapshot_valid_for_today(persisted):
            _screener_cache[mode] = {
                "at": now, "rows": persisted["rows"],
                "skipped": persisted["skipped"], "momentum": persisted["momentum"],
                "source_date": persisted.get("source_date"),
                "radar_diagnostics": (persisted.get("source_meta") or {}).get(
                    "radar_diagnostics", {}),
            }
            print("⚡ %s 鎖內改讀 Supabase 完整快照（來源日 %s），共 %s 檔" %
                  (mode, persisted.get("source_date") or "未標日期",
                   len(persisted["rows"])))
            return persisted["rows"], persisted["skipped"], persisted["momentum"]

        inst = fetch_institutional_data() or {} if inst is None else inst
        ind_map = get_industry_map() or {} if ind_map is None else ind_map
        market_map = {}
        if mode == "radar" and "get_market_map" in globals():
            market_map = get_market_map() or {}
        radar_universe_count = 0
        radar_diagnostics = {
            "universe_count": 0,
            "institution_buy_count": 0,
            "quote_missing_count": 0,
            "spark_pct_missing_count": 0,
            "spark_pct_below_threshold_count": 0,
            "spark_qualified_count": 0,
            "deep_candidate_count": 0,
            "price_missing_count": 0,
            "daily_limit_rejected_count": 0,
            "liquidity_rejected_count": 0,
            "final_count": 0,
        }
        if mode == "radar":
            # 雷達不需要營收／估值分數；先用一次 spark 批次行情掃過
            # stock_info 與 T86 出現過的完整股票 universe，再對真正候選補抓
            # high/low/20 日量能等技術欄位。這樣不是只從法人買超前120檔取樣。
            revenue = {} if revenue is None else revenue
            valuation = {} if valuation is None else valuation
            momentum = {}
            universe_codes = sorted({str(code).strip() for code in set(ind_map) | set(inst)
                                     if re.fullmatch(r"\d{4}", str(code).strip())})
            radar_universe_count = len(universe_codes)
            radar_diagnostics["universe_count"] = radar_universe_count
            spark_quotes = _fetch_yahoo_spark_bulk(
                universe_codes, rng="3mo", force_refresh=force_refresh,
                market_map=market_map)
            pool = []
            for code in universe_codes:
                investor = inst.get(code) or {}
                quote = spark_quotes.get(code) or {}
                total_lots = investor.get("total_net_lots")
                if total_lots is None or float(total_lots) <= 0:
                    continue
                radar_diagnostics["institution_buy_count"] += 1
                if not quote:
                    radar_diagnostics["quote_missing_count"] += 1
                    continue
                if quote.get("pct") is None:
                    radar_diagnostics["spark_pct_missing_count"] += 1
                    continue
                if float(quote["pct"]) < 1.5:
                    radar_diagnostics["spark_pct_below_threshold_count"] += 1
                    continue
                radar_diagnostics["spark_qualified_count"] += 1
                turnover = calc_turnover_billion(quote.get("close"), quote.get("volume"))
                pool.append((code, {
                    "name": investor.get("name") or stock_display_name(code, inst_data=inst, fallback=code),
                    "total_net_lots": int(total_lots),
                    "cum_lots": int(total_lots), "buy_days": 1,
                    "spark_pct": float(quote.get("pct") or 0),
                    "spark_turnover": turnover,
                }))
            # 全市場掃描後只對最有機會成為雷達訊號的有限候選補抓 3mo
            # 技術序列；候選限制是深度計算上限，不是即時 universe 上限。
            pool.sort(key=lambda x: (x[1].get("spark_pct", 0),
                                     x[1].get("spark_turnover", 0),
                                     x[1].get("total_net_lots", 0)), reverse=True)
            deep_limit = (int(radar_deep_limit) if radar_deep_limit else
                          RADAR_DEEP_SCAN_LIMIT)
            pool = pool[:max(12, min(deep_limit, RADAR_DEEP_SCAN_LIMIT))]
            radar_diagnostics["deep_candidate_count"] = len(pool)
        else:
            revenue = fetch_monthly_revenue() or {} if revenue is None else revenue
            valuation = fetch_valuation() or {} if valuation is None else valuation
            momentum = get_industry_momentum(revenue, ind_map)
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
        pool_prices = get_realtime_stocks_bulk(
            [c for c, _ in pool], workers=24 if mode == "radar" else 16,
            rng="3mo",
            market_suffix=(market_map if mode == "radar" else None),
            force_refresh=(force_refresh or mode == "radar"))
        for code, info in pool:
            price = pool_prices.get(code)
            if not price:
                if mode == "radar":
                    radar_diagnostics["price_missing_count"] += 1
                continue
            if abs(price["pct"]) > 10.5:
                if mode == "radar":
                    radar_diagnostics["daily_limit_rejected_count"] += 1
                continue
            min_close, min_turnover = LIQUIDITY.get(
                stock_category(code, ind_map), (8, 0.3))
            if price["close"] < min_close:
                skipped_liquidity += 1
                if mode == "radar":
                    radar_diagnostics["liquidity_rejected_count"] += 1
                continue
            turnover = calc_turnover_billion(price["close"], price["volume"])
            if turnover < min_turnover:
                skipped_liquidity += 1
                if mode == "radar":
                    radar_diagnostics["liquidity_rejected_count"] += 1
                continue
            if mode == "radar" and price["pct"] < 1.5:
                radar_diagnostics["spark_pct_below_threshold_count"] += 1
                continue

            cum_yoy = revenue.get(code, {}).get("cum_yoy_pct")
            streak = streaks.get(code, 0)
            ind_code = ind_map.get(code)
            ind_txt = industry_name(ind_code) if ind_code else "未分類"
            industry_stats = momentum.get(ind_code) if ind_code else None
            if mode == "radar" and ind_code:
                # 雷達只需確認產業對照存在，不必為完整度再抓產業動能統計。
                industry_stats = {"industry_code": ind_code}

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
                    info.get("cum_lots"), price, mode=mode),
                "radar_state": classify_radar_state(price),
            })

        source_date = _screener_source_date()
        cache_item = {"at": now, "rows": rows,
                      "skipped": skipped_liquidity, "momentum": momentum,
                      "source_date": source_date}
        if mode == "radar":
            radar_diagnostics["final_count"] = len(rows)
            cache_item["scan_universe_count"] = radar_universe_count
            cache_item["scan_finished_at"] = taiwan_now().isoformat()
            cache_item["radar_diagnostics"] = radar_diagnostics
        _screener_cache[mode] = cache_item
        if persist or (mode == "radar" and persist_live):
            persisted_meta = {}
            persisted_mode = mode
            if mode == "radar":
                persisted_meta["radar_diagnostics"] = radar_diagnostics
                persisted_meta["scan_universe_count"] = radar_universe_count
                persisted_meta["scan_finished_at"] = cache_item.get("scan_finished_at")
                if persist_live and not persist:
                    persisted_mode = "radar_live"
            _save_persisted_screener_snapshot(
                persisted_mode, rows, skipped_liquidity, momentum,
                source_date=source_date, source_meta=persisted_meta,
            )
        return rows, skipped_liquidity, momentum


_SCREENER_REFRESH_LOCK = threading.Lock()
_SCREENER_REFRESH_RUNNING = {"blackhorse": False, "radar": False}
SCREENER_STALE_SNAPSHOT_MAX_AGE_SECONDS = 3 * 86400


def _screener_snapshot_is_recent(snapshot, max_days=3):
    """檢查快照是否仍可作為明示日期的暫時畫面，絕不把它冒充成今日。"""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("rows"), list):
        return False
    source_date = snapshot.get("source_date")
    if isinstance(source_date, datetime):
        source_date = source_date.date()
    if isinstance(source_date, str):
        try:
            source_date = date.fromisoformat(source_date[:10])
        except ValueError:
            return False
    if not isinstance(source_date, date):
        return False
    today = taiwan_today()
    return source_date <= today and (today - source_date).days <= int(max_days)


def _screener_recent_snapshot(mode):
    """優先取 process 記憶體，再取共享持久化快照；回傳可明示來源日的資料。"""
    mode = str(mode or "").strip()
    cached = _screener_cache.get(mode)
    if (cached and _screener_snapshot_is_recent(cached, 3) and
            _radar_live_cache_is_fresh(cached)):
        return cached, "記憶體快照"
    persisted = _load_persisted_screener_snapshot(mode)
    if persisted and _screener_snapshot_is_recent(persisted, 3):
        _screener_cache[mode] = {
            "at": time.time(), "rows": persisted["rows"],
            "skipped": persisted.get("skipped", 0),
            "momentum": persisted.get("momentum", {}),
            "source_date": persisted.get("source_date"),
            "radar_diagnostics": (persisted.get("source_meta") or {}).get("radar_diagnostics", {}),
        }
        return _screener_cache[mode], "共享快照"
    return None, "尚未建立可用快照"


def _start_screener_background_refresh(mode, intraday=False,
                                      radar_deep_limit=None, on_complete=None,
                                      on_failure=None):
    """每個 process 每個模式只允許一個全量刷新，盤中結果不覆蓋收盤快照。

    `on_complete` 僅在計算成功後執行，供 LINE 在 webhook 已立即回覆後
    推送完成的真實結果；回呼失敗不能影響快照寫入或背景 worker 收尾。
    """
    mode = str(mode or "").strip()
    if mode not in ("blackhorse", "radar"):
        return False
    with _SCREENER_REFRESH_LOCK:
        if _SCREENER_REFRESH_RUNNING.get(mode):
            return False
        _SCREENER_REFRESH_RUNNING[mode] = True

    def worker():
        completed = False
        failure = None
        try:
            kwargs = {"persist": not intraday, "force_refresh": True,
                      "persist_live": bool(intraday and mode == "radar")}
            if radar_deep_limit is not None and mode == "radar":
                kwargs["radar_deep_limit"] = radar_deep_limit
            compute_screener_rows(mode, **kwargs)
            completed = True
            print("✅ 選股背景刷新完成 %s%s" %
                  (mode, "（盤中不寫入收盤快照）" if intraday else ""))
        except Exception as exc:
            failure = exc
            print("⚠️ 選股背景刷新失敗 %s: %s" % (mode, exc))
        finally:
            with _SCREENER_REFRESH_LOCK:
                _SCREENER_REFRESH_RUNNING[mode] = False
            if completed and callable(on_complete):
                try:
                    on_complete()
                except Exception as exc:
                    print("⚠️ 選股背景完成回呼失敗 %s: %s" % (mode, exc))
            elif failure is not None and callable(on_failure):
                try:
                    on_failure(failure)
                except Exception as exc:
                    print("⚠️ 選股背景失敗回呼失敗 %s: %s" % (mode, exc))

    threading.Thread(target=worker, name="screener-%s-refresh" % mode,
                     daemon=True).start()
    return True


def _do_scheduled_radar_live_snapshot():
    """排程用盤中雷達 producer；結果只寫 radar_live，不覆蓋收盤 radar。"""
    if not _is_taiwan_intraday_window():
        return "目前非台股一般盤中時段，未抓取盤中雷達快照"
    with _SCREENER_REFRESH_LOCK:
        if _SCREENER_REFRESH_RUNNING.get("radar"):
            return "已有雷達背景掃描正在執行，本次略過"
        _SCREENER_REFRESH_RUNNING["radar"] = True
    try:
        rows, skipped, _momentum = compute_screener_rows(
            "radar", persist=False, force_refresh=True,
            radar_deep_limit=RADAR_SCHEDULED_DEEP_SCAN_LIMIT, persist_live=True)
        return (f"已完成盤中雷達快照：掃描 {len(rows) + int(skipped or 0)} 檔候選、"
                f"輸出 {len(rows)} 檔；有效期 {RADAR_LIVE_SNAPSHOT_INTERVAL_SECONDS // 60} 分鐘")
    finally:
        with _SCREENER_REFRESH_LOCK:
            _SCREENER_REFRESH_RUNNING["radar"] = False


@app.route("/cron/radar-live", methods=["POST", "GET"])
def cron_radar_live():
    """Render Cron 每 10 分鐘呼叫；只在盤中背景建立 radar_live 快照。"""
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)
    if not _is_taiwan_intraday_window():
        return "目前非台股一般盤中時段，未啟動盤中雷達快照。", 200
    if _load_recent_live_radar_snapshot():
        return (f"最近 {RADAR_LIVE_SNAPSHOT_INTERVAL_SECONDS // 60} 分鐘已有盤中雷達快照，本次略過。", 200)
    return run_in_background("盤中雷達快照", _do_scheduled_radar_live_snapshot), 200


def _screener_building_fragment(mode, source_date=None):
    """無快照時的立即回應；只報告狀態與資料入口，不假造股票數字。"""
    label = "雷達" if mode == "radar" else "黑馬"
    date_text = str(source_date or "尚無")
    return f'''<section class="screener-fast-card" data-screener-pending="1">
  <div class="screener-fast-state"><span class="screener-fast-state-mark"></span><div>
    <b>{label}完整分析正在背景整理</b>
    <div class="screener-fast-note">目前沒有可使用的完整快照（最近資料日：{html.escape(date_text)}）。本頁已立即回應；整理完成後重新整理即可看到真實排名。</div>
  </div></div>
  <div class="screener-fast-note">不在請求中同步掃描全市場，也不會用空白或虛構數字代替結果。</div>
  <p><a class="btn" href="?mode={html.escape(str(mode))}">重新整理查看結果</a></p>
</section>'''


def _render_radar_empty_state(diagnostics=None, skipped_liquidity=0,
                              filtered_out_count=0):
    """雷達零結果時顯示真實掃描漏斗，不用一句「沒有符合」帶過。"""
    d = diagnostics if isinstance(diagnostics, dict) else {}
    universe = int(d.get("universe_count") or 0)
    buy_count = int(d.get("institution_buy_count") or 0)
    quote_missing = int(d.get("quote_missing_count") or 0)
    pct_missing = int(d.get("spark_pct_missing_count") or 0)
    pct_below = int(d.get("spark_pct_below_threshold_count") or 0)
    spark_qualified = int(d.get("spark_qualified_count") or 0)
    deep_count = int(d.get("deep_candidate_count") or 0)
    price_missing = int(d.get("price_missing_count") or 0)
    daily_limit = int(d.get("daily_limit_rejected_count") or 0)
    liquidity = int(d.get("liquidity_rejected_count") or skipped_liquidity or 0)
    final_count = int(d.get("final_count") or 0)

    if filtered_out_count > 0:
        reason = (f"即時掃描原本有 {filtered_out_count} 檔，"
                  "但目前的突破／量能／連買或類股篩選把它們全部排除。")
    elif universe <= 0:
        reason = "目前沒有可用的四位數股票代號 universe，無法形成雷達候選。"
    elif buy_count <= 0:
        reason = (f"已掃 {universe} 檔，但法人資料中沒有買超標的；"
                  "雷達基本條件是法人買超且當日漲幅至少 1.5%。")
    elif spark_qualified <= 0:
        reason = (f"法人買超 {buy_count} 檔，但沒有標的同時達到即時漲幅至少 1.5%；"
                  f"{pct_below} 檔低於門檻，{pct_missing + quote_missing} 檔缺少即時漲幅／行情。")
    elif deep_count <= 0:
        reason = (f"有 {spark_qualified} 檔達到即時漲幅門檻，"
                  "但沒有進入技術資料補抓候選。")
    elif final_count <= 0:
        reason = (f"已取得 {deep_count} 檔候選的技術行情；"
                  f"{price_missing} 檔缺價格、{daily_limit} 檔超過單日漲跌幅範圍、"
                  f"{liquidity} 檔未過流動性門檻，因此沒有可列入結果。")
    else:
        reason = "目前篩選後沒有可列入雷達的標的。"

    stats = (f"全市場掃描 {universe} 檔 · 法人買超 {buy_count} 檔 · "
             f"即時漲幅≥1.5% {spark_qualified} 檔 · 技術候選 {deep_count} 檔 · "
             f"最終結果 {final_count} 檔")
    return f'''<div class="empty radar-empty-state">
  <b>本次即時掃描沒有可列入雷達的標的</b>
  <div class="radar-empty-funnel">{html.escape(stats)}</div>
  <p>{html.escape(reason)}</p>
  <span style="font-size:12.5px">雷達條件：法人買超、當日漲幅至少 1.5%，再檢查價格位階、量能與流動性；目前沒有把舊快照當成即時結果。</span>
</div>'''


def _radar_empty_summary(diagnostics=None, skipped_liquidity=0):
    """LINE 用的雷達零結果簡版漏斗；沒有診斷資料就明示未保存，不補猜數字。"""
    if not isinstance(diagnostics, dict) or not diagnostics:
        return "雷達本次沒有結果；詳細淘汰漏斗尚未保存，請查看網頁完成狀態。"
    d = diagnostics
    universe = int(d.get("universe_count") or 0)
    buy_count = int(d.get("institution_buy_count") or 0)
    pct_below = int(d.get("spark_pct_below_threshold_count") or 0)
    pct_missing = int(d.get("spark_pct_missing_count") or 0)
    quote_missing = int(d.get("quote_missing_count") or 0)
    qualified = int(d.get("spark_qualified_count") or 0)
    deep = int(d.get("deep_candidate_count") or 0)
    price_missing = int(d.get("price_missing_count") or 0)
    daily_limit = int(d.get("daily_limit_rejected_count") or 0)
    liquidity = int(d.get("liquidity_rejected_count") or skipped_liquidity or 0)
    final_count = int(d.get("final_count") or 0)
    if universe <= 0:
        reason = "沒有可用股票代號 universe"
    elif buy_count <= 0:
        reason = "法人資料沒有買超標的"
    elif qualified <= 0:
        reason = (f"法人買超 {buy_count} 檔中，{pct_below} 檔漲幅低於 1.5%，"
                  f"{pct_missing + quote_missing} 檔缺即時行情／漲幅")
    elif deep <= 0:
        reason = f"即時漲幅達門檻的 {qualified} 檔沒有技術候選"
    elif final_count <= 0:
        reason = (f"技術候選 {deep} 檔中，缺價格 {price_missing} 檔、"
                  f"超過單日範圍 {daily_limit} 檔、流動性排除 {liquidity} 檔")
    else:
        reason = "完成掃描後沒有標的通過最後條件"
    return (f"全市場 {universe} 檔・法人買超 {buy_count} 檔・"
            f"即時漲幅≥1.5% {qualified} 檔・技術候選 {deep} 檔・"
            f"最終 {final_count} 檔\n主要原因：{reason}")


def build_screener_data_quality(cum_yoy, valuation, industry_stats,
                                 cum_lots, price, mode=None):
    """只用現有資料標示選股資料完整度，不用缺資料猜測分數。"""
    if mode == "radar":
        checks = [
            ("行情", bool(price and price.get("close") is not None)),
            ("量能", bool(price and price.get("vol_ratio") is not None)),
            ("位階", bool(price and any(price.get(k) is not None
                                        for k in ("high_20d", "high_60d", "ma20")))),
            ("法人", cum_lots is not None),
            ("產業", industry_stats is not None),
        ]
    else:
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

            sample_rows = []
            for sample in s.get("samples") or []:
                sample_date = sample.get("date")
                if hasattr(sample_date, "strftime"):
                    sample_date = sample_date.strftime("%m/%d")
                else:
                    sample_date = str(sample_date or "—")
                sample_name = html.escape(str(sample.get("name") or sample.get("code") or "未知"))
                sample_code = html.escape(str(sample.get("code") or "—"))
                sample_rank = sample.get("rank")
                rank_text = f"推薦 #{sample_rank}" if sample_rank else ""
                sample_ret = sample.get("ret")
                sample_market = sample.get("market")
                sample_excess = sample.get("excess")
                ret_text = f"{float(sample_ret):+.1f}%" if sample_ret is not None else "—"
                market_text = f"{float(sample_market):+.1f}%" if sample_market is not None else "無大盤快照"
                excess_text = f"{float(sample_excess):+.1f}%" if sample_excess is not None else "無法比較"
                ret_cls = "up" if sample_ret is not None and sample_ret >= 0 else "down" if sample_ret is not None else "flat"
                market_cls = "up" if sample_market is not None and sample_market >= 0 else "down" if sample_market is not None else "flat"
                excess_cls = "up" if sample_excess is not None and sample_excess >= 0 else "down" if sample_excess is not None else "flat"
                sample_rows.append(f"""
<div class="review-sample">
  <div class="review-sample-title"><b>{html.escape(str(sample_date))}　{sample_name}（{sample_code}）</b>
    <small>{rank_text}</small></div>
  <div class="review-sample-metrics">
    <span>個股 <b class="num {ret_cls}">{ret_text}</b></span>
    <span>大盤 <b class="num {market_cls}">{market_text}</b></span>
    <span>超額 <b class="num {excess_cls}">{excess_text}</b></span>
  </div>
</div>""")
            if sample_rows:
                rows_html.append(f"""
<details class="review-details">
  <summary>查看全部 {len(sample_rows)} 筆樣本（含大盤比較）</summary>
  <div class="review-samples">{"".join(sample_rows)}</div>
</details>""")

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
  <a href="/web/chips">籌碼超人</a>
  <a href="/web/screener?mode=review" class="on">成效</a>
  <a href="/web/screener?mode=turning">轉折觀察</a>
  <a href="/web/etf">ETF 專區</a>
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
    persisted = None
    radar_snapshot = None
    if mode == "radar":
        radar_snapshot = _load_recent_live_radar_snapshot(
            max_age_seconds=RADAR_LIVE_SNAPSHOT_MAX_AGE_SECONDS)
    if radar_snapshot:
        # 盤中快照即使 rows 為空也代表掃描已完成；不能因零結果回退舊歷史。
        persisted = radar_snapshot
    radar_live_preview = mode == "radar" and not radar_snapshot
    if radar_snapshot:
        cached_rows = list(radar_snapshot.get("rows") or [])[:5]
    elif not radar_live_preview:
        if cached and time.time() - cached.get("at", 0) < SCREENER_CACHE_SECONDS:
            cached_rows = list(cached.get("rows") or [])[:5]
        if not cached_rows:
            candidate = _load_persisted_screener_snapshot(mode)
            if candidate and _screener_snapshot_is_recent(candidate, 3):
                persisted = candidate
                cached_rows = list(candidate.get("rows") or [])[:5]

    rows = []
    if radar_live_preview:
        source_label = "即時全市場掃描啟動中"
        source_date = f"行情請求時間 {taiwan_now().strftime('%Y-%m-%d %H:%M:%S')}"
    elif cached_rows or persisted is not None:
        source_label = ("盤中定時快照" if radar_snapshot else
                        ("目前快取結果" if not persisted else
                         "warmup 完整快照（最近資料）"))
        source_date = (str(radar_snapshot.get("scan_finished_at") or
                           radar_snapshot.get("source_date") or "未標日期")
                       if radar_snapshot else
                       (str(cached.get("source_date") or "未標日期")
                        if not persisted else
                        str(persisted.get("source_date") or "未標日期")))
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
    elif not radar_live_preview:
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

    if radar_snapshot:
        state_title = "已顯示最近一次盤中雷達快照"
        state_note = (f"每 {RADAR_LIVE_SNAPSHOT_INTERVAL_SECONDS // 60} 分鐘由背景排程更新；"
                      "本次點擊直接讀快照，不重新掃描全市場。")
        preview_title = "盤中快照前 5 名"
        preview_note = "下一輪排程完成後自動更新"
    elif radar_live_preview:
        state_title = "即時雷達全市場掃描中"
        state_note = "正在重新取得全市場最新行情與雷達訊號；完成後才列出本次結果。"
        preview_title = "即時掃描完成後顯示結果"
        preview_note = "不展示舊報酬率"
    else:
        state_title = f"完整{label}分析載入中"
        state_note = "目前先顯示前 5 名預覽；系統正在補上完整清單、評分、型態、篩選、排序與產業分布。"
        preview_title = "前 5 名預覽"
        preview_note = "不是完整選股結果"

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
    <div><b>{state_title}</b>
      <span>{state_note}</span>
    </div>
  </div>
  <div class="screener-fast-preview">
    <div class="screener-fast-preview-title"><b>{preview_title}</b><span>{preview_note}</span></div>
    {''.join(rows)}
  </div>
  <div class="screener-fast-features">
    <b>完整選股頁稍後會顯示</b>
    完整清單 ・ 分數與資料完整度 ・ 黑馬／雷達條件 ・ 篩選與排序 ・ 產業分布
    <div class="screener-fast-skeleton" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
  </div>
</section>"""


def render_turning_observation_web_body(result, status_note=None):
    """轉折觀察完整網頁：三狀態清單與每檔觸發依據。"""
    items = result.get("items") or []
    data_date = result.get("data_date") or "未標日期"
    prior_days = int(result.get("prior_days") or 5)
    esc = html.escape
    state_defs = (("confirmed", "✅ 已確認", "價格、量能與法人方向已有多項同步"),
                  ("observing", "👀 觀察中", "已出現部分改變，尚未達確認門檻"),
                  ("invalid", "⚠️ 已失效", "原本轉折條件被目前價格結構破壞"))
    sections = []
    for state, label, note in state_defs:
        state_items = [x for x in items if x.get("state") == state]

        def render_turning_row(item, rank):
            close = item.get("close")
            pct = item.get("pct")
            close_text = f"{float(close):,.2f}" if close is not None else "—"
            pct_text = f"{float(pct):+.2f}%" if pct is not None else "漲跌資料不足"
            state = str(item.get("state") or "observing")
            direction = str(item.get("direction") or "neutral")
            flow_key = str(item.get("direction_flow") or "")
            flow_label = str(item.get("direction_flow_label") or "")
            if flow_key not in {"sell_to_buy", "buy_to_sell", "buying_strength", "selling_strength"}:
                legacy_label = str(item.get("direction_label") or "")
                event_type_fallback = str(item.get("event_type") or "")
                if legacy_label == "賣轉買" or event_type_fallback == "賣轉買" or "轉買" in event_type_fallback:
                    flow_key, flow_label = "sell_to_buy", "賣轉買"
                elif legacy_label == "買轉賣" or event_type_fallback == "買轉賣" or "轉賣" in event_type_fallback:
                    flow_key, flow_label = "buy_to_sell", "買轉賣"
                elif direction == "up":
                    flow_key, flow_label = "buying_strength", "買方增強"
                elif direction == "down":
                    flow_key, flow_label = "selling_strength", "賣方增強"
                else:
                    flow_key, flow_label = "unknown", "方向不明"
            if state == "invalid":
                state_text = "已失效"
                conclusion_label = "為何失效"
                conclusion = f"{flow_label}；{item.get('state_reason') or '失效原因資料不足'}"
            else:
                state_text = "已確認" if state == "confirmed" else "觀察中"
                conclusion_label = "目前判讀"
                event = str(item.get("event_type") or "法人方向變化")
                score = int(item.get("score") or 0)
                conclusion = (f"{flow_label}；{event}；目前符合 {score}/5 個轉折條件，"
                              f"{'已達確認門檻' if state == 'confirmed' else '尚未達確認門檻'}")
            details = [str(value) for value in
                       (item.get("reason_details") or item.get("reasons") or [])
                       if str(value).strip()]

            def first_fact(prefixes, fallback=None):
                for value in details:
                    if any(value.startswith(prefix) for prefix in prefixes):
                        return value
                return fallback or "資料不足"

            institutional = first_fact(("三大法人今日",),
                                       f"法人今日{('買超' if direction == 'up' else '賣超')} "
                                       f"{int(item.get('current_total_lots') or 0):+,} 張")
            price_fact = first_fact(("收盤",),
                                    f"收盤 {close_text}，20日均線資料不足")
            volume_fact = first_fact(("成交量",), "成交量資料不足")
            streak_fact = first_fact(("連續上漲", "連續下跌", "今日為近期", "今日翻黑"), None)
            support = str(item.get("support") or "資料不足")
            raw_resistance = item.get("resistance")
            resistance = ("無壓力位" if raw_resistance in (None, "", "資料不足", "—", "-")
                          else str(raw_resistance))
            return f'''<div class="turning-row turning-state-{esc(state)}">
  <div class="turning-row-head"><b>#{rank} {esc(str(item.get("name") or item.get("code")))}</b>
    <span>{esc(str(item.get("code") or ""))}・<b class="turning-flow turning-flow-{esc(flow_key)}">{esc(flow_label)}</b>・<strong>{esc(state_text)}</strong></span></div>
  <div class="turning-price">{close_text} <span>{pct_text}</span></div>
  <div class="turning-conclusion"><b>{esc(conclusion_label)}</b><span>{esc(conclusion)}</span></div>
  <div class="turning-facts">
    <div class="turning-fact"><b>法人</b><span>{esc(institutional)}</span></div>
    <div class="turning-fact"><b>價格</b><span>{esc(price_fact)}</span></div>
    <div class="turning-fact"><b>量能</b><span>{esc(volume_fact)}</span></div>
    {f'<div class="turning-fact"><b>連續</b><span>{esc(streak_fact)}</span></div>' if streak_fact else ''}
  </div>
  <div class="turning-levels"><span>支撐</span> {esc(support)}　<span>壓力</span> {esc(resistance)}</div>
</div>'''

        visible_rows = [render_turning_row(item, rank)
                        for rank, item in enumerate(state_items[:3], 1)]
        hidden_items = state_items[3:]
        if hidden_items:
            hidden_rows = [render_turning_row(item, rank)
                           for rank, item in enumerate(hidden_items, 4)]
            visible_rows.append(
                f'<details class="turning-more"><summary>查看其餘 {len(hidden_items)} 檔</summary>'
                f'{"".join(hidden_rows)}</details>')
        content = "".join(visible_rows) or '<div class="turning-empty">目前沒有符合此狀態的標的。</div>'
        sections.append(f'<section class="turning-section"><h2>{label}</h2><p>{note}</p>{content}</section>')
    return f'''<div class="tabs">
  <a href="/web/screener?mode=blackhorse">黑馬</a>
  <a href="/web/screener?mode=radar">雷達</a>
  <a href="/web/chips">籌碼超人</a>
  <a href="/web/screener?mode=review">成效</a>
  <a href="/web/screener?mode=turning" class="on">轉折觀察</a>
  <a href="/web/etf">ETF 專區</a>
</div>
<div class="turning-meta">法人資料日：<b>{esc(str(data_date))}</b>　前 <b>{prior_days}</b> 個交易日平均作為方向比較</div>
{f'<div class="turning-refresh-note">{esc(str(status_note))}</div>' if status_note else ''}
<div class="mode-note">轉折觀察不預測未來，只整理現有真實價格、均線、量能與 T86 法人方向是否出現改變。第一次出現時可能只是觀察中；條件被破壞時標示為已失效。</div>
<style>
.turning-section{{background:#fff;border:1px solid #e4e1d8;border-radius:14px;padding:14px;margin:12px 0;box-shadow:0 3px 14px rgba(35,39,35,.05)}}
.turning-section h2{{margin:0 0 4px;font-size:20px;line-height:1.3}}
.turning-section>p{{margin:0 0 4px;color:var(--ink-soft);font-size:12px;line-height:1.5}}
.turning-row{{padding:13px 0;border-top:1px solid #eee;line-height:1.5}}
.turning-row-head{{display:flex;justify-content:space-between;gap:8px;align-items:baseline}}
.turning-flow{{font-size:13px;font-weight:800;white-space:nowrap}}
.turning-flow-sell_to_buy{{color:#A82A20}}
.turning-flow-buy_to_sell{{color:#155C42}}
.turning-flow-buying_strength{{color:#A82A20}}
.turning-flow-selling_strength{{color:#155C42}}
.turning-row-head b{{font-size:17px;overflow-wrap:anywhere}}
.turning-row-head span{{color:var(--ink-soft);font-size:12px;white-space:nowrap}}
.turning-row-head strong{{color:var(--brass)}}
.turning-price{{font-size:20px;font-weight:700;margin-top:3px}}
.turning-price span{{font-size:14px;color:var(--red);margin-left:5px}}
.turning-conclusion{{display:flex;gap:8px;align-items:flex-start;padding:9px 10px;margin-top:8px;background:#FAF5E9;border-left:3px solid var(--brass);border-radius:8px;font-size:12.5px;line-height:1.55}}
.turning-conclusion b{{flex:none;color:var(--brass)}}
.turning-conclusion span{{color:var(--ink);overflow-wrap:anywhere}}
.turning-facts{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:9px}}
.turning-fact{{display:flex;gap:6px;align-items:flex-start;padding:8px 9px;background:#F7F7F3;border:1px solid #ECEDE8;border-radius:8px;font-size:12px;line-height:1.45;min-width:0}}
.turning-fact b{{color:var(--brass);flex:none;font-size:11px}}
.turning-fact span{{color:var(--ink-soft);overflow-wrap:anywhere}}
.turning-levels{{margin-top:8px;color:var(--ink-soft);font-size:12px;overflow-wrap:anywhere}}
.turning-levels span{{color:var(--brass);font-weight:700}}
.turning-more{{margin-top:8px;border-top:1px solid #eee;padding-top:8px}}
.turning-more summary{{cursor:pointer;color:var(--brass);font-weight:700;font-size:13px;padding:4px 0}}
.turning-more .turning-row:first-child{{border-top:1px solid #eee}}
.turning-empty{{color:var(--ink-soft);padding:8px 0}}
.turning-meta{{font-size:12px;color:var(--ink-soft);padding:8px 0;line-height:1.5}}
.turning-refresh-note{{padding:9px 11px;background:#FAF5E9;border-left:3px solid var(--brass);border-radius:8px;color:var(--ink-soft);font-size:12.5px;line-height:1.6}}
@media (max-width:480px){{.turning-facts{{grid-template-columns:1fr}}.turning-row-head b{{font-size:16px}}}}
</style>
{"".join(sections)}
<div class="callout"><b>資料限制</b><br><span style="font-size:12.5px;color:var(--ink-faint)">法人資料需等 T86 更新；資料不足時不建立訊號。轉折狀態是規則式觀察，不構成投資建議。</span></div>'''


def _workbench_number(value, digits=2):
    """將既有快照中的數值安全轉為 JSON 欄位；未知值保留 None，不以 0 冒充。"""
    try:
        value = float(value)
        if not math.isfinite(value):
            return None
        return round(value, digits)
    except (TypeError, ValueError):
        return None


def _workbench_text(value, fallback="未標示"):
    text = str(value or "").strip()
    return text if text else fallback


def _workbench_display_name(raw, code):
    """工作台名稱回填：沿用正式名稱表；測試或名稱表未載入時安全退回代號。"""
    candidate = raw.get("name") or raw.get("stock_name") or raw.get("display_name")
    if candidate:
        return _workbench_text(candidate, code)
    try:
        fallback = stock_display_name(code, fallback=code)
    except Exception:
        fallback = code
    return _workbench_text(fallback, code)


def _workbench_screener_rows(mode, snapshot=None):
    """把黑馬／雷達已保存列轉為前端工作台資料，不呼叫完整選股計算。"""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    rows = snapshot.get("rows") if isinstance(snapshot.get("rows"), list) else []
    normalized = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        quality_detail = raw.get("data_quality") if isinstance(raw.get("data_quality"), dict) else {}
        quality = (quality_detail.get("valid") if quality_detail else raw.get("data_quality"))
        quality_num = _workbench_number(quality, 0)
        source_label = "黑馬" if mode == "blackhorse" else "雷達"
        raw_radar_state = _workbench_text(raw.get("radar_state"), "")
        raw_breakout = _workbench_text(raw.get("breakout"), "")
        # 使用者只需要可行動的突破／帶量型態；「價格強、量能普通」不佔用狀態欄。
        display_radar_state = ("" if raw_radar_state == "價格強、量能普通" and not raw_breakout
                               else raw_radar_state)
        signal = (display_radar_state if mode == "radar" else raw_breakout)
        normalized.append({
            "source": source_label,
            "code": code,
            "name": _workbench_display_name(raw, code),
            "industry": _workbench_text(raw.get("industry"), "未分類"),
            "score": (None if mode == "radar" else _workbench_number(raw.get("score"))),
            "legacy_score": _workbench_number(raw.get("score")),
            "price": _workbench_number(raw.get("close")),
            "change_pct": _workbench_number(raw.get("pct")),
            "metric_label": "當日漲跌",
            "institutional_lots": _workbench_number(raw.get("cum_lots")),
            "institutional_amount": _workbench_number(raw.get("institutional_amount") or raw.get("cum_amount") or raw.get("institution_amount")),
            "foreign_lots": _workbench_number(raw.get("foreign_lots") or raw.get("foreign_cum_lots")),
            "trust_lots": _workbench_number(raw.get("trust_lots") or raw.get("investment_trust_lots")),
            "signal": _workbench_text(signal, ""),
            "quality": quality_num,
            "rev": _workbench_number(raw.get("rev")),
            "val": _workbench_number(raw.get("val")),
            "mom": _workbench_number(raw.get("mom")),
            "streak_score": _workbench_number(raw.get("streak_score")),
            "chip": _workbench_number(raw.get("chip")),
            "caps": list(raw.get("caps")) if isinstance(raw.get("caps"), (list, tuple)) else None,
            "val_desc": _workbench_text(raw.get("val_desc"), ""),
            "mom_desc": _workbench_text(raw.get("mom_desc"), ""),
            "radar_state": display_radar_state,
            "turnover": _workbench_number(raw.get("turnover")),
            "pe": _workbench_number(raw.get("pe")),
            "peg": _workbench_number(raw.get("peg")),
            "yield": _workbench_number(raw.get("yield")),
            "pb": _workbench_number(raw.get("pb")),
            "pos": _workbench_number(raw.get("pos")),
            "category": _workbench_text(raw.get("category"), "未分類"),
            "score_policy": ("金融股不評分，僅列事實供判讀"
                             if raw.get("category") == "金融" and raw.get("score") is None else ""),
            "buy_days": _workbench_number(raw.get("buy_days"), 0),
            "up_streak": _workbench_number(raw.get("up_streak"), 0),
            "cum_yoy": _workbench_number(raw.get("cum_yoy")),
            "detail": {
                "source_date": str(snapshot.get("source_date") or "未標日期"),
                "breakout": _workbench_text(raw.get("breakout"), ""),
                "high_status": _workbench_text(raw.get("high_status") or raw.get("position_status"), ""),
                "radar_state": display_radar_state,
                "raw_radar_state": raw_radar_state,
                "category": _workbench_text(raw.get("category"), ""),
                "score_policy": ("金融股不評分，僅列事實供判讀"
                                 if raw.get("category") == "金融" and raw.get("score") is None else ""),
                "caps": list(raw.get("caps")) if isinstance(raw.get("caps"), (list, tuple)) else None,
                "val_desc": _workbench_text(raw.get("val_desc"), ""),
                "mom_desc": _workbench_text(raw.get("mom_desc"), ""),
                "streak": _workbench_number(raw.get("streak") or raw.get("up_streak")),
                "buy_days": _workbench_number(raw.get("buy_days"), 0),
                "vol_ratio": _workbench_number(raw.get("vol_ratio")),
                "support": _workbench_number(raw.get("support")),
                "resistance": _workbench_number(raw.get("resistance")),
                "institutional_amount": _workbench_number(raw.get("institutional_amount") or raw.get("cum_amount") or raw.get("institution_amount")),
                "foreign_lots": _workbench_number(raw.get("foreign_lots") or raw.get("foreign_cum_lots")),
                "trust_lots": _workbench_number(raw.get("trust_lots") or raw.get("investment_trust_lots")),
                "pe": _workbench_number(raw.get("pe")),
                "peg": _workbench_number(raw.get("peg")),
                "turnover": _workbench_number(raw.get("turnover")),
                "score_breakdown": "營收／估值／產業／連續性／籌碼技術" if any(raw.get(k) is not None for k in ("rev", "val", "mom", "streak_score", "chip")) else "",
                "data_quality": quality_detail,
            },
        })
    if mode == "radar":
        # 沿用舊版 LINE／網頁雷達排序：突破（連漲過長扣一級）、量能、法人連買、當日漲幅。
        def radar_key(item):
            detail = item.get("detail") or {}
            breakout = (2 if detail.get("breakout") == "季線新高"
                        else (1 if detail.get("breakout") else 0))
            fatigue = -1 if (_workbench_number(item.get("up_streak"), 0) or 0) >= 5 else 0
            return (breakout + fatigue,
                    _workbench_number(detail.get("vol_ratio"), 0) or 0,
                    _workbench_number(detail.get("streak"), 0) or 0,
                    _workbench_number(item.get("change_pct"), 0) or 0)
        normalized.sort(key=radar_key, reverse=True)
        for rank, item in enumerate(normalized, 1):
            item["radar_rank"] = rank
    return normalized


def _workbench_turning_rows(snapshot):
    """轉折觀察只讀共享快照；保留方向與確認狀態，不把規則訊號改寫成買賣建議。"""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    rows = []
    for raw in snapshot.get("items") or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        state = str(raw.get("state") or "").strip()
        direction = str(raw.get("direction") or "").strip()
        flow_key = str(raw.get("direction_flow") or "").strip()
        flow = str(raw.get("direction_flow_label") or "").strip()
        # 舊轉折快照尚未一定有 direction_flow；與既有轉折頁使用同一套
        # event_type／方向相容規則，不能把「轉強／轉弱」泛稱成唯一可見資訊。
        if flow_key not in {"sell_to_buy", "buy_to_sell", "buying_strength", "selling_strength"}:
            legacy_label = str(raw.get("direction_label") or "")
            event_type = str(raw.get("event_type") or "")
            if legacy_label == "賣轉買" or event_type == "賣轉買" or "轉買" in event_type:
                flow_key, flow = "sell_to_buy", "賣轉買"
            elif legacy_label == "買轉賣" or event_type == "買轉賣" or "轉賣" in event_type:
                flow_key, flow = "buy_to_sell", "買轉賣"
            elif direction == "up":
                flow_key, flow = "buying_strength", "買方增強"
            elif direction == "down":
                flow_key, flow = "selling_strength", "賣方增強"
            else:
                flow_key, flow = "unknown", "方向待確認"
        state_label = str(raw.get("state_label") or {
            "confirmed": "已確認", "observing": "觀察中", "invalid": "已失效"}.get(state, "狀態待確認"))
        details = raw.get("reason_details") if isinstance(raw.get("reason_details"), list) else []
        rows.append({
            "source": "轉折",
            "code": code,
            "name": _workbench_display_name(raw, code),
            "industry": _workbench_text(raw.get("industry"), "轉折觀察"),
            "score": _workbench_number(raw.get("score")),
            "price": _workbench_number(raw.get("close") or raw.get("current_close") or raw.get("price")),
            "change_pct": _workbench_number(raw.get("pct") or raw.get("change_pct")),
            "metric_label": "當日漲跌",
            "institutional_lots": _workbench_number(raw.get("current_total_lots")),
            "signal": f"{flow}／{state_label}",
            "quality": _workbench_number(raw.get("score")),
            "detail": {
                "source_date": str(snapshot.get("data_date") or "未標日期"),
                "state": state or "未標示",
                "state_label": state_label,
                "flow": flow,
                "flow_key": flow_key,
                "event_type": _workbench_text(raw.get("event_type"), ""),
                "consensus": _workbench_text(raw.get("consensus"), ""),
                "state_reason": _workbench_text(raw.get("state_reason"), ""),
                "reasons": [str(item) for item in details if str(item).strip()],
                "invalid_reasons": [str(item) for item in (raw.get("invalid_reasons") or []) if str(item).strip()],
                "current_total_lots": _workbench_number(raw.get("current_total_lots")),
                "magnitude_ratio": _workbench_number(raw.get("magnitude_ratio")),
                "support": _workbench_number(raw.get("support")),
                "resistance": _workbench_number(raw.get("resistance")),
                "vol_ratio": _workbench_number(raw.get("vol_ratio")),
            },
        })
    return rows


def _workbench_etf_rows(payload):
    """ETF 工作台只讀既有商品排名快照；短期／長期指標都保留原始口徑標籤。"""
    payload = payload if isinstance(payload, dict) else {}
    categories = payload.get("categories") if isinstance(payload.get("categories"), dict) else {}
    periods = payload.get("periods") if isinstance(payload.get("periods"), dict) else {}
    rows, seen = [], set()
    for period_key, period_rows in categories.items():
        if not isinstance(period_rows, dict):
            continue
        period_label = ((periods.get(period_key) or {}).get("label") or
                        ("短期價格報酬" if period_key == "short" else "長期價格報酬"))
        for category_key, values in period_rows.items():
            if not isinstance(values, list):
                continue
            for raw in values:
                if not isinstance(raw, dict):
                    continue
                code = str(raw.get("code") or "").strip()
                unique_key = (period_key, category_key, code)
                if not re.fullmatch(r"\d{4,6}|\d{4,5}[A-Za-z]", code) or unique_key in seen:
                    continue
                seen.add(unique_key)
                category = _workbench_text(raw.get("category"), category_key)
                detail = {
                    "source_date": str(payload.get("market_data_date") or payload.get("data_date") or "未標日期"),
                    "period_key": period_key,
                    "period_label": period_label,
                    "category": category,
                    "return_pct": _workbench_number(raw.get("return_pct")),
                    "market_return_pct": _workbench_number(raw.get("market_return_pct")),
                    "excess_pct": _workbench_number(raw.get("excess_pct")),
                    "max_drawdown_pct": _workbench_number(raw.get("max_drawdown_pct")),
                    "volatility_pct": _workbench_number(raw.get("volatility_pct")),
                    "annualized_yield_pct": _workbench_number(raw.get("distribution_annualized_yield_pct")),
                    "distribution_status": _workbench_text(raw.get("distribution_status"), ""),
                    "distribution_stability_status": _workbench_text(raw.get("distribution_stability_status"), ""),
                    "distribution_recent_records": raw.get("distribution_recent_records") or [],
                    "asset_size_billion": _workbench_number(raw.get("asset_size_billion")),
                    "holders": _workbench_number(raw.get("holders")),
                    "ytd_avg_turnover_million": _workbench_number(raw.get("ytd_avg_turnover_million")),
                    "observations": _workbench_number(raw.get("observations")),
                    "start_date": _workbench_text(raw.get("start_date"), ""),
                    "end_date": _workbench_text(raw.get("end_date"), ""),
                    "comment": _workbench_text(raw.get("comment"), ""),
                }
                rows.append({
                    "source": "ETF",
                    "row_key": f"ETF:{period_key}:{category_key}:{code}",
                    "code": code,
                    "name": _workbench_display_name(raw, code),
                    "industry": category,
                    "score": _workbench_number(raw.get("score")),
                    "price": _workbench_number(raw.get("official_close") or raw.get("close") or raw.get("price")),
                    "change_pct": _workbench_number(raw.get("return_pct")),
                    "metric_label": period_label,
                    "institutional_lots": None,
                    "signal": _workbench_text(raw.get("comment"), "商品排名"),
                    "quality": _workbench_number(raw.get("observations"), 0),
                    "return_pct": _workbench_number(raw.get("return_pct")),
                    "excess_pct": _workbench_number(raw.get("excess_pct")),
                    "annualized_yield_pct": _workbench_number(raw.get("distribution_annualized_yield_pct")),
                    "detail": detail,
                })
    return rows


def _workbench_chips_rows(result):
    """把籌碼超人已保存快照轉為工作台列，不取得即時法人資料也不觸發重算。"""
    result = result if isinstance(result, dict) else {}
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    if not payload.get("available"):
        return []
    data_date = str(result.get("data_date") or payload.get("data_date") or "未標日期")
    group_labels = {
        "trust_buy": "投信認養", "foreign_buy": "外資認養",
        "both_buy": "外資投信同買", "trust_sell": "投信調節",
        "both_sell": "外資投信同賣",
    }
    group_notes = {
        "投信認養": "國內基金持續站在買方，至少 6／10 天才列入認養。",
        "外資認養": "外資近十日持續買超；外資也可能包含指數或 ETF 被動調整。",
        "外資投信同買": "外資與投信同時站買方，顯示兩類資金方向一致。",
        "投信調節": "投信近十日持續賣超，作為籌碼面的撤退訊號觀察。",
        "外資投信同賣": "外資與投信同時站賣方，顯示兩類資金方向一致轉弱。",
    }
    rows = []
    for group_key, label in group_labels.items():
        for raw in (payload.get("groups") or {}).get(group_key) or []:
            if not isinstance(raw, dict):
                continue
            code = str(raw.get("code") or "").strip()
            if not re.fullmatch(r"\d{4}", code):
                continue
            lots = _workbench_number(raw.get("lots"), 0)
            trust_lots = _workbench_number(raw.get("trust_lots"))
            foreign_lots = _workbench_number(raw.get("foreign_lots"))
            if group_key in {"trust_buy", "trust_sell"} and trust_lots is None:
                trust_lots = lots
            if group_key == "foreign_buy" and foreign_lots is None:
                foreign_lots = lots
            group_lots = lots if group_key in {"both_buy", "both_sell"} else None
            detail = {
                "source_date": data_date,
                "group": label,
                "institutional_lots": lots,
                "amount_billion": _workbench_number(raw.get("amount_billion")),
                "hit_days": _workbench_number(raw.get("hit_days")),
                "total_days": _workbench_number(raw.get("total_days")),
                "foreign_lots": foreign_lots,
                "trust_lots": trust_lots,
                "group_lots": group_lots,
                "plain_note": group_notes.get(label, "依已保存法人資料整理，未補入推測訊號。"),
            }
            rows.append({
                "source": "籌碼", "code": code,
                "name": _workbench_display_name(raw, code),
                "industry": "法人籌碼", "score": None,
                "price": None, "change_pct": None,
                "metric_label": "近十日金額",
                "institutional_lots": lots,
                "institutional_amount": _workbench_number(raw.get("amount_billion")),
                "turnover": _workbench_number(raw.get("amount_billion")),
                "signal": label,
                "quality": _workbench_number(raw.get("hit_days")), "detail": detail,
            })
    for raw in payload.get("institutional_shifts") or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        lots = _workbench_number(raw.get("current_total_lots"), 0)
        rows.append({
            "source": "籌碼", "code": code,
            "name": _workbench_display_name(raw, code),
            "industry": "法人籌碼", "score": None,
            "price": None, "change_pct": None,
            "metric_label": "法人方向變化",
            "institutional_lots": lots,
            "institutional_amount": _workbench_number(raw.get("amount_billion")),
            "signal": _workbench_text(raw.get("event_type"), "法人方向變化"),
            "quality": _workbench_number(raw.get("magnitude_ratio")),
            "detail": {
                "source_date": data_date,
                "event_type": _workbench_text(raw.get("event_type"), "法人方向變化"),
                "investor_changes": raw.get("investor_changes") or [],
                "consensus": _workbench_text(raw.get("consensus"), "待確認"),
                "institutional_lots": lots,
                "magnitude_ratio": _workbench_number(raw.get("magnitude_ratio")),
                "plain_note": "依投資人方向變化與已保存強度資料整理；不是即時預測。",
            },
        })
    return rows


def build_workbench_snapshot_payload(uid=None):
    """組合互動選股台所需的已保存資料。整個函式不得啟動全市場掃描或背景重算。"""
    sources, rows = {}, []
    blackhorse = _load_persisted_screener_snapshot("blackhorse")
    if blackhorse:
        rows.extend(_workbench_screener_rows("blackhorse", blackhorse))
        sources["黑馬"] = {"date": str(blackhorse.get("source_date") or "未標日期"),
                         "computed_at": str(blackhorse.get("computed_at") or "")}

    radar = (_load_recent_live_radar_snapshot()
             if _is_taiwan_intraday_window() else _load_persisted_screener_snapshot("radar"))
    if radar:
        rows.extend(_workbench_screener_rows("radar", radar))
        sources["雷達"] = {"date": str(radar.get("source_date") or "未標日期"),
                         "computed_at": str(radar.get("scan_finished_at") or radar.get("computed_at") or ""),
                         "intraday": bool(_is_taiwan_intraday_window() and radar.get("scan_finished_at"))}

    turning, turning_fresh, turning_source = _get_turning_web_snapshot()
    if turning:
        rows.extend(_workbench_turning_rows(turning))
        sources["轉折"] = {"date": str(turning.get("data_date") or "未標日期"),
                         "fresh": bool(turning_fresh), "source": turning_source}

    etf_payload, etf_fresh, etf_source = _load_etf_product_ranking_snapshot()
    if etf_payload:
        rows.extend(_workbench_etf_rows(etf_payload))
        sources["ETF"] = {"date": str(etf_payload.get("market_data_date") or etf_payload.get("data_date") or "未標日期"),
                         "fresh": bool(etf_fresh), "source": etf_source}

    chips = build_chips_payload(allow_compute=False)
    chips_payload = chips.get("payload") if isinstance(chips, dict) else {}
    if isinstance(chips_payload, dict) and chips_payload.get("available"):
        rows.extend(_workbench_chips_rows(chips))
        sources["籌碼"] = {
            "date": str(chips.get("data_date") or chips_payload.get("data_date") or "未標日期"),
            "computed_at": str(chips.get("computed_at") or ""),
            "source": chips.get("source") or "已保存籌碼快照",
            "available": True,
        }
    else:
        sources["籌碼"] = {"date": "未標日期", "available": False,
                         "note": "籌碼快照正在背景整理，完成後會自動納入此分頁。"}

    personal = {"positions": [], "rank_summary": {}}
    if uid:
        # 只讀取目前登入者的庫存與既有排行榜快照；不讀取其他使用者持股，也不重算排行榜。
        for position in merge_positions(get_positions(uid)):
            code = str(position.get("code") or "").strip()
            if not re.fullmatch(r"\d{4,6}", code):
                continue
            shares = _workbench_number(position.get("shares"), 0)
            cost = _workbench_number(position.get("cost"))
            personal["positions"].append({"code": code, "shares": shares, "cost": cost})
            rows.append({
                "source": "持股",
                "code": code,
                "name": _workbench_display_name(position, code),
                "industry": "個人庫存",
                "score": None,
                "price": None,
                "change_pct": None,
                "metric_label": "當日漲跌",
                "institutional_lots": None,
                "signal": f"持有 {int(shares or 0):,} 股",
                "quality": None,
                "analysis_available": False,
                "detail": {
                    "average_cost": cost,
                    "position_shares": shares,
                    "bought_on": str(position.get("bought_on") or ""),
                    "analysis_available": False,
                    "analysis_note": "此庫存尚未有同日分析快照",
                },
            })
        personal["rank_summary"] = get_fast_rank_summary(uid) or {}

    seen, deduped = set(), []
    for row in rows:
        key = row.get("row_key") or (row.get("source"), row.get("code"))
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    workbench_server_time = taiwan_now().isoformat()
    for metadata in sources.values():
        if isinstance(metadata, dict):
            metadata.setdefault("server_time", workbench_server_time)
    return {
        "ok": True,
        "rows": deduped,
        "sources": sources,
        "personal": personal,
        "market_open": bool(_is_taiwan_intraday_window()),
        "server_time": workbench_server_time,
        "note": ("開盤期間只更新目前可見標的的行情；黑馬、雷達、轉折與 ETF 排名仍依既有快照更新。"
                 if _is_taiwan_intraday_window() else
                 "目前顯示最近有效快照；收盤後固定採用既有正式收盤校正。"),
    }


def _workbench_json_response(payload, status=200):
    response = make_response(json.dumps(_jsonable(payload), ensure_ascii=False), status)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def build_workbench_review_payload():
    """選股台成效分頁按需讀取既有推薦紀錄；不會呼叫選股掃描或寫入快照。"""
    now = time.time()
    cached = getattr(build_workbench_review_payload, "_cache", None)
    if isinstance(cached, dict) and now - float(cached.get("at") or 0) < 300:
        return cached["payload"]
    modes = []
    for mode, label in (("blackhorse", "黑馬"), ("radar", "雷達")):
        evaluation = evaluate_picks(mode)
        if not evaluation:
            modes.append({"key": mode, "label": label, "available": False,
                          "note": "尚未累積足夠的已保存推薦紀錄。", "horizons": []})
            continue
        horizon_rows = []
        for period in ("5–19 日", "20–59 日", "60 日以上"):
            stats = (evaluation.get("horizons") or {}).get(period)
            if not isinstance(stats, dict):
                continue
            market = _workbench_number(stats.get("market"))
            average = _workbench_number(stats.get("avg"))
            horizon_rows.append({
                "period": period, "samples": _workbench_number(stats.get("n"), 0),
                "average_pct": average, "median_pct": _workbench_number(stats.get("median")),
                "win_rate": _workbench_number(stats.get("win_rate")),
                "market_pct": market,
                "excess_pct": (round(average - market, 2)
                               if average is not None and market is not None else None),
            })
        modes.append({"key": mode, "label": label, "available": bool(horizon_rows),
                      "total_picks": _workbench_number(evaluation.get("total_picks"), 0),
                      "pending": _workbench_number(evaluation.get("pending"), 0),
                      "note": "只統計已走完期間的推薦，並與同期大盤報酬對照。",
                      "horizons": horizon_rows})
    payload = {"ok": True, "modes": modes, "computed_at": taiwan_now().isoformat(),
               "note": "成效只在開啟此分頁時讀取既有推薦紀錄；不重新掃描市場。"}
    build_workbench_review_payload._cache = {"at": now, "payload": payload}
    return payload


def render_workbench_body(initial_tab=""):
    """正式工作台只回傳前端殼；真實資料由同源、權杖保護的快照 API 局部載入。"""
    initial_tab = str(initial_tab or "").strip()
    if initial_tab not in {"籌碼", "成效"}:
        initial_tab = ""
    body = r'''
<section class="wb-shell" id="stockbot-workbench" data-workbench="snapshot-first-v1" data-initial-tab="__INITIAL_TAB__">
  <div class="wb-intro"><div><p class="wb-kicker">選股工作台 · 快照優先</p><h2>選股工作台</h2><p class="wb-sub">先讀取最近有效快照；排序、篩選與個股詳情都不重新掃描市場。</p></div><div class="wb-status" id="wb-status">讀取最近有效快照…</div></div>
  <div class="wb-pulse" id="wb-pulse"><span>資料狀態</span><b>快照優先</b><i></i><i></i><em>先看已保存資料；個股與 ETF 分開比較，不混在同一榜單。</em></div>
  <div class="wb-asset-tabs" id="wb-asset-tabs" aria-label="資產類型"><button type="button" class="on" data-asset="stock">個股</button><button type="button" data-asset="etf">ETF 專區</button></div>
  <div class="wb-tabs" id="wb-tabs" aria-label="選股資料來源"></div>
  <div class="wb-tools"><label><span>⌕</span><input id="wb-search" placeholder="搜尋代號、名稱或產業" autocomplete="off"></label><button type="button" id="wb-filter">篩選條件</button><button type="button" id="wb-refresh">重新整理</button></div>
  <div class="wb-filter-panel" id="wb-filter-panel" hidden><div><b>當日漲跌</b><button type="button" data-dir="all" class="on">不限</button><button type="button" data-dir="up">上漲</button><button type="button" data-dir="down">下跌</button></div></div><div class="wb-mobile-sort" id="wb-mobile-sort" aria-label="排序方式"><span>排序</span><button type="button" data-sort="score" class="on">分數</button><button type="button" data-sort="change_pct">漲跌</button><button type="button" data-sort="institutional_lots">法人</button></div>
  <div class="wb-meta"><span id="wb-count">正在讀取…</span><span id="wb-note"></span></div>
  <div class="wb-table" id="wb-table" aria-live="polite"><div class="wb-head"><span>標的</span><button type="button" data-sort="score">綜合分數</button><button type="button" data-sort="change_pct">報酬／漲跌</button><button type="button" data-sort="institutional_lots">法人方向</button><span>訊號</span><span></span></div><div id="wb-rows"><div class="wb-skeleton"></div><div class="wb-skeleton"></div><div class="wb-skeleton"></div></div></div>
  <p class="wb-disclaimer">資料來源僅限已保存的黑馬、雷達、轉折、ETF 與籌碼快照；成效只在開啟分頁時讀取既有推薦紀錄。資料缺漏維持待確認，不以推測數字補足。</p>
</section>
<aside class="wb-drawer" id="wb-drawer" aria-hidden="true"><button type="button" id="wb-close" aria-label="關閉">×</button><div id="wb-detail"></div></aside><div class="wb-mask" id="wb-mask" hidden></div>
<style>
.wb-shell{margin:16px 0 28px;color:#1d2939}.wb-intro{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;border-bottom:2px solid #27364a;padding:4px 0 16px}.wb-kicker{margin:0;color:#526b84;font-size:11px;font-weight:800;letter-spacing:.08em}.wb-intro h2{margin:5px 0 4px;font-size:28px;letter-spacing:.02em}.wb-sub{margin:0;color:#667085;font-size:13px}.wb-status{border:1px solid #d4dce6;background:#f8fbff;padding:9px 11px;color:#526b84;font-size:11px;white-space:nowrap}.wb-pulse{display:grid;grid-template-columns:130px 180px 1fr 1fr 180px;align-items:center;gap:14px;border:1px solid #d7e0ea;border-left:3px solid #52718d;padding:14px 10px;background:#f7fbff;font-size:12px}.wb-pulse span{font-weight:800}.wb-pulse b{font-family:monospace;font-size:12px}.wb-pulse i{height:3px;background:#c94d45}.wb-pulse i:nth-of-type(2){background:#23795a}.wb-pulse em{font-style:normal;color:#667085}.wb-tabs{display:flex;gap:20px;border-bottom:1px solid #d7e0ea;padding:15px 10px 0}.wb-tabs button{border:0;background:transparent;color:#667085;padding:0 0 12px;font-size:13px;font-weight:700;border-bottom:2px solid transparent}.wb-tabs button.on{color:#274c77;border-color:#52718d}.wb-tools{display:grid;grid-template-columns:1fr auto auto;gap:8px;padding:16px 0}.wb-tools label{display:flex;gap:8px;align-items:center;border:1px solid #cfd9e5;background:#fff;padding:0 11px}.wb-tools input{width:100%;border:0;outline:0;padding:11px 0;font:inherit}.wb-tools button,.wb-filter-panel button{border:1px solid #cfd9e5;background:#fff;padding:9px 12px;color:#344054;font:inherit;font-size:12px}.wb-tools button:hover,.wb-filter-panel button.on{border-color:#52718d;color:#274c77;background:#f1f7fc}.wb-filter-panel{display:flex;gap:24px;margin:-8px 0 12px;padding:12px;border:1px solid #d9e3ed;background:#f7faff;font-size:12px}.wb-filter-panel div{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.wb-filter-panel b{margin-right:5px;color:#526b84}.wb-filter-panel button{padding:5px 9px}.wb-meta{display:flex;justify-content:space-between;gap:12px;padding:7px 0 10px;color:#667085;font-size:11px}.wb-meta b{color:#1d2939}.wb-table{border:1px solid #d7e0ea;background:#fff;border-radius:12px;overflow:hidden}.wb-head,.wb-row{display:grid;grid-template-columns:minmax(190px,2fr) minmax(110px,1fr) minmax(110px,1fr) minmax(120px,1fr) minmax(145px,1.25fr) 22px;gap:10px;align-items:center;padding:12px 14px}.wb-head{background:#f3f7fb;border-bottom:1px solid #d7e0ea;color:#526b84;font-size:11px}.wb-head button{border:0;background:transparent;color:inherit;text-align:left;font:inherit;font-weight:800;padding:0}.wb-head button.on{color:#274c77}.wb-row{border-bottom:1px solid #edf1f5;text-align:left;cursor:pointer;background:#fff}.wb-row:hover{background:#f7fbff;box-shadow:inset 3px 0 #52718d}.wb-name{display:block;font-weight:800;font-size:19px;line-height:1.35;color:#182b3e}.wb-code,.wb-small{display:block;color:#66788a;font-size:13px;font-weight:700;line-height:1.35;margin-top:3px}.wb-industry{display:inline-flex;align-items:center;margin-top:6px;padding:3px 7px;border:1px solid #cad8e6;border-radius:999px;background:#f4f8fc;color:#41617e;font-size:11px;font-weight:800;line-height:1.25}.wb-mobile-sort{display:none;align-items:center;gap:6px;margin:-4px 0 12px;color:#526b84;font-size:12px;font-weight:800}.wb-mobile-sort button{border:1px solid #cfd9e5;background:#fff;border-radius:6px;padding:6px 10px;color:#526b84;font:inherit;font-size:11px}.wb-mobile-sort button.on{border-color:#52718d;background:#edf5fb;color:#274c77}.wb-turning-flow{display:inline-flex;margin-top:5px;padding:3px 6px;border-radius:5px;font-size:10px;font-weight:800;line-height:1.25}.wb-turning-flow.sell_to_buy,.wb-turning-flow.buying_strength{background:#fceceb;color:#b42318}.wb-turning-flow.buy_to_sell,.wb-turning-flow.selling_strength{background:#e9f6ef;color:#13734c}.wb-turning-flow.unknown{background:#eef2f6;color:#667085}.wb-num{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:800}.wb-up{color:#b42318}.wb-down{color:#13734c}.wb-flat{color:#7b8795}.wb-tag{display:inline-block;font-size:10px;padding:3px 5px;background:#edf3f8;color:#345673;margin-right:5px}.wb-tag.ETF{background:#e8f4ed;color:#227052}.wb-tag.雷達{background:#e8f5f5;color:#256d6c}.wb-tag.轉折{background:#f0ebf8;color:#674d8c}.wb-tag.籌碼{background:#fff0df;color:#9a5b17}.wb-skeleton{height:54px;margin:0 14px;border-bottom:1px solid #edf1f5;background:linear-gradient(90deg,#fff 20%,#f2f6fa 45%,#fff 70%);background-size:220% 100%;animation:wbscan 1.1s linear infinite}@keyframes wbscan{to{background-position:-120% 0}}.wb-review-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:14px}.wb-review-card{border:1px solid #d7e0ea;background:#fff;border-radius:10px;padding:14px}.wb-review-card h3{margin:0 0 5px;font-size:18px}.wb-review-card p{margin:0 0 10px;color:#667085;font-size:12px}.wb-review-row{display:grid;grid-template-columns:1.1fr 1fr 1fr;gap:8px;padding:10px 0;border-top:1px solid #edf1f5;font-size:12px}.wb-review-row b{display:block;font-family:ui-monospace,monospace}.wb-disclaimer{margin:12px 0;color:#7b8795;font-size:11px;line-height:1.55}.wb-mask{position:fixed;inset:0;background:rgba(23,42,58,.22);z-index:30}.wb-drawer{position:fixed;z-index:31;right:0;top:0;bottom:0;width:min(430px,92vw);padding:22px;background:#fff;box-shadow:-12px 0 32px rgba(23,42,58,.16);transform:translateX(110%);transition:transform .18s ease-out;overflow:auto}.wb-drawer.open{transform:translateX(0)}.wb-drawer>button{float:right;border:0;background:transparent;font-size:26px;color:#526b84}.wb-detail h3{margin:5px 0;font-size:25px}.wb-detail p{color:#667085;font-size:12px}.wb-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#dbe4ec;margin:18px 0}.wb-detail-grid div{background:#f9fbfd;padding:11px}.wb-detail-grid small{display:block;color:#7b8795}.wb-detail-grid b{display:block;margin-top:5px;font-family:ui-monospace,monospace}.wb-facts{padding:0;margin:0;list-style:none}.wb-facts li{border-top:1px solid #e4ebf1;padding:10px 0;font-size:12px;line-height:1.55}@media(max-width:620px){.wb-intro{display:block}.wb-status{display:inline-block;margin-top:12px}.wb-pulse{grid-template-columns:1fr 1fr;padding:12px}.wb-pulse i{grid-column:span 1}.wb-pulse em{grid-column:1/-1;border-top:1px solid #d7e0ea;padding-top:8px}.wb-tabs{gap:14px;overflow:auto}.wb-tools{grid-template-columns:1fr auto}.wb-tools label{grid-column:1/-1}.wb-filter-panel{display:block}.wb-filter-panel div+div{margin-top:10px}.wb-meta{display:block}.wb-meta span{display:block;margin-top:4px}.wb-head{display:none}.wb-row{grid-template-columns:1.6fr 1fr 1fr 18px;gap:8px;padding:12px}.wb-row .wb-institutional,.wb-row .wb-signal{display:none}.wb-mobile-sort{display:flex}.wb-row .wb-name{font-size:18px}.wb-row .wb-code{font-size:13px}.wb-row .wb-industry{font-size:11px}.wb-row .wb-tag{font-size:10px}.wb-review-grid{grid-template-columns:1fr;padding:10px}.wb-drawer{width:100%;}.wb-detail-grid{grid-template-columns:1fr 1fr}}
</style>
<style>
.wb-asset-tabs{display:flex;gap:8px;margin:16px 0 4px;border-bottom:1px solid #d7e0ea}.wb-asset-tabs button{border:1px solid #cfd9e5;border-bottom:0;border-radius:8px 8px 0 0;background:#fff;padding:10px 18px;color:#526b84;font:inherit;font-weight:800}.wb-asset-tabs button.on{background:#edf5fb;border-color:#52718d;color:#274c77}.wb-rich-row{min-height:155px}.wb-row-main{min-width:0}.wb-score-block,.wb-price-block{display:flex;flex-direction:column;gap:5px}.wb-score{font-size:23px;color:#1d2939}.wb-score small{font-size:11px;color:#667085;margin-left:4px}.wb-score-parts,.wb-fact-line{display:block;color:#667085;font-size:11px;line-height:1.5;overflow-wrap:anywhere}.wb-breakout{display:inline-block;margin:5px 5px 0 0;padding:3px 7px;border:1px solid #8a6a35;border-radius:4px;background:#fffaf0;color:#72541e;font-size:11px;font-weight:800}.wb-high-status{display:inline-block;margin:5px 5px 0 0;padding:3px 7px;border:1px solid #52718d;border-radius:4px;background:#edf5fb;color:#274c77;font-size:11px;font-weight:800}.wb-holding-row{min-height:110px;align-items:center}.wb-holding-shares{display:flex;flex-direction:column;gap:5px;align-items:flex-end}.wb-holding-shares b{font-size:18px;color:#344054}.wb-holding-shares small{font-size:11px;color:#667085}.wb-holding-row .wb-fact-line{margin-top:8px}.wb-chip-group,.wb-turning-group{margin:12px 0;padding:12px;border:1px solid #d7e0ea;border-radius:12px;background:#f9fbfd}.wb-chip-group h3,.wb-turning-group h3{margin:0 0 7px;font-size:17px;color:#1d2939}.wb-chip-group h3 small,.wb-turning-group h3 small{font-size:11px;color:#667085;font-weight:600}.wb-turning-buy{border-left:4px solid #23795a}.wb-turning-sell{border-left:4px solid #b42318}.wb-turning-invalid{border-left:4px solid #b4a78d;background:#fffdf5}.wb-empty{padding:22px;color:#667085}.wb-tag.持股{background:#f5e8df;color:#85513a}.wb-rank-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;background:#ded7cc}.wb-rank-card{background:#fffdf8;padding:18px}.wb-rank-card small,.wb-rank-card em{display:block;color:#857d71;font-size:11px;font-style:normal}.wb-rank-card b{display:block;font-family:ui-monospace,monospace;font-size:23px;margin:7px 0}.wb-rank-card span{font-size:12px;font-weight:800}@media(max-width:620px){.wb-rank-grid{grid-template-columns:1fr}}
.wb-rich-row{align-items:start}.wb-rich-row .wb-row-main{grid-column:1 / span 2}.wb-rich-row .wb-score-block,.wb-rich-row .wb-price-block{align-self:start}.wb-rich-row .wb-institutional,.wb-rich-row .wb-signal{display:flex;flex-direction:column;gap:4px;color:#344054}.wb-rich-row .wb-institutional small,.wb-rich-row .wb-signal small{font-size:11px;color:#667085}.wb-chip-group .wb-rich-row{border:0;border-top:1px solid #e8edf2}.wb-turning-group .wb-rich-row{border:0;border-top:1px solid #e8edf2}.wb-turning-group .wb-row-main{grid-column:1 / span 2}.wb-turning-group .wb-score-block{display:none}.wb-turning-group .wb-price-block{grid-column:3}.wb-turning-group .wb-institutional,.wb-turning-group .wb-signal{display:none}
@media(max-width:620px){.wb-rich-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;padding:14px 10px}.wb-rich-row .wb-row-main{grid-column:1/-1}.wb-rich-row .wb-score-block,.wb-rich-row .wb-price-block,.wb-rich-row .wb-institutional,.wb-rich-row .wb-signal{display:flex;grid-column:auto}.wb-rich-row .wb-score-block{grid-column:1}.wb-rich-row .wb-price-block{grid-column:2}.wb-rich-row .wb-institutional{grid-column:1}.wb-rich-row .wb-signal{grid-column:2}.wb-turning-group .wb-rich-row .wb-institutional,.wb-turning-group .wb-rich-row .wb-signal{display:none}.wb-turning-group .wb-rich-row .wb-price-block{grid-column:2}.wb-name{font-size:21px}}
</style>
<script>
(function(){
  var root=document.getElementById('stockbot-workbench'); if(!root) return;
  var initialTab=(root.dataset.initialTab||new URLSearchParams(location.search).get('tab')||'').trim();
  var state={rows:[],sources:{},personal:{},assetMode:'stock',source:'黑馬',query:'',kind:'all',dir:'all',sort:'score',desc:true,marketOpen:false,timer:null,review:null,reviewLoading:false};
  var tabs=document.getElementById('wb-tabs'), rowsEl=document.getElementById('wb-rows'), note=document.getElementById('wb-note'), count=document.getElementById('wb-count'), status=document.getElementById('wb-status'), pulse=document.getElementById('wb-pulse'), drawer=document.getElementById('wb-drawer'), mask=document.getElementById('wb-mask');
  function token(){try{return new URLSearchParams(location.search).get('t')||localStorage.getItem('stockbot_web_token')||''}catch(e){return ''}}
  function api(path){return path+(path.indexOf('?')>-1?'&':'?')+'fragment=1'+(token()?'&t='+encodeURIComponent(token()):'')}
  function esc(v){return String(v==null?'—':v).replace(/[&<>'"]/g,function(c){return({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'})[c]})}
  function pct(v){if(v==null||isNaN(v))return '<span class="wb-flat">—</span>';return '<span class="'+(v>0?'wb-up':v<0?'wb-down':'wb-flat')+'">'+(v>0?'+':'')+Number(v).toFixed(2)+'%</span>'}
  function money(v){return v==null?'—':Number(v).toLocaleString('zh-TW',{minimumFractionDigits:2,maximumFractionDigits:2})}
  function latestSourceDate(data){var sourceDates=Object.keys((data&&data.sources)||{}).map(function(k){var s=data.sources[k]||{};return s.date||s.computed_at||'';}).filter(Boolean);return sourceDates.length?sourceDates.sort().slice(-1)[0]:'未標日期';}
  function workbenchStatusText(data, marketOpen){return '最近資料：'+latestSourceDate(data)+'｜'+(marketOpen?'只更新目前可見標的，每 90 秒一次；排名仍依快照':'價格不再輪詢，固定使用已確認的正式收盤校正');}
  function isEtf(row){return row&&row.source==='ETF';}
  function sources(){var order=state.assetMode==='etf'?['ETF']:['黑馬','雷達','轉折','籌碼','成效','持股','我的排行'];return order.filter(function(s){if(s==='成效'||s==='我的排行')return s==='成效'||(state.personal&&Object.keys(state.personal.rank_summary||{}).length);return state.rows.some(function(r){return r.source===s&&((state.assetMode==='etf')===isEtf(r));})||!!state.sources[s];});}
  function renderAssetTabs(){document.querySelectorAll('#wb-asset-tabs button').forEach(function(b){b.classList.toggle('on',b.dataset.asset===state.assetMode);});}
  function renderTabs(){var list=sources();if(list.indexOf(state.source)<0)state.source=list[0]||'';tabs.innerHTML=list.map(function(s){var n=s==='成效'||s==='我的排行'?'':state.rows.filter(function(r){return r.source===s}).length;return '<button type="button" class="'+(state.source===s?'on':'')+'" data-source="'+esc(s)+'">'+esc(s)+(n===''?'':' <small>'+n+'</small>')+'</button>'}).join('');}
  function filtered(){var q=state.query.trim().toLowerCase();return state.rows.filter(function(r){var etf=r.source==='ETF';return (state.source==='全部'||r.source===state.source)&&(!q||[r.code,r.name,r.industry].join(' ').toLowerCase().indexOf(q)>-1)&&(state.kind==='all'||(state.kind==='etf'?etf:!etf))&&(state.dir==='all'||(state.dir==='up'&&Number(r.change_pct)>0)||(state.dir==='down'&&Number(r.change_pct)<0));}).sort(function(a,b){var av=a[state.sort],bv=b[state.sort];av=av==null?-Infinity:Number(av);bv=bv==null?-Infinity:Number(bv);return state.desc?bv-av:av-bv;});}
  function reviewPct(v){return v==null?'待確認':(Number(v)>0?'+':'')+Number(v).toFixed(1)+'%';}
  function valueText(v,suffix){return v==null?'待確認':esc(v)+(suffix||'');}
  function renderRichRow(r){var d=r.detail||{},highStatus=d.high_status&&d.high_status!=='待確認'?'<span class="wb-high-status">'+esc(d.high_status)+'</span>':'',breakout=d.breakout&&d.breakout!=='未建立突破標示'?'<span class="wb-breakout">'+esc(d.breakout)+'</span>':'';var amount=r.institutional_amount==null?null:Number(r.institutional_amount);var inst=r.institutional_lots==null?'待確認':(r.institutional_lots>0?'+':'')+Number(r.institutional_lots).toLocaleString('zh-TW')+' 張';var legacyCaps=Array.isArray(r.caps)?r.caps:(r.category==='電子'?['25','25','20','20','10']:r.category==='傳產'?['20','25','25','20','10']:null),scoreParts=[];[['營收',r.rev,legacyCaps&&legacyCaps[0]],['估值',r.val,legacyCaps&&legacyCaps[1]],['產業',r.mom,legacyCaps&&legacyCaps[2]],['連續性',r.streak_score,legacyCaps&&legacyCaps[3]],['籌碼技術',r.chip,legacyCaps&&legacyCaps[4]]].forEach(function(x){if(x[1]!=null)scoreParts.push(x[0]+esc(x[1])+(x[2]?'/'+x[2]:''));});var basics=r.source==='籌碼'?('法人張數 '+valueText(r.institutional_lots,' 張')+'　近十日金額 '+valueText(r.institutional_amount,' 億')+'　'+esc(d.plain_note||'依已保存法人資料整理')):r.source==='雷達'?('法人近十日 '+valueText(r.institutional_lots,' 張')+(r.radar_state?'　'+esc(r.radar_state):'')):('營收年增 '+valueText(r.cum_yoy,'%')+'　PE '+valueText(r.pe)+'　PEG '+valueText(r.peg)+'　殖利率 '+valueText(r.yield,'%')+'　PB '+valueText(r.pb));var turnover=r.turnover==null?null:Number(r.turnover);var chipTrust=r.trust_lots??d.trust_lots,chipForeign=r.foreign_lots??d.foreign_lots,chipGroup=d.group_lots??r.group_lots;var trend=r.source==='籌碼'?('投信 '+valueText(chipTrust==null&&chipGroup==null?null:chipTrust==null?'合計 '+chipGroup:chipTrust,' 張')+'　外資 '+valueText(chipForeign==null&&chipGroup==null?null:chipForeign==null?'合計 '+chipGroup:chipForeign,' 張')+'　連續 '+valueText(d.hit_days||d.total_days,' 日')):r.source==='雷達'?('連買 '+valueText(d.streak||r.up_streak,' 日')+'　量能 '+valueText(d.vol_ratio,' 倍')+'　成交金額 '+(turnover==null?'待確認':turnover.toFixed(1)+' 億')):('法人近十日 '+valueText(r.institutional_lots,' 張')+'　連買 '+valueText(r.buy_days||d.streak,' 日')+'　成交金額 '+(turnover==null?'待確認':turnover.toFixed(1)+' 億'));var flow=r.source==='轉折'?'<small class="wb-turning-flow '+esc(d.flow_key||'unknown')+'">'+esc(r.signal)+'</small>':'',signalBlock=r.signal?'<span class="wb-signal"><b>'+esc(r.signal)+'</b><small>'+valueText(r.turnover,' 成交額')+'</small></span>':'<span class="wb-signal"></span>';return '<button type="button" class="wb-row wb-rich-row" data-code="'+esc(r.code)+'" data-source="'+esc(r.source)+'" data-row-key="'+esc(r.row_key||'')+'"><span class="wb-row-main"><span class="wb-tag '+esc(r.source)+'">'+esc(r.source)+'</span><b class="wb-name">'+esc(r.code)+'　'+esc(r.name)+'</b>'+highStatus+breakout+'<small class="wb-industry">'+esc(r.industry)+'</small>'+flow+'<small class="wb-fact-line">'+basics+'</small><small class="wb-fact-line">'+trend+'</small></span><span class="wb-score-block"><b class="wb-score">'+(r.source==='雷達'?'雷達第 '+esc(r.radar_rank||'—')+' 名<small>原始雷達排序</small>':(r.category==='金融'&&r.score==null?'金融股不評分':(r.score==null?'待確認':esc(r.score)+'<small>/100 分</small>')))+'</b><small class="wb-score-parts">'+(r.source==='雷達'?'突破／量能／法人連買／當日漲幅':(r.score_policy||d.score_policy||(scoreParts.length?scoreParts.join(' · '):'舊版未提供評分拆解')))+'</small></span><span class="wb-price-block"><b class="wb-num">'+money(r.price)+'</b><small>'+pct(r.change_pct)+'</small></span><span class="wb-institutional wb-num"><b>'+esc(inst)+'</b><small>法人近十日</small></span>'+signalBlock+'<span>›</span></button>'}   function renderHoldingRow(r){var d=r.detail||{},shares=Number(d.position_shares||0),noteText=d.analysis_note||'此庫存尚未有同日分析快照';return '<button type="button" class="wb-row wb-holding-row" data-code="'+esc(r.code)+'" data-source="'+esc(r.source)+'"><span class="wb-row-main"><span class="wb-tag 持股">持股</span><b class="wb-name">'+esc(r.code)+'　'+esc(r.name)+'</b><small class="wb-industry">個人庫存</small><small class="wb-fact-line">'+esc(noteText)+'</small></span><span class="wb-holding-shares"><b>持有 '+shares.toLocaleString('zh-TW')+' 股</b><small>尚未連結同日分析</small></span><span>›</span></button>';}   function renderTurningGrouped(){var groups=[['buy_to_sell','買轉賣','wb-turning-buy'],['sell_to_buy','賣轉買','wb-turning-sell'],['invalid','失效','wb-turning-invalid']];var html=groups.map(function(g){var list=state.rows.filter(function(r){return r.source==='轉折'&&((g[0]==='invalid'?(r.detail||{}).state==='invalid':(g[0]==='sell_to_buy'?['sell_to_buy','buying_strength'].indexOf((r.detail||{}).flow_key)>=0:['buy_to_sell','selling_strength'].indexOf((r.detail||{}).flow_key)>=0)));}).slice().sort(function(a,b){return (b.score||-1)-(a.score||-1)});if(!list.length)return '';var visible=list.slice(0,3).map(renderRichRow).join('');var more=list.slice(3).map(renderRichRow).join('');return '<section class="wb-turning-group '+g[2]+'"><h3>'+g[1]+' <small>'+list.length+' 檔</small></h3>'+visible+(more?'<details class="wb-turning-more"><summary>其餘 '+(list.length-3)+' 檔</summary>'+more+'</details>':'')+'</section>';}).join('');rowsEl.innerHTML=html||'<div class="wb-empty">目前沒有已保存的轉折資料。</div>';count.textContent='轉折依買轉賣、賣轉買、失效分組；每組先顯示 3 檔';}   function renderChipsGrouped(list){var groups=['投信認養','外資認養','外資投信同買','投信調節','外資投信同賣'];function section(label,xs,extraClass){if(!xs.length)return '';var visible=xs.slice(0,3).map(renderRichRow).join(''),more=xs.slice(3).map(renderRichRow).join('');return '<section class="wb-chip-group '+(extraClass||'')+'"><h3>'+esc(label)+' <small>'+xs.length+' 檔</small></h3>'+visible+(more?'<details class="wb-turning-more"><summary>其餘 '+(xs.length-3)+' 檔</summary>'+more+'</details>':'')+'</section>';}var shifts=list.filter(function(r){return groups.indexOf(r.signal)<0;});var html=section('法人籌碼突變',shifts,'wb-chip-shifts');html+=groups.map(function(label){return section(label,list.filter(function(r){return r.signal===label;}));}).join('');rowsEl.innerHTML=html||'<div class="wb-empty">目前沒有符合舊版近十日籌碼規則的資料。</div>';count.textContent='近十日籌碼：法人籌碼突變置頂；其後依投信認養、外資認養、同買、投信調節、同賣分組，每組先顯示 3 檔';}   function renderEtfGrouped(list){var periods=[['short','短期'],['long','長期']],categories=['主動式','高股息','市值型','主題型'];var html=periods.map(function(p){var periodRows=list.filter(function(r){return (r.detail||{}).period_key===p[0];});if(!periodRows.length)return '';var blocks=categories.map(function(category){var xs=periodRows.filter(function(r){return r.industry===category;});if(!xs.length)return '';var visible=xs.slice(0,3).map(renderRichRow).join(''),more=xs.slice(3).map(renderRichRow).join('');return '<section class="wb-etf-category"><h4>'+esc(category)+' <small>'+xs.length+' 檔</small></h4>'+visible+(more?'<details class="wb-turning-more"><summary>其餘 '+(xs.length-3)+' 檔</summary>'+more+'</details>':'')+'</section>';}).join('');return '<section class="wb-etf-period"><h3>'+esc(p[1])+'排名</h3>'+blocks+'</section>';}).join('');rowsEl.innerHTML=html||'<div class="wb-empty">目前沒有已保存的 ETF 短期／長期排名資料。</div>';count.textContent='ETF 依舊版短期／長期及四種類別分組；每類先顯示 3 檔';}   function renderReview(){count.textContent='成效只在開啟此分頁時讀取既有推薦紀錄';if(!state.review){rowsEl.innerHTML='<div class="wb-skeleton"></div><div class="wb-skeleton"></div>';if(!state.reviewLoading){state.reviewLoading=true;fetch(api('/web/api/workbench/review'),{credentials:'same-origin'}).then(function(r){if(r.status===401)throw new Error('AUTH');if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(data){state.review=data;state.reviewLoading=false;note.textContent=data.note||'';render();}).catch(function(e){state.reviewLoading=false;rowsEl.innerHTML='<div class="wb-skeleton" style="animation:none;background:#fff;color:#8b4034;padding:18px">成效資料暫時無法載入，請稍後再試。</div>';if(e.message==='AUTH')location.reload();});}return;}var modes=state.review.modes||[];rowsEl.innerHTML='<div class="wb-review-grid">'+modes.map(function(m){var rows=(m.horizons||[]).map(function(h){return '<div class="wb-review-row"><span><b>'+esc(h.period)+'</b><small>'+esc(h.samples)+' 筆・勝率 '+esc(reviewPct(h.win_rate))+'</small></span><span><small>平均／中位</small><b>'+esc(reviewPct(h.average_pct))+'／'+esc(reviewPct(h.median_pct))+'</b></span><span><small>相對大盤</small><b class="'+(Number(h.excess_pct)>0?'wb-up':Number(h.excess_pct)<0?'wb-down':'wb-flat')+'">'+esc(reviewPct(h.excess_pct))+'</b></span></div>';}).join('');return '<section class="wb-review-card"><h3>'+esc(m.label)+'</h3><p>'+esc(m.note||'尚無資料')+'</p>'+(rows||'<p>目前尚無走完期間的樣本。</p>')+'</section>';}).join('')+'</div>';}
  function render(){renderAssetTabs();renderTabs();document.querySelector('.wb-head').hidden=['成效','轉折','籌碼','ETF','持股','我的排行'].indexOf(state.source)>=0;if(state.source==='成效'){renderReview();return;}var list=filtered();if(state.source==='轉折'){renderTurningGrouped();return;}if(state.source==='籌碼'){renderChipsGrouped(list);return;}if(state.source==='ETF'){renderEtfGrouped(list);return;}if(state.source==='持股'){count.textContent='你的庫存；沒有同日分析快照時不顯示評分或待確認欄位';rowsEl.innerHTML=list.length?list.map(renderHoldingRow).join(''):'<div class="wb-empty">目前沒有已保存的持股。</div>';return;}document.querySelectorAll('.wb-head button').forEach(function(b){b.classList.toggle('on',b.dataset.sort===state.sort)});if(state.source==='我的排行'){var rank=state.personal&&state.personal.rank_summary||{};var html=['short','long'].map(function(k){var r=rank[k]||{},delta=r.delta==null?'尚無前次比較':(r.delta>0?'↑ '+r.delta:'↓ '+Math.abs(r.delta))+' 名';return '<div class="wb-rank-card"><small>'+esc(r.label||k)+'</small><b>'+(r.rank==null?'尚無名次':'第 '+esc(r.rank)+' 名')+'</b><span class="'+(r.direction==='up'?'wb-up':r.direction==='down'?'wb-down':'wb-flat')+'">'+esc(delta)+'</span><em>'+esc(r.snapshot_date||'尚無已保存排名')+'</em></div>';}).join('');count.textContent='只顯示你的已保存排行榜名次';rowsEl.innerHTML='<div class="wb-rank-grid">'+(html||'<div class="wb-rank-card">目前尚無已保存排名。</div>')+'</div>';return;}count.innerHTML='符合條件 <b>'+list.length+'</b> 檔';rowsEl.innerHTML=list.length?list.map(renderRichRow).join(''):'<div class="wb-skeleton" style="animation:none;background:#fff;color:#746d61;padding:18px">目前沒有符合條件的已保存資料。</div>';}
  function showDetail(row){var d=row.detail||{},facts=[];Object.keys(d).forEach(function(k){var v=d[k];if(v==null||v===''||(Array.isArray(v)&&!v.length))return;facts.push('<li><b>'+esc({source_date:'資料日',breakout:'突破狀態',high_status:'高點狀態',radar_state:'雷達狀態',category:'股票分類',caps:'原始各項上限',val_desc:'估值說明',mom_desc:'產業動能說明',streak:'法人連買天數',buy_days:'近十日買超天數',vol_ratio:'量能倍數',support:'支撐',resistance:'壓力',state:'轉折狀態',flow:'方向',score_breakdown:'分數組成',data_quality:'資料完整度',group:'籌碼分組',institutional_lots:'近十日法人張數',amount_billion:'近十日法人金額',hit_days:'同方向天數',total_days:'統計交易日',foreign_lots:'外資張數',trust_lots:'投信張數',group_lots:'同向法人張數',plain_note:'原始判讀',return_pct:'價格報酬',excess_pct:'同期超額',annualized_yield_pct:'年化配息殖利率',period_label:'比較期間'}[k]||k)+'</b><br>'+esc(Array.isArray(v)?v.join('；'):typeof v==='object'?JSON.stringify(v):v)+'</li>');});document.getElementById('wb-detail').innerHTML='<p>'+esc(row.source)+' · 已保存快照</p><h3>'+esc(row.name)+' <small>'+esc(row.code)+'</small></h3><div class="wb-detail-grid"><div><small>最新快照價格</small><b>'+money(row.price)+'</b></div><div><small>'+esc(row.metric_label||'當日漲跌')+'</small><b>'+pct(row.change_pct)+'</b></div><div><small>綜合分數</small><b>'+esc(row.score==null?'—':row.score)+'</b></div><div><small>訊號</small><b>'+esc(row.signal)+'</b></div></div><ul class="wb-facts">'+(facts.join('')||'<li>目前沒有更多已確認的快照欄位。</li>')+'</ul>';drawer.classList.add('open');drawer.setAttribute('aria-hidden','false');mask.hidden=false;}
  function updateQuotes(){if(!state.marketOpen||document.hidden)return;var codes=filtered().slice(0,30).map(function(r){return r.code}).join(',');if(!codes)return;fetch(api('/web/api/workbench/quotes?codes='+encodeURIComponent(codes)),{credentials:'same-origin'}).then(function(r){if(r.status===401)throw new Error('AUTH');return r.json()}).then(function(data){(data.updates||[]).forEach(function(q){state.rows.forEach(function(r){if(r.code===q.code&&q.price!=null){r.price=q.price;r.change_pct=q.change_pct;r.metric_label='當日漲跌';}});});if(data.note)note.textContent=data.note;render();}).catch(function(e){if(e.message==='AUTH')location.reload();});}
  function load(){status.textContent='讀取最近有效快照…';fetch(api('/web/api/workbench/snapshot'),{credentials:'same-origin'}).then(function(r){if(r.status===401)throw new Error('AUTH');if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){state.rows=Array.isArray(data.rows)?data.rows:[];state.sources=data.sources||{};state.personal=data.personal||{positions:[],rank_summary:{}};state.marketOpen=!!data.market_open;if(initialTab&&sources().indexOf(initialTab)>=0){state.source=initialTab;initialTab='';}status.textContent=state.marketOpen?'盤中行情局部更新中':'最近有效快照已載入';note.textContent=data.note||'';pulse.innerHTML='<span>資料狀態</span><b>'+esc(state.marketOpen?'盤中局部更新':'收盤正式快照')+'</b><i></i><i></i><em>'+esc(workbenchStatusText(data,state.marketOpen))+'</em>';render();if(state.timer)clearInterval(state.timer);if(state.marketOpen)state.timer=setInterval(updateQuotes,90000);}).catch(function(e){status.textContent='快照暫時無法載入';rowsEl.innerHTML='<div class="wb-skeleton" style="animation:none;background:#fff;color:#8b4034;padding:18px">資料暫時無法載入，請稍後重新整理。沒有顯示推測標的。</div>';if(e.message==='AUTH')location.reload();});}
  document.getElementById('wb-asset-tabs').onclick=function(e){var b=e.target.closest('button[data-asset]');if(!b)return;state.assetMode=b.dataset.asset;state.source=state.assetMode==='etf'?'ETF':'黑馬';state.query='';render();};document.getElementById('wb-search').addEventListener('input',function(e){state.query=e.target.value;render();});document.getElementById('wb-filter').onclick=function(){var p=document.getElementById('wb-filter-panel');p.hidden=!p.hidden;};document.getElementById('wb-refresh').onclick=function(){load();};tabs.onclick=function(e){var b=e.target.closest('button[data-source]');if(b){state.source=b.dataset.source;render();}};document.getElementById('wb-filter-panel').onclick=function(e){var b=e.target.closest('button');if(!b)return;if(b.dataset.kind){state.kind=b.dataset.kind;document.querySelectorAll('[data-kind]').forEach(function(x){x.classList.toggle('on',x===b)});}if(b.dataset.dir){state.dir=b.dataset.dir;document.querySelectorAll('[data-dir]').forEach(function(x){x.classList.toggle('on',x===b)});}render();};function setSort(b){if(!b)return;state.desc=state.sort===b.dataset.sort?!state.desc:true;state.sort=b.dataset.sort;document.querySelectorAll('[data-sort]').forEach(function(x){x.classList.toggle('on',x.dataset.sort===state.sort)});render();}document.querySelector('.wb-head').onclick=function(e){setSort(e.target.closest('button[data-sort]'));};document.getElementById('wb-mobile-sort').onclick=function(e){setSort(e.target.closest('button[data-sort]'));};rowsEl.onclick=function(e){var b=e.target.closest('.wb-row');if(!b)return;var row=b.dataset.rowKey?state.rows.find(function(x){return x.row_key===b.dataset.rowKey}):state.rows.find(function(x){return x.code===b.dataset.code&&x.source===b.dataset.source});if(row)showDetail(row);};document.getElementById('wb-close').onclick=function(){drawer.classList.remove('open');drawer.setAttribute('aria-hidden','true');mask.hidden=true;};mask.onclick=function(){document.getElementById('wb-close').click();};document.addEventListener('visibilitychange',function(){if(!document.hidden)updateQuotes();});load();
})();
</script>'''
    return body.replace("__INITIAL_TAB__", html.escape(initial_tab, quote=True))


@app.route("/web/workbench")
@web_login_required
def web_workbench(uid):
    """正式互動選股工作台：頁面本身秒回，資料僅透過受保護的快照 API 局部載入。"""
    return render_page("選股工作台", render_workbench_body(request.args.get("tab")), nav_active="screener")


@app.route("/web/api/workbench/snapshot")
@web_login_required
def web_workbench_snapshot(uid):
    try:
        return _workbench_json_response(build_workbench_snapshot_payload(uid))
    except Exception as exc:
        print(f"❌ 工作台快照 API 失敗（uid={uid}）：{exc}")
        return _workbench_json_response({"ok": False,
                                         "error": "快照暫時無法載入，不顯示推測資料。"}, 503)


@app.route("/web/api/workbench/quotes")
@web_login_required
def web_workbench_quotes(uid):
    if not _is_taiwan_intraday_window():
        return _workbench_json_response({"ok": True, "updates": [],
                                         "market_open": False,
                                         "note": "目前非一般盤中時段；維持既有正式收盤快照。"})
    requested = [code for code in str(request.args.get("codes") or "").split(",")
                 if re.fullmatch(r"\d{4,6}", code.strip())]
    requested = list(dict.fromkeys(code.strip() for code in requested))[:30]
    if not requested:
        return _workbench_json_response({"ok": True, "updates": [], "market_open": True})
    # 限制只能查工作台目前已有的快照標的，不讓權杖變成任意行情查詢 API。
    allowed = {str(row.get("code")) for row in build_workbench_snapshot_payload(uid).get("rows") or []}
    codes = [code for code in requested if code in allowed]
    if not codes:
        return _workbench_json_response({"ok": True, "updates": [], "market_open": True})
    try:
        quotes = get_realtime_stocks_bulk(codes, workers=min(12, len(codes)), rng="3mo")
        updates = []
        for code in codes:
            quote_data = quotes.get(code) if isinstance(quotes, dict) else None
            if not isinstance(quote_data, dict):
                continue
            price = _workbench_number(quote_data.get("close"))
            pct = _workbench_number(quote_data.get("pct"))
            if price is not None:
                updates.append({"code": code, "price": price, "change_pct": pct,
                                "updated_at": quote_data.get("close_time"),
                                "source": quote_data.get("source") or "既有批次行情"})
        return _workbench_json_response({"ok": True, "updates": updates, "market_open": True,
                                         "note": "盤中價格已局部更新；選股判斷維持最近完成快照。"})
    except Exception as exc:
        print(f"⚠️ 工作台盤中行情更新失敗（uid={uid}）：{exc}")
        return _workbench_json_response({"ok": False, "updates": [], "market_open": True,
                                         "note": "盤中行情暫時無法更新；保留最近有效快照。"}, 503)


@app.route("/web/api/workbench/review")
@web_login_required
def web_workbench_review(uid):
    try:
        return _workbench_json_response(build_workbench_review_payload())
    except Exception as exc:
        print(f"⚠️ 工作台成效 API 失敗（uid={uid}）：{exc}")
        return _workbench_json_response({"ok": False,
                                         "error": "成效資料暫時無法載入，保留其他選股快照。"}, 503)


@app.route("/web/screener")
@web_login_required
def web_screener(uid):
    mode = request.args.get("mode", "blackhorse")
    if mode == "review" and request.args.get("legacy") != "1":
        token = str(request.args.get("t") or "").strip()
        target = "/web/workbench?tab=成效"
        if token:
            target += "&t=" + quote(token, safe="")
        return redirect(target)
    detail_request = request.args.get("detail") == "1"
    if mode == "turning":
        if not wants_fragment():
            return render_loading_shell(
                "轉折觀察", "screener",
                ["正在讀取最近一次轉折快照…", "若需更新，背景整理法人與行情…"],
                note="先顯示最近可用的真實快照；完整刷新不阻塞頁面。",
                staged=False)
        snapshot, fresh, source = _get_turning_web_snapshot()
        if snapshot is None:
            _start_turning_background_refresh()
            snapshot = {"data_date": None, "prior_days": 5, "items": []}
            note = "目前尚未建立轉折快照；系統已在背景整理，稍後重新整理即可看到結果。"
        elif fresh:
            note = None
        else:
            _start_turning_background_refresh()
            note = (f"目前先顯示{source}的最近結果（資料日 {snapshot.get('data_date') or '未標日期'}）；"
                    "最新法人與行情正在背景更新，不會阻塞本頁。")
        return respond_page("轉折觀察", render_turning_observation_web_body(
            snapshot, status_note=note), "screener")
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

    # 完整片段（包含黑馬／雷達預覽後的 detail=1）一律先讀快照，
    # 不可因為使用者再次點擊或前端輪詢，又在 HTTP request 內同步掃描全市場。
    # 雷達盤中優先讀最近 10 分鐘定時 radar_live；只有完全沒有任何快照時，
    # 才做一次冷啟動背景工作，之後由 Render Cron 固定更新。
    snapshot_rows = None
    snapshot_skipped = 0
    snapshot_momentum = {}
    snapshot_source = None
    radar_diagnostics = {}
    live_radar_request = (mode == "radar" and detail_request)
    if (request.method == "GET" and wants_fragment()
            and mode in ("blackhorse", "radar")):
        if mode == "radar":
            live_snapshot = _load_recent_live_radar_snapshot(
                max_age_seconds=RADAR_LIVE_SNAPSHOT_MAX_AGE_SECONDS)
            if live_snapshot:
                live_meta = live_snapshot.get("source_meta") or {}
                live_cache = {
                    "at": time.time(),
                    "rows": live_snapshot.get("rows") or [],
                    "radar_live": True,
                    "skipped": live_snapshot.get("skipped", 0),
                    "momentum": live_snapshot.get("momentum") or {},
                    "source_date": live_snapshot.get("source_date"),
                    "scan_universe_count": live_snapshot.get("scan_universe_count") or live_meta.get("scan_universe_count"),
                    "scan_finished_at": live_snapshot.get("scan_finished_at") or live_meta.get("scan_finished_at"),
                    "radar_diagnostics": live_snapshot.get("radar_diagnostics") or live_meta.get("radar_diagnostics") or {},
                }
                _screener_cache["radar"] = live_cache
                snapshot_source = "盤中定時快照"
                snapshot_rows = list(live_cache.get("rows") or [])
                snapshot_skipped = live_cache.get("skipped", 0)
                snapshot_momentum = live_cache.get("momentum") or {}
                radar_diagnostics = live_cache.get("radar_diagnostics") or {}
            else:
                # 定時快照尚未到下一輪時，先顯示最近收盤結果，但明示不是盤中即時資料。
                recent, snapshot_source = _screener_recent_snapshot("radar")
                if recent is not None:
                    recent_cache = {
                        "at": time.time(),
                        "rows": recent.get("rows") or [],
                        "skipped": recent.get("skipped", 0),
                        "momentum": recent.get("momentum") or {},
                        "source_date": recent.get("source_date"),
                        "radar_diagnostics": recent.get("radar_diagnostics") or {},
                    }
                    _screener_cache["radar"] = recent_cache
                    snapshot_rows = list(recent_cache.get("rows") or [])
                    snapshot_skipped = recent_cache.get("skipped", 0)
                    snapshot_momentum = recent_cache.get("momentum") or {}
                    radar_diagnostics = recent_cache.get("radar_diagnostics") or {}
                else:
                    started = _start_screener_background_refresh(
                        "radar", intraday=True,
                        radar_deep_limit=RADAR_DEEP_SCAN_LIMIT)
                    state = ("已啟動一次冷啟動背景掃描；完成後本頁會自動顯示結果。"
                             if started else "已有背景掃描正在處理；完成後本頁會自動顯示結果。")
                    return respond_page("選股台", f'''<section class="screener-fast-card" data-screener-pending="1">
  <div class="screener-fast-state"><span class="screener-fast-state-mark"></span><div>
    <b>等待盤中雷達定時快照</b>
    <div class="screener-fast-note">{html.escape(state)}之後由每 10 分鐘排程更新；不在網頁請求中等待全市場掃描。</div>
  </div></div>
  <div class="screener-fast-note">資料源失敗時會保留真實錯誤狀態，不用空白或虛構數字代替。</div>
</section>''', "screener")
        else:
            recent, snapshot_source = _screener_recent_snapshot(mode)
            if recent is None:
                _start_screener_background_refresh(mode)
                return respond_page("選股台", _screener_building_fragment(mode), "screener")
            snapshot_rows = list(recent.get("rows") or [])
            snapshot_skipped = recent.get("skipped", 0)
            snapshot_momentum = recent.get("momentum") or {}
            radar_diagnostics = recent.get("radar_diagnostics") or {}

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

    # 先嘗試跨 worker 的完整快照；命中時不再先抓法人或逐檔 Yahoo 行情。
    # 若快照不存在、過期或來源日不合規，才走原本完整掃描流程。
    inst = None
    ind_map = {}
    if snapshot_rows is not None:
        rows = snapshot_rows
        skipped_liquidity = snapshot_skipped
        momentum = snapshot_momentum
        current_cache = _screener_cache.get(mode) or {}
        source_date = current_cache.get("source_date")
        if mode == "radar" and snapshot_source == "盤中定時快照":
            finished_at = current_cache.get("scan_finished_at") or "未標時間"
            source_note = (f'資料來源：盤中定時快照（每 {RADAR_LIVE_SNAPSHOT_INTERVAL_SECONDS // 60} 分鐘更新）；'
                           f'掃描完成 {str(finished_at).replace("T", " ")[:19]}；'
                           f'法人資料日 {source_date or "未標日期"}')
        else:
            source_note = (f'資料來源：{snapshot_source or "最近完整快照"}，資料日 '
                           f'{source_date or "未標日期"}；最新資料若尚未完成，背景會更新')
    else:
        if live_radar_request:
            # 盤中 detail 不能把全市場行情與深度技術計算放在 HTTP request 內；
            # 否則 Render 會先等到 request timeout，前端再也拿不到完成結果。
            # 由單例背景 worker 直接更新記憶體快取，前端以 detail 輪詢取得。
            radar_cache = _screener_cache.get("radar") or {}
            if "_load_recent_live_radar_snapshot" in globals():
                live_snapshot = _load_recent_live_radar_snapshot(
                    max_age_seconds=RADAR_LIVE_SNAPSHOT_MAX_AGE_SECONDS)
                if live_snapshot:
                    live_meta = live_snapshot.get("source_meta") or {}
                    radar_cache = {
                        "at": time.time(),
                        "rows": live_snapshot.get("rows") or [],
                        "radar_live": True,
                        "skipped": live_snapshot.get("skipped", 0),
                        "momentum": live_snapshot.get("momentum") or {},
                        "source_date": live_snapshot.get("source_date"),
                        "scan_universe_count": live_snapshot.get("scan_universe_count") or live_meta.get("scan_universe_count"),
                        "scan_finished_at": live_snapshot.get("scan_finished_at") or live_meta.get("scan_finished_at"),
                        "radar_diagnostics": live_snapshot.get("radar_diagnostics") or live_meta.get("radar_diagnostics") or {},
                    }
                    _screener_cache["radar"] = radar_cache
            finished_at = radar_cache.get("scan_finished_at")
            scan_is_recent = False
            if finished_at:
                try:
                    finished_dt = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
                    if finished_dt.tzinfo is None:
                        finished_dt = finished_dt.replace(tzinfo=TW_TZ)
                    scan_is_recent = (taiwan_now() - finished_dt.astimezone(TW_TZ)).total_seconds() <= RADAR_LIVE_SNAPSHOT_MAX_AGE_SECONDS
                except (TypeError, ValueError):
                    scan_is_recent = False
            if not scan_is_recent:
                _start_screener_background_refresh(
                    "radar", intraday=True, radar_deep_limit=RADAR_DEEP_SCAN_LIMIT)
                return respond_page("選股台", f'''<section class="screener-fast-card" data-screener-pending="1">
  <div class="screener-fast-state"><span class="screener-fast-state-mark"></span><div>
    <b>即時雷達全市場掃描中</b>
    <div class="screener-fast-note">正在取得全市場最新行情與雷達訊號；完成後本頁會自動更新，不展示舊報酬率。</div>
  </div></div>
  <div class="screener-fast-note">本次掃描由背景單例執行，避免網頁請求逾時；若資料源失敗，完成後會明示真實原因。</div>
</section>''', "screener")
            rows = list(radar_cache.get("rows") or [])
            skipped_liquidity = radar_cache.get("skipped", 0)
            momentum = radar_cache.get("momentum") or {}
            radar_diagnostics = radar_cache.get("radar_diagnostics") or {}
            source_date = radar_cache.get("source_date") or _screener_source_date()
            scan_count = int(radar_cache.get("scan_universe_count") or 0)
            finished_text = str(finished_at).replace("T", " ")[:19]
            source_note = (f'資料來源：雷達即時全市場掃描，已掃 {scan_count or "全市場"} 檔；'
                           f'行情更新時間 {finished_text}；法人資料日 {source_date or "未標日期"}')
        else:
            persisted = _load_persisted_screener_snapshot(mode)
            persisted_hit = bool(persisted and _screener_snapshot_valid_for_today(persisted))
            if persisted_hit:
                rows, skipped_liquidity, momentum = compute_screener_rows(mode)
                source_note = (f'資料來源：warmup 完成快照，資料日 '
                               f'{persisted.get("source_date") or "未標日期"}')
            else:
                inst = fetch_institutional_data()
                if not inst:
                    return respond_page("選股台", """
<div class="empty">目前無法取得三大法人資料。<br>
可能是非交易時段或資料尚未公布，請稍後再試。</div>""", "screener")
                ind_map = get_industry_map() or {}
                rows, skipped_liquidity, momentum = compute_screener_rows(
                    mode, inst=inst, ind_map=ind_map)
                source_note = (f'資料來源：本次完整計算，資料日 '
                               f'{_screener_source_date()}')
    # 直接網址、warmup 快照與 fragment 走不同分支；統一從同一份
    # worker cache 回填雷達診斷，避免零結果頁只剩「沒有符合條件」。
    if mode == "radar" and not radar_diagnostics:
        radar_diagnostics = (_screener_cache.get("radar") or {}).get(
            "radar_diagnostics") or {}
    rows = list(rows)   # 複製一份再篩選排序，避免就地排序動到快取裡那份
    unfiltered_row_count = len(rows)

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
    if view == "sector" and not ind_map:
        # 持久化 rows 已含產業名稱；只有依產業檢視需要補讀產業代碼對照，
        # 不讓預設總排行為了非必要資訊再等待共享資料。
        ind_map = get_industry_map() or {}
        by_ind = {}
        for r in rows:
            by_ind.setdefault(r["industry"], []).append(r)
        ranked_inds = []
        for ind_txt, members in by_ind.items():
            members.sort(key=lambda x: (x["score"] or -1), reverse=True)
            code_of = next((c for c, v in ind_map.items()
                            if industry_name(v) == ind_txt), None)
            st = momentum.get(ind_map.get(code_of)) if code_of else None
            ranked_inds.append({"name": ind_txt, "p75": st["p75"] if st else None,
                                "median": st["median"] if st else None,
                                "count": st["count"] if st else None,
                                "members": members})
        ranked_inds.sort(key=lambda x: (x["p75"] is not None,
                                        x["p75"] if x["p75"] is not None else 0),
                         reverse=True)
        sector_blocks = ranked_inds

    if view == "sector":
        main_html = ("".join(sector_block(b, per_sector) for b in sector_blocks)
                     or '<div class="empty">沒有符合條件的標的，試著放寬篩選。</div>')
        count_note = f"{len(sector_blocks)} 個產業・每個產業取前 {per_sector} 名"
    else:
        if shown:
            main_html = '<div class="rows">' + "".join(row_fn(r) for r in shown) + '</div>'
        elif mode == "radar" and unfiltered_row_count == 0:
            main_html = _render_radar_empty_state(
                radar_diagnostics, skipped_liquidity=skipped_liquidity)
        elif mode == "radar":
            main_html = _render_radar_empty_state(
                radar_diagnostics, skipped_liquidity=skipped_liquidity,
                filtered_out_count=unfiltered_row_count)
        else:
            main_html = f'''<div class="empty">沒有符合條件的標的。<br><br>
<span style="font-size:12.5px">
{cat_filter + "類" if cat_filter else ""}目前沒有同時滿足「法人買超」與流動性門檻
（電子 10 元／1 億，傳產與金融 8 元／0.3 億）的標的，
其中 {skipped_liquidity} 檔因流動性被排除。<br>
可試著切換類股範圍，或放寬上方篩選條件。</span></div>'''
        count_note = f"共 {len(rows)} 檔符合條件"

    body = f"""
<div class="tabs">
  <a href="/web/screener?mode=blackhorse&view={view}&cat={cat_filter}"
     class="{'on' if mode != 'radar' else ''}">黑馬</a>
  <a href="/web/screener?mode=radar&view={view}&cat={cat_filter}"
     class="{'on' if mode == 'radar' else ''}">雷達</a>
  <a href="/web/chips">籌碼超人</a>
  <a href="/web/screener?mode=review">成效</a>
  <a href="/web/screener?mode=turning">轉折觀察</a>
  <a href="/web/etf">ETF 專區</a>
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
  '　產業依「領先群營收年增率」由高至低排列。' if view == 'sector' else ''}<br>
  <span class="source-note">{html.escape(source_note)}</span></div>

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

    # 黑馬／雷達已改為快照優先，必須立即回覆；若沒有快照只啟動背景刷新，
    # 不再讓 LINE 聊天室顯示長時間載入動畫。其他仍可能同步整理的指令保留動畫。
    if text not in {"黑馬", "雷達"}:
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

    elif text in ["選股台", "選股工作台", "工作台"]:
        token = create_web_token(user_id)
        base = request.url_root.rstrip("/")
        if token:
            reply = ("🧰 選股工作台\n\n"
                     "黑馬、雷達、轉折與 ETF 會先顯示最近有效快照；\n"
                     "開盤期間只局部更新價格，不會重新掃全市場。\n\n"
                     f"{base}/web/workbench?t={token}")
        else:
            reply = "❌ 產生選股工作台連結失敗，請稍後再試。"

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
                "【選股工作台】黑馬／雷達／轉折／ETF：",
                f"{base}/web/workbench?t={token}",
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
            flex_reply = build_push_request_message(
                user_id, request.url_root.rstrip("/"))
            reply = None
        else:
            reply = "❌ 推播申請沒有成功寫入，請稍後再試。"
    elif text in ["推播關", "關閉推播", "取消訂閱"]:
        # 關閉不需要審核，使用者隨時可以自己退出；同時清掉未處理申請。
        if set_push_flags(user_id, notify=False, requested=False):
            reply = "🔕 已關閉每日推播。想再開啟請輸入「申請推播」。"
        else:
            reply = "❌ 推播狀態沒有成功更新，請稍後再試。"

    # 4+5. 自選清單與健檢已合併——完整內容留在 LINE，避免與實際庫存混淆。
    elif text in ["自選", "WATCHLIST", "健檢", "自選健檢"]:
        watchlist_reply = build_line_watchlist_message(
            user_id, request.url_root.rstrip("/"))
        if isinstance(watchlist_reply, FlexSendMessage):
            flex_reply = watchlist_reply
            reply = None
        elif isinstance(watchlist_reply, TextSendMessage):
            reply = watchlist_reply.text
        else:
            reply = str(watchlist_reply or "📂 自選股清單是空的")

    # 6. 單獨查代號 → ETF 與個股分開走，避免把 ETF 套進個股健檢。
    elif is_etf(pure_code) and 4 <= len(pure_code) <= 7 and len(text) <= 8 and " " not in text:
        etf_reply = build_single_etf_report(pure_code, user_id)
        if isinstance(etf_reply, FlexSendMessage):
            flex_reply = etf_reply
            reply = None
        elif isinstance(etf_reply, TextSendMessage):
            reply = etf_reply.text
        else:
            reply = str(etf_reply or "ETF 查詢失敗，請稍後再試。")
    # 6.1 個股單獨查代號 → 直接給完整健檢
    # 原本只回報價與位階，要看評分還得先加進自選再查健檢。
    # 查一檔股票時想知道的本來就是「這檔現在如何」，沒理由分成兩個指令。
    elif 4 <= len(pure_code) <= 7 and len(text) <= 8 and " " not in text:
        stock_reply = build_single_stock_report(pure_code, user_id)
        if isinstance(stock_reply, FlexSendMessage):
            flex_reply = stock_reply
            reply = None
        elif isinstance(stock_reply, TextSendMessage):
            reply = stock_reply.text
        else:
            reply = str(stock_reply or "查詢失敗，請稍後再試。")

    # 6.5 自選股新聞：LINE 顯示可點擊標題，完整網址不直接塞在訊息內
    elif text in ["新聞", "自選新聞"]:
        news_reply = build_news_digest(user_id)
        if isinstance(news_reply, FlexSendMessage):
            flex_reply = news_reply
            reply = None
        elif isinstance(news_reply, TextSendMessage):
            reply = news_reply.text
        else:
            reply = news_reply or "📂 自選清單是空的，先用「加 2330」新增自選"

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

    # 7.65 籌碼超人：LINE 只顯示三個重點區塊，完整五區改由網頁查看
    elif text in ["籌碼", "籌碼超人", "認養"]:
        flex_reply = build_line_chips_message(
            user_id, request.url_root.rstrip("/"))
        reply = None

    # 7.8 轉折觀察：獨立於黑馬與雷達，分為觀察、確認、失效三狀態。
    elif text in ["轉折", "轉折觀察"]:
        flex_reply = build_turning_observation_line_message(
            user_id, request.url_root.rstrip("/"))
        reply = None

    # 8. 黑馬／雷達：LINE 只做快速摘要與網頁入口，完整分析留在網頁版
    elif text in ["黑馬", "雷達"]:
        mode = "blackhorse" if text == "黑馬" else "radar"
        flex_reply = build_line_screener_message(
            user_id, mode, request.url_root.rstrip("/"))
        reply = None
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
