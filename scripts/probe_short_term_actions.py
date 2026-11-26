"""Try the existing long and short scans on public exchange data in CI."""

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import resource
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import short_side
import short_term


STOCKS = {"2330": "台積電", "2308": "台達電"}
URL = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TAIPEI = timezone(timedelta(hours=8))
COLUMNS = ("close_dates", "opens", "highs", "lows", "closes", "volumes")


def daily_history(code, today):
    history = {column: [] for column in COLUMNS}
    month_index = today.year * 12 + today.month - 1
    for index in range(month_index - 5, month_index + 1):
        year, month = divmod(index, 12)
        query = urlencode({"date": date(year, month + 1, 1).strftime("%Y%m%d"),
                           "stockNo": code, "response": "json"})
        request = Request(URL + "?" + query, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
        if result.get("stat") != "OK" or not result.get("data"):
            raise RuntimeError(f"TWSE daily history unavailable for {code} {year}-{month + 1:02}")
        for row in result["data"]:
            year_roc, row_month, row_day = map(int, row[0].split("/"))
            trade_date = date(year_roc + 1911, row_month, row_day)
            try:
                prices_and_volume = [float(row[i].replace(",", "")) for i in (3, 4, 5, 6, 1)]
            except ValueError:
                continue  # No trade or no valid OHLC on that session.
            for column, value in zip(COLUMNS, (trade_date.isoformat(), *prices_and_volume)):
                history[column].append(value)
        time.sleep(2)
    return history


def scan(code, name, now):
    history = daily_history(code, now.date())
    bars = short_term.normalize_bars(history, now)
    if len(bars) < 65 or (now.date() - date.fromisoformat(bars[-1]["date"])).days > 7:
        raise RuntimeError(f"Insufficient recent daily data for {code}: {len(bars)} bars")
    long_row = short_term.analyse(code, name, history, [], now)
    short_row = short_side.analyse(code, name, history, [], now)
    return {
        "code": code,
        "name": name,
        "bars": len(bars),
        "data_date": bars[-1]["date"],
        "long_candidate": long_row is not None,
        "short_candidate": short_row is not None,
    }


def main():
    now = datetime.now(TAIPEI)
    started = time.monotonic()
    rows = []
    errors = []
    for code, name in STOCKS.items():
        try:
            rows.append(scan(code, name, now))
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append({"code": code, "reason": str(exc)})

    result = {
        "mode": "close_sample",
        "source": "TWSE",
        "generated_at": now.isoformat(),
        "processed": len(rows),
        "total": len(STOCKS),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "peak_process_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "rows": rows,
        "errors": errors,
    }
    output = json.dumps(result, ensure_ascii=False, indent=2)
    Path("short-term-pilot.json").write_text(output + "\n", encoding="utf-8")
    print(output, flush=True)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
