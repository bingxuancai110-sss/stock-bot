import os
import re
import ssl
import socket
import time
import requests
from requests.adapters import HTTPAdapter
import psycopg2
from psycopg2 import pool
from psycopg2.extras import execute_values
from urllib.parse import urlparse
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (MessageEvent, TextMessage, TextSendMessage,
                            FlexSendMessage, FollowEvent,
                            QuickReply, QuickReplyButton, MessageAction)
from datetime import datetime, timedelta, timezone, date
import random

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

connection_pool = psycopg2.pool.SimpleConnectionPool(
    1, 10,
    database=_url.path[1:],
    user=_url.username,
    password=_url.password,
    host=_ipv4_addr,
    port=_url.port,
    sslmode='require'
)

def get_db_connection():
    return connection_pool.getconn()

def release_db_connection(conn):
    if conn:
        connection_pool.putconn(conn)

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

def is_admin(user_id):
    """
    管理者由環境變數 ADMIN_USER_ID 指定，多位管理者用逗號分隔，例如：
    ADMIN_USER_ID=Uaaa...,Ubbb...
    未設定時沒有人是管理者，管理指令對所有人都無效。
    """
    raw = os.environ.get("ADMIN_USER_ID", "")
    admins = {a.strip() for a in raw.split(",") if a.strip()}
    return str(user_id).strip() in admins if admins else False


def set_requested(user_id, flag=True):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET requested = %s WHERE user_id = %s",
                       (flag, str(user_id).strip()))
        conn.commit()
        cursor.close()
    except Exception as e:
        conn.rollback()
        print(f"❌ 更新申請狀態錯誤: {e}")
    finally:
        release_db_connection(conn)


def list_users():
    """
    回傳所有使用者，順序固定（依 user_id 排序），
    這樣「名單」顯示的編號跟「開通 N」用的編號才會一致。
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, COALESCE(display_name, '(未知)'),
                   COALESCE(notify, FALSE), COALESCE(requested, FALSE)
            FROM users ORDER BY user_id
        """)
        rows = cursor.fetchall()
        cursor.close()
        return rows
    except Exception as e:
        print(f"❌ 讀取使用者名單錯誤: {e}")
        return []
    finally:
        release_db_connection(conn)


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
        f"（免費方案每月 200 則，每人每交易日 1 則，建議上限 9 人）",
    ]
    return "\n".join(lines)


def set_notify(user_id, flag: bool):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET notify = %s WHERE user_id = %s",
            (flag, str(user_id).strip())
        )
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        print(f"❌ 更新通知設定錯誤: {e}")
        return False
    finally:
        release_db_connection(conn)

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

def add_watchlist_db(user_id, code):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO watchlists (user_id, code) VALUES (%s, %s) ON CONFLICT (user_id, code) DO NOTHING",
            (str(user_id).strip(), str(code).strip())
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
from functools import wraps
from flask import make_response, redirect, url_for

WEB_SESSION_DAYS = 30  # 權杖有效天數


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


def web_login_required(view):
    """未登入就導向說明頁，不直接報錯——使用者可能只是連結過期。"""
    @wraps(view)
    def wrapper(*args, **kwargs):
        uid = current_web_user()
        if not uid:
            return render_page("需要登入", NEED_LOGIN_HTML), 401
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
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM positions WHERE id = %s AND user_id = %s",
                       (int(pos_id), str(user_id).strip()))
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        print(f"❌ 刪除持股失敗: {e}")
        return False
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
def get_realtime_stock(code):
    code = str(code).strip()
    stock_name = STOCK_NAME_MAP.get(code, code)

    for suffix in [".TW", ".TWO"]:
        try:
            symbol = f"{code}{suffix}"
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d"
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
            support_candidates = [x for x in [low_20d, ma20] if x and x < close]
            support = round(max(support_candidates), 2) if support_candidates else (
                round(low_20d, 2) if low_20d else round(low * 0.99, 2))

            return {
                "code": code,
                "name": stock_name,
                "close": float(close),
                "pct": float(pct),
                "high": float(high),
                "low": float(low),
                "volume": int(volume),
                "resistance": resistance,
                "support": support,
                "high_20d": high_20d,
                "low_20d": low_20d,
                "high_60d": high_60d,
                "low_60d": low_60d,
                "ma20": ma20,
                "vol_ratio": vol_ratio,
                "pos_vs_60d_high": pos_vs_60d_high,
                "up_streak": up_streak,
                "down_streak": down_streak,
                # 近期收盤序列，供組合頁計算個股之間的相關係數用
                "closes": [b[1] for b in hist[-60:]] + [float(close)],
            }
        except:
            continue
    return None

# --- 三大法人買賣超（TWSE T86，全市場，一天快取一次） ---
# 快取用「今天日期」當 key，但實際資料可能是往前找到的最近一個交易日
_t86_cache = {"cache_date": None, "data_date": None, "data": {}}

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
                "foreign_net_lots": foreign_net // 1000,
                "trust_net_lots": trust_net // 1000,
                "dealer_net_lots": dealer_net // 1000,
                "total_net_lots": total_net // 1000,
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
    for market, url in sources:
        rows = _get_json(url)
        if not rows:
            counts[market] = 0
            continue
        n = 0
        for row in rows:
            code = _pick(row, "公司代號", "SecuritiesCompanyCode", "Code")
            name = _pick(row, "公司簡稱", "CompanyName", "Name", "公司名稱")
            industry = _pick(row, "產業別", "Industry")
            if not code:
                continue
            records.append((code, name, industry.zfill(2) if industry else "", market))
            n += 1
        counts[market] = n
        print(f"✅ {market}公司基本資料 {n} 筆")

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

    sample = [f"{m}：{c} 檔" for m, c in counts.items()]
    sample += sorted({ind for _, _, ind, _ in records if ind})[:30]
    return len(records), sample


_industry_cache = {"map": None}
_name_cache = {"map": None}


def get_name_map(force_reload=False):
    """
    代號→公司名稱。來自 stock_info（含上市、上櫃、興櫃），
    比程式裡那份只有十幾檔的寫死對照表完整得多。
    """
    if _name_cache["map"] is not None and not force_reload:
        return _name_cache["map"]
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code, name FROM stock_info WHERE name IS NOT NULL AND name <> ''")
        _name_cache["map"] = {c: n for c, n in cursor.fetchall()}
        cursor.close()
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
    """回傳 {代號: 產業別}。讀一次就快取在記憶體，避免每次選股都查資料庫。"""
    if _industry_cache["map"] is not None and not force_reload:
        return _industry_cache["map"]

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT code, industry FROM stock_info WHERE industry IS NOT NULL AND industry <> ''")
        rows = cursor.fetchall()
        cursor.close()
        _industry_cache["map"] = {code: ind for code, ind in rows}
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

def fetch_valuation():
    """
    抓全市場本益比、殖利率、股價淨值比。一天快取一次。
    注意：TWSE 的本益比是用「近四季已申報財報」算的歷史本益比，
    不是分析師預估的未來本益比，看的時候要記得這點。
    """
    today = datetime.now().strftime("%Y%m%d")
    if _valuation_cache["date"] == today and _valuation_cache["data"]:
        return _valuation_cache["data"]

    def to_float(v):
        try:
            f = float(str(v).replace(",", "").strip())
            return f if f > 0 else None
        except (ValueError, TypeError):
            return None

    result = {}
    # 上市
    for row in _get_json(f"{TWSE_BASE}/exchangeReport/BWIBBU_ALL") or []:
        code = _pick(row, "Code")
        if code:
            result[code] = {"pe": to_float(row.get("PEratio")),
                            "pb": to_float(row.get("PBratio")),
                            "yield": to_float(row.get("DividendYield"))}
    # 上櫃（欄位名稱與上市不同，要另外對應）
    for row in _get_json(f"{TPEX_BASE}/tpex_mainboard_peratio_analysis") or []:
        code = _pick(row, "SecuritiesCompanyCode", "Code")
        if code:
            result[code] = {"pe": to_float(row.get("PriceEarningRatio")),
                            "pb": to_float(row.get("PriceBookRatio")),
                            "yield": to_float(row.get("YieldRatio"))}

    if result:
        _valuation_cache["date"] = today
        _valuation_cache["data"] = result
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


