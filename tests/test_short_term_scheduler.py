from unittest.mock import Mock
from datetime import datetime
import os
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
