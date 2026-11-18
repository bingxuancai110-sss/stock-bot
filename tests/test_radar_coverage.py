"""Coverage, batching and snapshot-policy regression tests for the real engine."""
from datetime import datetime
import re
from unittest.mock import Mock
import pytest
from test_radar_admission import install, producer


def engine():
    ns=producer()
    ns.update(re=re,RADAR_PRICE_BATCH_SIZE=4,RADAR_SCAN_POLICY_VERSION=2,
              _fetch_twse_mis_quotes=Mock(return_value={}),
              is_etf=lambda code:str(code).startswith('00'),
              get_realtime_stocks_bulk=Mock(side_effect=lambda codes,**kw:{c:None for c in codes}))
    for name in ('_radar_equity_universe','_radar_snapshot_matches_policy','_iter_screener_pool_prices'):
        install(ns,name)
    return ns


def test_equity_universe_excludes_four_digit_etf_and_non_equity_symbols():
    ns=engine()
    assert ns['_radar_equity_universe']({'0050':1,'2330':1,'00981A':1,'2383':1},
                                        {'0056':{},'2308':{},'2330':{},'bad':{}})==['2308','2330','2383']


def test_batch_size_does_not_reduce_240_candidate_coverage_or_order():
    ns=engine();pool=[(str(1000+i),{'index':i}) for i in range(240)]
    result=list(ns['_iter_screener_pool_prices'](pool,'radar',20,{},True))
    calls=ns['get_realtime_stocks_bulk'].call_args_list
    assert [code for code,info,price in result]==[code for code,_ in pool]
    assert len(calls)==60 and all(len(call.args[0])<=4 for call in calls)
    assert all(call.kwargs['workers']==2 for call in calls)
    assert ns['_background_work_memory_safe'].call_count==60


def test_batch_iterator_is_lazy_and_stops_without_publishing_partial_snapshot():
    ns=engine();before=ns['_screener_cache']['radar']
    ns['_background_work_memory_safe'].side_effect=[True,False]
    it=ns['_iter_screener_pool_prices']([(str(1000+i),{}) for i in range(8)],'radar',2,{},True)
    assert ns['get_realtime_stocks_bulk'].call_count==0
    for _ in range(4):next(it)
    with pytest.raises(RuntimeError,match='保留原快照'):next(it)
    assert ns['get_realtime_stocks_bulk'].call_count==1
    assert ns['_screener_cache']['radar'] is before


def test_blackhorse_is_bounded_without_reducing_total_candidates():
    ns=engine();pool=[(str(1000+i),{}) for i in range(10)]
    assert len(list(ns['_iter_screener_pool_prices'](pool,'blackhorse',16,{},False)))==10
    calls=ns['get_realtime_stocks_bulk'].call_args_list
    assert len(calls)==3 and all(len(c.args[0])<=4 and c.kwargs['workers']==2 for c in calls)
    assert ns['_background_work_memory_safe'].call_count==3


def test_previous_four_candidate_or_etf_snapshot_requires_rebuild():
    ns=engine();check=ns['_radar_snapshot_matches_policy']
    assert not check({'rows':[{'code':'2330'}],'radar_diagnostics':{'deep_candidate_count':4}})
    diag={'scan_policy_version':2,'deep_candidate_limit':240}
    assert not check({'rows':[{'code':'0050'}],'radar_diagnostics':diag})
    assert not check({'rows':[],'radar_diagnostics':{**diag,'deep_candidate_limit':4}})
    assert check({'rows':[{'code':'2330'}],'source_meta':{'radar_diagnostics':diag}})


def test_real_compute_scores_all_selected_candidates_and_excludes_etf():
    ns=engine();codes=[str(1000+i) for i in range(250)]
    ind={code:'industry' for code in codes};ind['0050']='ETF'
    quote={'close':100,'pct':2,'volume':1000000}
    ns.update(SCREENER_CACHE_SECONDS=300,_screener_compute_lock=__import__('threading').Lock(),
        fetch_institutional_data=lambda:{},get_industry_map=lambda:ind,get_market_map=lambda:{},
        _fetch_yahoo_spark_bulk=Mock(side_effect=lambda selected,**kw:{code:quote for code in selected}),
        stock_display_name=lambda code,**kw:code,calc_turnover_billion=lambda *a:2,
        get_cumulative_net_buy=lambda **kw:[],get_consecutive_days_batch=lambda codes:{},
        stock_category=lambda *a:'電子',industry_name=lambda code:'industry',
        score_stock_by_category=lambda *a:{'pe':10,'pb':1,'yield':2,'total':1,'category':'電子'},
        score_radar_breakout=lambda price:{'total':50},golden_cross_state=lambda p:'',
        macd_cross_state=lambda p:'',chip_above_ratio=lambda p:None,
        build_screener_data_quality=lambda *a,**kw:{},classify_radar_state=lambda p:'',
        _screener_source_date=lambda:datetime(2026,11,13).date(),
        taiwan_now=lambda:datetime(2026,11,15,19),_save_persisted_screener_snapshot=Mock(return_value=True))
    ns['get_realtime_stocks_bulk'].side_effect=lambda selected,**kw:{code:quote for code in selected}
    install(ns,'compute_screener_rows')
    rows,skipped,momentum=ns['compute_screener_rows']('radar',force_refresh=True,radar_deep_limit=240,price_workers=2)
    assert len(rows)==240 and skipped==0
    assert '0050' not in ns['_fetch_yahoo_spark_bulk'].call_args.args[0]
    diag=ns['_screener_cache']['radar']['radar_diagnostics']
    assert diag['deep_candidate_count']==240 and diag['final_count']==240
    assert diag['scan_policy_version']==2 and diag['deep_candidate_limit']==240
    assert ns['_radar_snapshot_matches_policy'](ns['_screener_cache']['radar'])
    ns['_save_persisted_screener_snapshot'].assert_called_once()


def test_240_candidate_scan_prefetches_live_feed_once_and_passes_only_each_batch():
    ns=engine();pool=[(str(1000+i),{}) for i in range(240)]
    ns['_fetch_twse_mis_quotes'].return_value={code:{'close':100} for code,_ in pool}
    list(ns['_iter_screener_pool_prices'](pool,'radar',2,{},True))
    ns['_fetch_twse_mis_quotes'].assert_called_once()
    assert ns['_fetch_twse_mis_quotes'].call_args.kwargs['quick_mode'] is True
    calls=ns['get_realtime_stocks_bulk'].call_args_list
    assert len(calls)==60
    assert all(c.kwargs['skip_mis'] and len(c.kwargs['quote_overrides'])==4 for c in calls)
    assert [code for c in calls for code in c.kwargs['quote_overrides']]==[code for code,_ in pool]