def get_cumulative_net_buy(days=10, top_n=80):
    """
    從 inst_history 撈「近 N 個交易日累計買超」前 top_n 名。
    這是黑馬候選池的來源——長線佈局往往是「量小但持續」，
    看單日買超排行會漏掉那種每天買 800 張、連買 15 天的股票。
    回傳 [(code, name, 累計張數, 有買超的天數), ...]
    """
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
                   MAX(h.name) AS name,
                   SUM(h.total_net_lots) AS cum_lots,
                   COUNT(*) FILTER (WHERE h.total_net_lots > 0) AS buy_days
            FROM inst_history h
            JOIN recent r ON h.trade_date = r.trade_date
            WHERE length(h.code) = 4 AND h.code ~ '^[0-9]+$'
              AND h.code NOT LIKE '00%%'
            GROUP BY h.code
            HAVING SUM(h.total_net_lots) > 0
            ORDER BY cum_lots DESC
            LIMIT %s
            """,
            (days, top_n),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [(r[0], r[1], r[2] or 0, r[3] or 0) for r in rows]
    except Exception as e:
        print(f"❌ 查詢累計買超失敗: {e}")
        return []
    finally:
        release_db_connection(conn)


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


def fetch_institutional_data():
    """
    抓當日 T86 資料；若今天資料還沒公布（例如盤中、假日），
    自動往前找最近一個有資料的交易日，最多往前找 5 天。
    一天只需成功抓取一次，之後直接用快取。
    """
    today = datetime.now().strftime("%Y%m%d")

    if _t86_cache["cache_date"] == today and _t86_cache["data"]:
        return _t86_cache["data"]

    for days_back in range(0, 6):
        query_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
        data = _fetch_t86_for_date(query_date)
        if data:
            _t86_cache["cache_date"] = today
            _t86_cache["data_date"] = query_date
            _t86_cache["data"] = data
            save_t86_history(query_date, data)
            return data

    print("⚠️ 往前找了 5 天仍無 T86 資料")
    return _t86_cache["data"]

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
    ("美元指數", "DX-Y.NYB"), ("西德州原油", "CL=F"),
]
BRIEF_STOCKS = [
    ("輝達 NVDA", "NVDA"), ("博通 AVGO", "AVGO"), ("超微 AMD", "AMD"),
    ("美光 MU", "MU"), ("Lumentum LITE", "LITE"), ("台積電ADR", "TSM"),
]


def generate_morning_brief():
    """
    盤前總經簡報：美股指數、殖利率與波動率、台廠相關重要個股、總經新聞標題。
    數據全部來自實際報價；CPI／非農這類發布結果與解讀不自行生成，
    改列新聞標題與連結，由你自己判讀。
    """
    lines = [f"☀️ 盤前總經簡報　{datetime.now().strftime('%m/%d')}", "═" * 13]

    def fmt_rows(title, targets, as_yield=False):
        block = [f"\n【{title}】"]
        got = False
        for label, sym in targets:
            q = fetch_quote(sym)
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
    tnx = fetch_quote("^TNX")
    if tnx:
        close, pct, diff = tnx
        arrow = "⚪" if abs(diff) < 0.0005 else ("🔴" if diff > 0 else "🟢")
        lines.append(f"{arrow} 美10年債殖利率：{close:.3f}%（{diff*100:+.1f} bps）")
    for label, sym in BRIEF_MACRO[1:]:
        q = fetch_quote(sym)
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
_revenue_cache = {"period": None, "data": {}}

def fetch_monthly_revenue():
    """
    抓最新一期月營收，涵蓋上市、上櫃、興櫃。
    證交所只給上市，上櫃與興櫃在櫃買中心，缺了就會出現「營收無資料」。
    """
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

    result, period = {}, None
    for url in sources:
        rows = _get_json(url, timeout=20)
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
def fetch_taiex_summary():
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX"
        res = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'}).json()
        for row in res:
            if row.get("指數") == "發行量加權股價指數":
                return {
                    "close": row.get("收盤指數"),
                    "sign": row.get("漲跌"),
                    "pts": row.get("漲跌點數"),
                    "pct": row.get("漲跌百分比"),
                }
        return None
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

    lines = ["📊 盤後解盤", "─" * 14]

    taiex = fetch_taiex_summary()
    if taiex and taiex.get("close"):
        arrow = "▲" if taiex.get("sign") == "+" else ("▼" if taiex.get("sign") == "-" else "－")
        lines.append(f"大盤 {taiex['close']}　{arrow}{taiex.get('pts','?')}（{taiex.get('pct','?')}%）")
    else:
        lines.append("大盤：資料暫缺")

    inst_total = fetch_institutional_total()
    if inst_total:
        total_yi = inst_total["total"] / 100_000_000
        lines.append(f"三大法人合計：{total_yi:+.1f}億")
        for name, val in inst_total["breakdown"].items():
            lines.append(f"　{name}　{val/100_000_000:+.1f}億")
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
        lines.append(f"{info['name']}({code}) +{info['total_net_lots']:,}張")

    lines.append("")
    lines.append("🔴 法人賣超前3")
    for code, info in sell_leaders:
        lines.append(f"{info['name']}({code}) {info['total_net_lots']:,}張")

    return "\n".join(lines)

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
    # 這個組合比「跌破月線」更有資訊量（它說明了為什麼弱），所以排在前面，
    # 否則同時成立時會被較籠統的那句蓋掉。
    if (val_score <= 10 and pos is not None and pos <= -20):
        if cum_lots < 0:
            return "🔻 本益比偏高但股價已回落一段，法人同步減碼，市場恐在重新評價其成長性"
        return "🤔 本益比偏高但股價已回落一段，市場可能在重新評價成長性，留意估值是否過去給太高"

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


def build_healthcheck_report(user_id):
    codes = get_user_watchlist(user_id)
    if not codes:
        return "📂 自選股清單是空的\n輸入「加 3081」新增自選"

    institutional_data = fetch_institutional_data()
    revenue_data = fetch_monthly_revenue()
    valuation_data = fetch_valuation()
    streaks = get_consecutive_days_batch(codes)
    cum_map = get_cumulative_net_buy_for_codes(codes, days=10)

    rows = []
    for code in codes:
        stock = get_realtime_stock(code)
        if not stock:
            rows.append((-1, f"⚪ {code} 查無行情"))
            continue

        inst = institutional_data.get(code, {})
        name = stock_display_name(code, institutional_data, stock["name"])
        cum_lots, buy_days = cum_map.get(code, (0, 0))
        streak = streaks.get(code, 0)

        chip_score = score_watchlist_chips(cum_lots, buy_days, streak)   # 0-30
        pos_score = score_watchlist_position(stock)                       # 0-25

        cum_yoy = revenue_data.get(code, {}).get("cum_yoy_pct")
        rev_score = round(score_from_cum_revenue_growth(cum_yoy) * 25 / 40)  # 0-25

        pe = valuation_data.get(code, {}).get("pe")
        val_score, peg, _desc = score_from_valuation(pe, cum_yoy)
        val_score = round(val_score * 20 / 25)                            # 0-20

        total = chip_score + pos_score + rev_score + val_score
        flag = "🟢" if total >= 70 else ("🟡" if total >= 45 else "🔴")

        # 一句話點出目前最該注意的事實
        if cum_lots < 0:
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

        text = (
            f"{flag} {name} {code}　{total}分\n"
            f"{stock['close']:.2f}（{stock['pct']:+.2f}%）　{pos_txt}\n"
            f"{note}\n"
            f"{rev_txt}　{pe_txt}　🛡️{stock['support']} 🚧{fmt_resistance(stock['resistance'])}\n"
            f"{advice}"
        )
        rows.append((total, text))

    rows.sort(key=lambda x: x[0], reverse=True)
    body = "\n\n".join(text for _, text in rows)
    report = (
        f"📋 自選股健檢（{len(codes)}檔）\n\n{body}\n\n"
        f"評分＝籌碼30＋位階25＋營收25＋估值20\n"
        f"🟢70+ 🟡45-69 🔴<45\n"
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
    lines = [f"📰 自選股新聞（{datetime.now().strftime('%m/%d')}）", "─" * 14]

    found = 0
    for code in codes:
        name = stock_display_name(code, inst_data)
        news = fetch_stock_news(name, max_items=2)
        if not news:
            continue
        found += 1
        lines.append(f"\n🔹 {name} {code}")
        for n in news:
            src = f"（{n['source']}）" if n["source"] else ""
            lines.append(f"・{n['title']}{src}")
            if n["link"]:
                lines.append(f"　{n['link']}")
        time.sleep(0.5)  # 禮貌等待，避免短時間內連續請求

    if not found:
        lines.append("今日自選股無相關新聞")
    else:
        lines.append("\n─" * 1)
        lines.append("※ 僅列標題與連結，詳情請點原文")

    digest = "\n".join(lines)
    if len(digest) > 4800:
        digest = digest[:4750] + "\n\n…（內容過長，已截斷）"
    return digest


@app.route("/cron/push-news", methods=["POST", "GET"])
def cron_push_news():
    """盤後推播自選股新聞。建議每個交易日 15:00 跑一次，一天一則。"""
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)

    users = get_notify_users()
    sent, failed, empty = 0, 0, 0
    for uid in users:
        msg = build_news_digest(uid)
        if not msg:
            empty += 1
            continue
        try:
            line_bot_api.push_message(uid, TextSendMessage(text=msg))
            sent += 1
        except Exception as e:
            print(f"❌ 新聞推播失敗 {uid}: {e}")
            failed += 1
    return f"News push done. sent={sent}, failed={failed}, empty={empty}", 200


# --- 排程推播訊息建構與 Cron 端點 ---
def build_digest(user_id):
    codes = get_user_watchlist(user_id)
    if not codes:
        return None
    inst_data = fetch_institutional_data()
    lines = [f"☀️ 【每日自選股摘要】", "─" * 14]
    for code in codes:
        data = get_realtime_stock(code)
        if data:
            name = stock_display_name(code, inst_data, data["name"])
            light = "🔴" if data['pct'] >= 0 else "🟢"
            lines.append(
                f"\n{light} {name} {code}｜{data['close']:.2f}（{data['pct']:+.2f}%）\n"
                f"🛡️ 支撐：{data['support']} | 🚧 壓力：{fmt_resistance(data['resistance'])}"
            )
        else:
            lines.append(f"\n⚪ {code} 查無行情")
    return "\n".join(lines)

def build_morning_push(user_id):
    """
    早上推播的完整內容：總經簡報 ＋ 自選股摘要，合併成一則。
    合併而非分兩則，是因為 LINE 免費方案的推播則數有限，
    一天一則才撐得住整個月。
    """
    parts = [generate_morning_brief()]
    digest = build_digest(user_id)
    if digest:
        parts.append("\n" + "═" * 13 + "\n")
        parts.append(digest)
    msg = "\n".join(parts)
    if len(msg) > 4800:
        msg = msg[:4750] + "\n\n…（內容過長，已截斷）"
    return msg


@app.route("/cron/push-watchlist", methods=["POST", "GET"])
def cron_push_watchlist():
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)

    users = get_notify_users()
    sent, failed = 0, 0
    for uid in users:
        msg = build_morning_push(uid)
        if not msg:
            continue
        try:
            line_bot_api.push_message(uid, TextSendMessage(text=msg))
            sent += 1
        except Exception as e:
            print(f"❌ 推播失敗 {uid}: {e}")
            failed += 1
    return f"Push done. sent={sent}, failed={failed}", 200

@app.route("/cron/fetch-t86", methods=["POST", "GET"])
def cron_fetch_t86():
    """每個交易日收盤後自動抓一次 T86 並存進歷史表，不依賴使用者操作。"""
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)

    # 清掉快取強制重抓，確保拿到今天最新公布的資料
    _t86_cache["cache_date"] = None
    data = fetch_institutional_data()
    if not data:
        return "No T86 data available (non-trading day or not yet published).", 200
    return f"OK. date={_t86_cache.get('data_date')}, stocks={len(data)}", 200


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

    return "\n".join(str(x) for x in lines), 200


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
        query_date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
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
        ("📂 自選", "自選"),
        ("📊 解盤", "解盤"),
        ("📰 新聞", "新聞"),
        ("🌐 網頁", "網頁"),
    ]
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label=label, text=text))
        for label, text in items
    ])


def build_menu_flex():
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
        ("選股策略", "#B5822A", "#F7EFDF", [
            ("黑馬", "營收成長＋估值＋產業動能"),
            ("雷達", "帶量突破、法人買超強勢股"),
        ]),
        ("我的自選", "#2E7D5B", "#E6F1EC", [
            ("自選", "持股評分、位階與支撐壓力"),
            ("新聞", "自選股相關新聞與連結"),
        ]),
        ("網頁版", "#6B4E9E", "#EFEAF7", [
            ("網頁", "🚧 Coming soon　持股組合分析\n產業集中度、相關係數、加權基本面"),
        ]),
        ("推播設定", "#7A8290", "#EDEFF1", [
            ("申請推播", "🔒 VIP 限定　每日盤前自動發送\n非 VIP 可直接點上方「盤前」查看相同內容"),
            ("推播關", "停止自動發送"),
        ]),
    ]

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
        return {
            "type": "box", "layout": "vertical", "margin": "md", "spacing": "xs",
            "contents": [
                {"type": "text", "text": label, "size": "sm",
                 "color": "#6B737B", "weight": "bold"},
                {"type": "box", "layout": "baseline", "spacing": "sm",
                 "contents": [
                     {"type": "text", "text": cmd, "size": "lg",
                      "weight": "bold", "color": "#1B2027", "flex": 0},
                     {"type": "text", "text": note, "size": "sm",
                      "color": "#8E959C", "wrap": True},
                 ]},
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
             howto("加入自選", "加 2330", "換成你要的代號"),
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
.num{font-variant-numeric:tabular-nums;
  font-family:"SF Mono",ui-monospace,Menlo,Consolas,monospace}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--ink-faint)}
header{padding:30px 0 20px}
.eyebrow{font-size:11px;letter-spacing:.2em;color:var(--brass);
  text-transform:uppercase;margin-bottom:8px}
h1{font-size:27px;letter-spacing:.02em;font-weight:700;line-height:1.25}
.dateline{margin-top:6px;font-size:12.5px;color:var(--ink-faint)}
nav{display:flex;gap:18px;padding:14px 0;border-top:1px solid var(--rule);
  border-bottom:1px solid var(--rule);font-size:13.5px;margin-bottom:8px}
nav a{color:var(--ink-soft);text-decoration:none}
nav a.on{color:var(--ink);font-weight:500;border-bottom:2px solid var(--brass);
  padding-bottom:2px}
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
.lots{grid-column:1/-1;margin-top:6px;font-size:12px}
.lots summary{color:var(--brass);cursor:pointer;font-size:11.5px}
.lot{padding:7px 0 7px 12px;color:var(--ink-soft);
  border-left:2px solid var(--rule);margin-top:6px}
.empty{padding:40px 0;text-align:center;color:var(--ink-faint);font-size:14px}
.msg{margin:14px 0;padding:11px 14px;background:var(--paper-2);
  border-left:2px solid var(--brass);font-size:13px}
footer{margin-top:36px;padding-top:18px;border-top:1px solid var(--rule);
  font-size:15px;color:var(--ink-soft);line-height:1.9}
.totals{display:flex;gap:28px;flex-wrap:wrap;padding:18px 0 8px}
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
.dist{display:flex;flex-wrap:wrap;gap:14px;padding:11px 13px;
  background:var(--paper-2);font-size:12.5px;color:var(--ink-soft);
  border-left:2px solid var(--brass)}
.dist-item b{color:var(--ink);font-size:14px}
.dist-note{color:var(--ink-faint);margin-left:auto}
.controls{margin:14px 0 6px}
.controls .fields{grid-template-columns:repeat(auto-fit,minmax(110px,1fr))}
.badge{font-size:10.5px;color:var(--brass);border:1px solid var(--brass);
  border-radius:2px;padding:1px 5px;margin-left:6px;vertical-align:2px}
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
.total-label{font-size:12px;color:var(--ink-soft)}
.total-value{font-size:24px;font-weight:600;margin-top:2px}
.total-sub{font-size:12.5px}
"""

