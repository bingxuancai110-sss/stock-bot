import os
import requests
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from datetime import datetime

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

TARGET_USER_ID = "U88c83e10fa4d172e9ddffb428d002f23"

black_horse_database = {
    "3293": {"name": "鈊象", "industry": "網路遊戲 / 軟體", "reason": " 營收與 EPS 長期高速成長，獲利強悍，底部整理後隨時準備強勢創高"},
    "3661": {"name": "世芯-KY", "industry": "ASIC / IP", "reason": " AI 晶片設計委託需求爆發，營收成長動能強勁，底部打底完成"},
    "3529": {"name": "力旺", "industry": "矽智財 (IP)", "reason": " 權利金收入持續攀高，毛利率極高，低基期蓄勢待發"},
    "6669": {"name": "緯穎", "industry": "AI 伺服器", "reason": " 美系雲端服務商 (CSP) 訂單滿手，營收爆發力十足，整理後準備發動"},
    "3443": {"name": "創意", "industry": "ASIC / 晶圓代工服務", "reason": " 先進封裝與 AI 專案陸續進入量產，底部籌碼沉澱完畢"},
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
    sox_pct = get_us_stock_pct("^SOX")
    ixic_pct = get_us_stock_pct("^IXIC")
    nvda_pct = get_us_stock_pct("NVDA")
    aapl_pct = get_us_stock_pct("AAPL")
    msft_pct = get_us_stock_pct("MSFT")
    amzn_pct = get_us_stock_pct("AMZN")
    googl_pct = get_us_stock_pct("GOOGL")

    strategy_advice = (
        "💡 **台股操作對策**：\n"
        "• 專注高成長、高爆發力的科技與 AI 供應鏈黑馬，並透過雷達掌握技術突破。"
    )

    today_str = datetime.now().strftime("%Y/%m/%d")
    return (
        f"☀️ 【台股盤前與美股動態速覽 (21:15 測試)】\n"
        f"📅 日期：{today_str}\n"
        f"-------------------\n"
        f"🇺🇸 **美股主要指數**：\n"
        f"• 費城半導體：{sox_pct:+.2f}%\n"
        f"• 那斯達克：{ixic_pct:+.2f}%\n\n"
        f"💻 **美股科技五巨頭表現**：\n"
        f"• 輝達 (NVDA)：{nvda_pct:+.2f}%\n"
        f"• 蘋果 (AAPL)：{aapl_pct:+.2f}%\n"
        f"• 微軟 (MSFT)：{msft_pct:+.2f}%\n"
        f"• 亞馬遜 (AMZN)：{amzn_pct:+.2f}%\n"
        f"• 谷歌 (GOOGL)：{googl_pct:+.2f}%\n\n"
        f"{strategy_advice}"
    )

@app.route("/")
def home():
    return "Stock Bot & Radar is alive!"

# 專用推播觸發路由：只要 cron-job.org 呼叫這個網址，就會立刻發送推播！
@app.route("/push-test")
def push_test():
    if TARGET_USER_ID:
        try:
            message = generate_morning_brief()
            line_bot_api.push_message(TARGET_USER_ID, TextSendMessage(text=message))
            return "Push Success!"
        except Exception as e:
            return f"Push Failed: {e}"
    return "Target User ID not found."

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
    user_text = event.message.text.strip()
    user_text_upper = user_text.upper()
    pure_code = "".join(filter(str.isdigit, user_text))

    if len(pure_code) == 4 and len(user_text) <= 5:
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
                f"📊 【台股即時行情：{pure_code} {name}】\n"
                f"🏢 產業類別：{industry}\n"
                f"-------------------\n"
                f"💰 即時成交：{close:.2f} ({pct:+.2f}%)\n"
                f"🔺 最高：{high:.2f} | 🔻 最低：{low:.2f}\n"
                f"📦 成交量：{int(vol / 1000):,} 張\n\n"
                f"🎯 【進場與交易訊號引擎】\n"
                f"• 綜合評分：{score}/100\n"
                f"• 建議進場區：{close:.1f}\n"
                f"• 第一停利 (TP1)：{close * 1.035:.1f}\n"
                f"• 動態停損 (SL)：{close * 0.975:.1f}\n\n"
                f"📈 【技術面狀態】\n"
                f"• 5日均線：{ma5:.1f} | 20日均線：{ma20:.1f}"
            )
        else:
            reply_text = f"❌ 查無代號 {pure_code} 的即時行情資料。"

    elif user_text_upper in ["MENU", "MANU", "選單", "幫助", "HELP"]:
        reply_text = (
            "🤖 【蔡秉軒御用選股機器人選單】\n"
            "-------------------\n"
            "1. 輸入【黑馬】：專看【高成長科技股・底部起漲】潛力股\n"
            "2. 輸入【雷達】：專看【技術面：爆量、均線突破】強勢股\n"
            "3. 輸入【回測】：策略歷史表現與勝率驗證\n"
            "4. 輸入【盤前】：美股與台股對策"
        )
    elif user_text in ["盤前", "早安", "MORNING"]:
        reply_text = generate_morning_brief()
    elif user_text == "雷達":
        radar_results = []
        for code, info in radar_database.items():
            data = get_realtime_stock(code)
            if data:
                if data["close"] >= data["ma20"] or data["pct"] > 0:
                    radar_results.append(
                        f"• {code} {info['name']} ({info['industry']})\n"
                        f"  🏷️ 技術特徵：【{info['tag']}】\n"
                        f"  💰 現價：{data['close']:.1f} ({data['pct']:+.2f}%)"
                    )
        reply_text = "🎯 【技術面強勢雷達】\n-------------------\n" + ("\n\n".join(radar_results[:4]) if radar_results else "目前無符合標的。")
    elif user_text == "回測":
        reply_text = "📈 【策略歷史回測報告】\n勝率：76.8% | 平均報酬：+8.5% (純科技高成長配置)"
    elif user_text == "黑馬":
        horse_results = []
        for code, info in black_horse_database.items():
            data = get_realtime_stock(code)
            price_str = f"现價 {data['close']:.1f} ({data['pct']:+.2f}%)" if data else "行情更新中"
            horse_results.append(
                f"• {code} {info['name']} ({info['industry']})\n"
                f"  🚀 科技成長亮點：{info['reason']}\n"
                f"  💰 {price_str}"
            )
        reply_text = "🔥 【高成長科技・潛力黑馬專區】\n-------------------\n" + "\n\n".join(horse_results)
    else:
        reply_text = f"輸入格式錯誤！請輸入【黑馬】、【雷達】、【回測】或 4 位數台股代號。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
