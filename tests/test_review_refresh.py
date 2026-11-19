from datetime import datetime
from unittest.mock import Mock
import os,threading
import pytest
from flask import Flask,request,jsonify,abort
import taiex_cron_auth
from test_radar_admission import install


@pytest.mark.parametrize('authorized,method,building,status,state',[
    (False,'GET',False,403,None),(True,'GET',False,200,'fresh'),
    (True,'POST',True,200,'building'),
])
def test_review_refresh_is_scoped_and_get_is_read_only(monkeypatch,authorized,method,building,status,state):
    auth=Mock(return_value=authorized);monkeypatch.setattr(taiex_cron_auth,'authorized',auth)
    build=Mock()
    ns=dict(request=request,jsonify=jsonify,abort=abort,os=os,
        DEFAULT_WEB_BASE_URL='https://stock-bot-6xct.onrender.com',
        build_workbench_review_payload=build,_workbench_review_lock=threading.Lock(),
        _WORKBENCH_REVIEW_REFRESH_RUNNING=building,_WORKBENCH_REVIEW_CACHE_SECONDS=900,
        time=Mock(time=lambda:123),_workbench_review_cache=dict(at=100,computed_at='saved',
        evaluations={'turning':{'total_picks':10,'horizons':{'5–19 日':{'n':8}}}}))
    install(ns,'cron_review_refresh');app=Flask(__name__)
    app.add_url_rule('/run',view_func=ns['cron_review_refresh'],methods=['GET','POST'])
    response=app.test_client().open('/run',method=method)
    assert response.status_code==status
    if state:
        assert response.json['state']==state and response.json['modes']['turning']['samples']==8
    if authorized and method=='POST':build.assert_called_once()
    else:build.assert_not_called()
    assert auth.call_args.kwargs['workflow'].endswith('/review-refresh.yml@refs/heads/main')


def test_review_verifier_does_not_treat_enqueue_as_saved_completion(monkeypatch):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('review_runner',Path('scripts/refresh_review.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    call=Mock(side_effect=[dict(ok=True,state='building',building=True),
        dict(ok=True,state='fresh',building=False,computed_at='saved')])
    monkeypatch.setattr(module,'call',call)
    monkeypatch.setattr(module.time,'monotonic',Mock(return_value=0))
    sleep=Mock();monkeypatch.setattr(module.time,'sleep',sleep)
    module.main();assert [c.args[0] for c in call.call_args_list]==['POST','GET']
    sleep.assert_called_once_with(20)
