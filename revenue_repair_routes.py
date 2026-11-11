"""Authenticated machine repair endpoints. No user portfolios or trading writes."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Blueprint, abort, jsonify, request
from werkzeug.exceptions import HTTPException
from revenue_repair import AUDIENCE, WORKFLOW, RepairStore
from taiex_cron_auth import authorized


def create_blueprint(connect, release, invalidate, verify):
    bp = Blueprint("revenue_repair", __name__)

    @bp.route("/cron/revenue-repair", methods=["GET", "POST"])
    def repair():
        if not authorized(request.headers, None, None, audience=AUDIENCE, workflow=WORKFLOW):
            abort(403)
        day = datetime.now(ZoneInfo("Asia/Taipei")).date().replace(day=1) - timedelta(days=1)
        latest = day.year * 100 + day.month
        store = RepairStore(connect, release)
        try:
            if request.method == "GET":
                return jsonify({"latest":latest, "periods":store.plan(latest)})
            if request.content_length is None or request.content_length > 2 * 1024 * 1024:
                abort(413)
            body = request.get_json()
            if not isinstance(body, dict) or not isinstance(body.get("markets"), dict):
                abort(422)
            counts = body["markets"]
            if set(counts) != {"sii", "otc", "rotc"} or any(not isinstance(n, int) or n < 1 for n in counts.values()):
                return jsonify({"ok":False,"error":"三個市場資料未齊備"}), 422
            data = body.get("data")
            if not isinstance(data, dict) or sum(counts.values()) != len(data):
                return jsonify({"ok":False,"error":"市場筆數不一致"}), 422
            result = store.apply(body.get("period"), latest, data)
            invalidate()
            result.update(ok=True, verification=verify(data) if int(body.get("period")) == latest else {})
            print("營收欄位更正已保存：" + str(result), flush=True)
            return jsonify(result)
        except HTTPException:
            raise
        except ValueError as exc:
            return jsonify({"ok":False, "error":str(exc)}), 422
        except Exception as exc:
            print("營收資料更正未完成：" + type(exc).__name__, flush=True)
            return jsonify({"ok":False,"error":type(exc).__name__}), 503
    return bp
