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
import random

app = Flask(__name__)
line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

# --- 股票代號中文化對照表 (確保顯示繁體中文) ---
STOCK_NAME_MAP = {
    "3081": "聯亞", "4931": "新日興", "6442": "光聖", "2330": "台積電",
    "2454": "聯發科", "3661": "世芯-KY", "6669": "緯穎", "3037": "欣興",
    "2382": "廣達", "3231": "緯創"
}

@app.route("/", methods=["GET"])
def home():
    return "Bot is alive!", 200

def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    url = urlparse(db_url)
    # 移除 ipv4 轉譯，直接使用原生 URL
    conn = psycopg2.connect(
        database=url.path[1:],
        user=url.username,
        password=url.password,
        host=url.hostname,
        port=url.port,
        sslmode='require'
    )
    return conn

# --- 優化後的資料庫寫入 ---
def add_watchlist_db(user_id, code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # 修正：確保寫入邏輯不受衝突影響
        cursor.execute(
            "INSERT INTO watchlists (user_id, code) VALUES (%s, %s) ON CONFLICT (user_id, code) DO NOTHING",
            (str(user_id), str(code))
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"DEBUG_DB_ERROR: {e}")
        return False

def get_user_watchlist(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT code FROM watchlists WHERE user_id = %s", (str(user_id),))
        codes = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return codes
    except: return []

# --- 強化版 API：強制中文化 ---
def get_realtime_stock(code):
    code = str(code).strip()
    name = STOCK_NAME_MAP.get(code, code) # 優先使用自定義名稱
    
    for suffix in [".TW", ".TWO"]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}?range=1d&interval=1d"
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()
            meta = res['chart']['result'][0]['meta']
            close = meta.get('regularMarketPrice', 0.0)
            if close > 0:
                prev = meta.get('chartPreviousClose', close)
                return {
                    "code": code, "name": name, "close": close, 
                    "pct": ((close - prev) / prev) * 100,
                    "high": meta.get('regularMarketDayHigh', close),
                    "low": meta.get('regularMarketDayLow', close),
                    "volume": meta.get('regularMarketVolume', 0),
                    "support": round(close * 0.98, 2), "resistance": round(close * 1.02, 2)
                }
        except: continue
    return None

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except: abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    pure_code = "".join(filter(str.isdigit, text))

    if "加" in text and len(pure_code) >= 4:
        if add_watchlist_db(user_id, pure_code):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 已加入自選：{pure_code} {STOCK_NAME_MAP.get(pure_code, '')}"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ 資料庫寫入異常，請稍後再試。"))
    
    elif text == "自選":
        codes = get_user_watchlist(user_id)
        if not codes:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📂 清單為空，輸入「加 2330」新增。"))
        else:
            msg = "📂 我的自選股：\n"
            for c in codes:
                data = get_realtime_stock(c)
                name = data['name'] if data else c
                price = f"{data['close']:.2f}" if data else "查無"
                msg += f"• {c} {name}: {price}\n"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

    elif text == "雷達":
        # 修正：強制顯示名稱
        data = get_realtime_stock("2330") # 範例
        reply = f"🚨【盤中雷達】\n🔥 股票：{data['name']}\n💰 現價：{data['close']:.2f}\n📈 漲幅：{data['pct']:+.2f}%\n📊 成交量：{int(data['volume']/1000):,}張\n\n【強勢原因】\n• 帶量突破短期均線，買盤強勁。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

    elif len(pure_code) >= 4:
        data = get_realtime_stock(pure_code)
        if data:
            reply = f"📊 {data['code']} {data['name']}\n💰 現價：{data['close']:.2f} ({data['pct']:+.2f}%)\n🛡️ 支撐：{data['support']} | 🚧 壓力：{data['resistance']}"
        else:
            reply = "❌ 找不到資料"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
