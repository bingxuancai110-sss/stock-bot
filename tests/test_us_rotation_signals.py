from copy import deepcopy

import pytest

from us_rotation.risk_warning import describe_current_risk


def test_current_distance_values_and_snapshot_not_mutated():
    current = {'us': {'value': 19.42, 'threshold': 25, 'as_of':'2026-10-30'},
               'taiwan': {'relative_return_20d_pp': -2.367122, 'threshold_pp': -3},
               'us_stop': {'close': 7580.07, 'previous_5d_high': 7743.98},
               'taiwan_stop': {'close': 45211.828125, 'previous_5d_high': 47035.578125}}
    original=deepcopy(current)
    result=describe_current_risk(current)
    assert current == original
    assert result['us']['distance'] == pytest.approx(5.58)
    assert result['taiwan']['distance'] == pytest.approx(.632878)
    assert result['us_stop']['distance'] == pytest.approx(163.91)
    assert result['us_stop']['distance_pct'] == pytest.approx((7743.98/7580.07-1)*100)
    assert result['taiwan_stop']['distance'] == pytest.approx(1823.75)
    assert all(not r['triggered'] for r in result.values())


@pytest.mark.parametrize('value,triggered',[(24.99,False),(25,True),(25.01,True)])
def test_vix_inclusive_boundary(value,triggered):
    r=describe_current_risk({'us':{'value':value}})['us']
    assert r['triggered'] is triggered
    assert r['comparison'] == '>='


@pytest.mark.parametrize('value,triggered',[(-2.99,False),(-3,True),(-3.01,True)])
def test_credit_inclusive_boundary(value,triggered):
    r=describe_current_risk({'taiwan':{'relative_return_20d_pp':value}})['taiwan']
    assert r['triggered'] is triggered
    assert r['comparison'] == '<='


@pytest.mark.parametrize('value,triggered',[(99.99,False),(100,False),(100.01,True)])
def test_breakout_requires_strictly_higher_close(value,triggered):
    r=describe_current_risk({'us_stop':{'close':value,'previous_5d_high':100}})['us_stop']
    assert r['triggered'] is triggered
    assert r['equal'] is (value==100)
    assert r['comparison'] == '>'


@pytest.mark.parametrize('value',[None,'invalid',float('nan'),float('inf')])
def test_missing_or_invalid_values_are_not_reported_as_untriggered(value):
    result=describe_current_risk({'us':{'value':value}})
    assert not result['us']['available']
    assert 'triggered' not in result['us']
    assert all(not result[k]['available'] for k in ['taiwan','us_stop','taiwan_stop'])


def test_breakout_percentage_is_relative_to_threshold_once_above():
    result=describe_current_risk({'us_stop':{'close':110,'previous_5d_high':100}})['us_stop']
    assert result['triggered']
    assert result['distance_pct'] == pytest.approx(10)


def test_api_explains_existing_snapshot_without_running_refresh(monkeypatch):
    from flask import Flask
    from us_rotation import routes
    from unittest.mock import Mock
    snapshot={'current_risk_warning':{'us':{'value':19.42,'threshold':25}}}
    monkeypatch.setattr(routes,'read_latest',Mock(return_value=snapshot))
    for name in ('read_history','read_supply_chain','read_latest_tw_company_snapshots'):
        monkeypatch.setattr(routes,name,Mock(return_value=[]))
    fetch=Mock(side_effect=AssertionError('page requests must not fetch quotes'))
    monkeypatch.setattr(routes,'fetch_market_histories',fetch)
    # Register with an auth wrapper retaining each view's name.
    from functools import wraps
    def auth(f):
        @wraps(f)
        def wrapped():return f('test')
        return wrapped
    app=Flask(__name__)
    app.register_blueprint(routes.create_blueprint(web_login_required=auth,render_page=Mock(),
        get_db_connection=Mock(),release_db_connection=Mock(),run_in_background=Mock(),
        fetch_tw_company_snapshot=Mock()))
    response=app.test_client().get('/web/api/us-rotation')
    assert response.status_code == 200
    assert response.json['signal_explanations']['us']['distance'] == pytest.approx(5.58)
    assert not response.json['signal_explanations']['taiwan']['available']
    fetch.assert_not_called()
