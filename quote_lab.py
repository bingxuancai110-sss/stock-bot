"""Owner-only, browser-direct Fugle trial; the server never receives market-data keys."""
from functools import lru_cache
import hmac
import os
import re
from pathlib import Path

from flask import Blueprint, abort, make_response, jsonify, request
import json

ASSETS = Path(__file__).with_name('quote_lab_assets')


def allowed_user(uid, get_db_connection, release_db_connection):
    configured_uid = os.environ.get('FUGLE_TEST_USER_ID', '').strip()
    if configured_uid:
        return hmac.compare_digest(str(uid).encode(), configured_uid.encode())
    email = os.environ.get('FUGLE_TEST_USER_EMAIL', '').strip().lower()
    if not email:
        return False
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""SELECT DISTINCT user_id FROM user_auth_identities
                           WHERE provider='google' AND LOWER(email)=%s LIMIT 2""", (email,))
            rows = cur.fetchall()
        return len(rows) == 1 and hmac.compare_digest(str(uid).encode(), str(rows[0][0]).encode())
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn is not None:
            release_db_connection(conn)


@lru_cache(maxsize=1)
def page_markup():
    css = (ASSETS / 'quote-lab.css').read_text(encoding='utf-8')
    return '<style>' + css + '</style>' + '''
<main id="quote-lab" class="quote-lab">
  <a href="/web/portfolio" class="ql-back">← 返回今日</a>
  <header class="ql-hero"><span class="ql-eyebrow">PRIVATE · LIVE QUOTE LAB</span>
    <h1>富果即時行情</h1><p>你的帳號專用；啟用後同步更新今日與持股頁。</p>
    <span class="ql-tag">5 檔串流 · 其他持股定時查詢</span></header>
  <section class="ql-panel" aria-labelledby="ql-connect-title">
    <h2 id="ql-connect-title">連接你的行情</h2>
    <p class="ql-muted">先到 <a href="https://www.fugle.tw/account/register?referrer=api" target="_blank" rel="noopener noreferrer">富果註冊</a>並依<a href="https://developer.fugle.tw/docs/key/" target="_blank" rel="noopener noreferrer">官方說明申請行情 API Key</a>。不需要提供證券帳戶密碼。</p>
    <label for="ql-key">行情 API Key</label>
    <input id="ql-key" type="password" autocomplete="off" spellcheck="false" autocapitalize="none" placeholder="只在這個頁面記憶體使用" aria-describedby="ql-key-help">
    <p id="ql-key-help" class="ql-muted">金鑰直接送往富果，不傳給 BOT、不儲存。僅留在本分頁記憶體；切換今日／持股沿用，重新整理或停止後須重新輸入。</p>
    <label class="ql-follow"><input id="ql-follow" type="checkbox" checked> 自動追蹤我的 BOT 持股</label>
    <label for="ql-symbols">串流優先代碼（最多 5 檔）</label>
    <input id="ql-symbols" type="text" value="2308, 8996, 2383, 2368, 6442" spellcheck="false" autocomplete="off" aria-describedby="ql-symbol-help">
    <p id="ql-symbol-help" class="ql-muted">追蹤模式會略過已不在 BOT 持股的代碼，保留現有串流名額，缺額自動從其他持股補上。其餘持股約每 15 秒查詢；持股較多時自動放慢。取消追蹤可保留原本手動測試模式。</p>
    <div class="ql-controls"><button type="button" id="ql-start">啟用富果行情</button><button type="button" id="ql-stop" disabled>停止並清除金鑰</button></div>
  </section>
  <section class="ql-panel" aria-labelledby="ql-status-title">
    <div class="ql-status-head"><h2 id="ql-status-title">連線狀態</h2><span id="ql-state" class="ql-state" role="status">尚未連線</span></div>
    <p id="ql-message" role="status">輸入金鑰後開始。只建立一條連線，不會自動訂閱付費方案。</p>
    <div class="ql-stats"><div><small>本次新成交</small><b id="ql-events">0</b></div><div><small>自動重連次數</small><b id="ql-retries">0</b></div><div><small>串流檔數</small><b id="ql-count">0 / 5</b></div></div>
    <p id="ql-holdings-status" class="ql-muted" role="status">開始後每 15 秒確認 BOT 持股；券商交易尚未更新到 BOT 時無法得知。</p>
    <p id="ql-rest-status" class="ql-muted" role="status">定時查詢尚未開始。</p>
  </section>
  <section class="ql-panel" aria-labelledby="ql-quotes-title"><h2 id="ql-quotes-title">最新成交</h2>
    <p class="ql-muted">紅色代表比上次接收報價高，綠色代表較低；這裡的變動不是當日漲跌幅。每檔會標明串流或定時查詢。</p>
    <div id="ql-quotes"><p class="ql-empty">尚未開始，行情會顯示在這裡。</p></div>
  </section>
  <aside class="ql-note"><b>如何看速度</b><p>行情時間來自富果，接收時間來自你的裝置。「觀測時間差」受裝置時鐘與傳輸影響，不能當作完整延遲保證。沒有新成交時價格不會跳動；收盤後通常只有最近快照，請在交易時段驗證速度。</p>
  <p>全數賣出會在下次 BOT 持股同步後移除，部分減碼仍保留。同頁保持連線不用重輸金鑰；切換今日／持股沿用同一連線；切到背景暫停，回前景恢復；重新整理或關閉才清除。定時查詢限制本頁最多 40 次／分鐘，另留額度餘裕；同金鑰其他程式的用量仍會影響配額。整股成交與試撮分開標示。斷線會保留最後畫面，但不會標示為連線中；本頁不保存行情歷史。<a href="https://developer.fugle.tw/docs/pricing/" target="_blank" rel="noopener noreferrer">查看官方方案與限制 ↗</a></p></aside>
</main>'''


def create_blueprint(*, web_login_required, render_page, get_db_connection, release_db_connection,
                     csrf_token=None, valid_csrf=None, apply_quotes=None):
    bp = Blueprint('quote_lab', __name__)

    def check_owner(uid):
        if not (os.environ.get('FUGLE_TEST_USER_ID', '').strip() or
                os.environ.get('FUGLE_TEST_USER_EMAIL', '').strip()):
            abort(404)
        if not allowed_user(uid, get_db_connection, release_db_connection):
            abort(403)

    def private(response):
        response.headers['Cache-Control'] = 'no-store, private, max-age=0'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @bp.route('/web/quote-lab', methods=['GET'])
    @web_login_required
    def page(uid):
        check_owner(uid)
        return private(make_response(render_page('個人行情測試', page_markup(), 'portfolio')))

    @bp.route('/web/quote-lab/holdings', methods=['GET'])
    @web_login_required
    def holdings(uid):
        check_owner(uid)
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("""SELECT p.code, MAX(s.name)
                               FROM positions p LEFT JOIN stock_info s ON s.code=p.code
                               WHERE p.user_id=%s AND COALESCE(p.shares,0)>0
                               GROUP BY p.code HAVING SUM(p.shares)>0 ORDER BY MIN(p.id)""", (str(uid),))
                rows = cur.fetchall()
            output = []
            for code, name in rows:
                code = str(code or '').strip().upper()
                if re.fullmatch(r'\d{4,6}[A-Z]?', code):
                    output.append({'code': code, 'name': str(name or code)})
            return private(jsonify(ok=True, holdings=output, unsupported_count=len(rows)-len(output)))
        except Exception:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return private(make_response(jsonify(ok=False, error='持股同步暫時失敗，保留原本清單。'), 503))
        finally:
            if conn is not None:
                release_db_connection(conn)

    @bp.route('/web/quote-lab/app-config', methods=['GET'])
    @web_login_required
    def app_config(uid):
        check_owner(uid)
        if not callable(csrf_token):
            abort(503)
        return private(jsonify(ok=True, panel_html=page_markup(), csrf_token=csrf_token(uid)))

    @bp.route('/web/quote-lab/apply', methods=['POST'])
    @web_login_required
    def apply(uid):
        check_owner(uid)
        if request.content_length and request.content_length > 65536:
            abort(413)
        if not callable(valid_csrf) or not valid_csrf(uid):
            abort(403)
        if not callable(apply_quotes):
            abort(503)
        try:
            raw = json.loads(request.form.get('quotes', 'null'))
            market = json.loads(request.form['market']) if 'market' in request.form else None
            result = apply_quotes(uid, raw, market_raw=market) if 'market' in request.form else apply_quotes(uid, raw)
            return private(jsonify(result))
        except (ValueError, TypeError):
            return private(make_response(jsonify(ok=False, error='行情驗證失敗，請等待新的正式成交。'), 400))
        except Exception:
            return private(make_response(jsonify(ok=False, error='行情計算暫時失敗，保留最後有效資料。'), 503))

    return bp
