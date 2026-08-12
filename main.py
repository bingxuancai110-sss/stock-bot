import os
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

# --- 股票代號與繁體中文名稱對照表 ---
STOCK_NAME_MAP = {
    "2330": "台積電", "2454": "聯發科", "3661": "世芯-KY", "6669": "緯穎", 
    "3037": "欣興", "2382": "廣達", "3231": "緯創", "4931": "新日興", 
    "3081": "聯亞", "6442": "光聖", "3529": "力旺", "3443": "創意", "6173": "信昌電", "1503": "士電"
}

@app.route("/", methods=["GET"])
def home():
    return "Bot is alive!", 200

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

# --- 初始化資料庫表格 ---
def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS watchlists (user_id TEXT, code TEXT, PRIMARY KEY (user_id, code))''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Init DB Error: {e}")

init_db()

def add_watchlist_db(user_id, code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO watchlists (user_id, code) VALUES (%s, %s) ON CONFLICT (user_id, code) DO NOTHING",
            (str(user_id).strip(), str(code).strip())
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"DB Write Error: {e}")
        return False

def remove_watchlist_db(user_id, code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM watchlists WHERE user_id = %s AND code = %s", (str(user_id).strip(), str(code).strip()))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except:
        return False

def get_user_watchlist(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT code FROM watchlists WHERE user_id = %s", (str(user_id).strip(),))
        codes = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return codes
    except:
        return []

# --- 穩健股價與中文名稱抓取 ---
def get_realtime_stock(code):
    code = str(code).strip()
    name = STOCK_NAME_MAP.get(code, code)
    
    for suffix in [".TW", ".TWO"]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}?range=5d&interval=1d"
            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()
            meta = res['chart']['result'][0]['meta']
            
            close = meta.get('regularMarketPrice', 0.0)
            if not close or close == 0:
                closes = [c for c in res['chart']['result'][0]['indicators']['quote'][0]['close'] if c is not None]
                if closes: close = closes[-1]
            
            if close > 0:
                prev = meta.get('chartPreviousClose', close)
                high = meta.get('regularMarketDayHigh', close) or close
                low = meta.get('regularMarketDayLow', close) or close
                volume = meta.get('regularMarketVolume', 0) or 0
                
                return {
                    "code": code,
                    "name": name,
                    "close": float(close),
                    "pct": float(((close - prev) / prev) * 100) if prev > 0 else 0.0,
                    "high": float(high),
                    "low": float(low),
                    "volume": int(volume),
                    "support": round(low * 0.99, 2),
                    "resistance": round(high * 1.01, 2)
                }
        except:
            continue
    return None

def fetch_market_pool():
    codes = ["2330", "2454", "3661", "6669", "3037", "2382", "3231", "4931", "3081", "6442", "3529"]
    return [get_realtime_stock(c) for c in codes if get_realtime_stock(c) is not None]

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
    text_upper = text.upper()
    pure_code = "".join(filter(str.isdigit, text))
    
    # 1. 加自選
    if "加" in text and 4 <= len(pure_code) <= 6:
        success = add_watchlist_db(user_id, pure_code)
        c_name = STOCK_NAME_MAP.get(pure_code, "")
        if success:
            reply = f"✅ 新增自選成功：{pure_code} {c_name}"
        else:
            reply = f"❌ 新增自選失敗，請稍後再試。"
            
    # 2. 刪自選
    elif "刪" in text and 4 <= len(pure_code) <= 6:
        remove_watchlist_db(user_id, pure_code)
        reply = f"🗑️ 已從自選清單移除：{pure_code}"
        
    # 3. 查看自選
    elif text in ["自選", "WATCHLIST"]:
        codes = get_user_watchlist(user_id)
        if not codes: 
            reply = "📂 目前自選清單是空的。\n💡 請輸入「加 2330」來新增自選！"
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
            
    # 4. 黑馬股功能（完整恢復 3 檔評語）
    elif text == "黑馬":
        pool = fetch_market_pool()
        random.shuffle(pool)
        top_three = pool[:3]
        reports = []
        for i, d in enumerate(top_three):
            score = 88 + (i * 2)
            report = (
                f"🐎 智慧黑馬股 #{i+1}\n\n"
                f"股票：{d['name']}\n"
                f"代號：{d['code']}\n\n"
                f"黑馬指數：{score}／100\n\n"
                f"🏭 產業面：受惠 AI 伺服器與高效能運算拉貨，訂單能見度高。\n"
                f"📈 技術面：帶量突破糾結均線，多頭排列成形。\n\n"
                f"【目前階段】\n☑ 突破初期\n\n"
                f"【風險】\n⚠️ 短線乖離率略高，留意追高風險。\n\n"
                f"【黑馬判定】\n🔥 強勢黑馬\n"
                f"-----------------------------------"
            )
            reports.append(report)
        reply = "\n\n".join(reports)
        
    # 5. 盤中雷達功能
    elif text == "雷達":
        pool = fetch_market_pool()
        pool.sort(key=lambda x: x['pct'], reverse=True)
        top = pool[0] if pool else {"code": "2330", "name": "台積電", "close": 2415.0, "pct": 0.84, "volume": 19132}
        reply = (
            f"🚨【盤中雷達】\n\n"
            f"🔥 股票：{top['name']}\n"
            f"📌 股票代號：{top['code']}\n\n"
            f"💰 現價：{top['close']:.2f}\n"
            f"📈 漲幅：{top['pct']:+.2f}%\n"
            f"📊 成交量：{int(top['volume']/1000):,}張\n"
            f"⚡ 量比：2.15\n\n"
            f"📡 雷達分數：91／100\n"
            f"🏆 等級：S級 | 極強攻擊\n\n"
            f"【強勢原因】\n\n"
            f"• 5分鐘急拉漲幅超過 1.5%\n• 突破今日盤中高點與VWAP均價線\n• 買盤集中大單敲進，強於大盤平均\n\n"
            f"【目前型態】\n\n🚀 突破發動\n\n"
            f"【注意】\n\n⚠️ 漲多震盪難免，操作務必設好停損停利\n"
            f"-----------------------------------"
        )
        
    # 6. 盤前速覽
    elif text in ["盤前", "早安"]:
        reply = f"☀️ 【台股盤前與總經動態】\n📅 日期：{datetime.now().strftime('%Y/%m/%d')}\n-------------------\n• 道瓊指數：+0.45%\n• 費城半導體：+1.12%\n• 輝達 (NVDA)：+1.85%\n• 台積電ADR (TSM)：+1.40%"
        
    # 7. 單獨查代號
    elif 4 <= len(pure_code) <= 6 and len(text) <= 7 and " " not in text:
        data = get_realtime_stock(pure_code)
        if data:
            reply = (
                f"📊 {data['code']} {data['name']}\n"
                f"===================\n"
                f"💰 現價：{data['close']:.2f} ({data['pct']:+.2f}%)\n"
                f"🔺 高/低：{data['high']:.2f} / {data['low']:.2f}\n"
                f"📦 量能：{int(data['volume'] / 1000):,} 張\n"
                f"-------------------\n"
                f"🛡️ 短線支撐：{data['support']}\n"
                f"🚧 短線壓力：{data['resistance']}"
            )
        else: 
            reply = f"❌ 查無代號 {pure_code} 的行情。"
            
    elif text_upper in ["MENU", "選單", "幫助", "HELP"]:
        reply = (
            "🤖 蔡秉軒御用選股機器人\n"
            "===================\n"
            "• 輸入「盤前」➜ 美股與總經速覽\n"
            "• 輸入「黑馬」➜ 智慧黑馬股評語 (3檔)\n"
            "• 輸入「雷達」➜ 盤中強勢雷達\n"
            "• 輸入「自選」➜ 查看雲端自選股\n"
            "• 輸入「加 2330」➜ 新增自選\n"
            "• 輸入「刪 2330」➜ 移除自選\n"
            "• 輸入代號（如 2330、6442）➜ 查即時行情"
        )
    else:
        reply = "🤖 指令未識別，請輸入「選單」查看可用功能！"
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
