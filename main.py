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

# --- GitHub 遠端黑馬與雷達清單設定 ---
GITHUB_BLACK_HORSE_URL = "https://raw.githubusercontent.com/你的帳號/你的專案/main/black_horse.json"
GITHUB_RADAR_URL = "https://raw.githubusercontent.com/你的帳號/你的專案/main/radar_data.json"

def fetch_latest_black_horses():
    try:
        response = requests.get(GITHUB_BLACK_HORSE_URL, timeout=5)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"讀取黑馬清單失敗: {e}")
    return None

def fetch_latest_radars():
    try:
        response = requests.get(GITHUB_RADAR_URL, timeout=5)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        print(f"讀取雷達清單失敗: {e}")
    return None

# --- Supabase (PostgreSQL) 資料庫連線與初始化 ---
def get_db_connection():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        # 備援：如果環境變數還沒填，直接帶入你的 Supabase 連線字串 (記得補上密碼)
        db_url = "postgresql://postgres:你的密碼@db.nudvbywjkmhtpitqhgcc.supabase.co:5432/postgres"
    
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
        cursor.close()
        conn.close()
        print("Supabase 資料庫表格初始化成功！")
    except Exception as e:
        print(f"初始化資料庫失敗: {e}")

init_db()

def get_all_users():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        users = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
    except Exception:
        users = []
    if "Ue00f44b36b32a87adaca89034ec24e58" not in users:
        users.append("Ue00f44b36b32a87adaca89034ec24e58")
    return users

def add_user_to_db(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"新增使用者失敗: {e}")

