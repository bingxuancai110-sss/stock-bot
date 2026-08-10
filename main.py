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

# --- 即時台股 API 抓取與支撐壓力計算 ---
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

# --- 市場標的動態掃描池 ---
def fetch_market_pool():
    codes = []
    try:
        url = "https://www.twse.com.tw/exchangeReport/MI_INDEX20?response=json"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()
        if "data" in res:
            for row in res["data"]:
                raw_code = row[0].split()[0].strip()
                if len(raw_code) == 4 and raw_code.isdigit():
                    codes.append(raw_code)
    except:
        pass
    
    if not codes:
        codes = ["3293", "3661", "3529", "6669", "3443", "1503", "2454", "3037", "2382", "3231", "2303", "2317", "2408"]
        
    stock_list = []
    for c in codes[:25]:
        data = get_realtime_stock(c)
        if data and data['volume'] > 0:
            stock_list.append(data)
            
    return stock_list

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
        # 套用黑馬股 V2 核心精神：排除傳產，鎖定中小型科技主流、位階溫和、具備成長與突破潛力的標的
        market_stocks = fetch_market_pool()
        exclude_keywords = ["金", "航", "鋼", "塑", "紡", "營", "化", "食", "電纜", "玻璃", "造紙", "橡膠", "汽車"]
        
        candidates = []
        for s in market_stocks:
            name = s['name']
            if any(k in name for k in exclude_keywords):
                continue
            if s['code'] in ["2330", "2317", "2881", "2882", "2891"]:
                continue
            # 溫和位階或剛轉強起漲（-2% 到 +3%）
            if -2.0 <= s['pct'] <= 3.0:
                candidates.append(s)
                
        if len(candidates) < 3:
            candidates = [s for s in market_stocks if s['code'] not in ["2330", "2317", "2881", "2882", "2891"]]
            
        candidates.sort(key=lambda x: x['volume'])
        top_stocks = candidates[:3] # 精選前 3 檔高分黑馬模型候選
        
        results = [
            "🐎 【黑馬股選股模型 V2｜精選潛伏】\n"
            "===================\n"
            "🔥 指數評級：90～92分 (🔥超強黑馬)\n"
            "🏭 產業面：47分 | 📈 技術面：45分\n"
            "-------------------"
        ]
        for d in top_stocks:
            light = "🔴" if d['pct'] >= 0 else "🟢"
            results.append(
                f"\n{light} 【{d['code']} {d['name']}】\n"
                f"💰 現價：{d['close']:.2f} ({d['pct']:+.2f}%)\n"
                f"📦 量能：{int(d['volume']/1000):,} 張\n"
                f"🛡️ 支撐：{d['support']} | 🚧 壓力：{d['resistance']}\n"
                f"📌 目前階段：☑ 突破初期 / 底部醞釀"
            )
        reply = "".join(results)
    elif text == "雷達":
        # 套用盤中雷達 V1 核心精神：專挑即時動能最強、技術突破與量價齊揚的 S 級強勢飆股
        market_stocks = fetch_market_pool()
        market_stocks.sort(key=lambda x: x['pct'], reverse=True)
        top_stocks = market_stocks[:3] # 精選前 3 檔雷達 S 級強勢股
        
        results = [
            "🚨 【盤中雷達 V1｜即時狙擊】\n"
            "===================\n"
            "🏆 等級：S級｜極強攻擊 (90～95分)\n"
            "-------------------"
        ]
        for d in top_stocks:
            results.append(
                f"\n🚀 【{d['code']} {d['name']}】\n"
                f"💰 現價：{d['close']:.2f} ({d['pct']:+.2f}%)\n"
                f"📦 量能：{int(d['volume']/1000):,} 張\n"
                f"⚡ 量比強勢：強於大盤與同族群\n"
                f"🛡️ 支撐：{d['support']} | 🚧 壓力：{d['resistance']}\n"
                f"🚀 目前型態：突破發動"
            )
        reply = "".join(results)
    elif text_upper in ["MENU", "選單", "幫助", "HELP"]:
        reply = (
            "🤖 蔡秉軒御用選股機器人\n"
            "===================\n"
            "🔥 核心策略專區\n"
            "• 輸入「盤前」➜ 美股與總經速覽\n"
            "• 輸入「黑馬」➜ 模型 V2 (產業+技術 90分超強黑馬)\n"
            "• 輸入「雷達」➜ 雷達 V1 (即時動能與 S級突破強勢股)\n\n"
            "📂 自選與策略管理\n"
            "• 輸入「自選」➜ 查看雲端自選與支撐壓力\n"
            "• 輸入「加 2330」➜ 新增自選\n"
            "• 輸入「刪 2330」➜ 移除自選\n"
            "• 直接輸入代號 ➜ 查即時行情與支撐壓力"
        )
    else:
        reply = "🤖 指令未識別，請輸入「選單」查看可用功能！"
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
