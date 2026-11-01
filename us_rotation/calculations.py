"""Pure calculations for trading-session US industry rotation indicators."""

from __future__ import annotations

from math import isfinite

from .config import ALGORITHM_VERSION, INDUSTRIES, WEIGHTS


def _finite(value):
    try:
        number = float(value)
        return number if isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _session_return(rows, periods, field="adjusted_close"):
    # N trading sessions means N close-to-close intervals, so compare row[-1] to row[-N-1].
    if len(rows) <= periods:
        return None
    latest, previous = _finite(rows[-1].get(field)), _finite(rows[-periods - 1].get(field))
    if not latest or not previous or previous <= 0:
        return None
    return (latest / previous - 1.0) * 100.0


def _relative_volume(rows):
    if len(rows) < 21:
        return None
    latest = _finite(rows[-1].get("volume"))
    prior = [_finite(row.get("volume")) for row in rows[-21:-1]]
    prior = [x for x in prior if x is not None and x > 0]
    return latest / (sum(prior) / len(prior)) if latest is not None and prior else None


def _percentile_scores(records, key):
    available = sorted(((record[key], record) for record in records if record.get(key) is not None),
                       key=lambda item: (item[0], item[1]["id"]))
    if not available:
        return
    if len(available) == 1:
        available[0][1][f"{key}_score"] = 50.0
        return
    for rank, (_, record) in enumerate(available):
        record[f"{key}_score"] = rank / (len(available) - 1) * 100.0


def _score_and_state(records):
    components = {
        "relative_5d": "relative_5d_pct",
        "relative_20d": "relative_20d_pct",
        "relative_60d": "relative_60d_pct",
        "relative_volume": "relative_volume",
        "breadth": "breadth_pct",
    }
    for component, field in components.items():
        for record in records:
            value = record.get(field)
            record[component] = value
        _percentile_scores(records, component)

    for record in records:
        active = [(weight, record.get(f"{name}_score"))
                  for name, weight in WEIGHTS.items()
                  if record.get(f"{name}_score") is not None]
        total_weight = sum(weight for weight, _ in active)
        record["score_coverage_pct"] = round(total_weight * 100, 1)
        record["score"] = (round(sum(weight * score for weight, score in active) / total_weight, 1)
                           if total_weight else None)
        rel5, rel20, rel60 = (record.get("relative_5d_pct"), record.get("relative_20d_pct"),
                              record.get("relative_60d_pct"))
        prior20, score = record.get("prior_relative_20d_pct"), record.get("score")
        improving = rel20 is not None and prior20 is not None and rel20 > prior20
        weakening = rel20 is not None and prior20 is not None and rel20 < prior20
        if score is None:
            state = "資料不足"
        elif score >= 70 and rel20 is not None and rel20 > 0 and rel60 is not None and rel60 > 0:
            state = "強勢延續"
        elif score >= 55 and improving and rel5 is not None and rel5 > 0:
            state = "轉強觀察"
        elif score <= 30 and rel5 is not None and rel20 is not None and rel60 is not None and rel5 < 0 and rel20 < 0 and rel60 < 0:
            state = "弱勢延續"
        elif score <= 45 and weakening and rel5 is not None and rel5 < 0:
            state = "轉弱警戒"
        else:
            state = "中性整理"
        record["trend_state"] = state
        if rel20 is None or record.get("relative_momentum_20d_pct") is None:
            record["quadrant"] = "資料不足"
        elif rel20 > 0 and record["relative_momentum_20d_pct"] > 0:
            record["quadrant"] = "領先"
        elif rel20 <= 0 and record["relative_momentum_20d_pct"] > 0:
            record["quadrant"] = "改善"
        elif rel20 > 0 and record["relative_momentum_20d_pct"] <= 0:
            record["quadrant"] = "轉弱"
        else:
            record["quadrant"] = "落後"


def calculate_snapshot(price_histories, as_of=None, source="unknown", fetched_at=None):
    """Calculate a transparent snapshot from symbol -> chronological daily OHLCV rows."""
    benchmark = price_histories.get("SPY") or []
    benchmark_return = {period: _session_return(benchmark, period)
                        for period in (5, 20, 60)}
    records = []
    for item in INDUSTRIES:
        ticker = item["ticker"]
        rows = price_histories.get(ticker) or []
        record = dict(item)
        close = _finite(rows[-1].get("adjusted_close")) if rows else None
        record.update({
            "as_of": str(rows[-1].get("trade_date")) if rows else None,
            "close": _finite(rows[-1].get("close")) if rows else None,
            "return_1d_pct": _session_return(rows, 1),
            "return_5d_pct": _session_return(rows, 5),
            "return_20d_pct": _session_return(rows, 20),
            "return_60d_pct": _session_return(rows, 60),
            "relative_5d_pct": None,
            "relative_20d_pct": None,
            "relative_60d_pct": None,
            "relative_volume": _relative_volume(rows),
            "volume": int(_finite(rows[-1].get("volume")) or 0) if rows else None,
            "breadth_pct": None,
            "breadth_note": "沒有可驗證的歷史成分股廣度來源",
        })
        for period in (5, 20, 60):
            own = record.get(f"return_{period}d_pct")
            market = benchmark_return.get(period)
            record[f"relative_{period}d_pct"] = round(own - market, 4) if own is not None and market is not None else None

        # Compare the latest 20-session excess return with the preceding 20-session window.
        if len(rows) >= 41 and len(benchmark) >= 41:
            current20 = record.get("relative_20d_pct")
            prior_industry = _session_return(rows[:-20], 20)
            prior_market = _session_return(benchmark[:-20], 20)
            prior20 = prior_industry - prior_market if prior_industry is not None and prior_market is not None else None
            record["prior_relative_20d_pct"] = round(prior20, 4) if prior20 is not None else None
            record["relative_momentum_20d_pct"] = round(current20 - prior20, 4) if current20 is not None and prior20 is not None else None
        else:
            record["prior_relative_20d_pct"] = None
            record["relative_momentum_20d_pct"] = None
        record["observations"] = len(rows)
        records.append(record)

    _score_and_state(records)
    # Ranking is computed only among the 11 comparable sector proxies; XSD is shown as overlay.
    sector_records = [row for row in records if not row.get("overlay") and row.get("score") is not None]
    sector_records.sort(key=lambda row: (-(row.get("score") or 0), row["id"]))
    for index, record in enumerate(sector_records, 1):
        record["rank"] = index
    for record in records:
        if record.get("overlay"):
            record["rank"] = None
        elif record.get("score") is None:
            record["rank"] = None

    # Optional risk/index series can have different holidays; the sector snapshot date
    # follows its required SPY benchmark, not whichever optional series is newest.
    benchmark_dates = [str(row.get("trade_date")) for row in benchmark if row.get("trade_date")]
    date_value = as_of or (benchmark_dates[-1] if benchmark_dates else None)
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "as_of": str(date_value) if date_value else None,
        "fetched_at": fetched_at,
        "source": source,
        "delay_note": "美股日線資料；不是即時報價。",
        "adjustment_note": "報酬優先使用來源調整收盤價；若來源沒有調整收盤價，該筆長期報酬不計算。ETF 配息調整方式依資料來源。",
        "benchmark": BENCHMARK,
        "benchmark_returns": benchmark_return,
        "score_weights": WEIGHTS,
        "score_note": "分項以同日產業橫向百分位換算，僅對可用項目按權重重算；市場廣度目前缺可靠來源，不記零分。",
        "breadth_available": False,
        "industries": sorted(records, key=lambda row: (row.get("overlay", False), row.get("rank") or 99, row["id"])),
    }


