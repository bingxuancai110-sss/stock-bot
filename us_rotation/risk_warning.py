"""Current US/Taiwan risk and stabilization signals with historical study notes."""

from __future__ import annotations

from math import isfinite


def describe_current_risk(current):
    """Explain existing snapshot values without changing or predicting signals."""
    def number(value):
        try:
            value = float(value)
            return value if isfinite(value) else None
        except (TypeError, ValueError):
            return None

    output = {}
    for key in ("us", "taiwan", "us_stop", "taiwan_stop"):
        row = (current or {}).get(key) or {}
        is_stop = key.endswith("_stop")
        field = "close" if is_stop else "value" if key == "us" else "relative_return_20d_pp"
        threshold = number(row.get("previous_5d_high") if is_stop else
                           row.get("threshold", 25) if key == "us" else row.get("threshold_pp", -3))
        value = number(row.get(field))
        result = {"value": value, "threshold": threshold, "as_of": row.get("as_of"),
                  "available": value is not None and threshold is not None,
                  "unit": "點" if is_stop else "" if key == "us" else "個百分點",
                  "comparison": ">" if is_stop else ">=" if key == "us" else "<=",
                  "proxy_note": row.get("proxy_note") or ""}
        if result["available"]:
            active = value > threshold if is_stop else value >= threshold if key == "us" else value <= threshold
            gap = threshold - value if key != "taiwan" else value - threshold
            result.update(triggered=active, distance=abs(gap),
                          distance_pct=(abs(value / threshold - 1) * 100
                                        if is_stop and active and threshold > 0 else
                                        abs(threshold / value - 1) * 100
                                        if is_stop and value > 0 else None),
                          equal=value == threshold)
        output[key] = result
    return output


HISTORICAL_RISK_PROFILES = [
    {"market": "美股", "indicator": "VIX ≥ 25", "threshold_label": "VIX 收盤達 25",
     "target": "未來 20 個美股交易日急跌", "daily_trigger_probability_pct": 65.2,
     "daily_no_trigger_probability_pct": 16.5, "base_probability_pct": 24.7,
     "alert_episode_hits": 23, "alert_episodes": 57, "episode_success_rate_pct": 40.4,
     "median_lead_sessions": 4, "episode_no_hit_count": 34, "episode_no_hit_rate_pct": 59.6,
     "event_capture_count": 15, "event_count": 53, "event_capture_rate_pct": 28.3,
     "source_start": "2006", "study_note": "歷史研究樣本；非未來保證"},
    {"market": "台股", "indicator": "HYG 相對 IEF 20 日落後 ≥ 3 個百分點",
     "threshold_label": "HYG 20 日報酬 − IEF 20 日報酬 ≤ −3 個百分點",
     "target": "未來 20 個台股交易日急跌", "daily_trigger_probability_pct": 44.5,
     "daily_no_trigger_probability_pct": 18.2, "base_probability_pct": 21.0,
     "alert_episode_hits": 16, "alert_episodes": 48, "episode_success_rate_pct": 33.3,
     "median_lead_sessions": 8, "episode_no_hit_count": 32, "episode_no_hit_rate_pct": 66.7,
     "event_capture_count": 14, "event_count": 48, "event_capture_rate_pct": 29.2,
     "source_start": "2006", "study_note": "HYG/IEF 為美國信用風險代理，不是台股價格指數"},
]

HISTORICAL_STOP_PROFILES = [
    {"market": "美股", "index": "S&P 500", "slow": (19, 34, 36.0, 4.5, 1),
     "fast": (2, 5, 33.9, 6, 1)},
    {"market": "台股", "index": "加權指數", "slow": (14, 29, 37.2, 6, 1),
     "fast": (6, 11, 35.5, 5, 3.5)},
]


def _valid_rows(histories, symbol):
    rows = histories.get(symbol) or []
    return sorted((row for row in rows if row.get("close") is not None),
                  key=lambda row: str(row.get("trade_date") or ""))


def calculate_current_risk(histories):
    """Evaluate VIX, HYG/IEF relative weakness, and five-session close breakouts."""
    output = {}
    vix = _valid_rows(histories, "^VIX")
    if vix:
        value = float(vix[-1]["close"])
        output["us"] = {"status": "訊號觸發" if value >= 25 else "未觸發",
                        "value": value, "as_of": vix[-1].get("trade_date"), "threshold": 25}
    else:
        output["us"] = {"status": "資料不足", "value": None}

    hyg, ief = _valid_rows(histories, "HYG"), _valid_rows(histories, "IEF")
    hmap = {row.get("trade_date"): float(row["adjusted_close"] or row["close"])
            for row in hyg if row.get("trade_date")}
    imap = {row.get("trade_date"): float(row["adjusted_close"] or row["close"])
            for row in ief if row.get("trade_date")}
    common = sorted(set(hmap) & set(imap))
    if len(common) >= 21:
        last, prior = common[-1], common[-21]
        relative = ((hmap[last] / hmap[prior] - 1) - (imap[last] / imap[prior] - 1)) * 100
        output["taiwan"] = {"status": "訊號觸發" if relative <= -3 else "未觸發",
                            "relative_return_20d_pp": relative, "as_of": last,
                            "threshold_pp": -3,
                            "proxy_note": "HYG/IEF 是美國高收益債相對中期公債代理，非台股現貨指數。"}
    else:
        output["taiwan"] = {"status": "資料不足", "relative_return_20d_pp": None}

    for key, symbol, fallback in (("us_stop", "^GSPC", "SPY"), ("taiwan_stop", "^TWII", None)):
        rows = _valid_rows(histories, symbol)
        proxy = False
        if len(rows) < 6 and fallback:
            rows = _valid_rows(histories, fallback)
            proxy = bool(rows)
        if len(rows) < 6:
            output[key] = {"status": "資料不足", "close": None}
            continue
        previous_high = max(float(row["close"]) for row in rows[-6:-1])
        close = float(rows[-1]["close"])
        output[key] = {"status": "訊號觸發" if close > previous_high else "未觸發",
                       "close": close, "previous_5d_high": previous_high,
                       "as_of": rows[-1].get("trade_date"),
                       "proxy_note": "缺少 ^GSPC，暫以 SPY 收盤代理。" if proxy else ""}
    return output


def public_stop_profiles():
    result = []
    for item in HISTORICAL_STOP_PROFILES:
        row = {"market": item["market"], "index": item["index"]}
        for name, key in (("gradual", "slow"), ("fast", "fast")):
            hits, signals, baseline, median_days, successful_median = item[key]
            row[name] = {"hits": hits, "signals": signals,
                         "success_rate_pct": round(hits / signals * 100, 1),
                         "baseline_pct": baseline,
                         "median_days_to_next_20d_low": median_days,
                         "successful_median_days": successful_median}
        result.append(row)
    return result

