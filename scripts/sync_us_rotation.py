"""Download outside Render, then require a committed snapshot acknowledgment."""
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from us_rotation.daily_sync import AUDIENCE, PATH, encode_closed_history
from us_rotation.providers import fetch_market_histories

BASE = "https://stock-bot-6xct.onrender.com"


def identity_token():
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    url += ("&" if "?" in url else "?") + "audience=" + quote(AUDIENCE, safe="")
    response = requests.get(url, headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]}, timeout=15)
    response.raise_for_status()
    return response.json()["value"]


def main():
    histories, errors, _ = fetch_market_histories(allow_unofficial=True)
    body = encode_closed_history(histories).encode("utf-8")
    print("已取得並驗證收盤日線；外部來源未取得代號：" + ",".join(sorted(errors)), flush=True)
    # A push-triggered verification can begin while Render is still deploying.
    deadline = time.monotonic() + 420
    last_error = "尚未連線"
    while time.monotonic() < deadline:
        try:
            response = requests.post(BASE + PATH, data=body,
                headers={"Authorization": "Bearer " + identity_token(), "Content-Type": "text/csv; charset=utf-8"}, timeout=(10, 90))
            if response.status_code == 200:
                result = response.json()
                if not result.get("ok") or not result.get("snapshot_id") or result.get("vix_date") != result.get("as_of"):
                    raise ValueError("伺服器未確認保存完整 VIX／產業快照")
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return
            if response.status_code not in {401, 403, 404, 429, 502, 503, 504}:
                raise ValueError("匯入被拒絕：HTTP " + str(response.status_code) + " " + response.text[:200])
            last_error = "HTTP " + str(response.status_code)
        except requests.RequestException as exc:
            last_error = type(exc).__name__
        print("等待儲存服務：" + last_error, flush=True)
        time.sleep(15)
    raise TimeoutError("美股快照未確認保存：" + last_error)


if __name__ == "__main__":
    main()