NEED_LOGIN_HTML = """
<div class="msg">
  這個連結已失效或尚未登入。<br><br>
  請回到 LINE 的「台股 BOT」，輸入 <b>網頁</b>，
  機器人會給你一組新的連結。
</div>
"""


def render_page(title, body, nav_active=None, user_name=None):
    """所有網頁共用的外框。用字串組裝就好，這個規模不需要模板引擎。"""
    def tab(href, label, key):
        on = " class=\"on\"" if key == nav_active else ""
        return f'<a href="{href}"{on}>{label}</a>'

    nav = ""
    if nav_active:
        nav = ("<nav>"
               + tab("/web/portfolio", "組合", "portfolio")
               + tab("/web/positions", "持股", "positions")
               + tab("/web/screener", "選股", "screener")
               + tab("/web/settings", "設定", "settings")
               + "</nav>")

    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}｜台股 BOT</title>
<style>{BASE_CSS}</style>
</head><body><div class="wrap">
<header>
  <div class="eyebrow">Taiwan Stock Bot</div>
  <h1>{title}</h1>
  <div class="dateline">{datetime.now().strftime('%Y / %m / %d')}
    {'　' + user_name if user_name else ''}</div>
</header>
{nav}
{body}
<footer>
以上為你輸入之持股的數據整理，不構成投資建議。<br>
資料來源：臺灣證券交易所、Yahoo Finance。作者：蔡秉軒
</footer>
</div></body></html>"""


def fmt_pct(v):
    if v is None:
        return '<span class="flat">—</span>'
    cls = "flat" if abs(v) < 0.005 else ("up" if v > 0 else "down")
    return f'<span class="num {cls}">{v:+.2f}%</span>'


@app.route("/web/login")
def web_login():
    """帶 ?t=權杖 進來，驗證後寫入 cookie，之後就不必每次帶網址參數。"""
    token = request.args.get("t", "")
    uid = resolve_web_token(token)
    if not uid:
        return render_page("需要登入", NEED_LOGIN_HTML), 401
    resp = make_response(redirect("/web/positions"))
    resp.set_cookie("stockbot_token", token,
                    max_age=WEB_SESSION_DAYS * 86400,
                    httponly=True, samesite="Lax", secure=True)
    return resp


@app.route("/web")
@web_login_required
def web_home(uid):
    # 有持股就直接看分析，沒有就先去輸入
    return redirect("/web/portfolio" if get_positions(uid) else "/web/positions")


@app.route("/web/positions", methods=["GET", "POST"])
@web_login_required
def web_positions(uid):
    msg = ""
    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            delete_position(uid, request.form.get("id"))
            msg = "已刪除。"
        else:
            code = normalize_code(request.form.get("code", ""))
            try:
                shares = int(request.form.get("shares", "0"))
                cost = float(request.form.get("cost", "0"))
            except ValueError:
                shares, cost = 0, 0.0
            if not code or shares <= 0 or cost <= 0:
                msg = "請填入正確的代號、股數與成本價。"
            else:
                add_position(uid, code, shares, cost,
                             request.form.get("bought_on") or None)
                msg = f"已新增 {code}。"

    positions = merge_positions(get_positions(uid))
    inst = fetch_institutional_data() or {}
    fee_disc, min_fee = get_fee_settings(get_profile(uid))

    def lots_html(p, name):
        """單筆就一顆刪除鍵；分批買進則收在 details 裡，可個別刪除。"""
        lots = p.get("lots", [])
        if len(lots) <= 1:
            lid = lots[0]["id"] if lots else 0
            return (f'<form method="post" style="display:inline;margin:0" '
                    f'onsubmit="return confirm(\'確定刪除 {name}？\')">'
                    f'<input type="hidden" name="action" value="delete">'
                    f'<input type="hidden" name="id" value="{lid}">'
                    f'<button class="del" type="submit">刪除</button></form>')
        items = "".join(
            f'<div class="lot">'
            f'<span class="num">{l["shares"]:,}</span> 股　'
            f'成本 <span class="num">{l["cost"]:,.2f}</span>　'
            f'{l["bought_on"].strftime("%Y/%m/%d") if l["bought_on"] else "未填日期"}'
            f'<form method="post" style="display:inline;margin-left:10px">'
            f'<input type="hidden" name="action" value="delete">'
            f'<input type="hidden" name="id" value="{l["id"]}">'
            f'<button class="del" type="submit">刪除</button></form>'
            f'</div>' for l in lots)
        return (f'<details class="lots"><summary>分 {len(lots)} 筆買進</summary>'
                f'{items}</details>')

    rows_html, total_value, total_cost = [], 0.0, 0.0
    enriched = []
    for p in positions:
        price = get_realtime_stock(p["code"])
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
            _np, pl, cost_fee = net_profit(
                p["code"], p["shares"], p["cost"], price["close"],
                p.get("lots"), fee_disc, min_fee)
            pl = pl if pl is not None else gross_pl
            held = ((datetime.now().date() - p["bought_on"]).days
                    if p["bought_on"] else None)
            rows_html.append(f"""
