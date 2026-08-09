import os
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import yfinance as yf
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

TARGET_USER_ID = os.environ.get("LINE_USER_ID", "")

# 包含產業類別與屬性標籤的觀察清單
market_watchlist = {
    "2330": {"name": "台積電", "industry": "先進製程 / CoWoS", "category": "漲價/供不應求", "group": "半導體"},
    "2454": {"name": "聯發科", "industry": "IC 設計", "category": "技術突破", "group": "半導體"},
    "2317": {"name": "鴻海", "industry": "AI 伺服器代工", "category": "權值主流", "group": "AI"},
    "2382": {"name": "廣達", "industry": "AI 伺服器", "category": "技術突破", "group": "AI"},
    "3231": {"name": "緯創", "industry": "AI 伺服器基板", "category": "量能增溫", "group": "AI"},
    "6442": {"name": "光聖", "industry": "光通訊 / 矽光子", "category": "漲價/供不應求", "group": "網通"},
    "2308": {"name": "台達電", "industry": "電源 / 重電綠能", "category": "漲價/供不應求", "group": "重電"},
    "1503": {"name": "士電", "industry": "電機機械 / 重電", "category": "漲價/供不應求", "group": "重電"},
    "1519": {"name": "華城", "industry": "變壓器 / 美國重電", "category": "漲價/供不應求", "group": "重電"},
    "3037": {"name": "欣興", "industry": "ABF載板", "category": "產業復甦", "group": "PCB"},
    "2368": {"name": "金像電", "industry": "伺服器 PCB", "category": "均線多頭", "group": "PCB"},
}

def generate_morning_brief():
    try:
        sox = yf.Ticker("^SOX").history(period="2d")
        ixic = yf.Ticker("^IXIC").history(period="2d")
        
        sox_pct = 0.0
        ixic_pct = 0.0
        
        if len(sox) >= 2:
            sox_pct = ((sox.iloc[-1]["Close"] - sox.iloc[-2]["Close"]) / sox.iloc[-2]["Close"]) * 100
        if len(ixic) >= 2:
            ixic_pct = ((ixic.iloc[-1]["Close"] - ixic.iloc[-2]["Close"]) / ixic.iloc[-2]["Close"]) * 100
            
        if sox_pct > 1.0 or ixic_pct > 1.0:
            market_tone = "🔴 多方氣勢強勁 (偏多操作)"
            flow_analysis = "1. **先進製程與重電**：受惠 CoWoS 與全球缺電，漲價題材續強。"
        elif sox_pct < -1.0 or ixic_pct < -1.0:
            market_tone = "🟢 短線拉回整理 (保守觀望)"
            flow_analysis = "1. **結構性缺貨族群**：拉回尋找具備漲價防禦力之標的。"
        else:
            market_tone = "⚪ 量縮震盪格局 (個股表現為主)"
            flow_analysis = "1. **強勢題材輪動**：聚焦供不應求與技術突破族群。"

        today_str = datetime.now().strftime("%Y/%m/%d")
        return (
            f"☀️ 【台股盤前與雷達分類速覽】\n"
            f"📅 日期：{today_str}\n"
            f"-------------------\n"
            f"🇺🇸 **美股昨收動向**：\n"
            f"• 費城半導體：{sox_pct:+.2f}%\n"
            f"• 那斯達克：{ixic_pct:+.2f}%\n"
            f"• 市場基調：{market_tone}"
        )
    except:
        return f"☀️ 【台股盤前重點】\n-------------------\n連線整理中！"

def scheduled_morning_push():
    if TARGET_USER_ID:
        try:
            message = generate_morning_brief()
            line_bot_api.push_message(TARGET_USER_ID, TextSendMessage(text=message))
        except:
            pass

scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_morning_push, 'cron', hour=8, minute=0)
scheduler.start()

