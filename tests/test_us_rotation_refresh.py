"""Regression coverage for the morning warm-up -> rotation snapshot pipeline."""
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from us_rotation.calculations import calculate_snapshot
from us_rotation.config import BENCHMARK, INDUSTRIES
from us_rotation import routes


def histories():
    rows = []
    day = date(2026, 6, 1)
    while len(rows) < 80:
        if day.weekday() < 5:
            price = 100 + len(rows)
            rows.append(dict(trade_date=day.isoformat(), close=price,
                             adjusted_close=price, volume=1000))
        day += timedelta(days=1)
    return {s: [dict(r) for r in rows] for s in
            [x['ticker'] for x in INDUSTRIES] + [BENCHMARK, '^VIX', 'HYG', 'IEF', '^GSPC', '^TWII']}


def blueprint():
    return routes.create_blueprint(
        web_login_required=lambda f: f, render_page=Mock(),
        get_db_connection=Mock(), release_db_connection=Mock(),
        run_in_background=Mock(), fetch_tw_company_snapshot=Mock(return_value=None))


def test_snapshot_contains_benchmark_and_all_sector_scores():
    data = histories()
    snap = calculate_snapshot(data, source='test', fetched_at='2026-10-30T22:00:00Z')
    assert snap['benchmark'] == BENCHMARK == 'SPY'
    assert snap['as_of'] == data['SPY'][-1]['trade_date']
    assert len(snap['industries']) == 12
    assert all(r['score'] is not None for r in snap['industries'])
    assert sorted(r['rank'] for r in snap['industries'] if not r.get('overlay')) == list(range(1, 12))
    assert snap['benchmark_returns'][20] == pytest.approx((179/159-1)*100)


def test_morning_job_reaches_persistence_with_risk_and_index_data(monkeypatch):
    fetch = Mock(return_value=(histories(), {}, '2026-10-30T22:00:00Z'))
    save = Mock()
    monkeypatch.setattr(routes, 'fetch_market_histories', fetch)
    monkeypatch.setattr(routes, 'save_snapshot', save)
    monkeypatch.setattr(routes, 'read_supply_chain', Mock(return_value=[]))
    result = blueprint().us_rotation_refresh_job(allow_unofficial_override=True)
    fetch.assert_called_once_with(allow_unofficial=True)
    snap, prices, errors = save.call_args.args[2:]
    assert snap['benchmark'] == 'SPY'
    assert snap['current_risk_warning']['us']['value'] == 179
    assert snap['current_risk_warning']['taiwan']['relative_return_20d_pp'] == 0
    assert snap['current_risk_warning']['us_stop']['close'] == 179
    assert snap['current_risk_warning']['taiwan_stop']['close'] == 179
    assert len(prices) == 18 and not errors
    assert '12 個代理 ETF' in result


def test_optional_risk_failure_still_saves_sector_snapshot(monkeypatch):
    prices = {k: v for k,v in histories().items() if k not in {'^VIX', '^TWII'}}
    monkeypatch.setattr(routes, 'fetch_market_histories', Mock(return_value=(prices, {'^VIX':'timeout','^TWII':'timeout'}, '2026-10-30T22:00:00Z')))
    save = Mock()
    monkeypatch.setattr(routes, 'save_snapshot', save)
    monkeypatch.setattr(routes, 'read_supply_chain', Mock(return_value=[]))
    blueprint().us_rotation_refresh_job(allow_unofficial_override=True)
    snap = save.call_args.args[2]
    assert all(r['score'] is not None for r in snap['industries'])
    assert snap['current_risk_warning']['us']['status'] == '資料不足'
    assert snap['current_risk_warning']['taiwan_stop']['status'] == '資料不足'
    assert snap['current_risk_warning']['us_stop']['close'] == 179
