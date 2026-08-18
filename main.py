import os
import re
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
from urllib.parse import urlparse
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (MessageEvent, TextMessage, TextSendMessage,
                            FlexSendMessage, FollowEvent,
                            QuickReply, QuickReplyButton, MessageAction)
from datetime import datetime, timedelta, timezone, date
from concurrent.futures import ThreadPoolExecutor
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
                result TEXT
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


def sell_position(user_id, pos_id, sell_shares,
                  sell_price=None, fee=None, tax=None):
    """
    賣出持股。賣出股數等於整筆數量時直接刪除該筆；
    小於時只減少股數，每股成本不變──賣出不影響剩餘股份的成本基礎。

    賣價、手續費、證交稅都由使用者填寫，因為只有他知道實際成交的數字：
    折扣、最低收費、當沖減半各券商不同，程式算出來的永遠只是估計值，
    存進交易紀錄的數字應該要能跟對帳單對得起來。
    留空的欄位才用市價與公式試算，當作方便而非準確來源。

    已實現損益 =（賣出股數 × 賣價 − 手續費 − 證交稅）− 賣出股數 × 成本價
    成本價沿用券商口徑（已含買進手續費），所以這裡不再另外扣買進費用。

    回傳 (成功與否, 錯誤訊息或 None, 這筆的損益摘要或 None)
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT code, shares, cost, bought_on FROM positions "
            "WHERE id = %s AND user_id = %s",
            (int(pos_id), str(user_id).strip()))
        row = cursor.fetchone()
        if not row:
            cursor.close()
            return False, "找不到這筆持股", None
        code, current_shares, lot_cost, bought_on = row
        if sell_shares <= 0:
            cursor.close()
            return False, "賣出股數必須大於 0", None
        if sell_shares > current_shares:
            cursor.close()
            return False, f"賣出股數不能超過持有股數（{current_shares:,} 股）", None

        if sell_shares == current_shares:
            cursor.execute("DELETE FROM positions WHERE id = %s AND user_id = %s",
                           (int(pos_id), str(user_id).strip()))
        else:
            cursor.execute(
                "UPDATE positions SET shares = shares - %s WHERE id = %s AND user_id = %s",
                (sell_shares, int(pos_id), str(user_id).strip()))
        conn.commit()
        cursor.close()
    except Exception as e:
        conn.rollback()
        print(f"❌ 賣出持股失敗: {e}")
        return False, "系統錯誤，請稍後再試", None
    finally:
        release_db_connection(conn)

    # 沒填賣價就用當下市價；連市價都抓不到，這筆就沒有損益數字可記
    if sell_price is None:
        price_data = get_realtime_stock(code)
        sell_price = price_data["close"] if price_data else None
    if sell_price is None:
        return True, None, None

    gross = sell_shares * sell_price
    if fee is None:
        fee = broker_fee(gross)                                  # 未填則以牌價試算
    if tax is None:
        tax = gross * (TAX_RATE_ETF if is_etf(code) else TAX_RATE_STOCK)

    cost_total = sell_shares * lot_cost
    realized_pl = (gross - fee - tax) - cost_total
    realized_pct = (realized_pl / cost_total * 100) if cost_total else None

    record_realized_trade(user_id, code, sell_shares, lot_cost, sell_price,
                          realized_pl, realized_pct, bought_on, fee, tax)
    return True, None, {
        "code": code, "shares": sell_shares, "sell_price": sell_price,
        "cost": lot_cost, "pl": realized_pl, "pct": realized_pct,
        "fee": fee, "tax": tax,
        "held_days": ((date.today() - bought_on).days if bought_on else None),
    }


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
        cursor.execute("SELECT DISTINCT user_id FROM positions")
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
        cursor.execute("SELECT DISTINCT user_id FROM watchlists")
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
_t86_cache = {"cache_date": None, "data_date": None, "data": {}}

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


def fetch_institutional_data():
    """
    抓當日三大法人買賣超，涵蓋上市（TWSE T86）與上櫃（TPEx）。

    上市的部分若今天還沒公布（盤中、假日），會自動往前找最近一個
    有資料的交易日，最多往前找 5 天。上櫃端點只給最新一日，不能指定日期。
    一天只需成功抓取一次，之後直接用快取。
    """
    today = datetime.now().strftime("%Y%m%d")

    if _t86_cache["cache_date"] == today and _t86_cache["data"]:
        return _t86_cache["data"]

    merged, data_date = {}, None
    for days_back in range(0, 6):
        query_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
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
    lines = [f"☀️ 盤前總經簡報　{datetime.now().strftime('%m/%d')}", "═" * 13]

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
    lines = [f"📰 自選股新聞（{datetime.now().strftime('%m/%d')}）", "─" * 14]

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
            line_bot_api.push_message(uid, TextSendMessage(text=msg))
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
    """早上推播盤前簡報＋自選股摘要。受 PUSH_MAX_USERS 額度保護。"""
    secret = request.args.get("token")
    if secret != os.environ.get("CRON_SECRET"):
        abort(403)
    return run_in_background(
        "盤前推播",
        lambda: push_to_users(get_notify_users(), build_morning_push, "盤前推播")), 200

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


def _job_mark_start(name):
    """
    標記工作開始。若同名工作已在執行中且還沒過期，回傳 False 表示不要重複啟動。
    用 UPDATE ... WHERE 的條件一次判斷並搶佔，兩個 worker 同時觸發時
    只有一個會成功——先查再寫會有競爭空窗。
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO job_runs (name, running, started_at)
            VALUES (%s, TRUE, NOW())
            ON CONFLICT (name) DO UPDATE SET
                running = TRUE, started_at = NOW(), result = NULL,
                finished_at = NULL, seconds = NULL
            WHERE job_runs.running = FALSE
               OR job_runs.started_at < NOW() - INTERVAL '%s minutes'
            RETURNING name
            """,
            (name, JOB_STALE_MINUTES),
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


def _job_mark_done(name, result, seconds):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE job_runs SET running = FALSE, finished_at = NOW(),
                   seconds = %s, result = %s
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
              f"時間 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}（伺服器時區）")
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
            SELECT name, running, started_at, finished_at, seconds, result
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

    now_srv = datetime.now()
    tw = datetime.now(timezone(timedelta(hours=8)))
    lines = ["背景工作狀態", "=" * 58, "",
             f"伺服器現在時間　{now_srv.strftime('%m/%d %H:%M')}",
             f"台灣現在時間　　{tw.strftime('%m/%d %H:%M')}",
             ("（兩者相同，時間可直接對照）" if abs(now_srv.hour - tw.hour) == 0
              else f"（相差 {(tw.hour - now_srv.hour) % 24} 小時，"
                   f"下方時間為伺服器時區）"),
             ""]
    for name, running, started, finished, secs, result in rows:
        if running:
            mins = (datetime.now() - started).total_seconds() / 60 if started else 0
            stale = "　⚠️ 疑似當掉" if mins > JOB_STALE_MINUTES else ""
            lines.append(f"[執行中] {name}"
                         f"（已 {mins:.0f} 分鐘）{stale}")
        else:
            when = finished.strftime("%m/%d %H:%M") if finished else "?"
            lines.append(f"[完成] {name}　{when}　耗時 {secs or 0:.1f} 秒")
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
    """實際的快照工作。回傳一行摘要字串，會記在 /job-status 裡。"""
    taiex = fetch_taiex_summary()
    taiex_close = None
    if taiex and taiex.get("close"):
        try:
            taiex_close = float(str(taiex["close"]).replace(",", ""))
        except (TypeError, ValueError):
            taiex_close = None

    # 產業動能排名：全市場共用一份，只要存一次
    try:
        stats = get_industry_momentum(fetch_monthly_revenue() or {},
                                      get_industry_map() or {})
        save_industry_momentum(stats)
        ind_saved = len(stats)
    except Exception as e:
        print(f"❌ 產業動能快照失敗: {e}")
        ind_saved = 0

    # 選股名單：存下今天黑馬與雷達的前 5 名，之後才算得出這套評分有沒有用。
    # 名單跟使用者無關，全市場共用一份。
    picks_saved = 0
    for mode in ("blackhorse", "radar"):
        try:
            rows, _skipped, _mom = compute_screener_rows(mode)
            if mode == "radar":
                rows = sorted(rows, key=lambda r: (
                    2 if r["breakout"] == "季線新高" else (1 if r["breakout"] else 0),
                    r.get("vol_ratio") or 0, r["streak"], r["pct"]), reverse=True)
            else:
                rows = sorted(rows, key=lambda r: (r["score"] if r["score"] is not None else -1),
                              reverse=True)
            picks_saved += save_picks(mode, rows, top_n=5)
        except Exception as e:
            print(f"❌ {mode} 選股名單快照失敗: {e}")

    user_ids = get_all_position_user_ids()
    saved, skipped = 0, 0
    for uid in user_ids:
        try:
            positions = merge_positions(get_positions(uid))
            if not positions:
                skipped += 1
                continue
            total_value, total_cost = 0.0, 0.0
            price_map = get_realtime_stocks_bulk([p["code"] for p in positions])
            for p in positions:
                pr = price_map.get(p["code"])
                if pr:
                    total_value += pr["close"] * p["shares"]
                total_cost += p["cost"] * p["shares"]
            if total_value <= 0:
                skipped += 1
                continue
            if save_portfolio_snapshot(uid, total_value, total_cost, taiex_close):
                saved += 1
            else:
                skipped += 1
        except Exception as e:
            # 單一使用者出錯不該讓其他人的快照一起沒了
            print(f"❌ 組合快照失敗 {uid}: {e}")
            skipped += 1

    # 自選股評分：有自選的人都要存，跟有沒有持股無關
    wl_users, wl_saved = get_all_watchlist_user_ids(), 0
    for uid in wl_users:
        codes = get_user_watchlist(uid)
        if not codes:
            continue
        try:
            scores = compute_watchlist_scores(codes)
            if scores:
                save_watchlist_scores(uid, scores)
                wl_saved += 1
        except Exception as e:
            print(f"❌ 自選股評分快照失敗 {uid}: {e}")

    return (f"組合 {saved}/{len(user_ids)}（略過 {skipped}）、"
            f"自選 {wl_saved}/{len(wl_users)}、產業 {ind_saved}、"
            f"選股名單 {picks_saved}、大盤 {taiex_close}")


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
            ("籌碼超人", "投信、外資各自在認養與撤退的標的"),
        ]),
        ("我的自選", "#2E7D5B", "#E6F1EC", [
            ("自選", "持股評分、位階與支撐壓力"),
            ("新聞", "自選股相關新聞與連結"),
        ]),
        ("網頁版", "#6B4E9E", "#EFEAF7", [
            ("網頁", "組合分析、交易紀錄、選股成效"),
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
"""

