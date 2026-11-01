"""Validate user-provided end-of-day CSV data for the US rotation radar."""

from __future__ import annotations

import csv
from datetime import date
from io import StringIO
from math import isfinite

from .config import INDUSTRIES


SYMBOLS = {item["ticker"] for item in INDUSTRIES} | {"SPY"}
REQUIRED_COLUMNS = {"symbol", "trade_date", "open", "high", "low", "close", "volume"}
MAX_ROWS = 20_000


class ImportValidationError(ValueError):
    pass


def parse_history_csv(text):
    """Return validated chronological histories, using close when no adjusted close exists."""
    reader = csv.DictReader(StringIO(text.lstrip("\ufeff")))
    fields = {str(name or "").strip().lower() for name in (reader.fieldnames or [])}
    missing = REQUIRED_COLUMNS - fields
    if missing:
        raise ImportValidationError("缺少欄位：" + ", ".join(sorted(missing)))
    has_adjusted = "adjusted_close" in fields
    histories = {symbol: [] for symbol in SYMBOLS}
    seen = set()
    today = date.today()
    adjusted_seen = 0

    for row_number, raw in enumerate(reader, start=2):
        if row_number > MAX_ROWS + 1:
            raise ImportValidationError(f"資料列超過上限 {MAX_ROWS:,} 列。")
        row = {str(key or "").strip().lower(): str(value or "").strip()
               for key, value in raw.items()}
        if not any(row.values()):
            continue
        symbol = row.get("symbol", "").upper()
        if symbol not in SYMBOLS:
            raise ImportValidationError(f"第 {row_number} 列代號 {symbol or '(空白)'} 不在匯入清單。")
        try:
            trade_date = date.fromisoformat(row.get("trade_date", ""))
        except ValueError as exc:
            raise ImportValidationError(f"第 {row_number} 列日期需為 YYYY-MM-DD。") from exc
        if trade_date > today:
            raise ImportValidationError(f"第 {row_number} 列日期在未來，請只匯入已完成的交易日。")
        key = (symbol, trade_date)
        if key in seen:
            raise ImportValidationError(f"第 {row_number} 列重複：{symbol} {trade_date.isoformat()}。")
        seen.add(key)
        try:
            values = {name: float(row[name]) for name in ("open", "high", "low", "close")}
            volume = int(float(row["volume"]))
            if has_adjusted and row.get("adjusted_close"):
                adjusted_close = float(row["adjusted_close"])
                adjusted_seen += 1
            else:
                adjusted_close = values["close"]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ImportValidationError(f"第 {row_number} 列價格或成交量不是有效數字。") from exc
        if (any(not isfinite(value) or value <= 0 for value in values.values())
                or not isfinite(adjusted_close) or adjusted_close <= 0 or volume < 0):
            raise ImportValidationError(f"第 {row_number} 列價格必須大於 0、成交量不可小於 0。")
        if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
            raise ImportValidationError(f"第 {row_number} 列 OHLC 高低價不合理。")
        histories[symbol].append({
            "trade_date": trade_date.isoformat(),
            "open": values["open"], "high": values["high"], "low": values["low"],
            "close": values["close"], "adjusted_close": adjusted_close, "volume": volume,
        })

    empty = sorted(symbol for symbol, rows in histories.items() if not rows)
    if has_adjusted and adjusted_seen != len(seen):
        raise ImportValidationError("有提供 adjusted_close 欄位時，每一列都必須填寫，不能混用還原與未還原價格。")
    if empty:
        raise ImportValidationError("缺少以下代號資料：" + ", ".join(empty))
    too_short = sorted(f"{symbol}（{len(rows)} 日）" for symbol, rows in histories.items() if len(rows) < 61)
    if too_short:
        raise ImportValidationError("每個代號至少需 61 個交易日，缺少：" + ", ".join(too_short))
    for rows in histories.values():
        rows.sort(key=lambda item: item["trade_date"])
    date_sets = {tuple(row["trade_date"] for row in rows) for rows in histories.values()}
    if len(date_sets) != 1:
        raise ImportValidationError("所有代號的交易日期必須完整對齊，避免不同交易日混算。")
    last_days = {rows[-1]["trade_date"] for rows in histories.values()}
    if len(last_days) != 1:
        raise ImportValidationError("所有代號的最後交易日必須一致，避免不同日期混算。")
    return histories, has_adjusted


def csv_template():
    return "symbol,trade_date,open,high,low,close,adjusted_close,volume\n"

