import os
import socket
import requests
import psycopg2
from urllib.parse import urlparse
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

# --- GitHub 遠端黑馬與雷達清單設定 ---
GITHUB_BLACK_HORSE_URL = "https://raw.githubusercontent.com/你的帳號/你的專案/main/black_horse.json"
GITHUB_RADAR_URL = "https://raw.githubusercontent.com/你的帳號/你的專案/main/radar_data.json"

def fetch_latest_black_horses():
    try:
        response = requests.get(GITHUB_BLACK_HORSE_URL, timeout=5)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"讀取黑馬清單失敗: {e}")
    return None

def fetch_latest_radars():
    try:
        response = requests.get(GITHUB_RADAR_URL, timeout=5)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"讀取雷達清單失敗: {e}")
    return None

# --- Supabase 資料庫連線 (強制轉 IPv4，解決 Render 網路限制) ---
def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    url = urlparse(db_url)
    ipv4_addr = socket.gethostbyname(url.hostname)
    
    conn = psycopg2.connect(
        database=url.path[1:],
        user=url.username,
        password=url.password,
        host=ipv4_addr,
        port=url.port,
        sslmode='require'
    )
    return conn

# 初始化表格
def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS watchlists (user_id TEXT, code TEXT, PRIMARY KEY (user_id, code))''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS alerts (user_id TEXT, code TEXT, price REAL, PRIMARY KEY (user_id, code))''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY)''')
        conn.commit()
        cursor.close()
        conn.close()
        print("Supabase 資料庫連線與表格初始化成功！")
    except Exception as e:
        print(f"初始化資料庫發生錯誤: {e}")

init_db()

# --- 資料庫操作函式 ---
def add_user_to_db(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"新增使用者失敗: {e}")

def get_user_watchlist(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT code FROM watchlists WHERE user_id = %s", (user_id,))
        codes = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return codes
    except Exception as e:
        print(f"讀取自選清單失敗: {e}")
        return []

def add_watchlist_db(user_id, code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO watchlists (user_id, code) VALUES (%s, %s) ON CONFLICT (user_id, code) DO NOTHING", (user_id, code))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"新增自選失敗: {e}")

def remove_watchlist_db(user_id, code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM watchlists WHERE user_id = %s AND code = %s", (user_id, code))
        cursor.execute("DELETE FROM alerts WHERE user_id = %s AND code = %s", (user_id, code))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"刪除自選失敗: {e}")

def get_user_alerts(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT code, price FROM alerts WHERE user_id = %s", (user_id,))
        alerts = {row[0]: row[1] for row in cursor.fetchall()}
        cursor.close()
        conn.close()
        return alerts
    except Exception:
        return {}

def set_alert_db(user_id, code, price):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO alerts (user_id, code, price) VALUES (%s, %s, %s) ON CONFLICT (user_id, code) DO UPDATE SET price = EXCLUDED.price", (user_id, code, price))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"設定提醒失敗: {e}")

# 備用靜態資料庫
black_horse_database = {
    "3293": {"name": "鈊象", "industry": "網路遊戲 / 軟體", "reason": "營收與 EPS 長期高速成長，獲利強悍，底部整理後隨時準備強勢創高"},
    "3661": {"name": "世芯-KY", "industry": "ASIC / IP", "reason": "AI 晶片設計委託需求爆發，營收成長動能強勁，底部打底完成"},
    "3529": {"name": "力旺", "industry": "矽智財 (IP)", "reason": "權利金收入持續攀高，毛利率極高，低基期蓄勢待發"},
    "6669": {"name": "緯穎", "industry": "AI 伺服器", "reason": "美系雲端服務商 (CSP) 訂單滿手，營收爆發力十足，整理後準備發動"},
    "3443": {"name": "創意", "industry": "ASIC / 晶圓代工服務", "reason": "先進封裝與 AI 專案陸續進入量產，底部籌碼沉澱完畢"},
}

radar_database = {
    "2454": {"name": "聯發科", "industry": "IC 設計", "tag": "🚀 帶量突破月線"},
    "2317": {"name": "鴻海", "industry": "AI 伺服器代工", "tag": "📊 量能增溫強勢多頭"},
    "2382": {"name": "廣達", "industry": "AI 伺服器", "tag": "🔥 爆量長紅突破"},
    "3231": {"name": "緯創", "industry": "AI 伺服器基板", "tag": "⚡ 短線量縮回測強撐"},
    "1503": {"name": "士電", "industry": "重電機電", "tag": "🚀 量價齊揚突破箱型"},
}

def get_realtime_stock(code):
    headers = {'User-Agent': 'Mozilla/5.0'}
    for sym in [f"{code}.TW", f"{code}.TWO"]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
            res = requests.get(url, headers=headers, timeout=5).json()
            result = res['chart']['result'][0]
            close = float(result['indicators']['quote'][0]['close'][-1])
            prev_close = float(result['meta']['previousClose'])
            closes = [c for c in result['indicators']['quote'][0].get('close', []) if c is not None]
            ma20 = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 20 else close
            return {
                "close": close, 
                "pct": ((close - prev_close) / prev_close) * 100, 
                "high": float(result['meta'].get('regularMarketDayHigh', close)), 
                "low": float(result['meta'].get('regularMarketDayLow', close)), 
                "volume": int(result['meta'].get('regularMarketVolume', 0)),
                "ma20": ma20
            }
        except: 
            continue
    return None

def get_us_stock_pct(symbol):
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
        res = requests.get(url, headers=headers, timeout=4).json()
        closes = [c for c in res['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
        if len(closes) >= 2:
            return ((closes[-1] - closes[-2]) / closes[-2]) * 100
    except:
        pass
    return 0.0

def generate_morning_brief():
    dji = get_us_stock_pct("^DJI")
    sox = get_us_stock_pct("^SOX")
    nvda = get_us_stock_pct("NVDA")
    tsm = get_us_stock_pct("TSM")
    today_str = datetime.now().strftime("%Y/%m/%d")
    return (
        f"☀️ 【台股盤前與總經動態】\n📅 日期：{today_str}\n"
        f"-------------------\n"
        f"• 道瓊指數：{dji:+.2f}%\n"
        f"• 費城半導體：{sox:+.2f}%\n"
        f"• 輝達 (NVDA)：{nvda:+.2f}%\n"
        f"• 台積電ADR (TSM)：{tsm:+.2f}%"
    )

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

    user_watchlist = get_user_watchlist(user_id)
    user_alerts = get_user_alerts(user_id)

    if "加" in text and 4 <= len(pure_code) <= 6:
        if pure_code not in user_watchlist:
            add_watchlist_db(user_id, pure_code)
            reply = f"✅ 新增自選成功：{pure_code}"
        else:
            reply = f"📌 {pure_code} 已經在您的自選清單中囉！"

    elif "刪" in text and 4 <= len(pure_code) <= 6:
        if pure_code in user_watchlist:
            remove_watchlist_db(user_id, pure_code)
            reply = f"🗑️ 已從自選清單移除：{pure_code}"
        else:
            reply = f"❌ 找不到代號 {pure_code}"

    elif ("設" in text) or (" " in text and len(pure_code) >= 7):
        clean_text = text.replace("設定", "").replace("設", "").strip()
        parts = clean_text.split()
        target_code, target_price = None, None
        for p in parts:
            p_digits = "".join(filter(str.isdigit, p))
            if 4 <= len(p_digits) <= 6 and not target_code:
                target_code = p_digits
            else:
                try:
                    val = float(p)
                    if val > 10:
                        target_price = val
                except ValueError:
                    pass

        if target_code and target_price:
            if target_code not in user_watchlist:
                add_watchlist_db(user_id, target_code)
            set_alert_db(user_id, target_code, target_price)
            reply = f"🔔 到價通知設定成功！\n• 代號：{target_code}\n• 目標價：{target_price}"
        else:
            reply = "❌ 格式錯誤！正確範例：設 00981 15"

    elif text in ["自選", "WATCHLIST"]:
        if not user_watchlist:
            reply = "📂 目前自選清單是空的。\n💡 輸入「加 2330」即可新增！"
        else:
            results = ["📂 【我的雲端自選股與策略】\n==================="]
            for code in user_watchlist:
                data = get_realtime_stock(code)
                alert_p = user_alerts.get(code)
                if data:
                    close, pct, ma20 = data['close'], data['pct'], data['ma20']
                    light = "🔴" if pct >= 0 else "🟢"
                    strategy = "🔥【多方續強】帶量上攻，沿 5 日線續抱。" if close > ma20 and pct > 0 else "⚡【回測月線】多頭拉回，守穩支撐。"
                    block = f"\n{light} 【{code}】 現價：{close:.2f} ({pct:+.2f}%)\n📋 策略：{strategy}"
                    if alert_p:
                        block += f"\n🔔 目標價：{alert_p}"
                    results.append(block)
                else:
                    results.append(f"\n⚪ 【{code}】 讀取中...")
            reply = "\n".join(results)

    elif 4 <= len(pure_code) <= 6 and len(text) <= 7 and " " not in text:
        data = get_realtime_stock(pure_code)
        if data:
            name, industry = "上市櫃個股/ETF", "一般個股"
            if pure_code in black_horse_database:
                name, industry = black_horse_database[pure_code]["name"], black_horse_database[pure_code]["industry"]
            elif pure_code in radar_database:
                name, industry = radar_database[pure_code]["name"], radar_database[pure_code]["industry"]
            reply = (
                f"📊 {pure_code} {name} ({industry})\n"
                f"===================\n"
                f"💰 現價：{data['close']:.2f} ({data['pct']:+.2f}%)\n"
                f"🔺 高/低：{data['high']:.2f} / {data['low']:.2f}\n"
                f"📦 量能：{int(data['volume'] / 1000):,} 張"
            )
        else:
            reply = f"❌ 查無代號 {pure_code} 的行情。"

    elif text_upper in ["MENU", "選單", "幫助", "HELP"]:
        reply = (
            "🤖 蔡秉軒御用選股機器人\n"
            "===================\n"
            "• 輸入「盤前」➜ 美股與總經速覽\n"
            "• 輸入「黑馬」➜ 高潛力成長股\n"
            "• 輸入「雷達」➜ 全市場強勢突破\n"
            "• 輸入「自選」➜ 查看雲端自選股\n"
            "• 輸入「加 2330」➜ 新增自選\n"
            "• 輸入「刪 2330」➜ 刪除自選\n"
            "• 輸入「設 2330 600」➜ 設定到價"
        )
    elif text in ["盤前", "早安"]:
        reply = generate_morning_brief()
    elif text == "雷達":
        remote_radar = fetch_latest_radars()
        results = []
        if remote_radar and "stocks" in remote_radar:
            for s in remote_radar["stocks"]:
                d = get_realtime_stock(s.get('code'))
                p_str = f"現價 {d['close']:.1f} ({d['pct']:+.2f}%)" if d else "更新中"
                results.append(f"• {s.get('code')} {s.get('name')} | {p_str}\n  └ {s.get('tag', '突破')}")
            reply = f"🎯 強勢突破雷達 ({remote_radar.get('update_time', '')})\n-------------------\n" + "\n\n".join(results[:6])
        else:
            reply = "🎯 雷達目前連線中..."
    elif text == "黑馬":
        remote_data = fetch_latest_black_horses()
        results = []
        if remote_data and "stocks" in remote_data:
            for s in remote_data["stocks"]:
                d = get_realtime_stock(s.get('code'))
                p_str = f"現價 {d['close']:.1f} ({d['pct']:+.2f}%)" if d else "更新中"
                results.append(f"• {s.get('code')} {s.get('name')} | {p_str}\n  └ {s.get('reason')}")
            reply = f"🔥 潛力黑馬專區 ({remote_data.get('update_time', '')})\n-------------------\n" + "\n\n".join(results)
        else:
            reply = "🔥 黑馬專區目前連線中..."
    else:
        reply = "❌ 指令錯誤！請輸入「選單」查看所有功能。"
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
