"""Use the real holdings/fee/contribution functions with isolated positions and feeds."""
import ast
import threading
import time
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import Mock

NOW=datetime(2026,11,3,10,0,tzinfo=ZoneInfo('Asia/Taipei'))


def actual_namespace():
    ns=dict(datetime=datetime,date=date,threading=threading,time=time,
            _FUGLE_MARKET_CLOSE_LOCK=threading.Lock(),_FUGLE_MARKET_CLOSE_CACHE={"day":None,"data":None,"attempt_at":0,"running":False},
            _taiwan_post_close=lambda now=None:False,taiwan_now=lambda:NOW,taiwan_today=lambda:NOW.date(),
            stock_display_name=lambda code,inst=None,name=None:name or code,
            merge_positions=lambda p:p,get_positions=lambda uid:[dict(code='2308',shares=10,cost=90,lots=[]),dict(code='00981A',shares=20,cost=40,lots=[])],
            get_position_change_logs=lambda *a,**kw:[],get_realized_trades=lambda *a:[],
            fetch_taiex_intraday=lambda **kwargs:{'pct':1,'source':'fixture market','updated_at':'2026-11-03 10:00:00'},
            fetch_taiex_official_close=lambda **kw:{'pct':1},_is_taiwan_intraday_window=lambda now=None:True,
            get_profile=lambda uid:{},get_portfolio_target_weights=lambda uid:{'2308':50},
            _mis_quote_is_current_for_contribution=lambda q:True)
    tree=ast.parse((Path(__file__).parents[1]/'main.py').read_text())
    constants={'BROKER_FEE_RATE','TAX_RATE_STOCK','TAX_RATE_ETF','DEFAULT_FEE_DISCOUNT','DEFAULT_MIN_FEE'}
    for n in tree.body:
        if isinstance(n,ast.Assign) and any(getattr(t,'id','') in constants for t in n.targets):exec(compile(ast.Module(body=[n],type_ignores=[]),'<actual-constants>','exec'),ns)
    names={'_position_change_date','_build_daily_pretrade_exposure','_today_realized_by_code','is_etf','broker_fee','net_profit','get_fee_settings','_calculate_home_quote_payload','_apply_fugle_browser_quotes','_fugle_market_quote'}
    for n in tree.body:
        if isinstance(n,ast.FunctionDef) and n.name in names:exec(compile(ast.Module(body=[n],type_ignores=[]),'<actual-calculation>','exec'),ns)
    return ns


def quotes():
    return [dict(code=c,price=p,previous_close=y,time=int(NOW.timestamp()*1e6),observed_at=int(NOW.timestamp()*1000)) for c,p,y in [('2308',120,100),('00981A',45,50)]]


def test_fugle_prices_use_original_previous_close_weights_and_fee_calculation():
    import pytest
    ns=actual_namespace();r=ns['_apply_fugle_browser_quotes']('owner',quotes())
    assert r['home']['portfolio']['pct']==pytest.approx(5)
    assert r['home']['portfolio']['relative']==pytest.approx(4)
    totals=r['positions']['totals']
    assert totals['value']==2100 and totals['day_pl']==100 and totals['fee']==pytest.approx(44.5)
    assert totals['net_pl']==pytest.approx(355.5)
    assert sum(x['weight'] for x in r['positions']['updates'])==pytest.approx(100)
    assert r['positions']['updates'][0]['gross_pct']==pytest.approx(100/3)


def test_partial_fugle_coverage_does_not_show_a_partial_sum_as_whole_portfolio():
    r=actual_namespace()['_apply_fugle_browser_quotes']('owner',quotes()[:1])
    assert r['home']['portfolio']['pct'] is None and r['home']['portfolio']['stale_count']==1
    assert r['home']['portfolio']['ready'] is False
    assert r['positions']['totals']['complete'] is False and r['positions']['totals']['value'] is None
    assert r['positions']['updates'][0]['weight'] is None


def test_sale_segments_still_use_actual_trade_price():
    import pytest
    ns=actual_namespace()
    ns['get_position_change_logs']=lambda *a,**kw:[dict(code='2308',action='reduce',shares_delta=-10,trade_price=110,trade_date=NOW.date(),id=1,created_at='2026-11-03 09:30:00')]
    r=ns['_apply_fugle_browser_quotes']('owner',quotes())
    # Opening value = 20*100 + 20*50. Stock gain = 20*(110-100)+10*(120-110).
    # ETF loss = 20*(45-50). This is 200 / 3000, not current-weight times day return.
    assert r['home']['portfolio']['pct']==pytest.approx(200/3000*100)


def test_fugle_actual_index_replaces_missing_official_market():
    ns=actual_namespace()
    ns['fetch_taiex_intraday']=lambda **kw:None
    market=dict(code='IX0001',type='INDEX',price=19800,previous_close=20000,time=int(NOW.timestamp()*1e6),observed_at=int(NOW.timestamp()*1000))
    r=ns['_apply_fugle_browser_quotes']('owner',quotes(),market_raw=market)
    assert r['home']['portfolio']['relative']==6
    assert len(r['positions']['updates'])==2
    market['observed_at']-=121000
    r=ns['_apply_fugle_browser_quotes']('owner',quotes(),market_raw=market)
    assert r['home']['portfolio']['relative'] is None


def test_view_context_matches_partial_batch_but_changes_with_financial_inputs():
    ns=actual_namespace()
    full=ns['_apply_fugle_browser_quotes']('owner',quotes())
    partial=ns['_apply_fugle_browser_quotes']('owner',quotes()[:1])
    assert full['view_context']==partial['view_context']
    assert full['view_as_of']=='2026-11-03 10:00:00'
    positions=ns['get_positions']('owner')
    positions[0]['shares']+=1
    ns['get_positions']=lambda uid:positions
    changed=ns['_apply_fugle_browser_quotes']('owner',quotes()[:1])
    assert changed['view_context']!=full['view_context']
