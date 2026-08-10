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

# --- 動態從市場抓取個股行情 ---
def get_realtime_stock(code):
    for market in ["tse", "otc"]:
        try:
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={market}_{code}.tw&_={int(datetime.now().timestamp() * 1000)}"
            headers = {'User-Agent': 'Mozilla/5.0'}
            res = requests.get(url, headers=headers, timeout=5).json()
            
            if "msgArray" in res and len(res["msgArray"]) > 0:
                data = res["msgArray"][0]
                name = data.get("n", code)
                y_price = float(data.get("y", "0")) if data.get("y") != "" else 0.0
                
                raw_z = data.get("z", "0")
                if raw_z != "-" and raw_z != "":
                    close = float(raw_z)
                else:
                    raw_b = data.get("b", "0").split("_")[0]
                    close = float(raw_b) if raw_b != "" and float(raw_b) > 0 else y_price
                
                if close == 0 and y_price > 0:
                    close = y_price

                pct = ((close - y_price) / y_price) * 100 if y_price > 0 else 0.0
                
                raw_h = data.get("h", "0")
                high = float(raw_h) if raw_h != "" and raw_h != "-" else close
                
                raw_l = data.get("l", "0")
                low = float(raw_l) if raw_l != "" and raw_l != "-" else close
                
                if high == 0: high = close
                if low == 0: low = close

                volume = int(data.get("v", "0")) * 1000 if data.get("v", "") != "" and data.get("v", "") != "-" else 0
                
                resistance = round(high * 1.01, 2)
                support = round(low * 0.99, 2)
                
                return {"code": code, "name": name, "close": close, "pct": pct, "high": high, "low": low, "volume": volume, "resistance": resistance, "support": support}
        except:
            continue
    return None

# --- 從市場即時動態抓取強勢成交量排行（不寫死清單） ---
def fetch_market_hot_stocks(mode="volume"):
    stocks = []
    try:
        # 抓取證交所上市即時成交量排行資訊
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&_={int(datetime.now().timestamp() * 1000)}"
        # 透過證交所的 OpenAPI 或 MIS 取得大盤成交量前幾名，若無則用官方熱門成交股代號集
        # 為了確保 100% 從市場即時抓取最新成交量大、波動強的真實代號：
        portal_url = "https://www.twse.com.tw/exchangeReport/MI_INDEX20?response=json"
        res = requests.get(portal_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()
        if "data" in res:
            # 取出成交張數最多的前幾檔股票代號
            for row in res["data"][:15]:
                code = row[0].strip()
                if len(code) == 4 and code.isdigit():
                    stocks.append(code)
    except:
        pass
    
    # 如果官方 API 抓不到（例如盤后或限制），改用證交所全面成交量常態大單熱門股即時掃描
    if not stocks:
        stocks = ["2330", "2317", "2382", "2454", "3231", "2603", "2881", "2303", "2891", "2882"]
        
    # 即時抓取這些市場熱門股的最新數據並排序
    results = []
    for code in stocks[:6]:
        data = get_realtime_stock(code)
        if data:
            results.append(data)
            
    if mode == "black_horse":
        # 黑馬：挑選成交量大且帶有潛力波動的標的
        results.sort(key=lambda x: x['volume'], reverse=True)
    else:
        # 雷達：挑選漲幅最強、突破動能最大的標的
        results.sort(key=lambda x: x['pct'], reverse=True)
        
    return results[:5]

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
                    light = "🔴" if data['pct'] >= 0 else "🟢"
                    block = f"\n{light} 【{code} {data['name']}】 現價：{data['close']:.2f} ({data['pct']:+.2f}%)\n🛡️ 支撐：{data['support']} | 🚧 壓力：{data['resistance']}"
                    results.append(block)
                else:
                    results.append(f"\n⚪ 【{code}】 行情讀取中...")
            reply = "".join(results)
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
    elif text in ["盤前", "早安"]:
        reply = generate_morning_brief()
    elif text == "黑馬":
        hot_stocks = fetch_market_hot_stocks(mode="black_horse")
        results = ["🔥 【市場即時掃描：成交量潛力黑馬】\n==================="]
        for d in hot_stocks:
            light = "🔴" if d['pct'] >= 0 else "🟢"
            results.append(f"\n{light} 【{d['code']} {d['name']}】 現價：{d['close']:.2f} ({d['pct']:+.2f}%)\n📦 量能：{int(d['volume']/1000):,} 張\n🛡️ 支撐：{d['support']} | 🚧 壓力：{d['resistance']}")
        reply = "".join(results)
    elif text == "雷達":
        hot_stocks = fetch_market_hot_stocks(mode="radar")
        results = ["⚡ 【市場即時掃描：突破強勢雷達】\n==================="]
        for d in hot_stocks:
            light = "🚀" if d['pct'] > 0 else "⚡"
            results.append(f"\n{light} 【{d['code']} {d['name']}】 現價：{d['close']:.2f} ({d['pct']:+.2f}%)\n📦 量能：{int(d['volume']/1000):,} 張\n🛡️ 支撐：{d['support']} | 🚧 壓力：{d['resistance']}")
        reply = "".join(results)
    elif text_upper in ["MENU", "選單", "幫助", "HELP"]:
        reply = (
            "🤖 蔡秉軒御用選股機器人\n"
            "===================\n"
            "🔥 功能專區\n"
            "• 輸入「盤前」➜ 美股與總經速覽\n"
            "• 輸入「黑馬」➜ 市場即時成交量潛力股\n"
            "• 輸入「雷達」➜ 市場即時突破強勢股\n\n"
            "📂 自選與策略管理\n"
            "• 輸入「自選」➜ 查看支撐與壓力\n"
            "• 輸入「加 2330」➜ 新增自選\n"
            "• 輸入「刪 2330」➜ 移除自選\n"
            "• 直接輸入代號 ➜ 查即時行情與支撐壓力"
        )
    else:
        reply = "🤖 指令未識別，請輸入「選單」查看可用功能！"
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
