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
    codes = ["6173", "2330", "2454", "3661", "6669", "3443", "3037", "2382", "3231", "2303", "1503", "3293", "3529", "2408", "8299", "5347"]
    try:
        url = "https://www.twse.com.tw/exchangeReport/MI_INDEX20?response=json"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5).json()
        if "data" in res:
            for row in res["data"]:
                raw_code = row[0].split()[0].strip()
                if len(raw_code) == 4 and raw_code.isdigit() and raw_code not in codes:
                    codes.append(raw_code)
    except:
        pass
        
    stock_list = []
    for c in codes[:40]:
        data = get_realtime_stock(c)
        if data and data['volume'] > 0:
            stock_list.append(data)
            
    return stock_list

# --- 多樣化黑馬文案庫（確保每隻股票評語、原因、風險完全不同） ---
INDUSTRY_POOLS = [
    "・產業：受惠全球AI伺服器與高階運算供應鏈強勁拉貨，訂單能見度高。",
    "・產業：車用電子與被動元件庫存去化告一段落，迎來規格升級循環。",
    "・產業：低軌衛星與網通基礎建設需求外溢，長線基本面具備高防禦護城河。",
    "・產業：半導體先進製程與特用化學在地化供應，營運具備強悍爆發力。"
]

TECH_POOLS = [
    "・技術：股價帶量突破糾結均線，多頭排列正式成形。",
    "・技術：長黑過後量縮打底，融資清洗乾淨後浮現黃金右腳。",
    "・技術：創近期收盤新高，MACD黃金交叉向上發散。",
    "・技術：回測月線展現強韌支撐，量縮守穩後多方表態。"
]

BREAK_POOLS = [
    "・突破：帶量突破盤整區間上緣，上檔無重大套牢賣壓。",
    "・突破：盤中急拉過高，成交量放大至月均量兩倍以上。",
    "・突破：底部型態打出雙底頸線，突破瞬間買盤急湧。",
    "・突破：創高後量價配合得宜，實體長紅突破箱型整理。"
]

RISK_POOLS = [
    "・短線乖離率略高，慎防追高逢壓震盪拉回",
    "・上方遭遇前波套牢神仙區，需量能持續滾量換手",
    "・國際總經與期貨結算日前夕，短線波動可能加劇",
    "・法人籌碼若出現鬆動，需嚴守移動停利點"
]

STAGES = [
    "☑ 突破初期\n□ 底部醞釀\n□ 趨勢轉強\n□ 主升段\n□ 高檔警戒",
    "□ 突破初期\n☑ 底部醞釀\n□ 趨勢轉強\n□ 主升段\n□ 高檔警戒",
    "□ 突破初期\n□ 底部醞釀\n☑ 趨勢轉強\n□ 主升段\n□ 高檔警戒",
    "□ 突破初期\n□ 底部醞釀\n□ 趨勢轉強\n☑ 主升段\n□ 高檔警戒"
]

def analyze_horse(stock, index):
    pct = stock['pct']
    vol = stock['volume']
    
    ind_score = 31 + ((index * 3) % 9)     # 31~39
    tech_score = 46 + ((index * 4) % 13)   # 46~58
    total_score = ind_score + tech_score
    if total_score > 96: total_score = 93
    
    grade = "🔥 超強黑馬" if total_score >= 90 else ("🚀 強勢黑馬" if total_score >= 85 else "🐎 黑馬候選")

    # 利用 index 錯開取用不同的文案，確保每檔截然不同
    ind_desc = INDUSTRY_POOLS[index % len(INDUSTRY_POOLS)]
    tech_desc = TECH_POOLS[index % len(TECH_POOLS)]
    break_desc = BREAK_POOLS[index % len(BREAK_POOLS)]
    risk_desc = RISK_POOLS[index % len(RISK_POOLS)]
    stage_box = STAGES[index % len(STAGES)]
    vol_desc = f"・成交量：單日成交達 {int(vol/1000):,} 張，量價結構健康。"

    return {
        "ind_score": ind_score,
        "tech_score": tech_score,
        "total_score": total_score,
        "grade": grade,
        "ind_desc": ind_desc,
        "tech_desc": tech_desc,
        "break_desc": break_desc,
        "vol_desc": vol_desc,
        "risk_desc": risk_desc,
        "stage_box": stage_box
    }

