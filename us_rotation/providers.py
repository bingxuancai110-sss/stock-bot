"""Free US ETF price provider adapter.

Yahoo's chart endpoint is used by existing stock-bot code, but is unofficial and its
redistribution terms must be reviewed before enabling this provider in the multi-user site.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from .config import INDUSTRIES, SOURCE_NAME


class ProviderDisabled(RuntimeError):
    pass


def fetch_symbol_history(symbol, timeout=12):
    symbol = str(symbol).strip()
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='^')}"
    response = requests.get(
        url,
        params={"range": "9mo", "interval": "1d", "events": "div,splits"},
        headers={"User-Agent": "Mozilla/5.0 (compatible; StockBotResearch/1.0)"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json().get("chart", {})
    if payload.get("error"):
        raise RuntimeError(f"行情來源回報錯誤：{symbol}")
    result = (payload.get("result") or [None])[0]
    if not result:
        raise RuntimeError(f"行情來源沒有回傳 {symbol} 日線")
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = (indicators.get("quote") or [{}])[0]
    adjusted = ((indicators.get("adjclose") or [{}])[0].get("adjclose") or [])
    rows = []
    for index, stamp in enumerate(timestamps):
        try:
            close = quotes.get("close", [])[index]
            volume = quotes.get("volume", [])[index]
            adj_close = adjusted[index] if index < len(adjusted) else None
            # Index and volatility series have no meaningful dividend-adjusted close.
            if close is None:
                continue
            if adj_close is None and symbol in {"^VIX", "^GSPC", "^TWII"}:
                adj_close = close
            if adj_close is None:
                continue
            trade_day = datetime.fromtimestamp(int(stamp), timezone.utc).date().isoformat()
            rows.append({
                "trade_date": trade_day,
                "open": quotes.get("open", [None])[index],
                "high": quotes.get("high", [None])[index],
                "low": quotes.get("low", [None])[index],
                "close": float(close),
                "adjusted_close": float(adj_close),
                "volume": int(volume or 0),
            })
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
    return rows


def fetch_market_histories(allow_unofficial=False):
    """Fetch the 12 proxies plus SPY in a bounded batch; never called by page requests."""
    if not allow_unofficial:
        raise ProviderDisabled(
            "行情來源為非官方 Yahoo Finance 端點；公開展示／再散布條件尚未確認，未啟用資料更新。"
        )
    # Risk indicators and index closes are optional for sector ranking; failed requests
    # must not prevent the core industry snapshot from being saved.
    symbols = [item["ticker"] for item in INDUSTRIES] + ["SPY", "^VIX", "HYG", "IEF", "^GSPC", "^TWII"]
    output, errors = {}, {}
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="us-rotation") as pool:
        futures = {pool.submit(fetch_symbol_history, symbol): symbol for symbol in symbols}
        for future in as_completed(futures, timeout=75):
            symbol = futures[future]
            try:
                rows = future.result()
                if len(rows) < (61 if symbol in {item["ticker"] for item in INDUSTRIES} | {"SPY"} else 6):
                    errors[symbol] = "有效交易日不足 61 日"
                else:
                    output[symbol] = rows
            except Exception as exc:
                errors[symbol] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return output, errors, datetime.now(timezone.utc).isoformat()

