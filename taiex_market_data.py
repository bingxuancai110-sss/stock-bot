"""Official monthly TAIEX sessions: absence is not inferred from weekday lists."""
from datetime import date, timedelta
import math
import re

URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
SOURCE = "TWSE 加權指數官方月收盤"


def parse_month(payload, month, as_of):
    fields = payload.get("fields") or []
    if payload.get("stat") != "OK" or "日期" not in fields or "收盤指數" not in fields:
        raise ValueError("TWSE月收盤回應不完整")
    di, ci = fields.index("日期"), fields.index("收盤指數")
    prices = {}
    for row in payload.get("data") or []:
        if not isinstance(row, list) or len(row) <= max(di, ci):
            raise ValueError("TWSE月收盤列缺欄位")
        parts = [int(x) for x in re.findall(r"\d+", str(row[di]))]
        if len(parts) != 3:
            raise ValueError("TWSE月收盤日期無效")
        day = date(parts[0] + 1911 if parts[0] < 1911 else parts[0], parts[1], parts[2])
        value = float(str(row[ci]).replace(",", ""))
        if day.replace(day=1) != month or day in prices or not math.isfinite(value) or value <= 0:
            raise ValueError("TWSE月收盤資料無效或重複")
        if day <= as_of:
            prices[day] = value
    if not prices:
        raise ValueError("TWSE月收盤尚無資料")
    return sorted(prices.items())


def official_history(as_of, load, save, get):
    months, cursor = [], as_of.replace(day=1)
    for _ in range(4):
        months.append(cursor)
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    result = []
    for month in reversed(months):
        key = "taiex_official_month_" + month.strftime("%Y%m")
        cached = (load(key, max_age_seconds=180 * 86400) or {}).get("payload") or {}
        last = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        complete_on = min(as_of, last).isoformat()
        if cached.get("fetched_on", "") >= complete_on and cached.get("raw"):
            rows = parse_month(cached["raw"], month, as_of)
        else:
            response = get(URL, params={"date": month.strftime("%Y%m01"), "response": "json"},
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=(3, 8))
            response.raise_for_status()
            raw = response.json()
            rows = parse_month(raw, month, as_of)
            if month == as_of.replace(day=1) and rows[-1][0] != as_of:
                raise ValueError("TWSE尚未公布當日正式收盤")
            if not save(key, {"fetched_on": as_of.isoformat(), "raw": raw},
                        data_date=rows[-1][0], source_meta={"source": SOURCE}):
                raise RuntimeError("TWSE月收盤快取未保存")
        result.extend(rows)
    if not result or result[-1][0] != as_of:
        raise ValueError("TWSE尚未公布當日正式收盤")
    return result
