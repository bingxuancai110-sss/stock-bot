import os
import socket
import requests
import psycopg2
from psycopg2 import pool
from urllib.parse import urlparse
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime, timedelta
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
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
            headers = {'User-Agent': 'Mozilla/5.0'}
            res = requests.get(url, headers=headers, timeout=5).json()

            result_meta = res.get('chart', {}).get('result', [])
            if not result_meta:
                continue

            meta = result_meta[0].get('meta', {})
            close = meta.get('regularMarketPrice', 0.0)
            prev_close = meta.get('chartPreviousClose', close)

            if not close or close == 0:
                indicators = result_meta[0].get('indicators', {}).get('quote', [{}])[0]
                closes = [c for c in indicators.get('close', []) if c is not None]
                if closes:
                    close = closes[-1]
                    prev_close = closes[-2] if len(closes) >= 2 else close

            if not close or close == 0:
                continue

            pct = ((close - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
            high = meta.get('regularMarketDayHigh', close) or close
            low = meta.get('regularMarketDayLow', close) or close
            volume = meta.get('regularMarketVolume', 0) or 0

            resistance = round(high * 1.01, 2)
            support = round(low * 0.99, 2)

            return {
                "code": code,
                "name": stock_name,
                "close": float(close),
                "pct": float(pct),
                "high": float(high),
                "low": float(low),
                "volume": int(volume),
                "resistance": resistance,
                "support": support
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

def build_technical_desc(pct, volume_lots, net_lots):
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

    if volume_lots >= 10000:
        parts.append(f"・成交量達 {volume_lots:,} 張，量能爆發")
    elif volume_lots >= 3000:
        parts.append(f"・成交量 {volume_lots:,} 張，量能明顯放大")
    else:
        parts.append(f"・成交量 {volume_lots:,} 張")

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

# --- 自選股健檢：把 watchlist 全部跑一次評分，依總分排序 ---
def build_healthcheck_report(user_id):
    codes = get_user_watchlist(user_id)
    if not codes:
        return "📂 自選股清單是空的\n輸入「加 3081」新增自選"

    institutional_data = fetch_institutional_data()
    rows = []  # (total_score, display_text)

    for code in codes:
        stock = get_realtime_stock(code)
        if not stock:
            rows.append((-1, f"⚪ {code} 查無行情"))
            continue

        inst = institutional_data.get(code, {})
        net_lots = inst.get("total_net_lots", 0)

        chip_score = score_from_net_lots(net_lots)
        turnover = calc_turnover_billion(stock["close"], stock["volume"])
        tech_score = score_from_technical(stock["pct"], turnover)
        total_score = chip_score + tech_score

        flag = "🟢" if total_score >= 70 else ("🟡" if total_score >= 40 else "🔴")
        name = inst.get("name") or stock["name"]

        text = (
            f"{flag} {name} {code}\n"
            f"{stock['close']:.2f}（{stock['pct']:+.2f}%）"
            f"　法人{net_lots:+,}張　{total_score}分"
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

    # 8. 黑馬股（真實三大法人買超排行 + 真實技術面，依總分排序）
    elif text == "黑馬":
        inst_data = fetch_institutional_data()
        if not inst_data:
            reply = "❌ 目前無法取得三大法人資料，可能是非交易時段或非交易日，請稍後再試。"
        else:
            candidates = [
                (code, info) for code, info in inst_data.items()
                if len(code) == 4 and code.isdigit()
                and not code.startswith("00")  # 排除 ETF（0050、0056...）
                and info["total_net_lots"] > 0
            ]
            candidates.sort(key=lambda x: x[1]["total_net_lots"], reverse=True)

            scored = []
            for code, info in candidates[:30]:
                price = get_realtime_stock(code)
                if not price:
                    continue
                if price["close"] < 10:  # 排除低價股，容易被小額資金拉出失真漲幅
                    continue
                turnover = calc_turnover_billion(price["close"], price["volume"])
                if turnover < 1:  # 排除成交金額 <1億元，流動性不足
                    continue
                chip_score = score_from_net_lots(info["total_net_lots"])
                tech_score = score_from_technical(price["pct"], turnover)
                total_score = chip_score + tech_score
                scored.append((total_score, code, info, price, chip_score, tech_score))

            scored.sort(key=lambda x: x[0], reverse=True)  # 依綜合總分排序，不是純法人買超排名

            reports = []
            for rank, (total_score, code, info, price, chip_score, tech_score) in enumerate(scored[:3], start=1):
                grade = "🔥 超強黑馬" if total_score >= 80 else ("🚀 強勢黑馬" if total_score >= 60 else "📈 潛力股")
                report = (
                    f"🐎 智慧黑馬股 #{rank}\n\n"
                    f"股票：{info['name']}\n"
                    f"代號：{code}\n\n"
                    f"黑馬指數：{total_score}／100\n\n"
                    f"🏦 籌碼面：{chip_score}／40（三大法人買超 {info['total_net_lots']:,} 張）\n"
                    f"📈 技術面：{tech_score}／60\n\n"
                    f"【即時數據】\n"
                    f"{build_technical_desc(price['pct'], price['volume']//1000, info['total_net_lots'])}\n\n"
                    f"【風險】\n"
                    f"{build_risk_desc(price['pct'], info['total_net_lots'])}\n\n"
                    f"【黑馬判定】\n"
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
            for code, info in candidates[:30]:
                price = get_realtime_stock(code)
                if not price:
                    continue
                if price["close"] < 10:  # 排除低價股
                    continue
                turnover = calc_turnover_billion(price["close"], price["volume"])
                if turnover < 1:  # 排除成交金額 <1億元
                    continue
                priced.append((code, info, price))
            priced.sort(key=lambda x: x[2]["pct"], reverse=True)

            reports = []
            for code, info, price in priced[:3]:
                turnover = calc_turnover_billion(price["close"], price["volume"])
                r_score = min(100, 60 + score_from_technical(price["pct"], turnover))
                level = "S級 | 極強攻擊" if price["pct"] >= 2.0 else "A級 | 穩健突破"
                report = (
                    f"🚨【盤中雷達】\n\n"
                    f"🔥 強勢股票：{info['name']}\n"
                    f"📌 股票代號：{code}\n\n"
                    f"💰 現價：{price['close']:.2f}\n"
                    f"📈 漲幅：{price['pct']:+.2f}%\n"
                    f"📊 成交量：{int(price['volume']/1000):,}張\n"
                    f"🏦 三大法人買超：{info['total_net_lots']:,} 張\n\n"
                    f"📡 雷達分數：{r_score}／100\n"
                    f"🏆 等級：{level}\n\n"
                    f"【型態】\n"
                    f"{build_technical_desc(price['pct'], price['volume']//1000, info['total_net_lots'])}\n\n"
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
            "• 輸入「黑馬」➜ 法人買超前三強評分\n"
            "• 輸入「雷達」➜ 法人買超中漲幅最強前三\n\n"
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
