"""An unavailable market source must not gate page HTML or private Fugle calculations."""
import ast
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

NOW=datetime(2026,11,3,13,25,tzinfo=ZoneInfo('Asia/Taipei'))


@lru_cache(maxsize=1)
def main_tree():
    return ast.parse((Path(__file__).parents[1]/'main.py').read_text(encoding='utf-8'))


@lru_cache(maxsize=1)
def functions():
    tree=main_tree()
    names={'_get_homepage_render_prices','_homepage_merge_official_quotes','_fugle_market_quote',
           '_usable_saved_home_final_quotes','_is_completed_home_close_payload'}
    return compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),'<production-home>','exec')


@lru_cache(maxsize=1)
def official_close_function():
    fn=next(n for n in main_tree().body if isinstance(n,ast.FunctionDef) and n.name=='fetch_taiex_official_close')
    return compile(ast.Module(body=[fn],type_ignores=[]),'<official-close-cache>','exec')


@lru_cache(maxsize=1)
def official_history_close_function():
    fn=next(n for n in main_tree().body if isinstance(n,ast.FunctionDef) and n.name=='_fetch_taiex_historical_close_uncached')
    return compile(ast.Module(body=[fn],type_ignores=[]),'<official-close-history>','exec')


def test_home_render_uses_local_quote_without_even_starting_a_live_request():
    network=Mock(side_effect=AssertionError('network attempted while rendering'))
    history={'2308':dict(close=110,pct=1,source='history',close_dates=[date(2026,11,1),date(2026,11,2)],closes=[100,110])}
    live=dict(close=123,previous_close=120,pct=2.5,close_date='20261103',source='TWSE MIS',has_live_trade_price=True,updated_at='2026-11-03 13:24:00')
    cache=Mock(return_value=history)
    ns=dict(_get_homepage_prices_cached=cache,_fetch_twse_mis_quotes=network,_fetch_yahoo_intraday_backup_quotes=network,
            _home_market_suffix_map=network,_realtime_cache_lock=threading.Lock(),
            _TWSE_MIS_CACHE={'day':'2026-11-03','data':{'2308':live}},_sticky_home_quotes=lambda *a:{},
            _mis_quote_is_current_for_contribution=lambda q:q.get('close_date')=='20261103',taiwan_today=lambda:NOW.date())
    exec(functions(),ns)
    result=ns['_get_homepage_render_prices']('owner',['2308'])
    assert result['2308']['close']==123 and result['2308']['closes']==[100,110,123]
    cache.assert_called_once_with('owner',['2308'],refresh_official=False);network.assert_not_called()


def test_missing_official_price_does_not_force_network_or_relabel_old_history():
    ns=dict(_get_homepage_prices_cached=lambda *a,**k:{'2308':{'close':110,'source':'historical','close_date':'20261102'}},
        _fetch_twse_mis_quotes=Mock(side_effect=AssertionError('should not fetch')),
        _realtime_cache_lock=threading.Lock(),_TWSE_MIS_CACHE={'day':'2026-11-03','data':{}},
        _sticky_home_quotes=lambda *a:{},_mis_quote_is_current_for_contribution=lambda q:False,taiwan_today=lambda:NOW.date())
    exec(functions(),ns);q=ns['_get_homepage_render_prices']('owner',['2308'])['2308']
    assert q['close_date']=='20261102' and q['source']=='historical'
    ns['_fetch_twse_mis_quotes'].assert_not_called()


def test_private_stock_calculation_requests_only_nonblocking_intraday_index():
    fetch=Mock(return_value=None)
    ns=dict(_is_taiwan_intraday_window=lambda now:True,fetch_taiex_intraday=fetch)
    exec(functions(),ns)
    assert ns['_fugle_market_quote'](NOW) is None
    fetch.assert_called_once_with(stale_while_revalidate=True)


