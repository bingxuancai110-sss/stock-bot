"""Fetch real MOPS rows outside Render and correct existing periods with audit."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import html
import os
import re
import sys
import time
from urllib.parse import quote
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from revenue_parser import parse_html

BASE = "https://stock-bot-6xct.onrender.com"
PATH = "/cron/revenue-repair"
AUDIENCE = BASE + PATH


def token():
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    response = requests.get(url + ("&" if "?" in url else "?") + "audience=" + quote(AUDIENCE, safe=""),
        headers={"Authorization":"Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]}, timeout=15)
    response.raise_for_status()
    return response.json()["value"]


def call(method, payload=None):
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        try:
            response = requests.request(method, BASE + PATH, json=payload,
                headers={"Authorization":"Bearer " + token()}, timeout=(10,90))
            if response.status_code == 200 and response.json().get("ok", True):
                return response.json()
            if response.status_code not in {401,403,404,429,502,503,504}:
                raise ValueError("更正未完成：HTTP " + str(response.status_code) + " " + response.text[:200])
            print("等待更正服務：HTTP " + str(response.status_code), flush=True)
        except requests.RequestException as exc:
            print("等待更正服務：" + type(exc).__name__, flush=True)
        time.sleep(15)
    raise TimeoutError("尚未確認營收更正完成")


def fetch_market(period, market):
    year, month = divmod(int(period),100)
    url = f"https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{year-1911}_{month}_0.html"
    last = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=(5,20))
            response.raise_for_status()
            try:
                text = response.content.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = response.content.decode("cp950")
            label = re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", text)))
            if f"{year-1911}年{month}月" not in label:
                raise ValueError("官方表資料月份不符")
            rows = parse_html(text)
            if not rows:
                raise ValueError("官方營收表無有效欄位或資料")
            return market, rows
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if attempt < 2:
                time.sleep(2 * (attempt+1))
    raise RuntimeError(f"MOPS {period}/{market} 未取得：{type(last).__name__}")


def main():
    plan = call("GET")
    print("更正計畫：" + json.dumps(plan), flush=True)
    for period in plan["periods"]:
        data, markets = {}, {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            for market, rows in pool.map(lambda market: fetch_market(period, market), ("sii","otc","rotc")):
                overlaps = set(data) & set(rows)
                if any(data[code] != rows[code] for code in overlaps):
                    raise ValueError("同代號跨市場資料矛盾，不混併資料")
                unique = {code:item for code,item in rows.items() if code not in overlaps}
                data.update(unique)
                markets[market] = len(unique)
        result = call("POST", {"period":period,"markets":markets,"data":data})
        if not result.get("ok"):
            raise RuntimeError("資料庫更正未確認")
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
