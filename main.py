# -*- coding: utf-8 -*- 
from flask import Flask, request, abort 
from linebot import LineBotApi, WebhookHandler 
from linebot.exceptions import InvalidSignatureError 
from linebot.models import MessageEvent, TextSendMessage, TextMessage 
 
app = Flask(__name__) 
 
line_bot_api = LineBotApi('TOKEN_PLACEHOLDER') 
handler = WebhookHandler('SECRET_PLACEHOLDER') 
 
@app.route("/callback", methods=['POST']) 
def callback(): 
    signature = request.headers['X-Line-Signature'] 
    body = request.get_data(as_text=True) 
    try: 
        handler.handle(body, signature) 
    except InvalidSignatureError: 
        abort(400) 
    return 'OK' 
 
@handler.add(MessageEvent, message=TextMessage) 
def handle_message(event): 
    user_message = event.message.text 
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"Echo: {user_message}")) 
 
if __name__ == '__main__': 
    app.run() 