def test_closing_index_refresh_runs_once_in_background_and_does_not_reuse_yesterday():
    jobs=[];fetch=Mock(return_value={'pct':-1,'source':'TWSE MIS 正式收盤'})
    class Thread:
        def __init__(self,*,target,**kwargs):jobs.append(target)
        def start(self):pass
    ns=dict(_is_taiwan_intraday_window=lambda now:False,_taiwan_post_close=lambda now:True,
        _FUGLE_MARKET_CLOSE_LOCK=threading.Lock(),_FUGLE_MARKET_CLOSE_CACHE={'day':'2026-11-02','data':{'pct':50},'attempt_at':0,'running':False},
        fetch_taiex_official_close=fetch,time=time,threading=SimpleNamespace(Thread=Thread))
    exec(functions(),ns)
    assert ns['_fugle_market_quote'](NOW) is None
    assert ns['_fugle_market_quote'](NOW) is None and len(jobs)==1
    fetch.assert_not_called();jobs[0]()
    assert ns['_fugle_market_quote'](NOW)['pct']==-1 and len(jobs)==1


def test_official_close_restores_same_day_persisted_snapshot_without_network_fetch():
    saved_payload={'date':'20261103','close':'27000.00','previous_close':'26800.00',
        'pct':'+0.75','source':'TWSE MIS 加權指數正式收盤','close_is_final':True}
    load=Mock(return_value={'data_date':NOW.date(),'payload':saved_payload})
    fetch=Mock(side_effect=AssertionError('same-day close should be restored'))
    state={'day':None,'data':None,'attempt_at':0,'running':False,'snapshot_checked_day':None}
    ns=dict(_taiwan_post_close=lambda current:True,taiwan_now=lambda:NOW,time=time,
        _TAIEX_OFFICIAL_CLOSE_LOCK=threading.Lock(),_TAIEX_OFFICIAL_CLOSE_CACHE=state,
        TAIEX_OFFICIAL_CLOSE_RETRY_SECONDS=30,TAIEX_OFFICIAL_CLOSE_SNAPSHOT_KEY='taiex_official_close',
        _load_shared_data_snapshot=load,_fetch_taiex_official_close_uncached=fetch,
        _save_shared_data_snapshot=Mock())
    exec(official_close_function(),ns)
    first=ns['fetch_taiex_official_close'](NOW)
    second=ns['fetch_taiex_official_close'](NOW)
    assert first['close']=='27000.00' and first['persisted'] is True
    assert second['close']=='27000.00'
    load.assert_called_once_with('taiex_official_close',max_age_seconds=7*86400)
    fetch.assert_not_called()


def test_new_official_close_is_saved_once_and_tagged_by_trade_date():
    value={'date':'20261103','close':'27000.00','previous_close':'26800.00',
        'pct':'+0.75','source':'TWSE MIS 加權指數正式收盤','close_is_final':True}
    save=Mock(return_value=True)
    ns=dict(_taiwan_post_close=lambda current:True,taiwan_now=lambda:NOW,time=time,
        _TAIEX_OFFICIAL_CLOSE_LOCK=threading.Lock(),
        _TAIEX_OFFICIAL_CLOSE_CACHE={'day':None,'data':None,'attempt_at':0,'running':False,
            'snapshot_checked_day':None},TAIEX_OFFICIAL_CLOSE_RETRY_SECONDS=30,
        TAIEX_OFFICIAL_CLOSE_SNAPSHOT_KEY='taiex_official_close',
        TAIEX_OFFICIAL_CLOSE_ATTEMPT_KEY='taiex_official_close_attempt',
        _load_shared_data_snapshot=Mock(return_value=None),
        _fetch_taiex_official_close_uncached=Mock(return_value=value),
        _save_shared_data_snapshot=save)
    exec(official_close_function(),ns)
    assert ns['fetch_taiex_official_close'](NOW)==value
    save.assert_called_once_with('taiex_official_close',value,data_date=NOW.date(),
        source_meta={'source':'TWSE MIS 加權指數正式收盤','day':'2026-11-03'})


def test_historical_official_close_fallback_is_persisted_too():
    value={'date':'20261103','close':'27000.00','previous_close':'26800.00',
        'pct':'+0.75','source':'TWSE MI_5MINS_HIST 官方正式收盤','close_is_final':True}
    save=Mock(return_value=True)
    ns=dict(_taiwan_post_close=lambda current:True,taiwan_now=lambda:NOW,time=time,
        _TAIEX_OFFICIAL_CLOSE_LOCK=threading.Lock(),
        _TAIEX_OFFICIAL_CLOSE_CACHE={'day':None,'data':None,'attempt_at':0,'running':False,
            'snapshot_checked_day':None},TAIEX_OFFICIAL_CLOSE_RETRY_SECONDS=30,
        TAIEX_OFFICIAL_CLOSE_SNAPSHOT_KEY='taiex_official_close',
        TAIEX_OFFICIAL_CLOSE_ATTEMPT_KEY='taiex_official_close_attempt',
        _load_shared_data_snapshot=Mock(return_value=None),
        _fetch_taiex_official_close_uncached=Mock(return_value=value),
        _save_shared_data_snapshot=save,_delete_shared_data_snapshot=Mock())
    exec(official_close_function(),ns)
    assert ns['fetch_taiex_official_close'](NOW)==value
    save.assert_called_once_with('taiex_official_close',value,data_date=NOW.date(),
        source_meta={'source':'TWSE MI_5MINS_HIST 官方正式收盤','day':'2026-11-03'})


