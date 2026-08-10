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

# --- Supabase 資料庫連線 ---
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
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS watchlists (user_id TEXT, code TEXT, PRIMARY KEY (user_id, code))''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY)''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"❌ 初始化資料庫錯誤: {e}")

init_db()

# --- 資料庫輔助函式 ---
def add_user_to_db(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (str(user_id).strip(),))
        conn.commit()
        cursor.close()
        conn.close()
    except: pass

def get_user_watchlist(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT code FROM watchlists WHERE user_id = %s", (str(user_id).strip(),))
        codes = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return codes
    except: return []

def add_watchlist_db(user_id, code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO watchlists (user_id, code) VALUES (%s, %s) ON CONFLICT (user_id, code) DO NOTHING", (str(user_id).strip(), str(code).strip()))
        conn.commit()
        cursor.close()
        conn.close()
    except: pass

def remove_watchlist_db(user_id, code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM watchlists WHERE user_id = %s AND code = %s", (str(user_id).strip(), str(code).strip()))
        conn.commit()
        cursor.close()
        conn.close()
    except: pass

# --- 資料庫字典 ---
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

# --- 改用證交所官方 API 抓取台股即時行情 ---
def get_realtime_stock(code):
    try:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{code}.tw&_={int(datetime.now().timestamp() * 1000)}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=5).json()
        
        if "msgArray" in res and len(res["msgArray"]) > 0:
            data = res["msgArray"][0]
            if data.get("z") == "-":  # 如果剛好沒成交價，抓最近買進價
                close = float(data.get("b", "0").split("_")[0]) if data.get("b") != "" else 0.0
            else:
                close = float(data.get("z", "0"))
            
            y_price = float(data.get("y", "0"))
            pct = ((close - y_price) / y_price) * 100 if y_price > 0 else 0.0
            high = float(data.get("h", "0")) if data.get("h") != "" else close
            low = float(data.get("l", "0")) if data.get("l") != "" else close
            volume = int(data.get("v", "0")) * 1000 if data.get("v", "") != "" else 0
            
            return {"close": close, "pct": pct, "high": high, "low": low, "volume": volume, "ma20": close}
    except Exception as e:
        print(f"TWSE API 錯誤: {e}")
    
    # 備用：若櫃買 (OTC) 或是上市備用
    try:
        url_otc = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=otc_{code}.tw&_={int(datetime.now().timestamp() * 1000)}"
        res = requests.get(url_otc, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()
        if "msgArray" in res and len(res["msgArray"]) > 0:
            data = res["msgArray"][0]
            close = float(data.get("z", "0")) if data.get("z") != "-" else 0.0
            y_price = float(data.get("y", "0"))
            pct = ((close - y_price) / y_price) * 100 if y_price > 0 else 0.0
            high = float(data.get("h", "0")) if data.get("h") != "" else close
            low = float(data.get("l", "0")) if data.get("l") != "" else close
            volume = int(data.get("v", "0")) * 1000 if data.get("v", "") != "" else 0
            return {"close": close, "pct": pct, "high": high, "low": low, "volume": volume, "ma20": close}
    except:
        pass
        
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
    text_upper = text.upper()
    pure_code = "".join(filter(str.isdigit, text))
    
    add_user_to_db(user_id)

    if "加" in text and 4 <= len(pure_code) <= 6:
        add_watchlist_db(user_id, pure_code)
        reply = f"✅ 新增自選成功：{pure_code}"
    elif "刪" in text and 4 <= len(pure_code) <= 6:
        remove_watchlist_db(user_id, pure_code)
        reply = f"🗑️ 已從自選清單移除：{pure_code}"
    elif text in ["自選", "WATCHLIST"]:
        codes = get_user_watchlist(user_id)
        if not codes: 
            reply = "📂 目前自選清單是空的。\n💡 輸入「加 2330」即可新增！"
        else:
            results = ["📂 【我的雲端自選股與策略】\n==================="]
            for code in codes:
                data = get_realtime_stock(code)
                if data:
                    close, pct = data['close'], data['pct']
                    light = "🔴" if pct >= 0 else "🟢"
                    strategy = "🔥【多方續強】帶量上攻，沿 5 日線續抱。" if pct > 0 else "⚡【回測月線】多頭拉回，守穩支撐。"
                    block = f"\n{light} 【{code}】 現價：{close:.2f} ({pct:+.2f}%)\n📋 策略：{strategy}"
                    results.append(block)
                else:
                    results.append(f"\n⚪ 【{code}】 行情讀取中...")
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
    elif text in ["盤前", "早安"]:
        reply = generate_morning_brief()
    elif text == "黑馬":
        reply = "🔥 【高潛力黑馬股推薦】\n" + "\n".join([f"• {k} {v['name']} ({v['industry']})\n  💡 {v['reason']}" for k, v in black_horse_database.items()])
    elif text == "雷達":
        reply = "⚡ 【雷達突破清單】\n" + "\n".join([f"• {k} {v['name']} ({v['industry']}) - {v['tag']}" for k, v in radar_database.items()])
    elif text_upper in ["MENU", "選單", "幫助", "HELP"]:
        reply = (
            "🤖 蔡秉軒御用選股機器人\n"
            "===================\n"
            "🔥 功能專區\n"
            "• 輸入「盤前」➜ 美股與總經速覽\n"
            "• 輸入「黑馬」➜ 高成長潛力股\n"
            "• 輸入「雷達」➜ 技術面突破強勢\n\n"
            "📂 自選與策略管理\n"
            "• 輸入「自選」➜ 查看紅綠燈與操作策略\n"
            "• 輸入「加 2330」➜ 新增自選\n"
            "• 輸入「刪 2330」➜ 移除自選\n"
            "• 直接輸入代號 ➜ 查即時行情"
        )
    else:
        reply = "🤖 指令未識別，請輸入「選單」查看可用功能！"
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