<div class="row">
  <div><span class="name">{name}</span><span class="code">{p['code']}</span></div>
  <div class="price num">{price['close']:,.2f}</div>
  <div class="meta">
    <span><em>持有</em> <span class="num">{p['shares']:,}</span> 股</span>
    <span><em>成本</em> <span class="num">{p['cost']:,.2f}</span></span>
    <span><em>淨損益</em> {fmt_pct(pl)}
      <span class="sub">帳面 {gross_pl:+.2f}%</span></span>
    <span><em>市值</em> <span class="num">{value:,.0f}</span></span>
    <span><em>成本費</em> <span class="num">{cost_fee:,.0f}</span></span>
    <span><em>權重</em> <span class="num">{weight:.1f}%</span></span>
    {f'<span><em>持有</em> {held} 天</span>' if held is not None else ''}
    {lots_html(p, name)}
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
    {lots_html(p, p['code'])}
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
    <div><label>買進日期（可略）</label>
      <input name="bought_on" type="date"></div>
  </div>
  <button type="submit">新增</button>
</form>"""
    return render_page("持股", body, nav_active="positions")


# ── 問卷與門檻設定 ──
# 前四題必填，其餘可略過；沒填的用保守預設值，並在畫面上說明原因。
PROFILE_FIELDS = [
    ("age_band", "你的年齡區間", True,
     ["未滿 30 歲", "30–39 歲", "40–49 歲", "50–59 歲", "60 歲以上"]),
    ("horizon", "這筆錢預計多久之後可能會用到？", True,
     ["1 年內", "1–3 年", "3–10 年", "10 年以上", "沒有特定用途"]),
    ("asset_share", "這筆投資佔你可動用資產的比重大約是？", True,
     ["不到四分之一", "約四分之一到一半", "約一半以上", "幾乎全部"]),
    ("income_type", "你的收入穩定度", True,
     ["固定薪資", "固定薪資 + 變動獎金", "接案或營業收入", "目前無固定收入"]),
    ("drawdown_experience", "過去實際經歷過最大的帳面虧損？當時做了什麼？", False,
     ["沒有經歷過明顯虧損", "虧損 10% 以內就減碼了", "撐過 20–30% 沒有動作",
      "撐過 30% 以上沒有動作", "曾經在虧損時加碼"]),
    ("check_frequency", "你多久會看一次帳戶？", False,
     ["一天多次", "每天一次", "每週", "每月或更少"]),
    ("holding_period", "你的持股平均會抱多久？", False,
     ["幾天", "幾週", "幾個月", "一年以上", "不一定"]),
    ("other_assets", "除了台股，你還有哪些部位？", False,
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


@app.route("/web/settings", methods=["GET", "POST"])
@web_login_required
def web_settings(uid):
    msg = ""
    if request.method == "POST":
        data = {k: (request.form.get(k) or None) for k, _, _, _ in PROFILE_FIELDS}
        for k in ("loss_alert_pct", "position_alert_pct", "min_fee"):
            v = request.form.get(k)
            data[k] = int(v) if v and v.isdigit() else None
        try:
            data["fee_discount"] = float(request.form.get("fee_discount") or 0) or None
        except ValueError:
            data["fee_discount"] = None
        if all(data.get(k) for k, _, req, _ in PROFILE_FIELDS if req):
            msg = ("設定已儲存。" if save_profile(uid, data)
                   else "儲存失敗，請稍後再試或回報問題。")
        else:
            msg = "前四題為必填，請確認都已選擇。"

    p = get_profile(uid)
    th = get_thresholds(p)

    def radio_group(key, label, required, options):
        opts = "".join(
            f'<label class="opt"><input type="radio" name="{key}" value="{o}"'
            f'{" checked" if p.get(key) == o else ""}'
            f'{" required" if required else ""}> {o}</label>'
            for o in options
        )
        req = '<span class="req">必填</span>' if required else '<span class="opt-tag">可略過</span>'
        return f'<div class="q"><div class="q-title">{label} {req}</div>{opts}</div>'

    required_html = "".join(
        radio_group(k, l, r, o) for k, l, r, o in PROFILE_FIELDS if r)
    optional_html = "".join(
        radio_group(k, l, r, o) for k, l, r, o in PROFILE_FIELDS if not r)

    sel_fee = "".join(
        f'<option value="{v}"{" selected" if p.get("fee_discount") and abs(p["fee_discount"] - v) < 1e-9 else ""}>{t}</option>'
        for v, t in [(1.0, "無折扣（0.1425%）"), (0.65, "65 折"), (0.6, "6 折"),
                     (0.5, "5 折"), (0.38, "38 折"), (0.3, "3 折"), (0.28, "28 折"),
                     (0.25, "25 折"), (0.2, "2 折")])
    sel_min = "".join(
        f'<option value="{v}"{" selected" if p.get("min_fee") == v else ""}>{t}</option>'
        for v, t in [(20, "20 元"), (10, "10 元"), (5, "5 元"), (1, "1 元")])

    def sel(key, current, options):
        return "".join(
            f'<option value="{v}"{" selected" if str(current) == str(v) else ""}>{t}</option>'
            for v, t in options)

    body = f"""
{f'<div class="msg">{msg}</div>' if msg else ''}
<form method="post">

