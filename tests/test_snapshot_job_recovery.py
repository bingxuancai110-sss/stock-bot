"""Exercise real snapshot/connection functions without app startup or network."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import pytest
from test_radar_admission import install


def db_context(connections, idle=None):
    pool=Mock();pool.getconn.side_effect=connections
    ns=dict(connection_pool=pool,pool=SimpleNamespace(PoolError=type('PoolError',(Exception,),{})),
            time=SimpleNamespace(monotonic=lambda:100.0,sleep=Mock()),threading=threading,
            _DB_CONN_LOCK=threading.Lock(),_DB_CONN_CHECKOUT={},_DB_CONN_LAST_RETURN=idle or {},
            _DB_POOL_STARTED_AT=0,_DB_CONN_IDLE_PROBE_SECONDS=30,DB_DIAG_ENABLED=False)
    install(ns,'get_db_connection');return ns


def test_idle_ssl_socket_is_discarded_before_business_query():
    bad=Mock(closed=0);bad.cursor.return_value.execute.side_effect=RuntimeError('SSL bad record mac')
    good=Mock(closed=0);ns=db_context([bad,good],{id(good):99})
    assert ns['get_db_connection']() is good
    ns['connection_pool'].putconn.assert_called_once_with(bad,close=True)
    good.cursor.assert_not_called()
    assert id(bad) not in ns['_DB_CONN_LAST_RETURN']


def test_idle_healthy_probe_does_not_leak_a_transaction():
    conn=Mock(closed=0);ns=db_context([conn])
    assert ns['get_db_connection']() is conn
    conn.cursor.return_value.execute.assert_called_once_with('SELECT 1')
    conn.cursor.return_value.close.assert_called_once();conn.rollback.assert_called_once()


def test_hot_connection_has_no_probe_round_trip():
    conn=Mock(closed=0);ns=db_context([conn],{id(conn):99})
    assert ns['get_db_connection']() is conn;conn.cursor.assert_not_called()


def test_broken_pool_retries_are_bounded():
    conns=[Mock(closed=0) for _ in range(3)]
    for c in conns:c.cursor.return_value.execute.side_effect=RuntimeError('EOF')
    ns=db_context(conns)
    with pytest.raises(RuntimeError,match='無法取得'):ns['get_db_connection']()
    assert ns['connection_pool'].getconn.call_count==3
    assert ns['connection_pool'].putconn.call_count==3


def daily_context(official,mis=None):
    today=date(2026,11,16);state={'saved':set(),'rank':False};cursor=Mock();sql=['']
    cursor.execute.side_effect=lambda q,*a:sql.__setitem__(0,q)
    cursor.fetchone.side_effect=lambda:(1,) if 'leaderboard_members' in sql[0] else ((4,) if state['rank'] else (0,))
    conn=Mock();conn.cursor.return_value=cursor
    def save(uid,*args):state['saved'].add(uid);return True
    save=Mock(side_effect=save)
    def rank(**kwargs):state['rank']=True;return 4
    ns=dict(taiwan_today=lambda:today,_job_get_progress=lambda _:dict(stage='portfolio',index=0,total=2),
            get_all_position_user_ids=lambda:['A','B'],
            get_missing_portfolio_snapshot_user_ids=lambda _:[u for u in ['A','B'] if u not in state['saved']],
            get_db_connection=lambda:conn,release_db_connection=Mock(),is_twse_trading_day=lambda _:True,
            _taiwan_post_close=lambda:True,_run_taiex_reduction_alerts=lambda:'no alert fixture',
            _job_mark_progress=Mock(),fetch_taiex_summary=lambda:dict(close=30000,date='20261116',close_is_final=True),
            _market_date_matches=lambda value,day:value=='20261116',
            get_positions=lambda uid:[dict(code='2330' if uid=='A' else '2308',shares=2,cost=50)],merge_positions=lambda p:p,
            _load_stock_info_file_cache=Mock(),_market_cache={'map':{'2330':'上市','2308':'上市'}},
            _fetch_official_daily_close_quotes=Mock(return_value=official),_fetch_twse_mis_quotes=Mock(return_value=mis or {}),
            save_portfolio_snapshot=save,save_leaderboard_rank_snapshots=Mock(side_effect=rank),
            _load_persisted_leaderboard_page=lambda **k:dict(data_date=today,value=({k:[{}] for k in ['short','long','season','month']},None)) if state['rank'] else None,
            _leaderboard_date=lambda value:value)
    install(ns,'_do_daily_snapshot');return ns,cursor


def quote(day='20261116'):
    return dict(close=100,close_date=day,close_is_final=True,source='TWSE MI_INDEX 官方正式收盤')


def test_official_closes_finish_core_without_mis_retry_tree():
    ns,cur=daily_context({'2330':quote(),'2308':quote()})
    assert '核心快照已完成' in ns['_do_daily_snapshot'](core_only_recovery=True)
    ns['_fetch_twse_mis_quotes'].assert_not_called()
    assert ns['save_portfolio_snapshot'].call_count==2
    ns['save_portfolio_snapshot'].assert_any_call('A',200,100,30000)
    ns['save_leaderboard_rank_snapshots'].assert_called_once_with(snapshot_date=date(2026,11,16))
    assert not any('DELETE FROM portfolio_snapshots' in c.args[0] for c in cur.execute.call_args_list)


def test_missing_close_uses_only_bounded_mis_and_never_publishes_partial_rank():
    ns,cur=daily_context({'2330':quote()},{'2308':quote('20261113')})
    assert '尚未完整' in ns['_do_daily_snapshot'](core_only_recovery=True)
    assert ns['save_portfolio_snapshot'].call_count==1
    ns['save_leaderboard_rank_snapshots'].assert_not_called()
    assert all(c.args[0]==['2308'] and c.kwargs['quick_mode'] for c in ns['_fetch_twse_mis_quotes'].call_args_list)
    assert not any('DELETE FROM portfolio_snapshots' in c.args[0] for c in cur.execute.call_args_list)


def test_official_failure_falls_back_to_valid_same_day_mis():
    mis={c:dict(quote(),source='TWSE MIS 官方正式收盤') for c in ['2330','2308']}
    ns,_=daily_context({},mis);ns['_fetch_official_daily_close_quotes'].side_effect=RuntimeError('temporary')
    assert '核心快照已完成' in ns['_do_daily_snapshot'](core_only_recovery=True)
    assert ns['save_portfolio_snapshot'].call_count==2


def test_radar_quote_overrides_reach_history_calculation_without_refetching_mis():
    from concurrent.futures import ThreadPoolExecutor
    price=Mock(side_effect=lambda code,*a,**k:{'code':code,'close':(k.get('official_quote') or {}).get('close',90)})
    ns=dict(ThreadPoolExecutor=ThreadPoolExecutor,_market_cache={'map':{'2330':'上市','2308':'上市'}},
            _suffix_cache={},_load_stock_info_file_cache=Mock(),get_realtime_stock=price,
            _fetch_twse_mis_quotes=Mock(side_effect=AssertionError('duplicate MIS request')))
    install(ns,'get_realtime_stocks_bulk')
    value=ns['get_realtime_stocks_bulk'](['2330','2308'],workers=2,skip_mis=True,quote_overrides={'2330':{'close':100}})
    assert value['2330']['close']==100 and value['2308']['close']==90
    ns['_fetch_twse_mis_quotes'].assert_not_called()
    assert all(c.kwargs['include_mis'] is False for c in price.call_args_list)
