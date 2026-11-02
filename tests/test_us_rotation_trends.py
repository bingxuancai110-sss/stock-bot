from copy import deepcopy
from unittest.mock import Mock

import pytest

from us_rotation.risk_warning import build_signal_trends, calculate_current_risk
from us_rotation.repository import read_signal_prices
from test_us_rotation_refresh import histories


def test_saved_histories_give_60_sessions_and_same_current_signal():
    data=histories()
    current=calculate_current_risk(data)
    result=build_signal_trends(data,current)
    assert len(result['us']['points'])==60
    assert len(result['taiwan']['points'])==60
    for key,field in [('us','value'),('taiwan','relative_return_20d_pp'),('us_stop','close'),('taiwan_stop','close')]:
        assert result[key]['points'][-1]['value']==pytest.approx(current[key][field])
        assert result[key]['points'][-1]['date']==current[key]['as_of']
    assert result['us_stop']['points'][-1]['threshold']==current['us_stop']['previous_5d_high']
    assert all(p['threshold']==25 for p in result['us']['points'])
    assert all(p['threshold']==-3 for p in result['taiwan']['points'])


def test_credit_uses_common_sessions_not_calendar_days():
    data=histories();data['HYG'].pop(25)
    current=calculate_current_risk(data)
    points=build_signal_trends(data,current)['taiwan']['points']
    assert len(points)==59
    assert points[-1]['value']==pytest.approx(current['taiwan']['relative_return_20d_pp'])


def test_breakout_line_uses_previous_five_closes_not_current():
    data=histories();data['^GSPC'][-1]['close']=500
    current=calculate_current_risk(data)
    point=build_signal_trends(data,current)['us_stop']['points'][-1]
    assert point['value']==500
    assert point['threshold']==178


def test_future_rows_excluded_to_match_saved_snapshot_date():
    data=histories();current=calculate_current_risk(data)
    original=deepcopy(build_signal_trends(data,current))
    for rows in data.values(): rows.append(dict(trade_date='2099-01-01',close=999,adjusted_close=999))
    assert build_signal_trends(data,current)==original


def test_spy_proxy_keeps_one_consistent_series():
    data=histories();del data['^GSPC']
    current=calculate_current_risk(data)
    result=build_signal_trends(data,current)['us_stop']
    assert result['symbol']=='SPY'
    assert result['points'][-1]['value']==current['us_stop']['close']


def test_no_history_does_not_fabricate_a_flat_chart():
    assert all(not x['points'] for x in build_signal_trends({},{}).values())


def test_repository_reads_only_bounded_saved_prices_and_releases_connection():
    cur=Mock();cur.fetchall.return_value=[('^VIX','2026-10-30',19.42,19.42)]
    conn=Mock();conn.cursor.return_value=cur;release=Mock()
    rows=read_signal_prices(lambda:conn,release,'2026-10-30')
    sql,params=cur.execute.call_args.args
    assert 'n<=81' in sql and 'INSERT' not in sql and 'UPDATE' not in sql
    assert params[1]=='2026-10-30'
    assert len(params[0])==6
    assert rows['^VIX'][0]['close']==19.42
    conn.commit.assert_not_called();release.assert_called_once_with(conn)
