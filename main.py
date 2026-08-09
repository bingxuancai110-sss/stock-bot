import os
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import yfinance as yf

app = Flask(__name__)

# 從 Render 的 Environment Variables 讀取你的 LINE 金鑰
line_bot_api = LineBotApi(os.environ.get("CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("CHANNEL_SECRET"))


@app.route("/")
def home():
  return "Stock Bot is alive!"


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
  user_text = event.message.text.strip().upper()

  # 假設使用者輸入台股代號（例如 2330），我們自動補上台股代號後綴 .TW
  # 支援純數字或是直接輸入 2330.TW
  stock_code = user_text
  if stock_code.isdigit():
    stock_code = f"{stock_code}.TW"

  try:
    # 使用 yfinance 抓取即時資料
    stock = yf.Ticker(stock_code)
    df = stock.history(period="1d")

    if df.empty:
      # 如果 .TW 找不到，試看看 .TWO (櫃買中心)
      stock_code = (
          f"{user_text}.TWO" if not user_text.endswith(".TWO") else user_text
      )
      stock = yf.Ticker(stock_code)
      df = stock.history(period="1d")

    if not df.empty:
      latest = df.iloc[-1]
      close_price = latest["Close"]
      open_price = latest["Open"]
      high_price = latest["High"]
      low_price = latest["Low"]
      volume = latest["Volume"]

      # 計算漲跌幅 (相對於昨日收盤或今日開盤，這裡簡單計算當日變動)
      price_change = close_price - open_price
      pct_change = (price_change / open_price) * 100

      reply_text = (
          f"📊 【台股即時行情：{stock_code}】\n"
          f"-------------------\n"
          f"💰 最新成交價：{close_price:.2f}\n"
          f"📈 漲跌幅：{pct_change:+.2f}%\n"
          f"🔺 最高：{high_price:.2f}\n"
          f"🔻 最低：{low_price:.2f}\n"
          f"📦 成交量：{int(volume):,}"
      )
    else:
      reply_text = f"找不到代號「{user_text}」的台股資料，請確認代號是否正確哦！"

  except Exception as e:
    reply_text = f"查詢時發生錯誤，請稍後再試。"

  line_bot_api.reply_message(
      event.reply_token, TextSendMessage(text=reply_text)
  )


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)