<div class="section-head"><h2>基本設定</h2>
  <span class="section-note">四題必填</span></div>
<div class="hint">
  這些答案不會改變數據本身，而是決定「什麼該提醒你」。<br>
  例如同樣 60% 集中在半導體，資金一年內要用、
  且這是你全部身家的人，會看到比較強的警示；
  十年不動用的人則會看到不同的說明。
</div>
{required_html}

<div class="section-head"><h2>提醒門檻</h2>
  <span class="section-note">分析頁會依此判斷</span></div>
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

<div class="section-head"><h2>交易成本</h2>
  <span class="section-note">用來計算淨損益</span></div>
<div class="fields" style="margin-bottom:6px">
  <div><label>手續費折扣</label>
    <select name="fee_discount">
      {sel_fee}
    </select></div>
  <div><label>最低手續費</label>
    <select name="min_fee">
      {sel_min}
    </select></div>
</div>
<div class="hint">
  券商的「成本價」通常已含買進手續費，因此這裡只扣賣出手續費與證交稅
  （證交稅：一般股票 0.3%、ETF 0.1%）。<br>
  折扣與最低收費各家不同，設成跟你券商一致，淨損益才會對得起來。
</div>

<div class="section-head"><h2>進階設定</h2>
  <span class="section-note">可略過，填了分析會更貼近你</span></div>
{optional_html}

<button type="submit">儲存設定</button>
</form>"""
    return render_page("設定", body, nav_active="settings")


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
    rets = {c: daily_returns(price_map[c]["closes"]) for c in codes}
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
    # 判斷「集中風險」時要排除 ETF：ETF 本身就是一籃子股票，
    # 它是分散的來源而非集中的來源，把它當成單一族群會得出相反的結論。
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

    # ETF 佔比高：這是分散，不是集中，該說明而不是警示
    if etf_weight >= 25:
        out.append(("ETF 佔比",
                    f"ETF 佔組合 {etf_weight:.0f}%。這部分本身已分散於一籃子標的，"
                    f"因此上述集中度是以個股部分計算，未把 ETF 視為單一族群。"))

    # 看盤頻率高 × 已有虧損部位
    losers = [h for h in holdings if h["pl"] is not None and h["pl"] < 0]
    if freq == "一天多次" and len(losers) >= 2:
        out.append(("看盤頻率",
                    f"你每天多次查看帳戶，目前有 {len(losers)} 檔在虧損。"
                    f"高頻檢視在波動期容易放大情緒，判斷前先確認依據有沒有變。"))

    return out


@app.route("/web/portfolio")
@web_login_required
def web_portfolio(uid):
    positions = merge_positions(get_positions(uid))
    if not positions:
        return render_page("組合分析", """
<div class="empty">還沒有持股紀錄。<br><br>
<a href="/web/positions" style="color:var(--brass)">先去新增持股 →</a></div>""",
                           nav_active="portfolio")

    profile = get_profile(uid)
    th = get_thresholds(profile)
    fee_disc, min_fee = get_fee_settings(profile)
    inst = fetch_institutional_data() or {}
    revenue = fetch_monthly_revenue() or {}
    valuation = fetch_valuation() or {}
    ind_map = get_industry_map() or {}

    price_map, total_value, total_cost = {}, 0.0, 0.0
    for p in positions:
        pr = get_realtime_stock(p["code"])
        price_map[p["code"]] = pr
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
        elif is_etf(p["code"]):
            label = "ETF（一籃子）"   # ETF 本身已分散，跟「查不到產業」意義不同
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

    real_ordered = [x for x in ordered
                    if not x[0].startswith("ETF") and x[0] != "未分類"]
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

    if not profile:
        alerts.append(("尚未設定",
                       "你還沒填寫問卷，目前使用預設門檻。"
                       "填寫後提醒會更貼近你的狀況。"))

    pl_total = ((total_value - total_cost) / total_cost * 100) if total_cost else None
    # 把目前生效的設定攤開來，否則使用者填完問卷看不出差在哪
    pf_items = [
        ("虧損提醒", f"{th['loss']}%" + ("（預設）" if not profile.get("loss_alert_pct") else "")),
        ("單一持股", f"{th['position']}%" + ("（預設）" if not profile.get("position_alert_pct") else "")),
        ("資金年期", profile.get("horizon") or "未填"),
        ("資產比重", profile.get("asset_share") or "未填"),
        ("收入型態", profile.get("income_type") or "未填"),
        ("回檔經驗", profile.get("drawdown_experience") or "未填"),
        ("看盤頻率", profile.get("check_frequency") or "未填"),
        ("平均持有", profile.get("holding_period") or "未填"),
        ("其他部位", profile.get("other_assets") or "未填"),
    ]
    profile_html = "".join(
        f'<div class="pf"><span class="pf-k">{k}</span>'
        f'<span class="pf-v{"" if v != "未填" else " pf-empty"}">{v}</span></div>'
        for k, v in pf_items)
    if not profile:
        profile_html += ('<div class="pf" style="grid-column:1/-1">'
                         '<a href="/web/settings" style="color:var(--brass)">'
                         '尚未填寫問卷，前往設定 →</a></div>')

    corr_txt = (f"兩兩相關係數平均 <b>{avg_corr:.2f}</b>，"
                f"實際分散效果約等於 <b>{eff:.1f} 檔</b>。"
                if avg_corr is not None and eff else
                "持股數不足或資料不齊，尚無法計算相關係數。")

    body = f"""
<div class="totals">
  <div><div class="total-label">總市值</div>
       <div class="total-value num">{total_value:,.0f}</div>
       <div class="total-sub">{fmt_pct(pl_total)}</div></div>
  <div><div class="total-label">持股檔數</div>
       <div class="total-value num">{len(holdings)}</div>
       <div class="total-sub" style="color:var(--ink-faint)">
         {len(by_industry)} 個產業</div></div>
  <div><div class="total-label">最大單一持股</div>
       <div class="total-value num">{top['weight']:.1f}%</div>
       <div class="total-sub" style="color:var(--ink-faint)">{top['name']}</div></div>
</div>

<div class="section-head"><h2>產業集中度</h2>
  <span class="section-note">寬度即權重</span></div>
<div class="band">{''.join(band)}</div>
<div class="legend">{''.join(legend)}</div>
<div class="callout">{corr_txt}</div>

<div class="section-head"><h2>持股權重</h2>
  <span class="section-note">依權重排序</span></div>
<div class="rows">
{''.join(f'''
<div class="row">
  <div><span class="name">{h['name']}</span><span class="code">{h['code']}</span></div>
  <div class="price num">{h['weight']:.1f}%</div>
  <div class="meta">
    <span><em>產業</em> {h['industry']}</span>
    <span><em>損益</em> {fmt_pct(h['pl'])}</span>
    <span><em>營收年增</em> {f"{h['cum_yoy']:+.1f}%" if h['cum_yoy'] is not None else '—'}</span>
    <span><em>PE</em> {f"{h['pe']:.1f}" if h['pe'] else '—'}</span>
  </div>
  <div class="chg">{fmt_pct(h['price']['pct'])}</div>
  <div class="bar"><div style="width:{h['weight']:.1f}%"></div></div>
</div>''' for h in sorted(holdings, key=lambda x: x['weight'], reverse=True))}
</div>

<div class="section-head"><h2>你的設定</h2>
  <span class="section-note">下方提醒依此判斷</span></div>
<div class="profile-grid">{profile_html}</div>

<div class="section-head"><h2>值得注意</h2>
  <span class="section-note">依你設定的門檻</span></div>
<div class="rows">
{''.join(f'<div class="alert"><span class="tag">{tag}</span><span>{txt}</span></div>'
         for tag, txt in alerts) if alerts
 else '<div class="empty">目前沒有觸及門檻的項目。</div>'}
</div>"""
    return render_page("組合分析", body, nav_active="portfolio")


# ============================================================
# 選股台：黑馬／雷達的完整版
# LINE 受限於訊息長度只能給 5 檔；網頁可以給 20 檔並支援排序篩選。
# ============================================================
@app.route("/web/screener")
@web_login_required
def web_screener(uid):
    mode = request.args.get("mode", "blackhorse")
    limit = request.args.get("limit", "20")
    limit = int(limit) if limit.isdigit() and int(limit) in (10, 20, 50) else 20
    sort_key = request.args.get("sort", "score")
    min_score = request.args.get("min_score", "")
    max_pe = request.args.get("max_pe", "")
    industry_filter = request.args.get("industry", "")
    show_fin = request.args.get("fin", "") == "1"   # 預設排除金融股
    view = request.args.get("view", "list")         # list=總排行, sector=依產業

    inst = fetch_institutional_data()
    if not inst:
        return render_page("選股台", """
