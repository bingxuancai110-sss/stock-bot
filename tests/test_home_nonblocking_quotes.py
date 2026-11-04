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
def functions():
    tree=ast.parse((Path(__file__).parents[1]/'main.py').read_text())
    names={'_get_homepage_render_prices','_homepage_merge_official_quotes','_fugle_market_quote'}
    return compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),'<production-home>','exec')


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


def test_incomplete_home_refresh_uses_exponential_backoff():
    tree=ast.parse((Path(__file__).parents[1]/'main.py').read_text())
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
    tree=ast.parse((Path(__file__).parents[1]/'main.py').read_text())
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and
            n.name=='fetch_taiex_official_close')
    now={'value':100.0};calls=[]
    class Clock:
        @staticmethod
        def time():return now['value']
    ns=dict(time=Clock,_taiwan_post_close=lambda current:True,
        taiwan_now=lambda:NOW,_TAIEX_OFFICIAL_CLOSE_LOCK=threading.Lock(),
        _TAIEX_OFFICIAL_CLOSE_CACHE={'day':None,'data':None,'attempt_at':0,'running':False},
        TAIEX_OFFICIAL_CLOSE_RETRY_SECONDS=30,
        _fetch_taiex_official_close_uncached=lambda current:(calls.append(current) or None))
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'<official-close-cache>','exec'),ns)
    assert ns['fetch_taiex_official_close'](NOW) is None
    now['value']=101.0
    assert ns['fetch_taiex_official_close'](NOW) is None
    assert len(calls)==1
    now['value']=130.0
    ns['fetch_taiex_official_close'](NOW)
    assert len(calls)==2