NEED_LOGIN_HTML = """
<div class="msg">
  這個連結已失效或尚未登入。
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
    def tab(href, label, key):
        on = " class=\"on\"" if key == nav_active else ""
        return f'<a href="{href}"{on}>{label}</a>'

    nav = ""
    if nav_active:
        nav = ("<nav>"
               + tab("/web/portfolio", "組合", "portfolio")
               + tab("/web/positions", "持股", "positions")
               + tab("/web/trades", "紀錄", "trades")
               + tab("/web/screener", "選股", "screener")
               + tab("/web/compare", "比較", "compare")
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
你輸入的持股只用於產生你自己的分析，作者不會查看個別使用者的持股內容。<br>
資料來源：臺灣證券交易所、櫃買中心、Yahoo Finance。作者：蔡秉軒
</footer>
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
        keyframes.append(f"""
@keyframes q{i} {{
  0%{{opacity:0}}
  {start:.2f}%{{opacity:0}}
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


def render_loading_shell(title, nav_active, stages, note=""):
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
      document.getElementById('content').innerHTML = html;
      document.getElementById('loading').style.display = 'none';
    }}, 180);
  }}

  var url = window.location.pathname + window.location.search
          + (window.location.search ? '&' : '?') + 'fragment=1';

  fetch(url, {{ credentials: 'same-origin' }})
    .then(function (r) {{
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }})
    .then(finish)
    .catch(function (e) {{
      done = true;
      clearInterval(timer);
      stageEl.textContent = '載入失敗，請重新整理頁面。';
      pctEl.textContent = '';
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
        return body
    return render_page(title, body, nav_active=nav_active)


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
    # GET 且不是要片段時，先秒回骨架讓使用者馬上看到畫面，
    # 真正的抓價工作交給後續的 fragment 請求。
    # POST 不能這樣做——表單送出必須當場處理完，否則新增／賣出會遺失。
    if request.method == "GET" and not wants_fragment():
        return render_loading_shell(
            "持股", "positions",
            ["正在讀取你的持股…", "正在抓即時報價…", "正在計算損益與權重…"],
            note="報價來自 Yahoo Finance，逐檔抓取需要一點時間。")

    # 手續費設定要在處理賣出之前先讀出來，賣出當下記錄的已實現損益
    # 才能用使用者自己的折扣／最低收費計算，跟畫面上其他地方口徑一致。
    fee_disc, min_fee = get_fee_settings(get_profile(uid))

    msg = ""
    if request.method == "POST":
        action = request.form.get("action")
        if action == "delete":
            delete_position(uid, request.form.get("id"))
            msg = "已刪除。"
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
                add_position(uid, code, shares, cost,
                             request.form.get("bought_on") or None)
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
    # 抓一年而非預設的三個月：損益走勢要能涵蓋買進日才標得出買進點
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
            held = ((datetime.now().date() - p["bought_on"]).days
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
<div class="section-head"><h2>已實現損益</h2>
  <span class="section-note">共 {len(trades)} 筆交易</span></div>
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


@app.route("/web/portfolio", methods=["GET", "POST"])
@web_login_required
def web_portfolio(uid):
    if request.method == "GET" and not wants_fragment():
        return render_loading_shell(
            "組合分析", "portfolio",
            ["正在讀取你的持股…", "正在抓即時報價…",
             "正在抓法人與月營收資料…", "正在計算集中度與相關係數…",
             "正在整理提醒…"],
            note="組合分析會比對法人籌碼、月營收與估值，資料量較大。")

    msg = ""
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

    # 問卷沒填完就只給問卷。組合分析的價值有一大半來自依你的處境判讀，
    # 少了那些答案，剩下的數字誰看都一樣，沒有必要先給。
    if not is_profile_complete(profile):
        return respond_page("組合分析", risk_card, "portfolio")

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
        return respond_page("組合分析", body, "portfolio")

    th = get_thresholds(profile)
    fee_disc, min_fee = get_fee_settings(profile)
    inst = fetch_institutional_data() or {}
    revenue = fetch_monthly_revenue() or {}
    valuation = fetch_valuation() or {}
    ind_map = get_industry_map() or {}

    price_map = get_realtime_stocks_bulk([p["code"] for p in positions])
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

    trend_html = render_trend_chart(get_portfolio_snapshots(uid, days=120))
    realized_html = render_realized_summary(uid, inst)

    body = f"""
{risk_card}
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
{alert_card}
</div>

<div class="section-head"><h2>組合走勢</h2>
  <span class="section-note">相對起始日漲跌幅</span></div>
<div class="callout" style="padding:14px 15px 4px">{trend_html}</div>

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

{realized_html}
<div class="section-head" id="alerts"><h2>值得注意</h2>
  <span class="section-note"><a href="/web/settings" style="color:var(--ink-soft)">調整門檻 →</a></span></div>
<div class="rows">
{''.join(f'<div class="alert"><span class="tag">{tag}</span><span>{txt}</span></div>'
         for tag, txt in alerts) if alerts
 else '<div class="empty">目前沒有觸及門檻的項目。</div>'}
</div>"""
    return respond_page("組合分析", body, "portfolio")


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


def compute_screener_rows(mode):
    """
    算出某個模式的完整候選清單。回傳 (rows, 因流動性被排除的檔數, 產業動能)。
    結果快取 5 分鐘，讓調整篩選條件變成瞬間反應。
    """
    now = time.time()
    hit = _screener_cache.get(mode)
    if hit and now - hit["at"] < SCREENER_CACHE_SECONDS:
        return hit["rows"], hit["skipped"], hit["momentum"]

    inst = fetch_institutional_data() or {}
    revenue = fetch_monthly_revenue() or {}
    valuation = fetch_valuation() or {}
    ind_map = get_industry_map() or {}
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
        })

    _screener_cache[mode] = {"at": now, "rows": rows,
                             "skipped": skipped_liquidity, "momentum": momentum}
    return rows, skipped_liquidity, momentum


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
                  "候選池涵蓋電子、傳產、金融三類，需要逐檔取得報價與量能。"))

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
    rows, skipped_liquidity, momentum = compute_screener_rows(mode)
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
        那是型態問題不是估值問題；硬給一個總分只會跟黑馬混淆，
        而且分數在幾十檔的範圍內拉不開差距，等於噪音。
        """
        badge = (f'<span class="badge">{r["breakout"]}</span>'
                 if r["breakout"] else '<span class="badge muted">區間內</span>')
        vr = r.get("vol_ratio")
        vol_txt = f"{vr:.1f} 倍" if vr else "—"
        vol_cls = "hot" if vr and vr >= 2 else ("warm" if vr and vr >= 1.5 else "")
        streak_txt = f"{r['streak']} 日" if r["streak"] else "—"
        return f"""
