import os
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

# --- Supabase 資料庫連線 ---
def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    url = urlparse(db_url)
    conn = psycopg2.connect(
        database=url.path[1:],
        user=url.username,
        password=url.password,
        host=url.hostname,
        port=url.port,
        sslmode='require'
    )
    return conn

# 初始化表格
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS watchlists (user_id TEXT, code TEXT, PRIMARY KEY (user_id, code))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS alerts (user_id TEXT, code TEXT, price REAL, PRIMARY KEY (user_id, code))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY)''')
    conn.commit()
    cursor.close()
    conn.close()

init_db()

# --- 資料庫操作函式 ---
def add_user_to_db(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()

def get_user_watchlist(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM watchlists WHERE user_id = %s", (user_id,))
    codes = [row[0] for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return codes

def add_watchlist_db(user_id, code):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO watchlists (user_id, code) VALUES (%s, %s) ON CONFLICT (user_id, code) DO NOTHING", (user_id, code))
    conn.commit()
    cursor.close()
    conn.close()

def remove_watchlist_db(user_id, code):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watchlists WHERE user_id = %s AND code = %s", (user_id, code))
    cursor.execute("DELETE FROM alerts WHERE user_id = %s AND code = %s", (user_id, code))
    conn.commit()
    cursor.close()
    conn.close()

def get_user_alerts(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT code, price FROM alerts WHERE user_id = %s", (user_id,))
    alerts = {row[0]: row[1] for row in cursor.fetchall()}
    cursor.close()
    conn.close()
    return alerts

def set_alert_db(user_id, code, price):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO alerts (user_id, code, price) VALUES (%s, %s, %s) ON CONFLICT (user_id, code) DO UPDATE SET price = EXCLUDED.price", (user_id, code, price))
    conn.commit()
    cursor.close()
    conn.close()

# --- 股票行情與回覆 ---
def get_realtime_stock(code):
    headers = {'User-Agent': 'Mozilla/5.0'}
    for sym in [f"{code}.TW", f"{code}.TWO"]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
            res = requests.get(url, headers=headers, timeout=5).json()
            result = result = res['chart']['result'][0]
            close = float(result['indicators']['quote'][0]['close'][-1])
            prev_close = float(result['meta']['previousClose'])
            return {"close": close, "pct": ((close - prev_close) / prev_close) * 100, "high": float(result['meta']['regularMarketDayHigh']), "low": float(result['meta']['regularMarketDayLow']), "volume": int(result['meta']['regularMarketVolume'])}
        except: continue
    return None

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()
    pure_code = "".join(filter(str.isdigit, text))
    add_user_to_db(user_id)

    if "加" in text and 4 <= len(pure_code) <= 6:
        add_watchlist_db(user_id, pure_code)
        reply = f"✅ 已成功將 {pure_code} 加入您的雲端自選股！"
    elif "刪" in text and 4 <= len(pure_code) <= 6:
        remove_watchlist_db(user_id, pure_code)
        reply = f"🗑️ 已從自選清單移除 {pure_code}"
    elif text == "自選":
        codes = get_user_watchlist(user_id)
        if not codes: reply = "📂 目前清單是空的，輸入「加 股票代號」即可新增。"
        else:
            reply = "📂 【我的雲端自選股】\n==================="
            for code in codes:
                data = get_realtime_stock(code)
                reply += f"\n• {code}：{data['close']:.2f} ({data['pct']:+.2f}%)" if data else f"\n• {code}：更新中"
    else:
        reply = "🤖 蔡秉軒御用機器人\n使用方式：\n• 加 2330\n• 刪 2330\n• 自選"
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