def test_official_historical_index_fallback_parses_and_returns_same_day_close():
    response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{
        'stat':'OK','fields':['日期','開盤指數','最高指數','最低指數','收盤指數'],
        'data':[['115/11/02','27000','27100','26900','27000.00'],
                ['115/11/03','27050','27300','27000','27200.00']]})
    get=Mock(return_value=response)
    ns=dict(_taiwan_post_close=lambda current:True,taiwan_now=lambda:NOW,
        requests=SimpleNamespace(get=get),_taiex_live_number=lambda value:float(str(value).replace(',','')) if value else None,
        re=__import__('re'),date=date,timedelta=__import__('datetime').timedelta)
    exec(official_history_close_function(),ns)
    value=ns['_fetch_taiex_historical_close_uncached'](NOW)
    assert value['close']=='27,200.00' and value['previous_close']=='27,000.00'
    assert value['pct']=='+0.74' and value['date']=='20261103'
    assert value['close_is_final'] is True and 'MI_5MINS_HIST' in value['source']
    get.assert_called_once()


def test_saved_home_closes_are_reused_only_when_all_symbols_are_final_today():
    quotes={code:{'close_date':'20261103','close_is_final':True,'close':100}
        for code in ('2308','2383')}
    ns=dict(_taiwan_post_close=lambda current:True,
        _mis_quote_is_current_for_contribution=lambda q,now=None:bool(q and q.get('close_date')=='20261103' and q.get('close_is_final')))
    exec(functions(),ns)
    assert set(ns['_usable_saved_home_final_quotes'](quotes,['2308','2383'],NOW))=={'2308','2383'}
    assert ns['_usable_saved_home_final_quotes'](quotes,['2308','6488'],NOW) is None
    assert ns['_usable_saved_home_final_quotes']({**quotes,'2308':dict(quotes['2308'],close_is_final=False)},['2308'],NOW) is None
    partial=ns['_usable_saved_home_final_quotes'](quotes,['2308','6488'],NOW,require_all=False)
    assert set(partial)=={'2308'}


def test_completed_close_snapshot_requires_final_same_day_market_and_holdings_quotes():
    payload={'market_open':False,'market':{'source':'TWSE MIS 加權指數正式收盤','date':'20261103'},
        'portfolio':{'ready':True,'stale_count':0},
        'quotes':{'2308':{'close_date':'20261103','close_is_final':True}}}
    ns=dict(taiwan_today=lambda:NOW.date())
    exec(functions(),ns)
    assert ns['_is_completed_home_close_payload'](payload)
    assert not ns['_is_completed_home_close_payload']({**payload,'market':{'source':'正式收盤','date':'20261102'}})
    assert not ns['_is_completed_home_close_payload']({**payload,'portfolio':{'ready':True,'stale_count':1}})


