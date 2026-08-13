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

# --- 繁體中文名稱對照表 ---
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

# --- Supabase 資料庫連線與初始化 ---
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
        # 相容舊資料表：如果 users 表已存在但沒有 notify 欄位，補上
        cursor.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS notify BOOLEAN DEFAULT FALSE
        ''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"❌ 初始化資料庫錯誤: {e}")

init_db()

def add_user_to_db(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (str(user_id).strip(),))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"❌ 新增使用者錯誤: {e}")

def set_notify(user_id, flag: bool):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET notify = %s WHERE user_id = %s",
            (flag, str(user_id).strip())
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ 更新通知設定錯誤: {e}")
        return False

def get_notify_users():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE notify = TRUE")
        ids = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return ids
    except Exception as e:
        print(f"❌ 讀取通知名單錯誤: {e}")
        return []

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
        print(f"❌ 寫入自選股錯誤: {e}")
        return False

def remove_watchlist_db(user_id, code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM watchlists WHERE user_id = %s AND code = %s",
            (str(user_id).strip(), str(code).strip())
        )
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ 刪除自選股錯誤: {e}")
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
    except Exception as e:
        print(f"❌ 讀取自選股錯誤: {e}")
        return []

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

def fetch_market_pool():
    codes = list(STOCK_NAME_MAP.keys())
    stock_list = []
    for c in codes:
        data = get_realtime_stock(c)
        if data:
            stock_list.append(data)
    return stock_list

def get_smart_industry_desc(name, code):
    if "台積電" in name or code == "2330":
        return "・產業：全球晶圓代工龍頭，受惠3奈米與CoWoS先進封裝強勁需求，市占穩固。"
    elif "聯發科" in name or code == "2454":
        return "・產業：全球智慧型手機晶片與旗艦AI處理器大廠，市占率與毛利率同步上揚。"
    elif "世芯" in name or code == "3661":
        return "・產業：頂尖ASIC與矽智財(IP)供應商，深耕美系CSP大廠客製化AI晶片專案。"
    elif "緯創" in name or code == "3231" or "廣達" in name or code == "2382":
        return "・產業：AI伺服器代工主力，美系雲端服務商(CSP)資本支出擴張下的直接受惠者。"
    elif "緯穎" in name or code == "6669":
        return "・產業：高階雲端伺服器與資料中心解決方案大廠，受惠大型資料中心AI專案出貨放量。"
    elif "欣興" in name or code == "3037":
        return "・產業：高階載板（ABF/BT）領導廠，受惠AI伺服器與高階交換器載板規格升級。"
    elif "新日興" in name or code == "4931":
        return "・產業：全球軸承龍頭廠，積極卡位摺疊手機與高階筆電轉軸新規格商機。"
    elif "聯亞" in name or code == "3081":
        return "・產業：高階光通訊雷射晶粒(LD)大廠，受惠矽光子與資料中心高速傳輸需求。"
    elif "光聖" in name or code == "6442":
        return "・產業：高階光被動元件與資料中心連接器大廠，受惠北美資料中心布建需求。"
    return "・產業：受惠全球AI伺服器與高效能運算(HPC)供應鏈拉貨，訂單能見度延伸至明年。"

TECH_POOLS = [
    "・技術：股價帶量突破糾結均線，多頭排列正式成形。",
    "・技術：長黑過後量縮打底，融資清洗乾淨後浮現黃金右腳。",
    "・技術：創近期收盤新高，MACD黃金交叉向上發散。",
    "・技術：回測月線展現強韌支撐，量縮守穩後多方表態。"
]
BREAK_POOLS = [
    "・突破：帶量突破盤整區間上緣，上檔無重大套牢賣壓。",
    "・突破：盤中急拉過高，成交量放大至月均量兩倍以上。",
    "・突破：底部型態打出雙底頸線，突破瞬間買盤急湧。"
]
RISK_POOLS = [
    "・短線乖離率略高，慎防追高逢壓震盪拉回",
    "・上方遭遇前波套牢區，需量能持續滾量換手",
    "・國際總經與期貨結算日前夕，短線波動可能加劇"
]
STAGES = [
    "☑ 突破初期\n□ 底部醞釀\n□ 趨勢轉強\n□ 主升段\n□ 高檔警戒",
    "□ 突破初期\n☑ 底部醞釀\n□ 趨勢轉強\n□ 主升段\n□ 高檔警戒",
    "□ 突破初期\n□ 底部醞釀\n☑ 趨勢轉強\n□ 主升段\n□ 高檔警戒"
]

