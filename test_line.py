from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import twstock
import requests

app = Flask(__name__)

# --- LINE API 設定 ---
LINE_CHANNEL_ACCESS_TOKEN = "ZiH9mF56Pl/ENzxZb7Iefpvd3eHMcwBRT4T1jwZIxwh9Bl4PtV8TaVUvpM8On2ROo5mdo+z7FUhp3Ugs2G2/PsYYbG1LR2gu3ykXvAWTZSjDg0wYs8daKJXCT/h8jfXfWuyZvNtsZ3S1sukbW9jWVAdB04t89/1O/w1cDnyilFU="
LINE_CHANNEL_SECRET = "05db4d1695f7791d4a1b8185b8817f40"

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 籌碼與行情抓取邏輯 ---
def get_institutional_investors(stock_id):
    """從證交所 API 抓取最新開盤日的三大法人買賣超資料 (張數)"""
    try:
        for i in range(10):
            target_date = datetime.now() - timedelta(days=i)
            date_str = target_date.strftime("%Y%m%d")
            
            url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date_str}&selectType=ALL&response=json"
            headers = {'User-Agent': 'Mozilla/5.0'}
            res = requests.get(url, headers=headers, timeout=5)
            data = res.json()
            
            if data.get('stat') == 'OK' and data.get('data'):
                display_date = f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:]}"
                for row in data.get('data', []):
                    if row[0].strip() == stock_id:
                        foreign = int(row[4].replace(',', '')) // 1000
                        investment_trust = int(row[7].replace(',', '')) // 1000
                        dealer = int(row[10].replace(',', '')) // 1000
                        total = int(row[11].replace(',', '')) // 1000
                        
                        return {
                            "date": display_date,
                            "foreign": foreign,
                            "trust": investment_trust,
                            "dealer": dealer,
                            "total": total
                        }
    except Exception as e:
        print(f"籌碼抓取失敗: {e}")
    return None

def get_taiwan_stock_info(stock_id):
    """獲取台股即時行情 + 三大法人籌碼數據"""
    try:
        stock = twstock.Stock(stock_id)
        fetch_data = stock.fetch_31()
        if not fetch_data:
            return f"❌ 找不到股票代碼：{stock_id}\n請確認輸入是否為正確台股代碼（例如：2330）。"
        
        realtime = twstock.realtime.get(stock_id)
        
        if realtime.get('success'):
            stock_name = realtime['info']['name']
            latest_price = float(realtime['realtime']['latest_trade_price']) if realtime['realtime']['latest_trade_price'] != '-' else float(fetch_data[-1].close)
            prev_close = float(fetch_data[-2].close)
        else:
            stock_name = stock_id
            latest_price = float(fetch_data[-1].close)
            prev_close = float(fetch_data[-2].close)
        
        change = round(latest_price - prev_close, 2)
        pct_change = round((change / prev_close) * 100, 2)
        sign = "+" if change > 0 else ""
        
        recent_5_closes = [d.close for d in fetch_data[-5:]]
        ma5 = round(sum(recent_5_closes) / len(recent_5_closes), 2)
        
        chip_info = get_institutional_investors(stock_id)
        if chip_info:
            f_sign = "+" if chip_info['foreign'] > 0 else ""
            t_sign = "+" if chip_info['trust'] > 0 else ""
            d_sign = "+" if chip_info['dealer'] > 0 else ""
            tot_sign = "+" if chip_info['total'] > 0 else ""
            
            chip_text = (
                f"🏛️ 近日三大法人動向 ({chip_info['date']})：\n"
                f"  • 外資：{f_sign}{chip_info['foreign']:,} 張\n"
                f"  • 投信：{t_sign}{chip_info['trust']:,} 張\n"
                f"  • 自營：{d_sign}{chip_info['dealer']:,} 張\n"
                f"  🔥 法人合計：{tot_sign}{chip_info['total']:,} 張"
            )
        else:
            chip_text = "🏛️ 三大法人：(無法讀取證交所籌碼資料)"
        
        report = (
            f"📊【台股即時行情與籌碼 - {stock_name}({stock_id})】\n"
            f"----------------------------\n"
            f"💰 最新股價：{latest_price} ({sign}{change} / {sign}{pct_change}%)\n"
            f"📈 5日均價 (MA5)：{ma5}\n"
            f"🛡️ 技術型態：{'🔥 站上 5 日線 (短線偏強)' if latest_price >= ma5 else '⚠️ 低於 5 日線 (短線偏弱)'}\n"
            f"----------------------------\n"
            f"{chip_text}"
        )
        return report

    except Exception as e:
        return f"❌ 抓取股票資料發生錯誤：{e}"

# --- LINE Webhook 路由 ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    # 檢查使用者是否輸入數字（預設為台股代碼）
    if user_msg.isdigit() and (len(user_msg) == 4 or len(user_msg) == 5):
        reply_text = get_taiwan_stock_info(user_msg)
    elif user_msg.lower() in ["hi", "hello", "你好", "選股", "說明"]:
        reply_text = "👋 哥你好！我是選股 Bot！\n\n請直接在聊天室輸入 4 位數台股代碼（例如：`2330` 或 `2603`），我會立刻為您分析最新股價與三大法人籌碼！"
    else:
        reply_text = "💡 請輸入正確的 4 位數台股代碼（例如：2330）來查詢行情與籌碼喔！"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)