def test_incomplete_home_refresh_uses_exponential_backoff():
    tree=ast.parse((Path(__file__).parents[1]/'main.py').read_text(encoding='utf-8'))
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and
            n.name=='_schedule_home_intraday_payload_refresh')
    now={'value':100.0}
    class Clock:
        @staticmethod
        def time():return now['value']
        @staticmethod
        def monotonic():return now['value']
    class ImmediateThread:
        def __init__(self,*,target,**kwargs):self.target=target
        def start(self):self.target()
    ns=dict(time=Clock,threading=SimpleNamespace(Thread=ImmediateThread),
        _HOME_INTRADAY_REFRESH_LOCK=threading.Lock(),_HOME_INTRADAY_REFRESH_RUNNING=set(),
        _HOME_INTRADAY_REFRESH_LAST_START={},_HOME_INTRADAY_REFRESH_MIN_GAP=5.0,
        _HOME_INTRADAY_REFRESH_FAILURES={},_HOME_INTRADAY_REFRESH_RETRY_AFTER={},
        _HOME_INTRADAY_REFRESH_BACKOFF_MIN=15.0,_HOME_INTRADAY_REFRESH_BACKOFF_MAX=120.0,
        _refresh_home_intraday_payload_sync=lambda uid:{'ok':True,'market_open':True,
            'portfolio':{'stale_count':2,'positive':[],'negative':[]}},
        _HOME_INTRADAY_PAYLOAD_CACHE_LOCK=threading.Lock(),_HOME_INTRADAY_PAYLOAD_CACHE={},
        taiwan_today=lambda:NOW.date(),_save_home_intraday_payload_snapshot=lambda *a:None,
        _taiwan_post_close=lambda:False)
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'<home-refresh-scheduler>','exec'),ns)
    assert ns['_schedule_home_intraday_payload_refresh']('owner') is True
    assert ns['_HOME_INTRADAY_REFRESH_RETRY_AFTER']['owner']==115.0
    now['value']=101.0
    assert ns['_schedule_home_intraday_payload_refresh']('owner') is False
    now['value']=115.0
    assert ns['_schedule_home_intraday_payload_refresh']('owner') is True
    assert ns['_HOME_INTRADAY_REFRESH_RETRY_AFTER']['owner']==145.0


def test_failed_official_close_request_is_shared_and_cooled_down():
    now={'value':100.0};calls=[]
    class Clock:
        @staticmethod
        def time():return now['value']
    ns=dict(time=Clock,_taiwan_post_close=lambda current:True,
        taiwan_now=lambda:NOW,_TAIEX_OFFICIAL_CLOSE_LOCK=threading.Lock(),
        _TAIEX_OFFICIAL_CLOSE_CACHE={'day':None,'data':None,'attempt_at':0,'running':False,
            'snapshot_checked_day':None},
        TAIEX_OFFICIAL_CLOSE_RETRY_SECONDS=30,
        TAIEX_OFFICIAL_CLOSE_SNAPSHOT_KEY='taiex_official_close',
        TAIEX_OFFICIAL_CLOSE_ATTEMPT_KEY='taiex_official_close_attempt',
        _load_shared_data_snapshot=Mock(return_value=None),
        _save_shared_data_snapshot=Mock(),
        _fetch_taiex_official_close_uncached=lambda current:(calls.append(current) or None))
    exec(official_close_function(),ns)
    assert ns['fetch_taiex_official_close'](NOW) is None
    now['value']=101.0
    assert ns['fetch_taiex_official_close'](NOW) is None
    assert len(calls)==1
    now['value']=130.0
    ns['fetch_taiex_official_close'](NOW)
    assert len(calls)==2


def test_failed_close_cooldown_is_restored_after_process_restart():
    future_retry={'data_date':NOW.date(),'payload':{'day':'2026-11-03','retry_after_epoch':2000}}
    load=Mock(side_effect=[None,future_retry])
    fetch=Mock(side_effect=AssertionError('persisted cooldown should suppress upstream request'))
    ns=dict(time=SimpleNamespace(time=lambda:1000),_taiwan_post_close=lambda current:True,
        taiwan_now=lambda:NOW,_TAIEX_OFFICIAL_CLOSE_LOCK=threading.Lock(),
        _TAIEX_OFFICIAL_CLOSE_CACHE={'day':None,'data':None,'attempt_at':0,'running':False,
            'snapshot_checked_day':None},TAIEX_OFFICIAL_CLOSE_RETRY_SECONDS=300,
        TAIEX_OFFICIAL_CLOSE_SNAPSHOT_KEY='taiex_official_close',
        TAIEX_OFFICIAL_CLOSE_ATTEMPT_KEY='taiex_official_close_attempt',
        _load_shared_data_snapshot=load,_save_shared_data_snapshot=Mock(),
        _fetch_taiex_official_close_uncached=fetch,_delete_shared_data_snapshot=Mock())
    exec(official_close_function(),ns)
    assert ns['fetch_taiex_official_close'](NOW) is None
    assert ns['fetch_taiex_official_close'](NOW) is None
    assert load.call_count==2 and fetch.call_count==0