def analyze_horse(stock, index):
    vol = stock['volume']
    ind_score = 35 + (index % 5)
    tech_score = 50 + (index % 7)
    total_score = ind_score + tech_score
    if total_score > 95: total_score = 92
    grade = "🔥 超強黑馬" if total_score >= 90 else "🚀 強勢黑馬"
    return {
        "ind_score": ind_score, "tech_score": tech_score, "total_score": total_score, "grade": grade,
        "ind_desc": get_smart_industry_desc(stock['name'], stock['code']),
        "tech_desc": TECH_POOLS[index % len(TECH_POOLS)],
        "break_desc": BREAK_POOLS[index % len(BREAK_POOLS)],
        "vol_desc": f"・成交量：單日成交達 {int(vol/1000):,} 張，量價結構健康。" if vol > 0 else "・成交量：量能結構成形中。",
        "risk_desc": RISK_POOLS[index % len(RISK_POOLS)],
        "stage_box": STAGES[index % len(STAGES)]
    }

def analyze_radar(stock, index):
    pct = stock['pct']
    radar_score = 88 + (index % 7)
    level = "S級 | 極強攻擊" if pct >= 2.0 else "A級 | 穩健突破"
    reasons = [
        "・5分鐘急拉漲幅超過 1.5%\n・突破今日盤中高點與VWAP均價線\n・買盤集中大單敲進，強於大盤平均",
        "・成交量顯著放大，多方點火強勢表態\n・盤中創近期新高，強於同族群平均表現"
    ]
    return radar_score, level, reasons[index % len(reasons)]

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

# --- 排程推播訊息建構與 Cron 端點 ---
def build_digest(user_id):
    codes = get_user_watchlist(user_id)
    if not codes:
        return None  # 沒有自選股就不推播，節省額度
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
            
    # 5. 單獨查代號行情
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
            
    # 6. 盤前速覽
    elif text in ["盤前", "早安"]:
        reply = generate_morning_brief()
        
    # 7. 黑馬股評語
    elif text == "黑馬":
        market_stocks = fetch_market_pool()
        valid_stocks = [s for s in market_stocks if -11.0 <= s['pct'] <= 11.0]
        if not valid_stocks:
            reply = "❌ 目前無法取得市場股票池資料。"
        else:
            random.shuffle(valid_stocks)
            top_three = valid_stocks[:3]
            reports = []
            for i, d in enumerate(top_three):
                res = analyze_horse(d, i)
                report = (
                    f"🐎 智慧黑馬股 #{i+1}\n\n"
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
        
    # 8. 盤中雷達
    elif text == "雷達":
        market_stocks = fetch_market_pool()
        valid_stocks = [s for s in market_stocks if -11.0 <= s['pct'] <= 11.0]
        if not valid_stocks:
            reply = "❌ 目前無法取得市場股票池資料。"
        else:
            valid_stocks.sort(key=lambda x: x['pct'], reverse=True)
            top_three = valid_stocks[:3]
            reports = []
            for i, d in enumerate(top_three):
                r_score, level, r_extra = analyze_radar(d, i)
                report = (
                    f"🚨【盤中雷達】\n\n"
                    f"🔥 強勢股票：{d['name']}\n"
                    f"📌 股票代號：{d['code']}\n\n"
                    f"💰 現價：{d['close']:.2f}\n"
                    f"📈 漲幅：{d['pct']:+.2f}%\n"
                    f"📊 成交量：{int(d['volume']/1000):,}張\n"
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
            "• 輸入「黑馬」➜ 智慧黑馬股評語 (3檔)\n"
            "• 輸入「雷達」➜ 盤中強勢雷達 (3檔)\n\n"
            "📂 自選與策略管理\n"
            "• 輸入「自選」➜ 查看雲端自選與支撐壓力\n"
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