<div class="row">
  <div><span class="name">{r['name']}</span><span class="code">{r['code']}</span>{badge}</div>
  <div class="price">{fmt_pct(r['pct'])}</div>
  <div class="meta">
    <span><em>價</em> <span class="num">{r['close']:,.2f}</span></span>
    <span><em>量能</em> <span class="num {vol_cls}">{vol_txt}</span>（20日均量）</span>
    <span><em>距高點</em> <span class="num">{f"{r['pos']:+.1f}%" if r['pos'] is not None else '—'}</span></span>
    <span><em>連買</em> {streak_txt}</span>
    <span><em>金額</em> <span class="num">{r['turnover']:.1f}</span> 億</span>
    <span><em>產業</em> {r['industry']}</span>
  </div>
</div>"""

    CAT_TAG = {"電子": "電", "傳產": "傳", "金融": "金"}

    def stock_row(r):
        cat_tag = (f'<span class="cat cat-{r["category"]}">'
                   f'{CAT_TAG.get(r["category"], "")}</span>')
        badge = (f'<span class="badge">{r["breakout"]}</span>'
                 if r["breakout"] else "")
        if r["score"] is None:
            # 金融股不評分，只列事實
            return f"""
<div class="row">
  <div>{cat_tag}<span class="name">{r['name']}</span><span class="code">{r['code']}</span>{badge}</div>
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
        extra = (f'<span><em>PEG</em> {r["peg"]:.2f}</span>' if r["peg"]
                 else (f'<span><em>PB</em> {r["pb"]:.2f}</span>' if r["pb"] else ""))
        return f"""
<div class="row">
  <div>{cat_tag}<span class="name">{r['name']}</span><span class="code">{r['code']}</span>{badge}</div>
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
        "━━━━━━━━━━━━\n"
        "⚠️ 請先了解這件事\n\n"
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
        "看得懂數字背後的意思再做決定，"
        "不要因為看到一個分數就進場。\n"
        "━━━━━━━━━━━━\n\n"
        "下面是可用的功能，直接點按鈕就能執行。\n"
        "隨時輸入「選單」都能再叫出來。\n\n"
        "⏳ 小提醒\n"
        "每個指令都會即時去抓最新的行情、法人與財務資料，"
        "大約需要 10-20 秒才會回覆。送出後請稍等一下，"
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

    # 每則訊息都先叫出 LINE 官方的載入動畫（聊天室裡的三點跳動）。
    # 不分指令輕重都叫，是因為使用者無法預期哪個指令會慢——
    # 有些看似簡單的查詢遇到快取失效時一樣要跑十幾秒，
    # 沒有動畫時那段安靜會讓人以為訊息沒送出而重複點擊。
    # 這支 API 不計入每月推播額度，多叫幾次沒有成本。
    start_loading_animation(user_id)

    # 0. 管理指令（只有 ADMIN_USER_ID 本人可用，其他人輸入等同無效指令）
    if text in ["我的ID", "我的id", "MYID"]:
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
            set_notify(target[0], turn_on)
            if turn_on:
                set_requested(target[0], False)
            reply = (f"{'🔔 已開通' if turn_on else '🔕 已停用'}：{target[1]}\n\n"
                     + build_user_list_report())
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
        reply = build_morning_push(user_id)

    # 7.5 盤後解盤（使用者手動輸入才觸發，不自動推播）
    elif text in ["解盤", "盤後解盤", "盤後"]:
        reply = build_market_recap()

    # 7.65 籌碼超人：把三大法人拆開看誰在認養、誰在撤退
    elif text in ["籌碼", "籌碼超人", "認養"]:
        reply = build_chips_report()

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
            cand_prices = get_realtime_stocks_bulk(
                [c for c, _ in candidates], workers=16)

            scored = []
            for code, info in candidates:
                price = cand_prices.get(code)
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
                # 「高度看好」這種措辭會被當成推薦，但這只是一組數字排序的結果。
                # 改成描述「這個分數在評分標準裡的位置」，而不是對股票下判斷。
                grade = ("🔥 各項指標均強" if total_score >= 75
                         else ("🚀 多數指標偏強" if total_score >= 55
                               else "📈 指標中性偏強"))
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
                    f"【評分結果】\n"
                    f"{grade}\n"
                    f"-----------------------------------"
                )
                reports.append(report)
            if reports:
                reply = "\n\n".join(reports) + (
                    "\n\n※ 以上為依公開資料排序的結果，不是推薦。\n"
                    "分數高只代表這幾項指標數字好看，"
                    "不代表會漲、也不代表適合你的狀況。")
            else:
                reply = "❌ 暫無符合條件的標的。"

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
            radar_prices = get_realtime_stocks_bulk(
                [c for c, _ in candidates[:60]], workers=16)
            for code, info in candidates[:60]:
                price = radar_prices.get(code)
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
            if reports:
                reply = "\n\n".join(reports) + (
                    "\n\n※ 以上為當日帶量上漲且法人買超的標的，不是推薦。\n"
                    "短線強勢不代表會續強，追高風險自負。")
            else:
                reply = "❌ 暫無符合條件的標的。"

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
