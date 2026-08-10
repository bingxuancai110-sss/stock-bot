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

# --- Supabase 資料庫連線 (強制轉 IPv4) ---
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

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS watchlists (user_id TEXT, code TEXT, PRIMARY KEY (user_id, code))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY)''')
    conn.commit()
    cursor.close()
    conn.close()
    print("✅ 資料庫初始化成功！")

init_db()

# --- 資料庫操作 (拿掉會吞掉錯誤的 try-except，讓錯誤直接現形) ---
def add_user_to_db(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (str(user_id).strip(),))
    conn.commit()
    cursor.close()
    conn.close()

def get_user_watchlist(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM watchlists WHERE TRIM(user_id) = %s", (str(user_id).strip(),))
    codes = [row[0] for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return codes

def add_watchlist_db(user_id, code):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO watchlists (user_id, code) VALUES (%s, %s) ON CONFLICT (user_id, code) DO NOTHING", (str(user_id).strip(), code))
    conn.commit()
    cursor.close()
    conn.close()

def remove_watchlist_db(user_id, code):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watchlists WHERE TRIM(user_id) = %s AND code = %s", (str(user_id).strip(), code))
    conn.commit()
    cursor.close()
    conn.close()

# --- 行情獲取 ---
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
        except: continue
    return None

# --- 主程式 ---
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
    
    try:
        add_user_to_db(user_id)

        if "加" in text and 4 <= len(pure_code) <= 6:
            add_watchlist_db(user_id, pure_code)
            reply = f"✅ 已新增自選：{pure_code}"
        elif "刪" in text and 4 <= len(pure_code) <= 6:
            remove_watchlist_db(user_id, pure_code)
            reply = f"🗑️ 已移除自選：{pure_code}"
        elif text == "自選":
            codes = get_user_watchlist(user_id)
            if not codes: 
                reply = "📂 清單為空。"
            else:
                results = ["📂 【我的自選股】"]
                for code in codes:
                    data = get_realtime_stock(code)
                    if data: 
                        results.append(f"• {code}：{data['close']:.2f} ({data['pct']:+.2f}%)")
                    else:
                        results.append(f"• {code}：行情讀取中")
                reply = "\n".join(results)
        elif 4 <= len(pure_code) <= 6 and " " not in text:
            data = get_realtime_stock(pure_code)
            if data: 
                reply = f"📊 {pure_code} 現價：{data['close']:.2f} ({data['pct']:+.2f}%)"
            else: 
                reply = "❌ 查無行情。"
        else:
            reply = "🤖 蔡秉軒御用機器人，功能：加/刪/自選/行情。"
    except Exception as e:
        # 如果資料庫操作出錯，直接把錯誤傳回 LINE 讓我們看看到底發生什麼事
        reply = f"🔥 發生錯誤：{str(e)}"
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
