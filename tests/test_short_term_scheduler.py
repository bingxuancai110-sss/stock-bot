from unittest.mock import Mock
import os
import pytest
from flask import Flask, request, jsonify, abort
import taiex_cron_auth
from test_radar_admission import install


@pytest.mark.parametrize('authorized,in_session,building,status,state',[
    (False,True,True,403,None),
    (True,False,False,200,'outside_session'),
    (True,True,True,202,'queued'),
    (True,True,False,503,'deferred'),
])
def test_independent_scheduler_authentication_session_and_ack(monkeypatch,authorized,in_session,building,status,state):
    auth=Mock(return_value=authorized)
    monkeypatch.setattr(taiex_cron_auth,'authorized',auth)
    service=Mock();service.payload.return_value={'meta':{'building':building,'progress':80,'progress_total':1979}}
    ns=dict(request=request,jsonify=jsonify,abort=abort,os=os,
        DEFAULT_WEB_BASE_URL='https://stock-bot-6xct.onrender.com',
        _is_taiwan_intraday_window=lambda:in_session,_short_term_service=service)
    install(ns,'cron_short_term_live')
    app=Flask(__name__);app.add_url_rule('/trigger',view_func=ns['cron_short_term_live'],methods=['POST'])
    result=app.test_client().post('/trigger')
    assert result.status_code==status
    if state:assert result.json['state']==state
    if authorized and in_session:service.payload.assert_called_once_with(refresh=True)
    else:service.payload.assert_not_called()
    assert auth.call_args.kwargs['audience'].endswith('/cron/short-term-live')
    assert auth.call_args.kwargs['workflow'].endswith('/short-term-intraday.yml@refs/heads/main')


def test_staggered_schedule_has_no_new_paid_service_or_shared_secret():
    from pathlib import Path
    workflow=Path('.github/workflows/short-term-intraday.yml').read_text()
    assert "5,15,25,35,45,55 1-4" in workflow and "5,15,25 5" in workflow
    assert 'id-token: write' in workflow and 'CRON_SECRET' not in workflow
