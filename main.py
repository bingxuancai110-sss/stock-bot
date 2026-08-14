import os
import socket
import time
import requests
import psycopg2
from psycopg2 import pool
from psycopg2.extras import execute_values
from urllib.parse import urlparse
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime, timedelta, timezone
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
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (str(user_id).strip(),)
        )
        conn.commit()
        cursor.close()
    except Exception as e:
        conn.rollback()
        print(f"❌ 新增使用者錯誤: {e}")
    finally:
        release_db_connection(conn)

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

            # 連續上漲天數（含今天）
            up_streak = 0
            series = [b[1] for b in hist] + [close]
            for i in range(len(series) - 1, 0, -1):
                if series[i] > series[i - 1]:
                    up_streak += 1
                else:
                    break

            # 支撐壓力改用實際的近期高低點與均線，不再用「今日高低價微調」
            resistance = round(high_60d, 2) if (high_60d and close >= (high_20d or 0)) else (
                round(high_20d, 2) if high_20d else round(high * 1.01, 2))
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

def industry_name(code):
    """把產業別代碼轉成中文名稱；查不到就回傳原代碼，不會讓資料消失。"""
    code = str(code).strip().zfill(2)
    return INDUSTRY_NAME_MAP.get(code, f"未知類別({code})")


def fetch_and_save_industry():
    """
    從 TWSE OpenAPI 抓上市公司基本資料（含產業別），存進 stock_info。
    產業別幾乎不變，抓一次就夠，之後想更新再打一次端點即可。
    回傳 (筆數, 產業別樣本清單)。
    """
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
    try:
        rows = requests.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'}).json()
    except Exception as e:
        print(f"❌ 抓取公司基本資料失敗: {e}")
        return 0, []

    if not rows:
        return 0, []

    records = []
    for row in rows:
        code = str(row.get("公司代號", "")).strip()
        name = str(row.get("公司簡稱", "")).strip()
        industry = str(row.get("產業別", "")).strip()
        if code:
            records.append((code, name, industry))

    if not records:
        return 0, []

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        execute_values(
            cursor,
            """
            INSERT INTO stock_info (code, name, industry)
            VALUES %s
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                industry = EXCLUDED.industry
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

    sample = sorted({ind for _, _, ind in records if ind})[:30]
    return len(records), sample


_industry_cache = {"map": None}

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

    url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
    try:
        rows = requests.get(url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'}).json()
    except Exception as e:
        print(f"❌ 抓取本益比資料失敗: {e}")
        return _valuation_cache["data"] or {}

    def to_float(s):
        try:
            v = float(str(s).replace(",", "").strip())
            return v if v > 0 else None
        except (ValueError, TypeError):
            return None

    result = {}
    for row in rows or []:
        code = str(row.get("Code", "")).strip()
        if not code:
            continue
        result[code] = {
            "pe": to_float(row.get("PEratio")),
            "pb": to_float(row.get("PBratio")),
            "yield": to_float(row.get("DividendYield")),
        }

    if result:
        _valuation_cache["date"] = today
        _valuation_cache["data"] = result
        print(f"✅ 估值資料抓取成功，共 {len(result)} 筆")
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

    # 連漲天數
    if up_streak >= 5:
        parts.append(f"・已連續上漲 {up_streak} 天")
    elif up_streak >= 2:
        parts.append(f"・連續上漲 {up_streak} 天")
    elif up_streak <= 0:
        parts.append("・今日為近期首根上漲K棒")

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

def generate_morning_brief():
    today_str = datetime.now().strftime("%Y/%m/%d")
    return (
        f"☀️ 【台股盤前與總經動態】\n📅 日期：{today_str}\n"
        f"-------------------\n"
        f"• 道瓊指數：+0.45%\n"
        f"• 費城半導體：+1.12%\n"
        f"• 輝達 (NVDA)：+1.85%\n"
        f"• 台積電ADR (TSM)：+1.40%"
    )

# --- 月營收（TWSE OpenAPI t187ap05_L，全上市公司，一個月只有一期，用「資料年月」當快取key） ---
_revenue_cache = {"period": None, "data": {}}

def fetch_monthly_revenue():
    """抓上市公司最新一期月營收，回傳 {代號: {"yoy_pct": 去年同月增減%}}。
    這是「有題材／獲利成長」判斷的核心資料來源，用來讓黑馬邏輯跟雷達真正不一樣。"""
    try:
        url = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
        rows = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'}).json()
        if not rows:
            return _revenue_cache["data"]

        period = rows[0].get("資料年月")
        if _revenue_cache["period"] == period and _revenue_cache["data"]:
            return _revenue_cache["data"]

        def to_float(s):
            try:
                return float(str(s).replace(",", ""))
            except (ValueError, TypeError):
                return None

        result = {}
        for row in rows:
            code = str(row.get("公司代號", "")).strip()
            if not code:
                continue
            result[code] = {
                "yoy_pct": to_float(row.get("營業收入-去年同月增減(%)")),
                "cum_yoy_pct": to_float(row.get("累計營業收入-前期比較增減(%)")),
                "mom_pct": to_float(row.get("營業收入-上月比較增減(%)")),
                "month_revenue": to_float(row.get("營業收入-當月營收")),
            }

        _revenue_cache["period"] = period
        _revenue_cache["data"] = result
        print(f"✅ 月營收抓取成功（{period}），共 {len(result)} 筆")
        save_revenue_history(period, result)
        return result
    except Exception as e:
        print(f"❌ 抓取月營收資料錯誤: {e}")
        return _revenue_cache["data"]

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

# --- 自選股健檢：把 watchlist 全部跑一次評分，依總分排序 ---
def build_healthcheck_report(user_id):
    codes = get_user_watchlist(user_id)
    if not codes:
        return "📂 自選股清單是空的\n輸入「加 3081」新增自選"

    institutional_data = fetch_institutional_data()
    streaks = get_consecutive_days_batch(codes)
    rows = []  # (total_score, display_text)

    for code in codes:
        stock = get_realtime_stock(code)
        if not stock:
            rows.append((-1, f"⚪ {code} 查無行情"))
            continue

        inst = institutional_data.get(code, {})
        net_lots = inst.get("total_net_lots", 0)
        streak = streaks.get(code, 0)

        chip_score = score_from_net_lots(net_lots)
        turnover = calc_turnover_billion(stock["close"], stock["volume"])
        tech_score = score_from_technical(stock["pct"], turnover)
        total_score = chip_score + tech_score

        flag = "🟢" if total_score >= 70 else ("🟡" if total_score >= 40 else "🔴")
        name = inst.get("name") or stock["name"]
        streak_text = f"　連{streak}買" if streak >= 2 else ""

        text = (
            f"{flag} {name} {code}\n"
            f"{stock['close']:.2f}（{stock['pct']:+.2f}%）"
            f"　法人{net_lots:+,}張{streak_text}　{total_score}分"
        )
        rows.append((total_score, text))

    rows.sort(key=lambda x: x[0], reverse=True)
    body = "\n\n".join(text for _, text in rows)
    report = f"📋 自選股健檢（{len(codes)}檔）\n\n{body}\n\n🟢70+ 🟡40-69 🔴<40"

    # LINE 單則文字訊息長度上限保護（約 5000 字），過長就截斷並提示
    if len(report) > 4800:
        report = report[:4750] + "\n\n…（清單過長，已截斷）"
    return report

# --- 排程推播訊息建構與 Cron 端點 ---
def build_digest(user_id):
    codes = get_user_watchlist(user_id)
    if not codes:
        return None
    lines = ["☀️ 【每日自選股盤前摘要】\n==================="]
    for code in codes:
        data = get_realtime_stock(code)
        if data:
            light = "🔴" if data['pct'] >= 0 else "🟢"
            lines.append(f"{light} {code} {data['name']}｜{data['close']:.2f}（{data['pct']:+.2f}%）\n🛡️ 支撐：{data['support']} | 🚧 壓力：{data['resistance']}")
        else:
            lines.append(f"⚪ {code} 查無行情")
    return "\n\n".join(lines)

@app.route("/cron/push-watchlist", methods=["POST", "GET"])
def cron_push_watchlist():
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)

    users = get_notify_users()
    sent, failed = 0, 0
    for uid in users:
        msg = build_digest(uid)
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
    return (
        f"產業別同步完成\n"
        f"共存入：{count} 檔\n"
        f"產業別樣本（前30種）：\n" + "\n".join(sample)
    ), 200


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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    text_upper = text.upper()
    pure_code = "".join(filter(str.isdigit, text))

    add_user_to_db(user_id)

    # 1. 加自選
    if "加" in text and 4 <= len(pure_code) <= 6:
        success = add_watchlist_db(user_id, pure_code)
        c_name = STOCK_NAME_MAP.get(pure_code, pure_code)
        if success:
            reply = f"✅ 新增自選成功：{pure_code} {c_name}"
        else:
            reply = f"❌ 新增自選失敗，資料庫寫入異常：{pure_code}"

    # 2. 刪自選
    elif "刪" in text and 4 <= len(pure_code) <= 6:
        remove_watchlist_db(user_id, pure_code)
        reply = f"🗑️ 已從自選清單移除：{pure_code}"

    # 3. 推播開關設定
    elif text in ["推播開", "開啟推播", "訂閱"]:
        set_notify(user_id, True)
        reply = "🔔 已開啟每日自選股推播！將於每個交易日盤前 08:45 為你發送摘要。"
    elif text in ["推播關", "關閉推播", "取消訂閱"]:
        set_notify(user_id, False)
        reply = "🔕 已關閉每日推播。"

    # 4. 看自選清單
    elif text in ["自選", "WATCHLIST"]:
        codes = get_user_watchlist(user_id)
        if not codes:
            reply = "📂 目前自選清單是空的。\n💡 請輸入「加 3081」或「加 6442」來新增自選！"
        else:
            results = ["📂 【我的雲端自選股與策略】\n==================="]
            for code in codes:
                data = get_realtime_stock(code)
                if data:
                    light = "🔴" if data['pct'] >= 0 else "🟢"
                    block = f"\n{light} 【{code} {data['name']}】 現價：{data['close']:.2f} ({data['pct']:+.2f}%)\n🛡️ 支撐：{data['support']} | 🚧 壓力：{data['resistance']}"
                    results.append(block)
                else:
                    results.append(f"\n⚪ 【{code}】 查無行情")
            reply = "".join(results)

    # 5. 自選股一鍵健檢（新增功能）
    elif text in ["健檢", "自選健檢"]:
        reply = build_healthcheck_report(user_id)

    # 6. 單獨查代號行情
    elif 4 <= len(pure_code) <= 6 and len(text) <= 7 and " " not in text:
        data = get_realtime_stock(pure_code)
        if data:
            reply = (
                f"📊 {data['code']} {data['name']}\n"
                f"==================-\n"
                f"💰 現價：{data['close']:.2f} ({data['pct']:+.2f}%)\n"
                f"🔺 高/低：{data['high']:.2f} / {data['low']:.2f}\n"
                f"📦 量能：{int(data['volume'] / 1000):,} 張\n"
                f"-------------------\n"
                f"🛡️ 短線支撐：{data['support']}\n"
                f"🚧 短線壓力：{data['resistance']}"
            )
        else:
            reply = f"❌ 查無代號 {pure_code} 的行情，請確認代號是否正確。"

    # 7. 盤前速覽
    elif text in ["盤前", "早安"]:
        reply = generate_morning_brief()

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
            candidates = [
                (code, info) for code, info in inst_data.items()
                if len(code) == 4 and code.isdigit()
                and not code.startswith("00")  # 排除 ETF（0050、0056...）
                and info["total_net_lots"] > 0
                # 排除金融保險業：銀行的「營收」是利息與手續費收入，
                # 性質與製造業不同，套用同一套營收成長標準會失真
                and bh_industry_map.get(code, "").zfill(2) != "17"
            ]
            candidates.sort(key=lambda x: x[1]["total_net_lots"], reverse=True)

            streaks = get_consecutive_days_batch([c for c, _ in candidates[:40]])

            scored = []
            for code, info in candidates[:40]:
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

                chip_raw = score_from_net_lots(info["total_net_lots"])
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
                    f"📊 當日籌碼技術：{chip_tech_score}／10\n\n"
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
        reply = (
            "🤖 蔡秉軒御用選股機器人\n"
            "===================\n"
            "🔥 核心策略專區（真實三大法人籌碼）\n"
            "• 輸入「盤前」➜ 美股與總經速覽\n"
            "• 輸入「解盤」➜ 盤後大盤與法人資金解析\n"
            "• 輸入「黑馬」➜ 有題材／營收成長的潛力股\n"
            "• 輸入「雷達」➜ 法人連續買超、剛啟動的股票\n\n"
            "📂 自選與策略管理\n"
            "• 輸入「自選」➜ 查看雲端自選與支撐壓力\n"
            "• 輸入「健檢」➜ 自選股一鍵健檢評分報告\n"
            "• 輸入「加 3081」➜ 新增自選\n"
            "• 輸入「刪 3081」➜ 移除自選\n"
            "• 輸入「推播開 / 推播關」➜ 開啟或關閉盤前摘要推播\n"
            "• 直接輸入代號（如 3081、6442）➜ 查即時行情與支撐"
        )
    else:
        reply = "🤖 指令未識別，請輸入「選單」查看可用功能！"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
