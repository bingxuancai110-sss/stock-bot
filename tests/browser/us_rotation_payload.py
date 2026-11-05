"""Explicitly synthetic UI input; never served by the production application."""
from datetime import date, timedelta
from importlib.util import module_from_spec, spec_from_file_location


def populated_payload(repo):
    spec = spec_from_file_location("fixture_risk_warning", repo / "us_rotation/risk_warning.py")
    risk = module_from_spec(spec)
    spec.loader.exec_module(risk)
    as_of = "2026-11-04"
    current = {
        "us": dict(value=26, threshold=25, as_of=as_of),
        "taiwan": dict(relative_return_20d_pp=-4, threshold_pp=-3, as_of=as_of),
        "us_stop": dict(close=102, previous_5d_high=101, as_of=as_of),
        "taiwan_stop": dict(close=99, previous_5d_high=100, as_of=as_of),
    }
    industries = [
        dict(id="technology", name="資訊科技", ticker="XLK", rank=1, score=80,
             return_5d_pct=2, return_20d_pct=8, return_60d_pct=12,
             relative_5d_pct=1, relative_20d_pct=4, relative_60d_pct=5,
             relative_momentum_20d_pct=2, trend_state="強勢延續", quadrant="領先", as_of=as_of),
        dict(id="industrials", name="工業", ticker="XLI", rank=2, score=60,
             return_5d_pct=-1, return_20d_pct=3, return_60d_pct=5,
             relative_5d_pct=-2, relative_20d_pct=-1, relative_60d_pct=-2,
             relative_momentum_20d_pct=1, trend_state="轉強觀察", quadrant="改善", as_of=as_of),
    ]
    company = dict(code="2330", name="隔離測試公司", close=100, ma20=95,
                   price_date=as_of, comparison_start_20d="2026-10-07",
                   price_return_5d_pct=1, price_return_20d_pct=5, price_return_60d_pct=8,
                   relative_market_20d_pp=2, above_ma20=True,
                   revenue_yoy_pct=10, revenue_period_label="2026/09", revenue_state="已公布",
                   total_net_lots=100, institutional_source_date=as_of, institutional_aligned=True,
                   state="aligned", state_label="美台同向", next_step="隔離畫面驗證資料",
                   pe=20, pb=2, market="上市", official_industry_code="24",
                   source_url="https://www.twse.com.tw/", note="非正式研究結果")
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(65)]
    days = [d.isoformat() for d in days if d.weekday() < 5]
    trends = {}
    for key, value, threshold in [("us", 26, 25), ("taiwan", -4, -3),
                                   ("us_stop", 102, 101), ("taiwan_stop", 99, 100)]:
        trends[key] = dict(symbol=key, points=[dict(date=d, value=value-(len(days)-1-i)*.01, threshold=threshold)
                                             for i, d in enumerate(days)])
    return dict(
        snapshot=dict(as_of=as_of, fetched_at="2026-11-04T23:00:00+00:00",
                      source="隔離測試日線", industries=industries,
                      score_note="隔離測試分數", current_risk_warning=current),
        tw_research=dict(sectors={
            "technology": dict(rows=[company], total=1, priced=1, aligned=1, scope="隔離測試樣本"),
            "industrials": dict(rows=[], total=0, priced=0, aligned=0, scope="資料不足"),
        }),
        history=[dict(as_of="2026-11-03", industries=industries),
                 dict(as_of=as_of, industries=industries)],
        supply_chain=[], industry_guides={}, tw_companies=[],
        risk_profiles=risk.HISTORICAL_RISK_PROFILES,
        stop_profiles=risk.public_stop_profiles(),
        signal_explanations=risk.describe_current_risk(current), signal_trends=trends,
    )
