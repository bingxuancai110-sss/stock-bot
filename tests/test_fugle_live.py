from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from unittest.mock import Mock
import json

from flask import Flask, request, abort
import pytest
from fugle_live import normalize_quotes
from quote_lab import create_blueprint

NOW=datetime(2026,11,3,10,0,tzinfo=ZoneInfo('Asia/Taipei'))


def row(**kw):
    return dict(code='00981A',price=105,previous_close=100,time=int(NOW.timestamp()*1e6),observed_at=int(NOW.timestamp()*1000),**kw)


def test_official_trade_baseline_and_times_are_kept():
    q=normalize_quotes([row(mode='stream')],{'00981A'},NOW)['00981A']
    assert q['pct']==5 and q['previous_close']==100
    assert q['source']=='富果成交串流（本機直連）' and q['close_date']=='20261103'
    assert q['updated_at']=='2026-11-03 10:00:00'
    r=row();r['time']=int((NOW-timedelta(minutes=40)).timestamp()*1e6)
    assert normalize_quotes([r],{'00981A'},NOW)  # a quiet stream can still be alive


@pytest.mark.parametrize('change',[{'price':float('nan')},{'previous_close':0},{'is_trial':True},{'type':'ODDLOT'},
    {'time':int((NOW-timedelta(days=1)).timestamp()*1e6)}, {'time':int((NOW+timedelta(minutes=2)).timestamp()*1e6)},
    {'observed_at':int((NOW-timedelta(minutes=3)).timestamp()*1000)}, {'time':int(NOW.timestamp())}])
def test_bad_stale_trial_oddlot_or_wrong_units_do_not_become_current(change):
    r=row();r.update(change);assert normalize_quotes([r],{'00981A'},NOW)=={}


def test_foreign_holdings_duplicates_and_large_inputs_are_rejected():
    for raw,allowed in [([row()],set()),([row(),row()],{'00981A'}),([row()]*61,{'00981A'}),(None,{'00981A'})]:
        with pytest.raises(ValueError):normalize_quotes(raw,allowed,NOW)


def test_apply_is_owner_and_csrf_bound_and_does_not_receive_provider_key(monkeypatch):
    monkeypatch.setenv('FUGLE_TEST_USER_ID','owner')
    calc=Mock(return_value={'ok':True,'home':{},'positions':{}})
    def login(fn):
        @wraps(fn)
        def wrapper():
            uid=request.headers.get('X-User')
            if not uid:abort(401)
            return fn(uid)
        return wrapper
    app=Flask(__name__)
    app.register_blueprint(create_blueprint(web_login_required=login,render_page=lambda *a:'',get_db_connection=Mock(),release_db_connection=Mock(),
        csrf_token=lambda uid:'bound-'+uid,valid_csrf=lambda uid:request.form.get('csrf_token')=='bound-'+uid,apply_quotes=calc))
    c=app.test_client()
    assert c.get('/web/quote-lab/app-config').status_code==401
    assert c.get('/web/quote-lab/app-config',headers={'X-User':'other'}).status_code==403
    config=c.get('/web/quote-lab/app-config',headers={'X-User':'owner'})
    assert config.json['csrf_token']=='bound-owner' and 'no-store' in config.headers['Cache-Control']
    path='/web/quote-lab/apply'
    assert c.post(path,headers={'X-User':'other'}).status_code==403
    assert c.post(path,headers={'X-User':'owner'},data={'quotes':'[]'}).status_code==403
    calc.assert_not_called()
    response=c.post(path,headers={'X-User':'owner'},data={'csrf_token':'bound-owner','quotes':json.dumps([row()]),'uid':'other'})
    assert response.status_code==200 and 'no-store' in response.headers['Cache-Control']
    calc.assert_called_once_with('owner',[row()])
    assert c.post(path,headers={'X-User':'owner'},data={'csrf_token':'bound-owner','quotes':'invalid'}).status_code==400
    assert c.get(path,headers={'X-User':'owner'}).status_code==405
    assert c.post(path,headers={'X-User':'owner'},data={'quotes':'x'*70000}).status_code==413


def test_intraday_price_cannot_be_mislabelled_as_final_close():
    after=NOW.replace(hour=14)
    r=row();r['time']=int(NOW.replace(hour=13,minute=30).timestamp()*1e6);r['observed_at']=int(after.timestamp()*1000)
    assert normalize_quotes([r],{'00981A'},after)=={}
    r['closed']=True
    assert normalize_quotes([r],{'00981A'},after)['00981A']['close_is_final'] is True


def test_corporate_action_uses_provider_daily_reference_not_unadjusted_yesterday():
    r=row();r.update(price=95,previous_close=100,reference_price=95)
    q=normalize_quotes([r],{'00981A'},NOW)['00981A']
    assert q['pct']==0 and q['previous_close']==95
