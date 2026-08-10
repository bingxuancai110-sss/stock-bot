import os
import sqlite3
import requests
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

# --- SQLite 資料庫初始化 ---
def init_db():
    conn = sqlite3.connect('stock_bot.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watchlists (
            user_id TEXT,
            code TEXT,
            PRIMARY KEY (user_id, code)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            user_id TEXT,
            code TEXT,
            price REAL,
            PRIMARY KEY (user_id, code)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_all_users():
    conn = sqlite3.connect('stock_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    users = [row[0] for row in cursor.fetchall()]
    conn.close()
    if "Ue00f44b36b32a87adaca89034ec24e58" not in users:
        users.append("Ue00f44b36b32a87adaca89034ec24e58")
    return users

def add_user_to_db(user_id):
    conn = sqlite3.connect('stock_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def get_user_watchlist(user_id):
    conn = sqlite3.connect('stock_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT code FROM watchlists WHERE user_id = ?", (user_id,))
    codes = [row[0] for row in cursor.fetchall()]
    conn.close()
    return codes

def add_watchlist_db(user_id, code):
    conn = sqlite3.connect('stock_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO watchlists (user_id, code) VALUES (?, ?)", (user_id, code))
    conn.commit()
    conn.close()

def remove_watchlist_db(user_id, code):
    conn = sqlite3.connect('stock_bot.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watchlists WHERE user_id = ? AND code = ?", (user_id, code))
    cursor.execute("DELETE FROM alerts WHERE user_id = ? AND code = ?", (user_id, code))
    conn.commit()
    conn.close()

def get_user_alerts(user_id):
    conn = sqlite3.connect('stock_bot.db')
    cursor = conn.cursor()
    cursor.execute("SELECT code, price FROM alerts WHERE user_id = ?", (user_id,))
    alerts = {row[0]: row[1] for row in cursor.fetchall()}
    conn.close()
    return alerts

def set_alert_db(user_id, code, price):
    conn = sqlite3.connect('stock_bot.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO alerts (user_id, code, price) VALUES (?, ?, ?)", (user_id, code, price))
    conn.close()

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
    symbols = [f"{code}.TW", f"{code}.TWO"]
    headers = {'User-Agent': 'Mozilla/5.0'}
    for sym in symbols:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
            res = requests.get(url, headers=headers, timeout=5)
            data = res.json()
            result = data['chart']['result'][0]
            meta = result['meta']
            quotes = result['indicators']['quote'][0]
            closes = [c for c in quotes.get('close', []) if c is not None]
            if len(closes) < 2:
                continue
            close = float(closes[-1])
            prev_close = float(closes[-2])
            high = float(meta.get('regularMarketDayHigh', max(closes[-3:])))
            low = float(meta.get('regularMarketDayLow', min(closes[-3:])))
            vol = int(meta.get('regularMarketVolume', 0))
            pct = ((close - prev_close) / prev_close) * 100
            ma5 = sum(closes[-5:]) / len(closes[-5:]) if len(closes) >= 5 else close
            ma20 = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 20 else close
            return {
                "close": close, "prev_close": prev_close, "high": high, "low": low,
                "volume": vol, "pct": pct, "ma5": ma5, "ma20": ma20
            }
        except Exception:
            continue
    return None

def get_us_stock_pct(symbol):
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
        res = requests.get(url, headers=headers, timeout=4).json()
        quotes = res['chart']['result'][0]['indicators']['quote'][0]['close']
        closes = [c for c in quotes if c is not None]
        if len(closes) >= 2:
            return ((closes[-1] - closes[-2]) / closes[-2]) * 100
    except Exception:
        pass
    return 0.0

def generate_morning_brief():
    dji_pct = get_us_stock_pct("^DJI")
    gspc_pct = get_us_stock_pct("^GSPC")
    sox_pct = get_us_stock_pct("^SOX")
    ixic_pct = get_us_stock_pct("^IXIC")
    
    nvda_pct = get_us_stock_pct("NVDA")
    tsm_pct = get_us_stock_pct("TSM")
    amd_pct = get_us_stock_pct("AMD")
    aapl_pct = get_us_stock_pct("AAPL")
    msft_pct = get_us_stock_pct("MSFT")
    amzn_pct = get_us_stock_pct("AMZN")
    googl_pct = get_us_stock_pct("GOOGL")
    mu_pct = get_us_stock_pct("MU")
    lite_pct = get_us_stock_pct("LITE")

    nfp_data = "7月非農就業減少 2.3 萬人，失業率 4.1% (8/7公布)"
    cpi_data = "待下次數據公佈更新 (CPI)"

    strategy_advice = (
        "💡 總經與台股操作對策：\n"
        "• 非農數據偏弱顯示經濟降溫，市場升息壓力解除、降息預期升溫，資金轉趨寬鬆，對高成長科技與 AI 股利多。\n"
        "• 專注高成長、高爆發力的科技與 AI 供應鏈黑馬，並透過雷達掌握技術突破。"
    )

    today_str = datetime.now().strftime("%Y/%m/%d")
    return (
        f"☀️ 【台股盤前與總經動態速覽】\n"
        f"📅 日期：{today_str}\n"
        f"-------------------\n"
        f"🇺🇸 美股主要指數：\n"
        f"• 道瓊工業：{dji_pct:+.2f}%\n"
        f"• 標普 500：{gspc_pct:+.2f}%\n"
        f"• 費城半導體：{sox_pct:+.2f}%\n"
        f"• 那斯達克：{ixic_pct:+.2f}%\n\n"
        f"💻 美股科技巨頭與重點股：\n"
        f"• 輝達 (NVDA)：{nvda_pct:+.2f}%\n"
        f"• 台積電ADR (TSM)：{tsm_pct:+.2f}%\n"
        f"• 超微 (AMD)：{amd_pct:+.2f}%\n"
        f"• 蘋果 (AAPL)：{aapl_pct:+.2f}%\n"
        f"• 微軟 (MSFT)：{msft_pct:+.2f}%\n"
        f"• 亞馬遜 (AMZN)：{amzn_pct:+.2f}%\n"
        f"• 谷歌 (GOOGL)：{googl_pct:+.2f}%\n"
        f"• 美光 (MU)：{mu_pct:+.2f}%\n"
        f"• Lumentum (LITE)：{lite_pct:+.2f}%\n\n"
        f"📊 總經關鍵數據：\n"
        f"• 非農就業 (NFP)：{nfp_data}\n"
        f"• 消費者物價指數 (CPI)：{cpi_data}\n\n"
        f"{strategy_advice}"
    )

# 抓取大盤、重要權值股與個人自選股組合成完整盤後戰報
def generate_afternoon_brief(user_id):
    today_str = datetime.now().strftime("%Y/%m/%d")
    report_lines = [f"🌙 【台股盤後戰報總結】\n📅 日期：{today_str}\n==================="]

    # 1. 📈 大盤概況
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/^TWII?range=5d&interval=1d"
        res = requests.get(url, headers=headers, timeout=5)
        data = res.json()
        result = data['chart']['result'][0]
        meta = result['meta']
        quotes = result['indicators']['quote'][0]
        closes = [c for c in quotes.get('close', []) if c is not None]
        
        close = float(closes[-1])
        prev_close = float(closes[-2]) if len(closes) >= 2 else close
        change = close - prev_close
        pct = (change / prev_close) * 100
        high = float(meta.get('regularMarketDayHigh', max(closes[-3:])))
        low = float(meta.get('regularMarketDayLow', min(closes[-3:])))
        vol = int(meta.get('regularMarketVolume', 0))
        
        if pct > 1.0:
            market_mood = "🔥 強勢大漲，多方格局掌控全場。"
        elif pct > 0:
            market_mood = "📈 震盪收紅，盤勢偏多整理。"
        elif pct > -1.0:
            market_mood = "📉 震盪收黑，高檔逢壓調節。"
        else:
            market_mood = "⚠️ 拉回修正，留意支撐防守。"

        report_lines.append(
            f"📈 【台股大盤概況】\n"
            f"• 收盤指數：{close:,.2f} ({pct:+.2f}%)\n"
            f"• 漲跌點數：{change:+,.2f} 點\n"
            f"• 成交量能：{int(vol / 100000000):,} 億\n"
            f"• 盤勢解讀：{market_mood}"
        )
    except Exception:
        report_lines.append("📈 【台股大盤概況】\n• 數據讀取中...")

    # 2. 🏢 重要權值個股
    report_lines.append("\n===================\n🏢 【重要權值個股】")
    key_weights = [("2330", "台積電"), ("2317", "鴻海"), ("2454", "聯發科")]
    for code, name in key_weights:
        w_data = get_realtime_stock(code)
        if w_data:
            report_lines.append(f"• {name} ({code})：{w_data['close']:.1f} ({w_data['pct']:+.2f}%)")
        else:
            report_lines.append(f"• {name} ({code})：數據更新中")

    # 3. ⭐ 個人自選股追蹤
    report_lines.append("\n===================\n⭐ 【我的自選股追蹤】")
    user_watchlist = get_user_watchlist(user_id)
    user_alerts = get_user_alerts(user_id)

    if not user_watchlist:
        report_lines.append("目前自選清單是空的。\n💡 輸入「加 2330」即可新增自選！")
    else:
         for code in user_watchlist:
            data = get_realtime_stock(code)
            alert_p = user_alerts.get(code)
            if data:
                close = data['close']
                pct = data['pct']
                light = "🔴" if pct >= 0 else "🟢"
                item_str = f"{light} {code} 現價：{close:.1f} ({pct:+.2f}%)"
                if alert_p:
                    item_str += f" [目標:{alert_p}]"
                report_lines.append(item_str)
            else:
                report_lines.append(f"⚪ {code} 讀取中...")

    report_lines.append("===================\n💡 保持紀律操作，祝您操盤順利！")
    return "\n".join(report_lines)

@app.route("/")
def home():
    return "Stock Bot & Radar (Keyword Edition) is alive!"

@app.route("/push-test")
def push_test():
    target_users = get_all_users()
    if target_users:
        try:
            message = generate_morning_brief()
            for uid in target_users:
                line_bot_api.push_message(uid, TextSendMessage(text=message))
            return f"Push Success to {len(target_users)} users!"
        except Exception as e:
            return f"Push Failed: {e}"
    return "Target User IDs not found."

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
    user_text = event.message.text.strip()
    user_text_upper = user_text.upper()
    pure_code = "".join(filter(str.isdigit, user_text))

    add_user_to_db(user_id)

    user_watchlist = get_user_watchlist(user_id)
    user_alerts = get_user_alerts(user_id)

    # 1. 加自選股指令
    if "加" in user_text and len(pure_code) == 4:
        if pure_code not in user_watchlist:
            add_watchlist_db(user_id, pure_code)
            reply_text = f"✅ 新增自選成功：{pure_code}\n輸入「自選」即可檢視完整清單與策略。"
        else:
            reply_text = f"📌 {pure_code} 已經在您的自選股清單中囉！"

    # 2. 刪除自選股指令
    elif "刪" in user_text and len(pure_code) == 4:
        if pure_code in user_watchlist:
            remove_watchlist_db(user_id, pure_code)
            reply_text = f"🗑️ 已從自選清單移除：{pure_code}"
        else:
            reply_text = f"❌ 找不到代號 {pure_code}"

    # 3. 設定到價通知指令
    elif ("設" in user_text) or (" " in user_text and len(pure_code) >= 7):
        clean_text = user_text.replace("設定", "").replace("設", "").strip()
        parts = clean_text.split()
        
        target_code = None
        target_price = None
        
        for p in parts:
            p_digits = "".join(filter(str.isdigit, p))
            if len(p_digits) == 4 and not target_code:
                target_code = p_digits
            else:
                try:
                    val = float(p)
                    if val > 10:
                        target_price = val
                except ValueError:
                    pass
                    
        if not target_code and len(parts) >= 2:
            c_part = "".join(filter(str.isdigit, parts[0]))
            if len(c_part) == 4:
                target_code = c_part
                try:
                    target_price = float(parts[1])
                except ValueError:
                    pass

        if target_code and target_price:
            if target_code not in user_watchlist:
                add_watchlist_db(user_id, target_code)
            set_alert_db(user_id, target_code, target_price)
            reply_text = f"🔔 到價通知設定成功！\n• 股票代號：{target_code}\n• 目標價格：{target_price}"
        else:
            reply_text = "❌ 格式錯誤！\n正確範例：\n• 設 2330 1500\n• 2330 1500"

    # 4. 查看自選股指令（附帶操作策略、紅綠燈、支撐壓力）
    elif user_text in ["自選", "WATCHLIST"]:
        updated_watchlist = get_user_watchlist(user_id)
        updated_alerts = get_user_alerts(user_id)
        
        if not updated_watchlist:
            reply_text = (
                "📂 【我的自選股清單】\n"
                "===================\n"
                "目前清單是空的。\n\n"
                "💡 新增指令：加 2330\n"
                "💡 設定到價：2330 1500"
            )
        else:
            results = ["📂 【我的自選股與策略清單】\n==================="]
            for code in updated_watchlist:
                data = get_realtime_stock(code)
                alert_p = updated_alerts.get(code)
                
                if data:
                    close = data['close']
                    pct = data['pct']
                    ma20 = data['ma20']
                    
                    light_icon = "🔴" if pct >= 0 else "🟢"
                    
                    support = data['low'] * 0.99
                    resistance = data['high'] * 1.01
                    
                    if close > ma20 and pct > 0:
                        strategy = "🔥【多方續強】帶量上攻，可沿 5 日線續抱，逢壓不急追。"
                    elif close > ma20 and pct <= 0:
                        strategy = "⚡【回測月線】多頭格局中的量縮拉回，守穩支撐可分批低接。"
                    elif close <= ma20 and pct > 0:
                        strategy = "⚠️【弱勢反彈】跌破月線後的跌深反彈，反彈逢壓建議先減碼。"
                    else:
                        strategy = "🛡️【保守觀望】空方趨勢或量縮盤整，等待打底完成再進場。"
                    
                    stock_block = (
                        f"\n{light_icon} 【 {code} 】 現價：{close:.1f} ({pct:+.2f}%)\n"
                        f"-------------------\n"
                        f"🛡️ 支撐價：{support:.1f}  |  🎯 壓力價：{resistance:.1f}\n"
                        f"📋 操作策略：\n{strategy}"
                    )
                    if alert_p:
                        stock_block += f"\n🔔 到價目標：{alert_p}"
                        
                    results.append(stock_block)
                else:
                    results.append(f"\n⚪ 【 {code} 】 行情讀取中...")
            
            results.append("\n===================\n💡 刪除：刪 2330 | 設價：2330 1500")
            reply_text = "\n".join(results)

    # 5. 一般 4 位數股票行情查詢
    elif len(pure_code) == 4 and len(user_text) <= 5 and " " not in user_text:
        data = get_realtime_stock(pure_code)
        if data:
            close = data["close"]
            high = data["high"]
            low = data["low"]
            vol = data["volume"]
            pct = data["pct"]
            ma5 = data["ma5"]
            ma20 = data["ma20"]
            
            name = f"台股 {pure_code}"
            industry = "一般上市櫃個股"
            if pure_code in black_horse_database:
                name = black_horse_database[pure_code]["name"]
                industry = black_horse_database[pure_code]["industry"]
            elif pure_code in radar_database:
                name = radar_database[pure_code]["name"]
                industry = radar_database[pure_code]["industry"]

            score = min(max(65 + (15 if close > ma20 else 0) + (10 if ma5 > ma20 else 0) + int(pct * 4), 30), 98)

            reply_text = (
                f"📊 {pure_code} {name} ({industry})\n"
                f"===================\n"
                f"💰 現價：{close:.2f} ({pct:+.2f}%)\n"
                f"🔺 高/低：{high:.2f} / {low:.2f}\n"
                f"📦 量能：{int(vol / 1000):,} 張\n\n"
                f"🎯 綜合評分：{score}分\n"
                f"🛡️ 支撐防守：{low * 0.99:.1f}  |  🎯 壓力目標：{high * 1.01:.1f}\n"
                f"💡 建議進場：{close:.1f} | 停利：{close * 1.035:.1f} | 停損：{close * 0.975:.1f}"
            )
        else:
            reply_text = f"❌ 查無代號 {pure_code} 的即時行情。"

    # 6. 選單與功能指令
    elif user_text_upper in ["MENU", "MANU", "選單", "幫助", "HELP"]:
        reply_text = (
            "🤖 蔡秉軒御用選股機器人\n"
            "===================\n"
            "🔥 功能專區\n"
            "• 輸入「盤前」➜ 美股與總經速覽\n"
            "• 輸入「盤後」➜ 大盤、權值股與自選戰報\n"
            "• 輸入「黑馬」➜ 高成長潛力股\n"
            "• 輸入「雷達」➜ 技術面突破強勢\n\n"
            "📂 自選與策略管理\n"
            "• 輸入「自選」➜ 查看紅綠燈與操作策略\n"
            "• 輸入「加 2330」➜ 新增自選\n"
            "• 輸入「刪 2330」➜ 移除自選\n"
            "• 輸入「2330 1500」➜ 設定到價通知\n"
            "==================="
        )
    elif user_text in ["盤前", "早安", "MORNING"]:
        reply_text = generate_morning_brief()
    elif user_text in ["盤後", "收盤", "AFTERNOON"]:
        reply_text = generate_afternoon_brief(user_id)
    elif user_text == "雷達":
        radar_results = []
        for code, info in radar_database.items():
            data = get_realtime_stock(code)
            if data:
                if data["close"] >= data["ma20"] or data["pct"] > 0:
                    radar_results.append(f"• {code} {info['name']}：現價 {data['close']:.1f} ({data['pct']:+.2f}%)")
        reply_text = "🎯 技術面強勢雷達\n-------------------\n" + ("\n".join(radar_results[:4]) if radar_results else "目前無符合標的。")
    elif user_text == "黑馬":
        horse_results = []
        for code, info in black_horse_database.items():
            data = get_realtime_stock(code)
            price_str = f"現價 {data['close']:.1f} ({data['pct']:+.2f}%)" if data else "行情更新中"
            horse_results.append(f"• {code} {info['name']} | {price_str}\n  └ {info['reason']}")
        reply_text = "🔥 潛力黑馬專區\n-------------------\n" + "\n\n".join(horse_results)
    else:
        reply_text = "❌ 指令錯誤！請輸入「選單」查看功能。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