def get_user_watchlist(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT code FROM watchlists WHERE user_id = %s", (user_id,))
        codes = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return codes
    except Exception:
        return []

def add_watchlist_db(user_id, code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO watchlists (user_id, code) VALUES (%s, %s) ON CONFLICT (user_id, code) DO NOTHING", (user_id, code))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"新增自選失敗: {e}")

def remove_watchlist_db(user_id, code):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM watchlists WHERE user_id = %s AND code = %s", (user_id, code))
        cursor.execute("DELETE FROM alerts WHERE user_id = %s AND code = %s", (user_id, code))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"刪除自選失敗: {e}")

def get_user_alerts(user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT code, price FROM alerts WHERE user_id = %s", (user_id,))
        alerts = {row[0]: row[1] for row in cursor.fetchall()}
        cursor.close()
        conn.close()
        return alerts
    except Exception:
        return {}

def set_alert_db(user_id, code, price):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO alerts (user_id, code, price) VALUES (%s, %s, %s)
            ON CONFLICT (user_id, code) DO UPDATE SET price = EXCLUDED.price
        """, (user_id, code, price))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"設定提醒失敗: {e}")

# 備用靜態字典
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
        f"• Lumentum (LITE)：{lite_pct:+.2f}%\n"
    )

def generate_afternoon_brief(user_id):
    today_str = datetime.now().strftime("%Y/%m/%d")
    report_lines = [f"🌙 【台股盤後戰報總結】\n📅 日期：{today_str}\n==================="]

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
        vol = int(meta.get('regularMarketVolume', 0))
        
        report_lines.append(
            f"📈 【台股大盤概況】\n"
            f"• 收盤指數：{close:,.2f} ({pct:+.2f}%)\n"
            f"• 漲跌點數：{change:+,.2f} 點\n"
            f"• 成交量能：{int(vol / 100000000):,} 億\n"
        )
    except Exception:
        report_lines.append("📈 【台股大盤概況】\n• 數據讀取中...")

    report_lines.append("\n===================\n⭐ 【我的自選股追蹤】")
    user_watchlist = get_user_watchlist(user_id)
    user_alerts = get_user_alerts(user_id)

    if not user_watchlist:
        report_lines.append("目前自選清單是空的。\n💡 輸入「加 00981」即可新增自選！")
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
    return "Stock Bot & Supabase Database Edition is alive!"

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

    if "加" in user_text and 4 <= len(pure_code) <= 6:
        if pure_code not in user_watchlist:
            add_watchlist_db(user_id, pure_code)
            reply_text = f"✅ 新增自選成功：{pure_code}\n輸入「自選」即可檢視完整清單與策略。"
        else:
            reply_text = f"📌 {pure_code} 已經在您的自選股清單中囉！"

    elif "刪" in user_text and 4 <= len(pure_code) <= 6:
        if pure_code in user_watchlist:
            remove_watchlist_db(user_id, pure_code)
            reply_text = f"🗑️ 已從自選清單移除：{pure_code}"
        else:
            reply_text = f"❌ 找不到代號 {pure_code}"

    elif ("設" in user_text) or (" " in user_text and len(pure_code) >= 7):
        clean_text = user_text.replace("設定", "").replace("設", "").strip()
        parts = clean_text.split()
        target_code, target_price = None, None
        
        for p in parts:
            p_digits = "".join(filter(str.isdigit, p))
            if 4 <= len(p_digits) <= 6 and not target_code:
                target_code = p_digits
            else:
                try:
                    val = float(p)
                    if val > 10:
                        target_price = val
                except ValueError:
                    pass

        if target_code and target_price:
            if target_code not in user_watchlist:
                add_watchlist_db(user_id, target_code)
            set_alert_db(user_id, target_code, target_price)
            reply_text = f"🔔 到價通知設定成功！\n• 股票/ETF代號：{target_code}\n• 目標價格：{target_price}"
        else:
            reply_text = "❌ 格式錯誤！\n正確範例：\n• 設 00981 15\n• 00981 15"

    elif user_text in ["自選", "WATCHLIST"]:
        updated_watchlist = get_user_watchlist(user_id)
        updated_alerts = get_user_alerts(user_id)
        
        if not updated_watchlist:
            reply_text = (
                "📂 【我的自選股清單】\n"
                "===================\n"
                "目前清單是空的。\n\n"
                "💡 新增指令：加 00981\n"
                "💡 設定到價：00981 15"
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
                        strategy = "🔥【多方續強】帶量上攻，可沿 5 日線續抱。"
                    elif close > ma20 and pct <= 0:
                        strategy = "⚡【回測月線】多頭拉回，守穩支撐可分批低接。"
                    else:
                        strategy = "🛡️【保守觀望】等待打底完成再進場。"
                    
                    stock_block = (
                        f"\n{light_icon} 【 {code} 】 現價：{close:.2f} ({pct:+.2f}%)\n"
                        f"-------------------\n"
                        f"🛡️ 支撐：{support:.2f}  |  🎯 壓力：{resistance:.2f}\n"
                        f"📋 策略：\n{strategy}"
                    )
                    if alert_p:
                        stock_block += f"\n🔔 目標價：{alert_p}"
                    results.append(stock_block)
                else:
                    results.append(f"\n⚪ 【 {code} 】 行情讀取中...")
            reply_text = "\n".join(results)

    elif 4 <= len(pure_code) <= 6 and len(user_text) <= 7 and " " not in user_text:
        data = get_realtime_stock(pure_code)
        if data:
            close, high, low, vol, pct = data["close"], data["high"], data["low"], data["volume"], data["pct"]
            name = f"台股/ETF {pure_code}"
            industry = "上市櫃個股或 ETF"
            if pure_code in black_horse_database:
                name, industry = black_horse_database[pure_code]["name"], black_horse_database[pure_code]["industry"]
            elif pure_code in radar_database:
                name, industry = radar_database[pure_code]["name"], radar_database[pure_code]["industry"]

            reply_text = (
                f"📊 {pure_code} {name} ({industry})\n"
                f"===================\n"
                f"💰 現價：{close:.2f} ({pct:+.2f}%)\n"
                f"🔺 高/低：{high:.2f} / {low:.2f}\n"
                f"📦 量能：{int(vol / 1000):,} 張\n\n"
                f"🛡️ 支撐防守：{low * 0.99:.2f}  |  🎯 壓力目標：{high * 1.01:.2f}"
            )
        else:
            reply_text = f"❌ 查無代號 {pure_code} 的即時行情。"

    elif user_text_upper in ["MENU", "MANU", "選單", "幫助", "HELP"]:
        reply_text = (
            "🤖 蔡秉軒御用選股機器人\n"
            "===================\n"
            "• 輸入「盤前」➜ 美股與總經速覽\n"
            "• 輸入「盤後」➜ 大盤與自選戰報\n"
            "• 輸入「黑馬」➜ 高潛力成長股\n"
            "• 輸入「雷達」➜ 全市場強勢突破股\n"
            "• 輸入「自選」➜ 查看雲端自選股\n"
            "• 輸入「加 00981」➜ 新增自選\n"
            "• 輸入「刪 00981」➜ 刪除自選"
        )
    elif user_text in ["盤前", "早安", "MORNING"]:
        reply_text = generate_morning_brief()
    elif user_text in ["盤後", "收盤", "AFTERNOON"]:
        reply_text = generate_afternoon_brief(user_id)
    elif user_text == "雷達":
        remote_radar = fetch_latest_radars()
        radar_results = []
        if remote_radar and "stocks" in remote_radar:
            for stock in remote_radar["stocks"]:
                code, name, tag = stock.get('code'), stock.get('name', ''), stock.get('tag', '🚀 強勢突破')
                data = get_realtime_stock(code)
                price_str = f"現價 {data['close']:.1f} ({data['pct']:+.2f}%)" if data else "行情更新中"
                radar_results.append(f"• {code} {name} | {price_str}\n  └ {tag}")
            header_str = f"🎯 全市場強勢突破雷達 (更新於: {remote_radar.get('update_time', '')})\n-------------------\n"
        else:
            for code, info in radar_database.items():
                data = get_realtime_stock(code)
                if data:
                    radar_results.append(f"• {code} {info['name']}：現價 {data['close']:.1f} ({data['pct']:+.2f}%)")
            header_str = "🎯 技術面強勢雷達 (備援模式)\n-------------------\n"
        reply_text = header_str + ("\n\n".join(radar_results[:6]) if radar_results else "目前無符合標的。")
    elif user_text == "黑馬":
        remote_data = fetch_latest_black_horses()
        horse_results = []
        if remote_data and "stocks" in remote_data:
            for stock in remote_data["stocks"]:
                code, name, reason = stock.get('code'), stock.get('name', ''), stock.get('reason', '')
                data = get_realtime_stock(code)
                price_str = f"現價 {data['close']:.1f} ({data['pct']:+.2f}%)" if data else "行情更新中"
                horse_results.append(f"• {code} {name} | {price_str}\n  └ {reason}")
            header_str = f"🔥 潛力黑馬專區 (更新於: {remote_data.get('update_time', '')})\n-------------------\n"
        else:
            for code, info in black_horse_database.items():
                data = get_realtime_stock(code)
                price_str = f"現價 {data['close']:.1f} ({data['pct']:+.2f}%)" if data else "行情更新中"
                horse_results.append(f"• {code} {info['name']} | {price_str}\n  └ {info['reason']}")
            header_str = "🔥 潛力黑馬專區 (備援模式)\n-------------------\n"
        reply_text = header_str + "\n\n".join(horse_results)
    else:
        reply_text = "❌ 指令錯誤！請輸入「選單」查看功能。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
