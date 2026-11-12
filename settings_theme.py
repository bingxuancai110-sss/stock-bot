"""Small, user-bound theme writes with explicit saved/unknown acknowledgments."""
from flask import Blueprint, jsonify, request


class ThemeStore:
    def __init__(self, connect, release, themes):
        self.connect, self.release, self.themes = connect, release, themes

    def _query(self, uid, selected=None):
        uid = str(uid or "").strip()
        if not uid or (selected is not None and selected not in self.themes):
            raise ValueError("帳號或配色無效")
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout=8000")
                cur.execute("SET LOCAL lock_timeout=3000")
                if selected is None:
                    cur.execute("SELECT ui_theme FROM user_profile WHERE user_id=%s", (uid,))
                else:
                    cur.execute("""INSERT INTO user_profile(user_id,ui_theme,updated_at) VALUES(%s,%s,NOW())
                        ON CONFLICT(user_id) DO UPDATE SET ui_theme=EXCLUDED.ui_theme,updated_at=NOW()
                        RETURNING ui_theme""", (uid, selected))
                row = cur.fetchone()
                if selected is not None and not row:
                    raise ValueError("資料庫未回傳已儲存配色")
                theme = str(row[0] or "mist").strip() if row else "mist"
                if theme not in self.themes or (selected is not None and theme != selected):
                    raise ValueError("資料庫未確認指定配色")
            conn.commit()
            return theme
        except Exception:
            conn.rollback()
            raise
        finally:
            self.release(conn)

    def read(self, uid):
        return self._query(uid)

    def save(self, uid, selected):
        return self._query(uid, selected)


def create_blueprint(*, web_login_required, valid_csrf, store_factory, themes):
    bp = Blueprint("settings_theme", __name__)

    def reply(payload, status=200):
        response = jsonify(payload)
        response.status_code = status
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.route("/web/api/settings/theme", methods=["GET", "POST"])
    @web_login_required
    def theme(uid):
        selected = None
        if request.method == "POST":
            if not valid_csrf(uid):
                return reply({"ok":False, "state":"rejected", "message":"安全驗證已過期，請重新整理後再儲存。"}, 403)
            selected = str(request.form.get("ui_theme") or "").strip()
            if selected not in themes:
                return reply({"ok":False, "state":"rejected", "message":"請選擇有效的配色。"}, 400)
        try:
            store = store_factory()
            saved = store.save(uid, selected) if selected is not None else store.read(uid)
            return reply({"ok":True, "state":"saved" if selected is not None else "current",
                          "theme":saved, "label":themes[saved]["label"]})
        except Exception as exc:
            print("配色設定結果未確認：" + type(exc).__name__, flush=True)
            return reply({"ok":False, "state":"unknown", "message":"暫時無法確認已保存的配色，請稍後確認。"}, 503)
    return bp