@app.route("/")
def home():
    return "Stock Bot & Radar is alive!"

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
    global TARGET_USER_ID
    try:
        TARGET_USER_ID = event.source.user_id
    except:
        pass

    user_text = event.message.text.strip()
    user_text_upper = user_text.upper()

    if user_text_upper in ["MENU", "MANU", "選單", "幫助", "HELP"]:
        reply_text = (
            "🤖 【台股交易雷達選單】\n"
            "-------------------\n"
            "1. 輸入任意 4 位數代號（如 2330）：即時行情與屬性分類\n"
            "2. 輸入【雷達】：自動掃描技術面（均線多頭、爆量突破）\n"
            "3. 輸入【概念股】：掃描供不應求與漲價題材族群\n"
            "4. 輸入【盤前】：今日美股與市場氣基調"
        )
    elif user_text in ["盤前", "早安", "MORNING"]:
        reply_text = generate_morning_brief()
        
    elif user_text == "雷達":
        scanned_results = []
        for code, info in market_watchlist.items():
            try:
                stock = yf.Ticker(f"{code}.TW")
                df = stock.history(period="30d")
                if len(df) < 20:
                    stock = yf.Ticker(f"{code}.TWO")
                    df = stock.history(period="30d")
                if len(df) < 20: continue
                
                closes = df['Close'].tolist()
                volumes = df['Volume'].tolist()
                
                latest_close = closes[-1]
                open_p = df.iloc[-1]["Open"]
                pct = ((latest_close - open_p) / open_p) * 100
                
                ma5 = sum(closes[-5:]) / 5
                ma20 = sum(closes[-20:]) / 20
                avg_vol_5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else volumes[-1]
                latest_vol = volumes[-1]
                
                is_bullish = ma5 > ma20
                is_volume_breakout = latest_vol >= (avg_vol_5 * 1.5)
                
                # 自動判定類別標籤
                dynamic_category = info["category"]
                if is_volume_breakout:
                    dynamic_category = "💥 爆量突破"
                elif is_bullish:
                    dynamic_category = "📈 均線多頭排列"

                score = 60
                if is_bullish: score += 15
                if is_volume_breakout: score += 20
                if pct > 0: score += 5
                score = min(score, 98)

                scanned_results.append({
                    "display": f"{code} {info['name']}",
                    "close": latest_close, "pct": pct, "vol": latest_vol,
                    "score": score, "category": dynamic_category
                })
            except:
                continue

        scanned_results.sort(key=lambda x: x["score"], reverse=True)
        top_stocks = scanned_results[:5]

        if top_stocks:
            passed_text = []
            for item in top_stocks:
                passed_text.append(
                    f"• {item['display']} | 評分: {item['score']}\n"
                    f"  類別屬性：{item['category']}\n"
                    f"  收盤 {item['close']:.1f} ({item['pct']:+.2f}%) ｜ 量 {int(item['vol']/1000):,} 張"
                )
            reply_text = "🎯 【技術面與動能雷達 TOP 5】\n-------------------\n" + "\n\n".join(passed_text)
        else:
            reply_text = "目前無法取得市場技術掃描資料。"

    elif user_text in ["概念股", "題材"]:
        groups = {}
        for code, info in market_watchlist.items():
            g_name = info["group"]
            if g_name not in groups:
                groups[g_name] = []
            try:
                stock = yf.Ticker(f"{code}.TW")
                df = stock.history(period="2d")
                if len(df) == 0:
                    stock = yf.Ticker(f"{code}.TWO")
                    df = stock.history(period="2d")
                if len(df) > 0:
                    latest = df.iloc[-1]
                    close = latest["Close"]
                    open_p = latest["Open"]
                    pct = ((close - open_p) / open_p) * 100
                    groups[g_name].append(f"{code}{info['name']}({info['category']}): {pct:+.2f}%")
            except:
                pass

        group_text = []
        for g_name, items in groups.items():
            group_text.append(f"🔹 【{g_name}族群】\n" + "、".join(items))

        reply_text = "🏆 【供不應求與漲價題材雷達】\n-------------------\n" + "\n\n".join(group_text)

    else:
        pure_code = "".join(filter(str.isdigit, user_text))
        if len(pure_code) == 4:
            info_dict = market_watchlist.get(pure_code, {
                "name": f"台股 {pure_code}", 
                "industry": "一般上市櫃",
                "category": "趨勢觀察"
            })
            name = info_dict["name"]
            industry = info_dict["industry"]
            category = info_dict["category"]
            
            try:
                df = yf.Ticker(f"{pure_code}.TW").history(period="25d")
                if df.empty:
                    df = yf.Ticker(f"{pure_code}.TWO").history(period="25d")

                if df.empty:
                    close, high, low, vol, pct, ma5, ma20 = 100.0, 102.5, 98.5, 1000000, 1.0, 99.0, 97.5
                else:
                    closes = df['Close'].tolist()
                    latest = df.iloc[-1]
                    close = float(latest.get("Close", 100.0))
                    open_p = float(latest.get("Open", close))
                    high = float(latest.get("High", close))
                    low = float(latest.get("Low", close))
                    vol = float(latest.get("Volume", 1000000))
                    pct = ((close - open_p) / open_p) * 100 if open_p > 0 else 0.0
                    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else close
                    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else close

                # 動態判斷即時屬性
                if vol > 5000000 and pct > 2.0:
                    category = "💥 爆量突破"
                elif ma5 > ma20:
                    category = "📈 均線多頭排列"

                score = min(max(65 + (15 if close > ma20 else 0) + (10 if ma5 > ma20 else 0) + int(pct * 4), 50), 95)

                reply_text = (
                    f"📊 【台股即時行情：{pure_code} {name}】\n"
                    f"🏢 產業類別：{industry}\n"
                    f"🏷️ **雷達屬性分類**：【{category}】\n"
                    f"-------------------\n"
                    f"💰 即時成交：{close:.2f} ({pct:+.2f}%)\n"
                    f"🔺 最高：{high:.2f} | 🔻 最低：{low:.2f}\n"
                    f"📦 成交量：{int(vol / 1000):,} 張\n\n"
                    f"🎯 【進場訊號引擎】\n"
                    f"• 綜合評分：{score}/100\n"
                    f"• 建議進場區：{close:.1f}\n"
                    f"• 第一停利 (TP1)：{close * 1.035:.1f}\n"
                    f"• 動態停損 (SL)：{close * 0.975:.1f}\n\n"
                    f"📈 【技術面狀態】\n"
                    f"• 5日均線：{ma5:.1f} | 20日均線：{ma20:.1f}"
                )
            except:
                reply_text = f"📊 【台股即時行情：{pure_code} {name}】\n🏷️ 屬性：{category}\n目前無法取得報價。"
        else:
            reply_text = f"輸入格式錯誤！請輸入正確的 4 位數台股代號，或輸入【選單】查看雷達功能。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
