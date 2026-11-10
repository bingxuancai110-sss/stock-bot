"""GitHub OIDC trigger + bounded completion polling. Never prints credentials."""
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE = "https://stock-bot-6xct.onrender.com"
PATH = "/cron/taiex-reduction"
_token = None
_issued = 0


def token():
    global _token, _issued
    if _token and time.monotonic() - _issued < 120:
        return _token
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    url += ("&" if "?" in url else "?") + "audience=" + quote(BASE + PATH, safe="")
    req = Request(url, headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
    with urlopen(req, timeout=15) as response:
        _token = json.load(response)["value"]
    _issued = time.monotonic()
    return _token


def call(method, path):
    req = Request(BASE + path, method=method, headers={"Authorization": "Bearer " + token()})
    try:
        with urlopen(req, timeout=25) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, {}


def run(timeout=420, sleep=time.sleep, clock=time.monotonic):
    deadline, run_id, last = clock() + timeout, None, None
    while clock() < deadline:
        try:
            status, payload = call("GET" if run_id else "POST",
                                   PATH + "/status?id=" + run_id if run_id else PATH)
        except (URLError, TimeoutError):
            print("等待服務連線，稍後重試。", flush=True)
            sleep(10)
            continue
        if status in (401, 403, 404, 502, 503):
            # Render normally takes ~2 minutes to deploy the new auth handler.
            print(f"等待服務就緒：HTTP {status}", flush=True)
            sleep(15)
            continue
        if not run_id:
            if status != 202 or not payload.get("id"):
                raise RuntimeError(f"無法排入月線檢查：HTTP {status}")
            run_id = payload["id"]
        state = payload.get("state")
        if state != last:
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            last = state
        if state in ("ok", "skipped"):
            return payload
        if state == "error":
            raise RuntimeError("月線檢查失敗：" + payload.get("message", "未取得結果"))
        if state == "busy":
            run_id = None
        sleep(10)
    raise TimeoutError("未能在期限內確認月線檢查完成；202排隊不代表已完成。")


if __name__ == "__main__":
    run()
