# -*- coding: utf-8 -*- 
import os
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 從 Render 的 Environment Variables 讀取你的 LINE 金鑰
line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))


@app.route("/")
def home():
  return "Stock Bot is alive!"


@app.route("/callback", methods=["POST"])
def callback():
  # 取得 LINE 傳來的安全簽章
  signature = request.headers.get("X-Line-Signature", "")
  body = request.get_data(as_text=True)

  try:
    handler.handle(body, signature)
  except InvalidSignatureError:
    abort(400)

  return "OK"


# 當使用者在 LINE 傳訊息時會觸發這段
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
  user_text = event.message.text.strip()

  # 這裡就是你可以加入選股邏輯或 AI 生成回覆的地方
  # 目前我們讓它先簡單回覆你輸入的股票代號或測試文字
  reply_text = f"收到你的指令：{user_text}！選股機器人正在處理中..."

  line_bot_api.reply_message(
      event.reply_token, TextSendMessage(text=reply_text)
  )


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 10000))
  app.run(host="0.0.0.0", port=port)