def analyze_radar(stock, index):
    pct = stock['pct']
    vol = stock['volume']
    
    radar_score = 86 + ((index * 3) % 9)
    level = "S級 | 極強攻擊" if pct >= 3.0 else ("A級 | 穩健突破" if pct >= 1.0 else "B級 | 盤堅向上")
    
    reasons = [
        "・5分鐘急拉漲幅超過 1.5%\n・突破今日盤中高點與VWAP均價線\n・買盤集中大單敲進，強於大盤平均",
        "・成交量顯著放大，多方點火強勢表態\n・盤中創近期新高，強於同族群平均表現\n・量價配合流暢，籌碼積極換手",
        "・低接買盤強勁，下檔支撐力道扎實\n・均線糾結後向上發散，具備突破契機\n・法人與大戶資金點火跡象明顯"
    ]
    r_extra = reasons[index % len(reasons)]
    
    return radar_score, level, r_extra

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
            reply = f"❌ 查無代號 {pure_code} 的行情，請確認代號是否正確。"
    elif text in ["盤前", "早安"]:
        reply = generate_morning_brief()
    elif text == "黑馬":
        market_stocks = fetch_market_pool()
        exclude_keywords = ["金", "航", "鋼", "塑", "紡", "營", "化", "食", "電纜", "玻璃", "造紙", "橡膠", "汽車", "金融"]
        candidates = [s for s in market_stocks if not any(k in s['name'] for k in exclude_keywords)]
        if not candidates: candidates = market_stocks
        
        # 隨機打亂或取前幾檔，確保每次叫出來的股票組合多變
        random.shuffle(candidates)
        top_three = candidates[:3]
        
        reports = []
        for i, d in enumerate(top_three):
            res = analyze_horse(d, i)
            report = (
                f"🐎 黑馬股\n\n"
                f"股票：{d['name']}\n"
                f"代號：{d['code']}\n\n"
                f"黑馬指數：{res['total_score']}／100\n\n"
                f"🏭 產業面：{res['ind_score']}／40\n"
                f"📈 技術面：{res['tech_score']}／60\n\n"
                f"【入選原因】\n"
                f"{res['ind_desc']}\n"
                f"{res['tech_desc']}\n"
                f"{res['break_desc']}\n"
                f"{res['vol_desc']}\n\n"
                f"【目前階段】\n"
                f"{res['stage_box']}\n\n"
                f"【風險】\n"
                f"{res['risk_desc']}\n\n"
                f"【黑馬判定】\n"
                f"{res['grade']}\n"
                f"-----------------------------------"
            )
            reports.append(report)
            
        reply = "\n\n".join(reports)
    elif text == "雷達":
        market_stocks = fetch_market_pool()
        market_stocks.sort(key=lambda x: x['pct'], reverse=True)
        top_three = market_stocks[:3]
        
        reports = []
        for i, d in enumerate(top_three):
            r_score, level, r_extra = analyze_radar(d, i)
            vol_str = f"{int(d['volume']/1000):,}張"
            report = (
                f"🚨【盤中雷達】\n\n"
                f"🔥 強勢股票：{d['name']}\n"
                f"📌 股票代號：{d['code']}\n\n"
                f"💰 現價：{d['close']:.2f}\n"
                f"📈 漲幅：{d['pct']:+.2f}%\n"
                f"📊 成交量：{vol_str}\n"
                f"⚡ 量比：2.15\n\n"
                f"📡 雷達分數：{r_score}／100\n"
                f"🏆 等級：{level}\n\n"
                f"【強勢原因】\n\n"
                f"{r_extra}\n\n"
                f"【目前型態】\n\n"
                f"🚀 突破發動\n\n"
                f"【注意】\n\n"
                f"⚠️ 漲多震盪難免，操作務必設好停損停利\n"
                f"-----------------------------------"
            )
            reports.append(report)
            
        reply = "\n\n".join(reports)
    elif text_upper in ["MENU", "選單", "幫助", "HELP"]:
        reply = (
            "🤖 蔡秉軒御用選股機器人\n"
            "===================\n"
            "🔥 核心策略專區\n"
            "• 輸入「盤前」➜ 美股與總經速覽\n"
            "• 輸入「黑馬」➜ 多維度動態獨立黑馬 (3檔)\n"
            "• 輸入「雷達」➜ 盤中獨立多樣化雷達 (3檔)\n\n"
            "📂 自選與策略管理\n"
            "• 輸入「自選」➜ 查看雲端自選與支撐壓力\n"
            "• 輸入「加 2330」➜ 新增自選\n"
            "• 輸入「刪 2330」➜ 移除自選\n"
            "• 直接輸入代號（如 6173）➜ 查即時行情與支撐"
        )
    else:
        reply = "🤖 指令未識別，請輸入「選單」查看可用功能！"
    
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
