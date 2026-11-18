from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import os
import threading
import pytest
from flask import Flask, request, jsonify, abort
import taiex_cron_auth
from screener_freshness import blackhorse_due, quote_date
from test_radar_admission import install

TZ=timezone(timedelta(hours=8))
NOW=datetime(2026,11,18,10,tzinfo=TZ)
calendar=lambda d:d.weekday()<5


def snapshot(quote='2026-11-18',inst='2026-11-17',age=10):
    return dict(source_date=inst,computed_at=(NOW-timedelta(seconds=age)).isoformat(),
        rows=[{'code':'2330'}],source_meta={'blackhorse_policy':1,'quote_date':quote})


def test_intraday_refresh_thirty_minutes_and_stale_quotes_are_distinct_from_institution_day():
    assert not blackhorse_due(snapshot(),NOW,True,calendar,'2026-11-17')[0]
    assert blackhorse_due(snapshot(age=1800),NOW,True,calendar,'2026-11-17')[0]
    assert blackhorse_due(snapshot(quote='2026-11-17'),NOW,True,calendar,'2026-11-17')[0]
    assert blackhorse_due(snapshot(quote='2026-11-19'),NOW,True,calendar,'2026-11-17')[0]
    assert blackhorse_due(snapshot(),NOW,True,calendar,'2026-11-18')[0]


def test_post_close_late_institution_release_refreshes_even_when_quote_date_current():
    after=NOW.replace(hour=20)
    assert blackhorse_due(snapshot(),after,False,calendar,'2026-11-18')[0]
    fresh=snapshot(inst='2026-11-18');fresh['computed_at']=(after-timedelta(seconds=10)).isoformat()
    assert not blackhorse_due(fresh,after,False,calendar,'2026-11-18')[0]
    assert blackhorse_due(snapshot(inst='2026-11-18'),after,False,calendar,'2026-11-18')[0]
    old=snapshot();old['source_meta']={}
    assert blackhorse_due(old,after,False,calendar,None)[0]


def test_quote_date_uses_source_data_not_fetch_time():
    observed=datetime(2026,11,17,13,30,tzinfo=TZ).timestamp()
    assert quote_date({'market_observed_at':observed,'close_dates':['2026-11-18']})=='2026-11-17'
    assert quote_date({'close_dates':['2026-11-18']})=='2026-11-18'
    assert quote_date({}) is None


@pytest.mark.parametrize('method,due,running,expected_calls', [('GET',True,False,0),('POST',True,False,1),('POST',False,False,0)])
def test_independent_job_status_reads_do_not_start_duplicate_market_scans(monkeypatch,method,due,running,expected_calls):
    monkeypatch.setattr(taiex_cron_auth,'authorized',lambda *a,**kw:True)
    start=Mock(return_value=True)
    ns=dict(request=request,jsonify=jsonify,abort=abort,os=os,
        DEFAULT_WEB_BASE_URL='https://stock-bot-6xct.onrender.com',
        _screener_recent_snapshot=lambda mode:(snapshot(),'saved'),_blackhorse_snapshot_due=lambda snap:(due,10),
        _start_screener_background_refresh=start,_SCREENER_REFRESH_LOCK=threading.Lock(),
        _SCREENER_REFRESH_RUNNING={'blackhorse':running})
    install(ns,'cron_blackhorse_refresh')
    app=Flask(__name__);app.add_url_rule('/run',view_func=ns['cron_blackhorse_refresh'],methods=['GET','POST'])
    result=app.test_client().open('/run',method=method)
    assert result.status_code==200 and start.call_count==expected_calls
    assert result.json['quote_date']=='2026-11-18' and result.json['institution_date']=='2026-11-17'


def test_independent_job_authentication_is_required(monkeypatch):
    monkeypatch.setattr(taiex_cron_auth,'authorized',lambda *a,**kw:False)
    ns=dict(request=request,jsonify=jsonify,abort=abort,os=os,DEFAULT_WEB_BASE_URL='https://stock-bot-6xct.onrender.com')
    install(ns,'cron_blackhorse_refresh');app=Flask(__name__)
    app.add_url_rule('/run',view_func=ns['cron_blackhorse_refresh'])
    assert app.test_client().get('/run').status_code==403