<div class="empty">目前無法取得三大法人資料。<br>
可能是非交易時段或資料尚未公布，請稍後再試。</div>""", nav_active="screener")

    revenue = fetch_monthly_revenue() or {}
    valuation = fetch_valuation() or {}
    ind_map = get_industry_map() or {}
    momentum = get_industry_momentum(revenue, ind_map)

    # ── 候選池 ──
    if mode == "radar":
        # 與黑馬一致排除金融保險業（產業代碼 17）：
        # 銀行保險的「營收」是利息、手續費與投資收益，
        # 年增率動輒數百％多半來自評價變動而非本業成長，
        # 套進成長型評分會讓金融股整片霸榜。
        pool = [(c, i) for c, i in inst.items()
                if len(c) == 4 and c.isdigit() and not c.startswith("00")
                and i["total_net_lots"] > 0
                and (show_fin or not is_financial(c, ind_map))]
        pool.sort(key=lambda x: x[1]["total_net_lots"], reverse=True)
        pool = [(c, {"name": i.get("name", c), "total_net_lots": i["total_net_lots"],
                     "cum_lots": i["total_net_lots"], "buy_days": 1})
                for c, i in pool[:80]]
    else:
        cum = get_cumulative_net_buy(days=10, top_n=120)
        pool = [(c, {"name": n, "total_net_lots": inst.get(c, {}).get("total_net_lots", 0),
                     "cum_lots": cl, "buy_days": bd})
                for c, n, cl, bd in cum
                if show_fin or not is_financial(c, ind_map)]

    streaks = get_consecutive_days_batch([c for c, _ in pool])

    rows = []
    for code, info in pool:
        price = get_realtime_stock(code)
        if not price or price["close"] < 10 or abs(price["pct"]) > 10.5:
            continue
        turnover = calc_turnover_billion(price["close"], price["volume"])
        if turnover < 1:
            continue
        if mode == "radar" and price["pct"] < 1.5:
            continue

        cum_yoy = revenue.get(code, {}).get("cum_yoy_pct")
        pe = valuation.get(code, {}).get("pe")
        streak = streaks.get(code, 0)
        ind_code = ind_map.get(code)
        ind_txt = industry_name(ind_code) if ind_code else "未分類"

        rev_score = round(score_from_cum_revenue_growth(cum_yoy) * 25 / 40)
        val_score, peg, val_desc = score_from_valuation(pe, cum_yoy)
        mom_score, mom_desc = score_from_industry_momentum(momentum.get(ind_code))
        streak_score = round(score_from_streak(streak) * 20 / 30)
        chip_tech = round((score_from_net_lots(info["cum_lots"]) / 40 * 5)
                          + (score_from_technical(price["pct"], turnover) / 60 * 5))
        total = rev_score + val_score + mom_score + streak_score + chip_tech

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
            "cum_yoy": cum_yoy, "pe": pe, "peg": peg, "turnover": turnover,
            "cum_lots": info["cum_lots"], "buy_days": info["buy_days"],
            "breakout": breakout, "vol_ratio": price.get("vol_ratio"),
            "pos": price.get("pos_vs_60d_high"),
        })

    # ── 篩選 ──
    if min_score.isdigit():
        rows = [r for r in rows if r["score"] >= int(min_score)]
    try:
        if max_pe:
            rows = [r for r in rows if r["pe"] and r["pe"] <= float(max_pe)]
    except ValueError:
        pass
    if industry_filter:
        rows = [r for r in rows if r["industry"] == industry_filter]

    # ── 排序 ──
    sorters = {
        "score": lambda r: r["score"],
        "pct": lambda r: r["pct"],
        "yoy": lambda r: r["cum_yoy"] if r["cum_yoy"] is not None else -999,
        "pe": lambda r: -r["pe"] if r["pe"] else -9999,
        "streak": lambda r: r["streak"],
        "turnover": lambda r: r["turnover"],
    }
    rows.sort(key=sorters.get(sort_key, sorters["score"]), reverse=True)
    shown = rows[:limit]

    # ── 分數分布：判斷今天整體訊號強不強 ──
    bands = [(80, "80 以上"), (70, "70–79"), (60, "60–69"), (0, "60 以下")]
    dist, rest = [], sorted(rows, key=lambda r: r["score"], reverse=True)
    for i, (lo, label) in enumerate(bands):
        hi = bands[i - 1][0] if i else 999
        n = len([r for r in rest if lo <= r["score"] < hi])
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
            members.sort(key=lambda x: x["score"], reverse=True)
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

    def stock_row(r):
        return f"""
<div class="row">
  <div><span class="name">{r['name']}</span><span class="code">{r['code']}</span>
    {f'<span class="badge">{r["breakout"]}</span>' if r['breakout'] else ''}</div>
  <div class="price num">{r['score']}<span class="sub">分</span></div>
  <div class="meta">
    <span><em>價</em> <span class="num">{r['close']:,.2f}</span> {fmt_pct(r['pct'])}</span>
    <span><em>營收年增</em> {f"{r['cum_yoy']:+.1f}%" if r['cum_yoy'] is not None else '—'}</span>
    <span><em>PE</em> {f"{r['pe']:.1f}" if r['pe'] else '—'}</span>
    <span><em>PEG</em> {f"{r['peg']:.2f}" if r['peg'] else '—'}</span>
    <span><em>連買</em> {r['streak']} 日</span>
    <span><em>金額</em> <span class="num">{r['turnover']:.1f}</span> 億</span>
  </div>
  <div class="chg sub">營收{r['rev']}·估值{r['val']}·產業{r['mom']}·籌碼{r['streak_score']}·技術{r['chip']}</div>
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

    controls = f"""
<form method="get" class="controls">
  <input type="hidden" name="mode" value="{mode}">
  <input type="hidden" name="view" value="{view}">
  <div class="fields">
    <div><label>排序</label><select name="sort" onchange="this.form.submit()">
      {opt('score','綜合分數',sort_key)}{opt('pct','當日漲幅',sort_key)}
      {opt('yoy','營收年增',sort_key)}{opt('pe','本益比（低→高）',sort_key)}
      {opt('streak','法人連買天數',sort_key)}{opt('turnover','成交金額',sort_key)}
    </select></div>
    <div><label>顯示筆數</label><select name="limit" onchange="this.form.submit()">
      {opt(10,'10 筆',limit)}{opt(20,'20 筆',limit)}{opt(50,'50 筆',limit)}
    </select></div>
    <div><label>最低分數</label><select name="min_score" onchange="this.form.submit()">
      {opt('','不限',min_score)}{opt(60,'60 以上',min_score)}
      {opt(70,'70 以上',min_score)}{opt(80,'80 以上',min_score)}
    </select></div>
    <div><label>本益比上限</label><select name="max_pe" onchange="this.form.submit()">
      {opt('','不限',max_pe)}{opt(15,'15 倍',max_pe)}{opt(20,'20 倍',max_pe)}
      {opt(30,'30 倍',max_pe)}{opt(50,'50 倍',max_pe)}
    </select></div>
    <div><label>產業</label><select name="industry" onchange="this.form.submit()">
      {opt('','全部',industry_filter)}
      {''.join(opt(i, i, industry_filter) for i in industries)}
    </select></div>
    <div><label>金融保險股</label><select name="fin" onchange="this.form.submit()">
      {opt('','排除（建議）','1' if show_fin else '')}
      {opt('1','納入','1' if show_fin else '')}
    </select></div>
  </div>
</form>"""

    dist_html = "".join(
        f'<span class="dist-item"><b>{n}</b> 檔 {label}</span>' for label, n in dist)

    per_sector = 2 if limit >= 20 else 1
    if view == "sector":
        main_html = ("".join(sector_block(b, per_sector) for b in sector_blocks)
                     or '<div class="empty">沒有符合條件的標的，試著放寬篩選。</div>')
        count_note = f"{len(sector_blocks)} 個產業・每個產業取前 {per_sector} 名"
    else:
        main_html = ('<div class="rows">'
                     + "".join(stock_row(r) for r in shown) + '</div>'
                     if shown else
                     '<div class="empty">沒有符合條件的標的，試著放寬篩選。</div>')
        count_note = f"共 {len(rows)} 檔符合條件"

    body = f"""
<div class="tabs">
  <a href="/web/screener?mode=blackhorse&view={view}"
     class="{'on' if mode != 'radar' else ''}">黑馬</a>
  <a href="/web/screener?mode=radar&view={view}"
     class="{'on' if mode == 'radar' else ''}">雷達</a>
  <span class="tabs-gap"></span>
  <a href="/web/screener?mode={mode}&view=list"
     class="{'on' if view != 'sector' else ''}">總排行</a>
  <a href="/web/screener?mode={mode}&view=sector"
     class="{'on' if view == 'sector' else ''}">依產業</a>
</div>
<div class="mode-note">{
  '' if show_fin else '預設排除金融保險業：其「營收」為利息與投資收益，年增率常因評價變動而失真。　'}{
  '近 10 日累計買超前 120 名，依營收成長、估值、產業動能、法人連續性綜合評分。'
  if mode != 'radar' else
  '當日法人買超且漲幅 1.5% 以上，著重帶量突破與位階。'}{
  '　產業依「領先群營收年增率」由高至低排列。' if view == 'sector' else ''}</div>

<div class="dist">{dist_html}<span class="dist-note">{count_note}</span></div>
{controls}
{main_html}
"""
    return render_page("選股台", body, nav_active="screener")


