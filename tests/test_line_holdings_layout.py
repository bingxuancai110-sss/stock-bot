import json

from line_holdings_layout import render_holdings, quote_footer, LIMIT_COLORS


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def row(name="台達電", code="2308", weight=21.7, pct=9.91,
        note="2026-11-10 13:14:13 · 富果伺服器背景行情", limit=None):
    return (name, code, weight, pct, note, limit)


def test_same_source_and_time_are_shared_once_in_footer_not_repeated_per_stock():
    result = render_holdings([row(), row("高力", "8996", 19.8, 9.77)])
    raw = json.dumps(result, ensure_ascii=False)
    assert raw.count("富果伺服器背景行情") == 1
    assert raw.count("2026-11-10 13:14:13") == 1
    assert "今日漲幅" not in raw
    assert "持股配置比例" in raw and "今日漲跌" in raw
    assert "21.7%" in raw and "+9.91%" in raw


def test_only_explicit_limit_states_have_solid_background_and_white_text():
    result = render_holdings([row(limit="漲停"), row("高力", "8996", 19.8, -9.99, limit="跌停")])
    boxes = [n for n in nodes(result) if n.get("backgroundColor") in LIMIT_COLORS.values()]
    assert [b["backgroundColor"] for b in boxes] == [LIMIT_COLORS["漲停"], LIMIT_COLORS["跌停"]]
    assert all(t["color"] == "#FFFFFF" for b in boxes for t in b["contents"])
    ordinary = render_holdings([row(pct=10), row(pct=-10)])
    assert not any(n.get("backgroundColor") in LIMIT_COLORS.values() for n in nodes(ordinary))


def test_pending_quote_retains_individual_time_and_source_without_limit_badge():
    result = render_holdings([row(), row(pct=None, note="2026-11-09 13:30:00 · 昨收快取", limit="跌停")])
    raw = json.dumps(result, ensure_ascii=False)
    assert "待更新" in raw and "行情待更新 · 2026-11-09 13:30:00 · 昨收快取" in raw
    assert not any(n.get("backgroundColor") == LIMIT_COLORS["跌停"] for n in nodes(result))


def test_full_etf_name_and_code_do_not_get_truncated_and_percentages_are_unbroken():
    result = render_holdings([row("主動統一台股增長", "00981A", 14.1, 4.47)])
    texts = [n for n in nodes(result) if n.get("type") == "text"]
    name = next(n for n in texts if n["text"] == "主動統一台股增長（00981A）")
    assert name["wrap"] is True
    assert "00981A" in name["text"]
    assert all(not n.get("wrap", False) for n in texts if n["text"].endswith("%"))


def test_time_range_different_dates_and_mixed_sources_are_not_claimed_as_one_timestamp():
    result = quote_footer([row(note="2026-11-09 13:30:00 · TWSE"),
                           row(note="2026-11-10T05:14:00Z · 富果")])
    raw = json.dumps(result, ensure_ascii=False)
    assert "2026-11-09 13:30:00–2026-11-10 13:14:00" in raw
    assert "TWSE／富果" in raw


def test_bar_lengths_use_actual_weights_and_current_zero_return_is_preserved():
    result = render_holdings([row(weight=0, pct=0), row(weight=100)])
    fills = [n for n in nodes(result) if n.get("backgroundColor") == "#2D8B92"]
    assert [n["width"] for n in fills] == ["0.0%", "100.0%"]
    assert "+0.00%" in json.dumps(result)


def test_weight_and_daily_return_are_on_separate_rows_and_code_stays_with_name():
    result = render_holdings([row()])
    stock = result[2]["contents"][0]["contents"]
    name_row, bar_row, return_row = stock[:3]
    assert name_row["contents"][0]["text"] == "台達電（2308）"
    assert name_row["contents"][1]["text"] == "21.7%"
    assert bar_row["height"] == "6px" and bar_row["backgroundColor"] == "#E9EEF3"
    assert return_row["contents"][0]["text"] == "今日漲跌"
    assert return_row["contents"][1]["text"] == "+9.91%"
    assert "+9.91%" not in json.dumps(name_row)