def blackhorse_engine(saved=True):
    from test_radar_coverage import engine
    ns=engine();codes=[str(1100+i) for i in range(10)]
    q={'close':100,'pct':2,'volume':1000000,'close_dates':['2026-11-18']}
    ns.update(SCREENER_CACHE_SECONDS=300,_screener_compute_lock=threading.Lock(),
        fetch_institutional_data=lambda:{},get_industry_map=lambda:{c:'industry' for c in codes},
        get_market_map=lambda:{},fetch_monthly_revenue=lambda:{},fetch_valuation=lambda:{},
        get_industry_momentum=lambda *a:{},stock_category=lambda *a:'電子',industry_name=lambda *a:'fixture',
        get_cumulative_net_buy=lambda **kw:[(c,c,1,1) for c in codes],get_consecutive_days_batch=lambda codes:{},
        calc_turnover_billion=lambda *a:2,score_stock_by_category=lambda *a:{'total':50,'category':'電子','pe':10,'pb':1,'yield':2},
        golden_cross_state=lambda p:'',macd_cross_state=lambda p:'',chip_above_ratio=lambda p:None,
        build_screener_data_quality=lambda *a,**kw:{},classify_radar_state=lambda p:'',
        _screener_source_date=lambda:NOW.date(),taiwan_now=lambda:NOW,
        _save_persisted_screener_snapshot=Mock(return_value=saved))
    ns['get_realtime_stocks_bulk'].side_effect=lambda codes,**kw:{c:q for c in codes}
    install(ns,'compute_screener_rows')
    return ns,codes


def test_blackhorse_forced_rebuild_updates_all_candidates_and_saves_actual_quote_date():
    ns,codes=blackhorse_engine()
    rows,_,_=ns['compute_screener_rows']('blackhorse',force_refresh=True,price_workers=16)
    assert {r['code'] for r in rows}==set(codes)
    calls=ns['get_realtime_stocks_bulk'].call_args_list
    assert len(calls)==3 and all(len(c.args[0])<=4 and c.kwargs['workers']==2 for c in calls)
    saved=ns['_save_persisted_screener_snapshot'].call_args.kwargs
    assert saved['source_meta']['quote_date']=='2026-11-18' and saved['source_date']==NOW.date()


def test_unconfirmed_save_does_not_replace_last_saved_blackhorse_memory_snapshot():
    ns,_=blackhorse_engine(False);previous={'rows':[{'code':'old'}]}
    ns['_screener_cache']['blackhorse']=previous
    with pytest.raises(RuntimeError,match='未確認保存'):
        ns['compute_screener_rows']('blackhorse',force_refresh=True)
    assert ns['_screener_cache']['blackhorse'] is previous


def test_new_quote_observation_is_not_backdated_to_yesterdays_institution_day():
    import json
    from datetime import date
    cur=Mock();cur.fetchone.return_value=(date(2026,11,17),[{'code':'2330','score':40}])
    conn=Mock();conn.cursor.return_value=cur
    ns=dict(get_db_connection=lambda:conn,release_db_connection=Mock(),json=json,
        taiwan_now=lambda:NOW,_jsonable=lambda value:value,
        _screener_source_date=lambda:date(2026,11,17))
    install(ns,'_save_persisted_screener_snapshot')
    assert ns['_save_persisted_screener_snapshot']('blackhorse',[{'code':'2330','score':50}],0,{},
        source_date=date(2026,11,17),source_meta={'blackhorse_policy':1,'quote_date':'2026-11-18'})
    queries=cur.execute.call_args_list
    assert not any('INSERT INTO screener_score_change_history' in call.args[0] for call in queries)
    insert=next(call for call in queries if 'INSERT INTO screener_result_snapshots' in call.args[0])
    assert insert.args[1][1]==date(2026,11,18)
    assert json.loads(insert.args[1][-1])['source_data_date']=='2026-11-17'


def test_snapshot_failure_cannot_commit_orphan_score_change_history():
    import json
    cur=Mock();cur.fetchone.return_value=(NOW.date(),[{'code':'2330','score':40}])
    def execute(sql,*args):
        if 'INSERT INTO screener_result_snapshots' in sql:raise OSError('fixture save failure')
    cur.execute.side_effect=execute;conn=Mock();conn.cursor.return_value=cur
    ns=dict(get_db_connection=lambda:conn,release_db_connection=Mock(),json=json,
        taiwan_now=lambda:NOW,_jsonable=lambda v:v,_screener_source_date=lambda:NOW.date())
    install(ns,'_save_persisted_screener_snapshot')
    assert not ns['_save_persisted_screener_snapshot']('blackhorse',[{'code':'2330','score':50}],0,{},
        source_date=NOW.date(),source_meta={'blackhorse_policy':1,'quote_date':'2026-11-18'})
    assert any('INSERT INTO screener_score_change_history' in c.args[0] for c in cur.execute.call_args_list)
    conn.commit.assert_not_called();conn.rollback.assert_called_once()
