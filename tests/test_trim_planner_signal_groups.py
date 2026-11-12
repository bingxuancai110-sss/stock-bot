import ast
import html
import math
from datetime import date, timedelta
from pathlib import Path


def test_relative_trend_groups_and_unknown_names():
    source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                and item.name == "_trim_planner_relative_trend")
    namespace = {
        "html": html,
        "math": math,
        "_parse_history_date": lambda value: value,
        "stock_display_name": lambda code, fallback=None: {
            "2308": "台達電", "2330": "台積電", "2317": "鴻海",
        }.get(code, code),
        "short_company_name": lambda value: value,
        "STOCK_NAME_MAP": {},
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<planner>", "exec"), namespace)
    days = [date(2026, 8, 1) + timedelta(days=n) for n in range(60)]
    market = [(day, 100.0) for day in days]
    prices = {
        "2308": {"close_dates": days, "adj_closes": [100.0] * 59 + [80.0]},
        "2330": {"close_dates": days, "adj_closes": [100.0] * 59 + [120.0]},
        "2317": {"close_dates": days, "adj_closes": [100.0] * 40 + [80.0] * 19 + [90.0]},
        "2026": {"close_dates": days, "adj_closes": [100.0] * 59 + [70.0]},
    }
    positions = [{"code": code} for code in prices]
    panel, ranking = namespace["_trim_planner_relative_trend"](prices, positions, market)

    assert ranking["2308"]["category"] == "weak"
    assert ranking["2330"]["category"] == "strong"
    assert ranking["2317"]["category"] == "mixed"
    assert "2026" not in ranking
    assert "台達電" in panel and "台積電" in panel and "鴻海" in panel
    assert "優先減碼・相對弱" in panel and "優先保留・相對強" in panel and "訊號分歧" in panel
    assert "名稱待確認" in panel
