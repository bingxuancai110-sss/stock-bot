from unittest.mock import Mock
from datetime import datetime
import os
import threading
import pytest
from flask import Flask, request, jsonify, abort
import taiex_cron_auth
from test_radar_admission import install


@pytest.mark.parametrize('authorized,in_session,building,status,state',[
    (False,True,True,403,None),
    (True,False,False,200,'outside_session'),
    (True,True,True,202,'queued'),
    (True,True,False,200,'deferred'),
])
def test_independent_scheduler_authentication_session_and_ack(monkeypatch,authorized,in_session,building,status,state):
    auth=Mock(return_value=authorized)
    monkeypatch.setattr(taiex_cron_auth,'authorized',auth)
    service=Mock();service.payload.return_value={'meta':{'building':building,'progress':80,'progress_total':1979}}
    ns=dict(request=request,jsonify=jsonify,abort=abort,os=os,
        DEFAULT_WEB_BASE_URL='https://stock-bot-6xct.onrender.com',
        taiwan_now=lambda:datetime(2026,11,19,10 if in_session else 8),
        is_twse_trading_day=lambda d:True,
        _is_taiwan_intraday_window=lambda:in_session,_short_term_service=service)
    install(ns,'cron_short_term_live')
    app=Flask(__name__);app.add_url_rule('/trigger',view_func=ns['cron_short_term_live'],methods=['POST'])
    result=app.test_client().post('/trigger')
    assert result.status_code==status
    if state:assert result.json['state']==state
    if authorized and in_session:service.payload.assert_called_once_with(schedule=True)
    else:service.payload.assert_not_called()
    assert auth.call_args.kwargs['audience'].endswith('/cron/short-term-live')
    assert auth.call_args.kwargs['workflow'].endswith('/short-term-intraday.yml@refs/heads/main')


def test_staggered_schedule_has_no_new_paid_service_or_shared_secret():
    from pathlib import Path
    workflow=Path('.github/workflows/short-term-intraday.yml').read_text()
    assert "5,15,25,35,45,55 1-4" in workflow and "5,15,25 5" in workflow
    assert 'id-token: write' in workflow and 'CRON_SECRET' not in workflow
    assert '5,15,25,35,45,55 6-12' in workflow


@pytest.mark.parametrize('hour,minute,open_day,saved,expected', [
    (14, 0, True, False, 'warming_up'),
    (14, 5, True, False, 'stalled'),
    (14, 5, True, True, 'fresh'),
    (14, 5, False, False, 'outside_session'),
])
def test_leaderboard_watchdog_checks_saved_data_after_close(hour, minute, open_day, saved, expected):
    from unittest.mock import MagicMock
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (False, datetime(2026, 11, 25, 13, 50) if saved else None)
    ns = dict(_schedule_daily_snapshot_recovery=Mock(), _market_monitor_reported={},
              _market_monitor_lock=threading.Lock(), is_twse_trading_day=lambda _: open_day,
              get_db_connection=Mock(return_value=conn), release_db_connection=Mock())
    install(ns, '_check_market_freshness')
    now = datetime(2026, 11, 25, hour, minute)
    assert ns['_check_market_freshness'](now)['state'] == expected
    assert ns['_schedule_daily_snapshot_recovery'].called == open_day
    if open_day:
        sql, args = cur.execute.call_args.args
        assert 's.data_date = %s' in sql and 'leaderboard_rank_snapshots' in sql
        assert args == (now.date(),)


def test_scheduled_verifier_fails_when_a_running_job_has_stalled(monkeypatch):
    from scripts import trigger_short_term
    monkeypatch.delenv('VERIFY_COMPLETE', raising=False)
    monkeypatch.setattr(trigger_short_term, 'call', Mock(return_value={
        'ok': False, 'monitor': {'state': 'stalled', 'building': True}}))
    with pytest.raises(RuntimeError, match='停滯'):
        trigger_short_term.main()


@pytest.mark.parametrize('method,hour,trading,expected_call',[
    ('POST',18,True,True),('POST',18,False,None),
    ('POST',13,True,None),('GET',18,True,False),('GET',8,False,False),
])
def test_close_scheduler_and_read_only_status(monkeypatch,method,hour,trading,expected_call):
    monkeypatch.setattr(taiex_cron_auth,'authorized',Mock(return_value=True))
    service=Mock();service.payload.return_value={'meta':dict(building=False,scan_complete=True,
        signals_current=True,scanned=1979,total=650,scope={'deep_total':1979},
        position_counts={'unknown':3},date='2026-11-19',generated_at='2026-11-19T18:00:00+08:00')}
    ns=dict(request=request,jsonify=jsonify,abort=abort,os=os,DEFAULT_WEB_BASE_URL='https://stock-bot-6xct.onrender.com',
        _is_taiwan_intraday_window=lambda:False,_short_term_service=service,
        taiwan_now=lambda:datetime(2026,11,19,hour),is_twse_trading_day=lambda d:trading)
    install(ns,'cron_short_term_live')
    app=Flask(__name__);app.add_url_rule('/trigger',view_func=ns['cron_short_term_live'],methods=['POST','GET'])
    result=app.test_client().open('/trigger',method=method)
    assert result.status_code==200
    if expected_call is None:
        assert result.json['state']=='outside_session';service.payload.assert_not_called()
    else:
        service.payload.assert_called_once_with(schedule=expected_call)
        assert result.json['state']=='fresh' and result.json['complete']
        assert result.json['progress']==1979 and result.json['confirmed']==647 and result.json['pending']==3


def test_manual_verifier_waits_for_saved_completion_and_resumes_when_deferred(monkeypatch):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('short_trigger',Path('scripts/trigger_short_term.py'))
    script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
    monkeypatch.setenv('VERIFY_COMPLETE','1')
    call=Mock(side_effect=[dict(ok=True,state='queued',building=True),
        dict(ok=True,state='deferred',building=False),dict(ok=True,state='fresh',complete=True,generated_at='saved')])
    monkeypatch.setattr(script,'call',call)
    monkeypatch.setattr(script.time,'monotonic',Mock(side_effect=[0,1,2,30,31,70]))
    monkeypatch.setattr(script.time,'sleep',Mock())
    script.main()
    assert [c.args[0] for c in call.call_args_list]==['POST','GET','POST']


@pytest.mark.parametrize('auth,manual,hour,trading,accepted',[
    (True,True,22,True,True),(True,False,22,True,False),
    (False,True,22,True,False),(True,True,22,False,False),
    (True,True,8,True,False),
])
def test_authenticated_manual_close_rebuild_keeps_schedule_guards(monkeypatch,auth,manual,hour,trading,accepted):
    monkeypatch.setattr(taiex_cron_auth,'authorized',Mock(return_value=auth))
    service=Mock();service.payload.return_value={'meta':{'building':True}}
    ns=dict(request=request,jsonify=jsonify,abort=abort,os=os,DEFAULT_WEB_BASE_URL='https://stock-bot-6xct.onrender.com',
        _is_taiwan_intraday_window=lambda:False,_short_term_service=service,
        taiwan_now=lambda:datetime(2026,11,20,hour),is_twse_trading_day=lambda d:trading)
    install(ns,'cron_short_term_live');app=Flask(__name__)
    app.add_url_rule('/trigger',view_func=ns['cron_short_term_live'],methods=['POST'])
    result=app.test_client().post('/trigger',headers={'X-Stockbot-Manual-Close':'1'} if manual else {})
    assert result.status_code==(202 if accepted else 200 if auth else 403)
    assert service.payload.called==accepted