# --- LINE Bot 訊息接收與路由分派 ---
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
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
        "歡迎使用台股 BOT 📈\n\n"
        "這裡可以查台股行情、法人籌碼、營收與估值，"
        "也能建立自己的自選股清單。\n\n"
        "下面是可用的功能，直接點按鈕就能執行。\n"
        "隨時輸入「選單」都能再叫出來。\n\n"
        "⏳ 小提醒\n"
        "每個指令都會即時去抓最新的行情、法人與財務資料，"
        "大約需要 20 秒才會回覆。送出後請稍等一下，"
        "不用重複點擊，謝謝包涵。\n\n"
        "———\n"
        "作者：蔡秉軒　敬上"
    ))
    try:
        menu = build_menu_flex()
        menu.quick_reply = build_quick_reply()
        line_bot_api.reply_message(event.reply_token, [welcome, menu])
    except Exception as e:
        print(f"❌ 歡迎訊息發送失敗 {user_id}: {e}")


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    flex_reply = None  # 若為 Flex 訊息（彩色選單），改用這個回覆
    text = event.message.text.strip()
    text_upper = text.upper()
    pure_code = normalize_code(text)  # 保留主動式ETF的英文尾碼，如 00981A

    add_user_to_db(user_id)

    # 0. 管理指令（只有 ADMIN_USER_ID 本人可用，其他人輸入等同無效指令）
    if text in ["我的ID", "我的id", "MYID"]:
        reply = f"你的 user_id：\n{user_id}"

    elif text in ["網頁", "WEB", "網頁版"]:
        token = create_web_token(user_id)
        if token:
            base = request.url_root.rstrip("/")
            reply = (f"🌐 台股 BOT 網頁版　🚧 Coming soon\n\n"
                     f"{base}/web/login?t={token}\n\n"
                     f"可輸入持股，查看產業集中度、相關係數與加權基本面。\n"
                     f"連結 {WEB_SESSION_DAYS} 天內有效，過期再輸入「網頁」取得新的。\n\n"
                     f"⚠️ 目前仍在開發中，功能與畫面可能隨時調整。")
        else:
            reply = "❌ 產生連結失敗，請稍後再試。"

    elif is_admin(user_id) and text in ["名單", "使用者", "VIP"]:
        reply = build_user_list_report()

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
            set_notify(target[0], turn_on)
            if turn_on:
                set_requested(target[0], False)
            reply = (f"{'🔔 已開通' if turn_on else '🔕 已停用'}：{target[1]}\n\n"
                     + build_user_list_report())
        elif ambiguous:
            reply = f"符合「{arg}」的有多人：{ambiguous}\n請改用編號，例如「開通 3」"
        else:
            reply = f"找不到「{arg}」。輸入「名單」查看編號。"

    # 1. 加自選
    elif "加" in text and 4 <= len(pure_code) <= 7:
        success = add_watchlist_db(user_id, pure_code)
        c_name = STOCK_NAME_MAP.get(pure_code, pure_code)
        if success:
            reply = f"✅ 新增自選成功：{pure_code} {c_name}"
        else:
            reply = f"❌ 新增自選失敗，資料庫寫入異常：{pure_code}"

    # 2. 刪自選
    elif "刪" in text and 4 <= len(pure_code) <= 7:
        remove_watchlist_db(user_id, pure_code)
        reply = f"🗑️ 已從自選清單移除：{pure_code}"

    # 3. 推播開關設定
    elif text in ["推播開", "開啟推播", "訂閱", "申請推播"]:
        # 每日推播為名額制：LINE 免費方案每月僅 200 則主動訊息，
        # 以每人每個交易日 1 則計算，最多只能服務約 9 人，
        # 因此改為申請制，由管理者在後台開通，使用者無法自行啟用。
        set_requested(user_id, True)
        reply = (
            "📮 已收到每日推播的申請\n\n"
            "每日盤前推播為名額制，需由管理者開通。\n"
            "已收到你的申請，開通後隔天早上就會自動收到。\n\n"
            "在此之前，隨時輸入「盤前」都能看到相同內容。"
        )
    elif text in ["推播關", "關閉推播", "取消訂閱"]:
        # 關閉不需要審核，使用者隨時可以自己退出
        set_notify(user_id, False)
        reply = "🔕 已關閉每日推播。想再開啟請輸入「申請推播」。"

    # 4+5. 自選清單與健檢已合併——兩者原本都在列自選股，差別只在有沒有評分，
    # 併成同一份報告，「自選」與「健檢」都指向它
    elif text in ["自選", "WATCHLIST", "健檢", "自選健檢"]:
        reply = build_healthcheck_report(user_id)

    # 6. 單獨查代號行情
    elif 4 <= len(pure_code) <= 7 and len(text) <= 8 and " " not in text:
        data = get_realtime_stock(pure_code)
        if data:
            inst_all = fetch_institutional_data() or {}
            disp_name = short_company_name(
                stock_display_name(pure_code, inst_all, data["name"]))
            reply = (
                f"📊 {data['code']} {disp_name}\n"
                f"──────────────\n"
                f"💰 現價：{data['close']:.2f} ({data['pct']:+.2f}%)\n"
                f"🔺 高/低：{data['high']:.2f} / {data['low']:.2f}\n"
                f"📦 量能：{int(data['volume'] / 1000):,} 張\n"
                f"──────────────\n"
                f"🛡️ 支撐：{data['support']}\n"
                f"🚧 壓力：{fmt_resistance(data['resistance'])}\n"
                f"\n【位階】\n{build_position_desc(data)}"
            )

            news = fetch_stock_news(disp_name, max_items=2)
            if news:
                reply += "\n\n📰 相關新聞"
                for n in news:
                    src = f"（{n['source']}）" if n["source"] else ""
                    reply += f"\n・{n['title']}{src}"
                    if n["link"]:
                        reply += f"\n　{n['link']}"
        else:
            reply = f"❌ 查無代號 {pure_code} 的行情，請確認代號是否正確。"

    # 6.5 自選股新聞（手動查詢，跟盤後推播同一份內容）
    elif text in ["新聞", "自選新聞"]:
        reply = build_news_digest(user_id) or "📂 自選清單是空的，先用「加 2330」新增自選"

    # 7. 盤前速覽
    elif text in ["盤前", "早安"]:
        reply = build_morning_push(user_id)

    # 7.5 盤後解盤（使用者手動輸入才觸發，不自動推播）
    elif text in ["解盤", "盤後解盤", "盤後"]:
        reply = build_market_recap()

    # 8. 黑馬股（不同於雷達：以「月營收年增率」為主軸，找有題材／獲利成長的股票）
    elif text == "黑馬":
        inst_data = fetch_institutional_data()
        if not inst_data:
            reply = "❌ 目前無法取得三大法人資料，可能是非交易時段或非交易日，請稍後再試。"
        else:
            revenue_data = fetch_monthly_revenue()
            valuation_data = fetch_valuation()
            bh_industry_map = get_industry_map()
            industry_momentum = get_industry_momentum(revenue_data, bh_industry_map)
            # 候選池改用「近10日累計買超」而非「今日買超前40名」。
            # 長線佈局常是量小但持續，看單日排行會漏掉每天小買、連買十幾天的股票；
            # 池子也因此變寬變雜，產業動能那一項才有鑑別度。
            cum_buyers = get_cumulative_net_buy(days=10, top_n=80)
            if not cum_buyers:
                # 歷史資料還沒累積時，退回原本的當日買超排行，功能不會整個斷掉
                cum_buyers = [
                    (code, info.get("name", code), info["total_net_lots"], 1)
                    for code, info in sorted(
                        inst_data.items(),
                        key=lambda x: x[1]["total_net_lots"], reverse=True
                    )
                    if len(code) == 4 and code.isdigit()
                    and not code.startswith("00") and info["total_net_lots"] > 0
                ][:80]

            candidates = [
                (code, {"name": name, "total_net_lots": inst_data.get(code, {}).get("total_net_lots", 0),
                        "cum_lots": cum_lots, "buy_days": buy_days})
                for code, name, cum_lots, buy_days in cum_buyers
                # 排除金融保險業：銀行的「營收」是利息與手續費收入，
                # 性質與製造業不同，套用同一套營收成長標準會失真
                if not is_financial(code, bh_industry_map)
            ]

            streaks = get_consecutive_days_batch([c for c, _ in candidates])

            scored = []
            for code, info in candidates:
                price = get_realtime_stock(code)
                if not price:
                    continue
                if price["close"] < 10:  # 排除低價股，容易被小額資金拉出失真漲幅
                    continue
                if abs(price["pct"]) > 10.5:  # 防呆：台股單日漲跌幅上限10%，超過視為資料異常
                    continue
                turnover = calc_turnover_billion(price["close"], price["volume"])
                if turnover < 1:  # 排除成交金額 <1億元，流動性不足
                    continue

                yoy_pct = revenue_data.get(code, {}).get("yoy_pct")
                cum_yoy_pct = revenue_data.get(code, {}).get("cum_yoy_pct")
                # 營收成長壓縮成 0-25（原本 0-40）
                revenue_score = round(score_from_cum_revenue_growth(cum_yoy_pct) * 25 / 40)

                val = valuation_data.get(code, {})
                pe = val.get("pe")
                val_score, peg, val_desc = score_from_valuation(pe, cum_yoy_pct)  # 0-25

                supply_score, supply_desc = score_from_industry_momentum(
                    industry_momentum.get(bh_industry_map.get(code))
                )  # 0-20

                streak = streaks.get(code, 0)
                streak_score = round(score_from_streak(streak) * 20 / 30)  # 0-20

                chip_raw = score_from_net_lots(info.get("cum_lots", 0))
                tech_raw = score_from_technical(price["pct"], turnover)
                # 當日籌碼與技術合併壓縮成 0-10，長線選股不該讓單日表現主導
                chip_tech_score = round((chip_raw / 40 * 5) + (tech_raw / 60 * 5))

                total_score = (revenue_score + val_score + supply_score
                               + streak_score + chip_tech_score)  # 滿分 100
                scored.append((total_score, code, info, price, revenue_score,
                               val_score, val_desc, peg, supply_score, supply_desc,
                               streak_score, streak, chip_tech_score,
                               yoy_pct, cum_yoy_pct))

            scored.sort(key=lambda x: x[0], reverse=True)  # 依綜合總分排序，長線指標權重最高

            reports = []
            industry_map = get_industry_map()
            for rank, (total_score, code, info, price, revenue_score,
                       val_score, val_desc, peg, supply_score, supply_desc,
                       streak_score, streak, chip_tech_score,
                       yoy_pct, cum_yoy_pct) in enumerate(scored[:5], start=1):
                grade = "🔥 高度看好" if total_score >= 75 else ("🚀 值得關注" if total_score >= 55 else "📈 觀察名單")
                cum_text = f"{cum_yoy_pct:+.1f}%" if cum_yoy_pct is not None else "尚無資料"
                yoy_text = f"{yoy_pct:+.1f}%" if yoy_pct is not None else "尚無資料"
                streak_text = f"連續{streak}日買超" if streak >= 1 else "近期無連續買超"
                ind_code = industry_map.get(code)
                ind_text = industry_name(ind_code) if ind_code else "未分類"
                report = (
                    f"🐎 智慧黑馬股 #{rank}\n\n"
                    f"股票：{info['name']}\n"
                    f"代號：{code}\n"
                    f"產業：{ind_text}\n\n"
                    f"黑馬指數：{total_score}／100\n\n"
                    f"💡 營收成長：{revenue_score}／25\n"
                    f"　　累計年增 {cum_text}（單月 {yoy_text}）\n"
                    f"💰 估值：{val_score}／25\n"
                    f"　　{val_desc}\n"
                    f"🏭 產業動能：{supply_score}／20\n"
                    f"　　{supply_desc}\n"
                    f"🔁 法人連續性：{streak_score}／20（{streak_text}）\n"
                    f"📊 籌碼技術：{chip_tech_score}／10\n"
                    f"　　近10日累計買超 {info.get('cum_lots', 0):,} 張"
                    f"（{info.get('buy_days', 0)} 天買超）\n\n"
                    f"【位階】\n"
                    f"{build_position_desc(price)}\n\n"
                    f"【判定】\n"
                    f"{grade}\n"
                    f"-----------------------------------"
                )
                reports.append(report)
            reply = "\n\n".join(reports) if reports else "❌ 暫無符合條件的標的。"

    # 9. 盤中雷達（法人買超股票中，依漲幅排序）
    elif text == "雷達":
        inst_data = fetch_institutional_data()
        if not inst_data:
            reply = "❌ 目前無法取得三大法人資料，可能是非交易時段或非交易日，請稍後再試。"
        else:
            candidates = [
                (code, info) for code, info in inst_data.items()
                if len(code) == 4 and code.isdigit()
                and not code.startswith("00")  # 排除 ETF
                and info["total_net_lots"] > 0
            ]
            candidates.sort(key=lambda x: x[1]["total_net_lots"], reverse=True)

            priced = []
            for code, info in candidates[:60]:
                price = get_realtime_stock(code)
                if not price:
                    continue
                if price["close"] < 10:  # 排除低價股
                    continue
                if price["pct"] < 1.5:  # 還沒發動的先不看；上限不設，漲停也可能是突破的起點
                    continue
                if price["pct"] > 10.5:  # 防呆：超過台股漲跌幅上限視為資料異常
                    continue
                turnover = calc_turnover_billion(price["close"], price["volume"])
                if turnover < 1:  # 排除成交金額 <1億元
                    continue
                priced.append((code, info, price))

            streaks = get_consecutive_days_batch([c for c, _, _ in priced])

            def radar_rank(item):
                """
                排序邏輯：突破位階 > 帶量 > 法人連續買超 > 當日漲幅。
                目的是讓「帶量突破前高」排在「已連漲多日的追高盤」前面。
                """
                code, info, price = item
                close = price["close"]
                breakout = 0
                if price.get("high_60d") and close >= price["high_60d"]:
                    breakout = 2  # 創季線新高
                elif price.get("high_20d") and close >= price["high_20d"]:
                    breakout = 1
                vol_ratio = price.get("vol_ratio") or 0
                # 連漲太多天的扣分，避免推薦已經噴到末端的股票
                fatigue = -1 if price.get("up_streak", 0) >= 5 else 0
                return (breakout + fatigue, vol_ratio, streaks.get(code, 0), price["pct"])

            priced.sort(key=radar_rank, reverse=True)

            reports = []
            for code, info, price in priced[:5]:
                turnover = calc_turnover_billion(price["close"], price["volume"])
                streak = streaks.get(code, 0)
                close = price["close"]
                if price.get("high_60d") and close >= price["high_60d"]:
                    level = "🚀 帶量突破季線新高"
                elif price.get("high_20d") and close >= price["high_20d"]:
                    level = "📈 突破近月高點"
                elif price.get("up_streak", 0) >= 5:
                    level = "⚠️ 已連漲多日，位階偏高"
                else:
                    level = "👀 區間內上漲"
                streak_line = f"🔁 法人連續買超：{streak} 日\n" if streak >= 2 else ""
                report = (
                    f"🚨【盤中雷達】\n\n"
                    f"🔥 強勢股票：{info['name']}\n"
                    f"📌 股票代號：{code}\n\n"
                    f"💰 現價：{price['close']:.2f}\n"
                    f"📈 漲幅：{price['pct']:+.2f}%\n"
                    f"📊 成交金額：{turnover:.1f} 億\n"
                    f"🏦 三大法人買超：{info['total_net_lots']:,} 張\n"
                    f"{streak_line}\n"
                    f"🏆 狀態：{level}\n\n"
                    f"【位階】\n"
                    f"{build_position_desc(price)}\n\n"
                    f"【注意】\n"
                    f"{build_risk_desc(price['pct'], info['total_net_lots'])}\n"
                    f"-----------------------------------"
                )
                reports.append(report)
            reply = "\n\n".join(reports) if reports else "❌ 暫無符合條件的標的。"

    elif text_upper in ["MENU", "選單", "幫助", "HELP"]:
        flex_reply = build_menu_flex()
        reply = None

    else:
        # 指令沒對上時直接把選單給他，不要只回一句「請輸入選單」
        flex_reply = build_menu_flex()
        reply = None

    qr = build_quick_reply()
    if flex_reply is not None:
        flex_reply.quick_reply = qr
        line_bot_api.reply_message(event.reply_token, flex_reply)
    else:
        line_bot_api.reply_message(
            event.reply_token, TextSendMessage(text=reply, quick_reply=qr))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
