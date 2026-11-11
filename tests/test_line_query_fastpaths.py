import ast
import re
from datetime import date
from pathlib import Path
from unittest.mock import Mock


MAIN = Path(__file__).parents[1] / "main.py"
SOURCE = MAIN.read_text(encoding="utf-8")


def function_node(name):
    match = re.search(r"(?ms)^def " + name + r"\b.*?(?=^(?:def |class |@)|\Z)", SOURCE)
    return next(node for node in ast.parse(match[0]).body
                if isinstance(node, ast.FunctionDef) and node.name == name)


def load_function(name, namespace):
    node = function_node(name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<main-function>", "exec"), namespace)
    return namespace[name]


def test_line_scoring_reads_saved_revenue_snapshot_instead_of_refreshing_official_sources():
    monthly = Mock(return_value={})
    ns = {
        "fetch_institutional_data": Mock(return_value={}),
        "fetch_monthly_revenue": monthly,
        "_repair_missing_cumulative_revenue": Mock(return_value={}),
        "fetch_valuation": Mock(return_value={}),
        "get_industry_map": Mock(return_value={}),
        "get_industry_momentum": Mock(return_value={}),
        "get_consecutive_days_batch": Mock(return_value={}),
        "get_cumulative_net_buy_for_codes": Mock(return_value={}),
        "get_realtime_stocks_bulk": Mock(return_value={}),
        "_load_shared_data_snapshot": Mock(return_value=None),
        "is_etf": lambda _code: False,
    }
    load_function("_compute_stock_watchlist_scores", ns)(
        ["2383"], allow_network_revenue_repair=False)
    monthly.assert_called_once_with(homepage=True)


def test_line_holdings_quote_snapshot_is_private_to_current_symbols_and_reuses_saved_data():
    stored = {}
    ns = {
        "_load_shared_data_snapshot": lambda key, max_age_seconds: stored.get(key),
        "_save_shared_data_snapshot": lambda key, payload, **kwargs: stored.update({
            key: {"payload": payload, **kwargs}}) or True,
        "taiwan_today": lambda: date(2026, 11, 5),
        "time": __import__("time"),
        "print": lambda *_args, **_kwargs: None,
    }
    for name in ("_line_holdings_quote_snapshot_key",
                 "_save_line_holdings_quote_snapshot",
                 "_load_line_holdings_quote_snapshot"):
        load_function(name, ns)
    saved = ns["_save_line_holdings_quote_snapshot"](
        "owner-a", ["2383", "2330"],
        {"2383": {"close": 100, "pct": 1.5, "close_date": "20261104"},
         "2330": {"close": 0}, "9999": {"close": 55}})
    assert saved is True
    assert ns["_line_holdings_quote_snapshot_key"]("owner-a") != ns["_line_holdings_quote_snapshot_key"]("owner-b")
    assert ns["_load_line_holdings_quote_snapshot"]("owner-a", ["2383"])["2383"]["close"] == 100
    assert ns["_load_line_holdings_quote_snapshot"]("owner-a", ["2330", "9999"]) == {}


def test_line_combined_holdings_report_has_no_per_symbol_history_fanout():
    combined = function_node("_line_hidden_combined_portfolio_flex")
    report = function_node("_line_hidden_operation_report")
    called = {node.func.id for fn in (combined, report)
              for node in ast.walk(fn)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "_load_line_holdings_quote_snapshot" in called
    assert "_fetch_twse_mis_quotes" in called
    assert "get_realtime_stocks_bulk" not in called
