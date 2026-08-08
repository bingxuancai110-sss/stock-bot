from linebot.exceptions import InvalidSignatureError 
from linebot.models import MessageEvent, TextSendMessage, TextMessage 
 
app = Flask(__name__) 
 
line_bot_api = LineBotApi('你的Channel Access Token') 
handler = WebhookHandler('你的Channel Secret') 
 
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
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"你說了: {user_message}")) 
 
if __name__ == '__main__': 
    app.run() 
