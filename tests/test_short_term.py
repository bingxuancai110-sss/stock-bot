from datetime import datetime, timedelta
from unittest.mock import Mock
import copy
import pytest
from flask import Flask
import short_term as st


def market(n=100):
    dates=[];d=datetime(2026,5,1)
    while len(dates)<n:
        if d.weekday()<5: dates.append(d.strftime('%Y-%m-%d'))
        d+=timedelta(days=1)
    bars=[]
    for i,date in enumerate(dates):
        c=100+i*.1 if i<70 else 108+(i-70)*.25
        bars.append(dict(date=date,open=c-.3,high=c+.2,low=c-.6,close=c,volume=1_000_000 if i<70 else 1_400_000))
    history=[dict(date=d,foreign=40.,trust=10.) for d in dates]
    return bars, history


def quote(bars):
    return dict(close_dates=[b['date'] for b in bars],opens=[b['open'] for b in bars],
                highs=[b['high'] for b in bars],lows=[b['low'] for b in bars],
                closes=[b['close'] for b in bars],volumes=[b['volume'] for b in bars],source='fixture-only')


def now(bars):
    return datetime.fromisoformat(bars[-1]['date']+'T16:00:00')


def test_real_rules_find_breakout_with_aligned_institutional_evidence():
    bars,h=market(75)
    result=st.analyse('2330','fixture',quote(bars),h,now(bars))
    assert result and result['signals'][0]['type']=='entry'
    assert result['signals'][0]['date']==bars[70]['date']
    assert result['signals'][0]['stop']<result['signals'][0]['price']<result['signals'][0]['target']
    assert result['chip']['date']<result['date']


@pytest.mark.parametrize('kind',['thin','hot','missing','selling','null'])
def test_technical_entry_is_not_blocked_by_lot_size_or_auxiliary_chips(kind):
    bars,h=market(71)
    if kind=='thin':
        for b in bars:b['volume']=10000
        bars[-1]['volume']=14000
    elif kind=='hot':
        bars[-1].update(open=115,low=114,high=121,close=120)
    elif kind=='missing':h=[]
    elif kind=='selling':
        for r in h:r.update(foreign=-40,trust=-10)
    else:h[-3]['foreign']=None
    result=st.analyse('2330','fixture',quote(bars),h,now(bars))
    if kind=='hot':
        assert result is None
    else:
        assert result['entry_ready'] and result['current_action']=='entry'
        assert result['signals'][-1]['type']=='entry'
        assert '法人僅供參考' in result['signals'][-1]['reason']
        assert not any(c['label']=='籌碼' for c in result['checks'])


def test_today_chip_report_is_not_used_for_today_entry():
    bars,h=market(71)
    for r in h:r.update(foreign=-40,trust=-10)
    h[-1].update(foreign=999999,trust=999999)
    result=st.analyse('2330','fixture',quote(bars),h,now(bars))
    assert result['entry_ready'] and result['chip']['foreign']==-200
    assert result['chip']['date']==bars[-2]['date']


def test_future_bars_cannot_change_existing_signals_or_trailing_history():
    bars,h=market(95)
    early=st.analyse('2330','fixture',quote(bars[:75]),h,now(bars[:75]))
    later=st.analyse('2330','fixture',quote(bars),h,now(bars))
    end=bars[74]['date']
    assert early['signals']==[s for s in later['signals'] if s['date']<=end]
    assert early['trailing']==[t for t in later['trailing'] if t['date']<=end]
    assert all(b['value']>=a['value'] for a,b in zip(later['trailing'],later['trailing'][1:]))


def test_stop_wins_over_target_in_same_daily_bar_and_gap_is_not_filled_at_stop():
    bars,h=market(73)
    early=st.analyse('2330','fixture',quote(bars[:72]),h,now(bars[:72]))
    stop=early['stop']; target=early['target']
    bars[-1].update(open=stop-1,low=stop-2,close=stop-.5,high=target+5)
    out=st.analyse('2330','fixture',quote(bars),h,now(bars))
    today=[s for s in out['signals'] if s['date']==bars[-1]['date']]
    assert [s['type'] for s in today]==['exit']
    assert today[0]['price']==round(stop-1,2)
    assert not out['active'] and out['current_action']=='exit'


def test_additions_are_limited_and_cannot_expand_initial_downside_budget():
    bars,h=market(100)
    bars[90]['volume']=2_200_000  # after confirmed support releases enough risk budget
    out=st.analyse('2330','fixture',quote(bars),h,now(bars))
    adds=[s for s in out['signals'] if s['type']=='add']
    assert len(adds)==1
    entry=out['signals'][0]; add=adds[0]
    assert add['price']>=entry['price']+(entry['price']-entry['stop'])-.02
    cost=entry['price']+.5*add['price']
    assert cost-1.5*add['stop']<=entry['price']-entry['stop']+.03


def test_pressure_rejection_above_the_trend_does_not_automatically_reduce():
    bars,h=market(78)
    initial=st.analyse('2330','fixture',quote(bars[:77]),h,now(bars[:77]))
    target=initial['target']; stop=initial['stop']
    bars[-1].update(open=target-.1,high=target+.5,low=max(stop+.1,target-1),close=target-.8)
    out=st.analyse('2330','fixture',quote(bars),h,now(bars))
    reductions=[s for s in out['signals'] if s['type']=='reduce']
    assert reductions==[] and out['active']


def test_unfinished_bar_invalid_ohlc_and_price_scaling_are_not_signals():
    bars,h=market(71);q=quote(bars)
    assert len(st.normalize_bars(q,now(bars).replace(hour=10)))==70
    broken=copy.deepcopy(q);broken['highs'][-1]=1
    assert st.normalize_bars(broken,now(bars))==[]
    broken=copy.deepcopy(q);broken['close_dates'][-1]=broken['close_dates'][-2]
    assert st.normalize_bars(broken,now(bars))==[]
    split=copy.deepcopy(bars)
    for b in split[-2:]:
        for k in ('open','high','low','close'):b[k]/=10
    assert st.analyse('2330','fixture',quote(split),h,now(bars)) is None


def service(safe=True):
    s=st.ShortTermService(connect=Mock(),store=Mock(load=Mock(return_value=None)),
        quote=Mock(),executor=Mock(),safe=Mock(return_value=safe),
        now=lambda:datetime(2026,11,17,16),equity=lambda c:True)
    return s


def test_all_viewers_share_one_background_job_and_get_no_fake_rows():
    s=service()
    for _ in range(4):
        p=s.payload()
        assert p['meta']['building'] and p['rows']==[]
    s.executor.submit.assert_called_once()
    s.store.load.assert_called_once()


def test_memory_rejection_preserves_saved_rows_and_does_not_submit_work():
    s=service(False)
    s.store.load.return_value={'payload':{'rows':[{'code':'2330'}],'generated_at':'2026-11-16T16:00:00','date':'2026-11-16'}}
    p=s.payload()
    assert p['rows'][0]['code']=='2330' and p['rows'][0]['current_action']=='unconfirmed'
    assert p['meta']['stale'] and not p['meta']['building']
    assert p['meta']['error']
    s.executor.submit.assert_not_called()


def test_publish_pages_twenty_rows_and_preserves_observed_events(monkeypatch):
    s=service();s.running=True
    bars,_=market();s.quote.return_value=quote(bars)
    codes=[str(i) for i in range(st.MAX_POOL)]
    s.inputs=lambda keep:(codes,{c:c for c in codes},{c:[] for c in codes})
    old_event={'id':'saved','date':'2026-11-17','type':'entry','price':123,'reason':'original'}
    s.cached={'rows':[{'active':True,'code':'0','name':'0','signals':[old_event]}]}
    monkeypatch.setattr(st,'analyse',lambda c,*args,**kwargs:dict(code=c,name=c,active=True,
        latest_event='entry',avg_turnover=100,date='2026-11-17',signals=[dict(old_event,price=999)]))
    s.build()
    assert not s.running and len(s.cached['rows'])==20 and s.cached['scanned']==80
    assert s.cached['rows'][0]['signals'][0]['price']==123
    s.store.save.assert_called_once()


def test_observation_stock_is_visible_before_breakout_without_fabricated_entry():
    bars,h=market(70)
    result=st.analyse('2330','fixture',quote(bars),h,now(bars))
    assert result and result['watch_only'] and not result['active']
    assert result['signals']==[] and not result['entry_ready']
    assert '等待突破／回測' in result['pending']
    assert result['entry']>=result['price'] and result['stop']<result['entry']<result['target']
    assert {c['label']:c['passed'] for c in result['checks']}['進場型態'] is False


def test_a_rebound_can_be_watched_without_claiming_uptrend_confirmation():
    bars,h=market(70)
    # Earlier high prices keep MA60 above MA20; recent recovery is near MA20.
    for i,b in enumerate(bars[:40]):
        c=116-i*.3;b.update(open=c-.2,close=c,high=c+.2,low=c-.6)
    result=st.analyse('2330','fixture',quote(bars),h,now(bars))
    assert result and result['watch_only'] and not result['signals']
    assert not result['trend'] and not result['entry_ready'] and result['patterns']


def test_old_selection_snapshot_is_rebuilt_even_when_age_is_fresh():
    s=service()
    s.store.load.return_value={'payload':{'rows':[],'generated_at':'2026-11-17T16:00:00'}}
    result=s.payload()
    assert result['meta']['building'] and result['meta']['pool_limit']==80
    s.executor.submit.assert_called_once()


def test_empty_result_retains_real_quote_date_and_exclusion_counts():
    s=service();bars,h=market(70)
    bars[-1].update(open=115,low=114,high=121,close=120)
    s.inputs=lambda keep:(['2330'],{'2330':'fixture'},{'2330':h})
    s.quote.return_value=quote(bars);s.now=lambda:now(bars)
    s.build()
    assert s.cached['rows']==[] and s.cached['scanned']==1
    assert s.cached['date']==bars[-1]['date']
    assert s.cached['excluded']=={'過熱或波動過高':1}


def test_all_missing_quotes_do_not_publish_a_false_empty_scan():
    s=service();s.cached={'rows':[],'date':'2026-11-16'};previous=s.cached
    s.inputs=lambda keep:(['2330'],{'2330':'fixture'},{'2330':[]})
    s.quote.return_value=None;s.build()
    assert s.cached is previous and '行情來源' in s.error
    s.store.save.assert_not_called()


def test_slow_market_source_cannot_monopolize_background_queue(monkeypatch):
    s=service();previous={'rows':[],'date':'2026-11-16'};s.cached=previous
    s.inputs=lambda keep:(['2330'],{'2330':'fixture'},{'2330':[]})
    clock=iter([0,121]);monkeypatch.setattr(st.time,'monotonic',lambda:next(clock))
    s.build()
    assert not s.running and s.cached is previous and '逾時' in s.error
    s.quote.assert_not_called();s.store.save.assert_not_called()


def test_failed_save_keeps_previous_result_and_reports_failure(monkeypatch):
    s=service();s.cached={'rows':[],'date':'2026-11-16'};old=s.cached
    s.inputs=lambda keep:([],{},{});s.store.save.return_value=False
    s.build()
    assert s.cached is old and '保存' in s.error and not s.running


def test_api_is_authenticated_read_only_and_does_not_cache():
    app=Flask(__name__);s=service()
    def login(fn):
        def wrapped():
            from flask import request
            return fn('fixture-user') if request.headers.get('X-Test-Login') else ('denied',401)
        return wrapped
    app.register_blueprint(st.create_blueprint(login=login,service=s))
    client=app.test_client();url='/web/api/workbench/short-term'
    assert client.get(url).status_code==401
    response=client.get(url,headers={'X-Test-Login':'1'})
    assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
    assert client.post(url,headers={'X-Test-Login':'1'}).status_code==405


def test_october_entry_and_november_exit_are_not_current_buy_instructions():
    signals=[dict(id='old-entry',date='2026-10-13',type='entry',price=148),
             dict(id='old-exit',date='2026-11-06',type='exit',price=160)]
    status=st.current_status(signals,False,'2026-11-17',[])
    assert status['action']=='wait' and status['event'] is None
    assert status['state']=='已出場，等待新訊號'
    assert '2026-11-06 已出場' in status['action_note'] and '不是現在買進提示' in status['action_note']
    assert st.latest_alerts([dict(code='2412',name='fixture',date='2026-11-17',signals=signals)],'2026-11-17')==[]


def test_old_active_entry_is_hold_not_a_new_entry():
    status=st.current_status([dict(date='2026-10-13',type='entry')],True,'2026-11-17',[])
    assert status['action']=='hold' and status['event'] is None
    assert '非新進場' in status['state'] and '不代表現在補買' in status['action_note']


def test_current_events_are_announced_and_stale_symbol_dates_are_excluded():
    rows=[dict(code=str(i),name='fixture',date='2026-11-17',signals=[dict(date='2026-11-17',type='entry',price=100)]) for i in range(8)]
    rows.append(dict(code='old',name='stale',date='2026-11-16',signals=[dict(date='2026-11-16',type='entry',price=90)]))
    alerts=st.latest_alerts(rows,'2026-11-17')
    assert len(alerts)==8 and all(a['date']=='2026-11-17' for a in alerts)
    status=st.current_status(rows[0]['signals'],True,'2026-11-17',[])
    assert status['action']=='entry' and status['event']
    assert '下一交易日' in status['action_note']


@pytest.mark.parametrize('hour,source,valid',[(10,'2026-11-16',False),(16,'2026-11-16',False),(16,'2026-11-17',True)])
def test_notification_freshness_uses_completed_session_not_just_snapshot_generation(hour,source,valid):
    s=service();s.now=lambda:datetime(2026,11,17,hour)
    s.store.load.return_value={'payload':dict(date=source,generated_at=s.now().isoformat(),selection_version=st.SELECTION_VERSION,rows=[])}
    assert s.payload()['meta']['signals_current'] is valid


def test_notification_freshness_respects_exchange_holidays():
    s=service();s.now=lambda:datetime(2026,11,17,8)
    s.calendar=lambda d:d.weekday()<5 and str(d)!='2026-11-16'
    s.store.load.return_value={'payload':dict(date='2026-11-13',generated_at=s.now().isoformat(),selection_version=st.SELECTION_VERSION,rows=[])}
    payload=s.payload()
    assert payload['meta']['signals_current'] and payload['meta']['expected_date']=='2026-11-13'


def test_individual_stale_quote_cannot_keep_a_new_entry_badge():
    s=service()
    old=dict(code='2330',date='2026-11-16',state='本次新進場',current_action='entry',entry_ready=True,
             signals=[dict(date='2026-11-16',type='entry')])
    s.store.load.return_value={'payload':dict(date='2026-11-17',generated_at=s.now().isoformat(),
        selection_version=st.SELECTION_VERSION,rows=[old],alerts=[])}
    row=s.payload()['rows'][0]
    assert row['current_action']=='unconfirmed' and not row['entry_ready']
    assert row['signals']==old['signals']  # original history remains available


def test_stale_session_suppresses_cached_entry_notifications():
    s=service()
    s.store.load.return_value={'payload':dict(date='2026-11-16',generated_at=s.now().isoformat(),
        selection_version=st.SELECTION_VERSION,rows=[],alerts=[dict(date='2026-11-16',type='entry')])}
    payload=s.payload()
    assert not payload['meta']['signals_current'] and payload['alerts']==[]


@pytest.mark.parametrize('entry,exit,spread',[(24,36,12),(100,1000,900),(36,24,-12),(24,24,0)])
def test_raw_per_share_spread_matches_requested_examples(entry,exit,spread):
    result=st.trade_spreads([dict(type='entry',date='2026-10-13',price=entry),
                             dict(type='exit',date='2026-11-06',price=exit)])
    assert result['total_closed_spread']==spread
    assert result['completed'][0]['spread']==spread and result['open'] is None


def test_simple_spread_ignores_add_reduce_costs_and_keeps_unrealized_separate():
    result=st.trade_spreads([dict(type='entry',date='2026-10-13',price=24),
        dict(type='add',date='2026-10-14',price=30),dict(type='reduce',date='2026-10-15',price=40),
        dict(type='exit',date='2026-10-16',price=36),dict(type='entry',date='2026-11-17',price=100)],105,'2026-11-17')
    assert result['total_closed_spread']==12 and result['open']['spread']==5
    assert len(result['completed'])==1


def test_closed_spread_totals_completed_cycles_without_inventing_unmatched_exits():
    result=st.trade_spreads([dict(type='exit',date='2026-09-01',price=80),
        dict(type='entry',date='2026-10-01',price=24),dict(type='exit',date='2026-10-03',price=36),
        dict(type='entry',date='2026-11-01',price=100),dict(type='exit',date='2026-11-06',price=90)])
    assert len(result['completed'])==2 and result['total_closed_spread']==2


def test_high_price_tiny_volume_can_enter_when_technical_conditions_confirm():
    bars,h=market(71)
    for b in bars:
        for k in ('open','high','low','close'):b[k]*=10
        b['volume']=10
    bars[-1]['volume']=14
    result=st.analyse('2330','fixture',quote(bars),[],now(bars))
    assert result['entry_ready'] and result['liquidity_warning']
    assert result['price']>1000 and result['avg_volume']<1000


def test_full_catalogue_streams_bounded_batches_and_includes_both_markets_without_chip_prefilter():
    from unittest.mock import MagicMock
    import time
    bars,_=market(71)
    light=dict(closes=[b['close'] for b in bars],timestamps=[int(datetime.fromisoformat(b['date']+'T05:30:00').replace(tzinfo=st.timezone.utc).timestamp()) for b in bars])
    catalogue=[(str(1000+i),'fixture','24' if i%2 else '17','上櫃' if i%2 else '上市') for i in range(130)]
    catalogue.append(('0050','ETF','', '上市'))
    first=MagicMock();second=MagicMock()
    for conn,rows in [(first,catalogue),(second,[])]:
        cur=conn.cursor.return_value;cur.__enter__.return_value=cur;cur.fetchall.return_value=rows
    s=service();s.connect=Mock(side_effect=[first,second]);s.equity=lambda c:not c.startswith('0')
    s.category=lambda c,i:'電子' if i=='24' else '金融'
    s.light=Mock(side_effect=lambda codes,markets:{c:light for c in codes})
    s.now=lambda:now(bars);s.deadline=time.monotonic()+120
    codes,names,history=s.inputs([dict(code='1129',name='fixture')])
    assert len(codes)==80 and codes[0]=='1129'
    assert s.scan_meta['universe_count']==130 and s.scan_meta['light_scanned']==130
    assert s.scan_meta['sectors']=={'金融':65,'電子':65}
    assert s.scan_meta['markets']=={'上市':65,'上櫃':65}
    assert s.scan_meta['price_coverage']['100–499']['quoted']==130
    assert s.scan_meta['price_coverage']['100–499']['deep']==80
    assert all(len(call.args[0])<=64 for call in s.light.call_args_list)
    assert s.light.call_count==3 and all(history[c]==[] for c in codes)
    sql=' '.join(str(c.args[0]) for c in first.cursor.return_value.execute.call_args_list)
    assert 'stock_info' in sql and 'inst_history' not in sql


def test_negative_institutional_flows_do_not_affect_technical_preselection():
    bars,_=market(71)
    light=dict(closes=[b['close'] for b in bars],timestamps=[int(datetime.fromisoformat(b['date']+'T05:30:00').replace(tzinfo=st.timezone.utc).timestamp()) for b in bars])
    metric=st.light_technical(light,now(bars))
    assert metric and st.technical_pool({'2330':metric,'6488':metric},[],80)==['2330','6488']


def test_full_market_spark_stream_does_not_overwrite_other_pages_quote_cache():
    import re,threading
    from concurrent.futures import ThreadPoolExecutor
    from test_radar_admission import install
    old={'day':'2026-11-17','at':999,'data':{'2330':{'label':'existing shared quote'}}}
    response=Mock()
    response.json.return_value={'spark':{'result':[dict(symbol='2330.TW',response=[dict(
        meta={'regularMarketPrice':100,'regularMarketVolume':100},timestamp=[1,2],
        indicators={'quote':[{'close':[99,100]}]})])]}}
    ns=dict(re=re,time=Mock(time=Mock(return_value=1000)),taiwan_now=lambda:datetime(2026,11,17,16),
        requests=Mock(get=Mock(return_value=response)),_RADAR_SPARK_CACHE_LOCK=threading.Lock(),
        _RADAR_SPARK_CACHE=old,RADAR_SPARK_CACHE_SECONDS=15,RADAR_SPARK_BATCH_SIZE=20,
        RADAR_SPARK_MAX_WORKERS=1,ThreadPoolExecutor=Mock(side_effect=ThreadPoolExecutor))
    install(ns,'_fetch_yahoo_spark_bulk')
    data=ns['_fetch_yahoo_spark_bulk'](['2330'],rng='3mo',force_refresh=True,market_map={'2330':'上市'},
        include_history=True,cache_result=False,workers=2)
    assert data['2330']['closes']==[99,100] and old['data']=={'2330':{'label':'existing shared quote'}}
    ns['ThreadPoolExecutor'].assert_called_once_with(max_workers=1)  # one HTTP batch only
    ns['requests'].get.reset_mock()
    assert ns['_fetch_yahoo_spark_bulk'](['2330'])=={'2330':{'label':'existing shared quote'}}
    ns['requests'].get.assert_not_called()


def test_institutional_database_failure_does_not_block_technical_scan():
    from unittest.mock import MagicMock
    bars,_=market(71)
    light=dict(closes=[b['close'] for b in bars],timestamps=[int(datetime.fromisoformat(b['date']+'T05:30:00').replace(tzinfo=st.timezone.utc).timestamp()) for b in bars])
    conn=MagicMock();cur=conn.cursor.return_value;cur.__enter__.return_value=cur
    cur.fetchall.return_value=[('2330','fixture','24','上市')]
    s=service();s.now=lambda:now(bars);s.category=lambda c,i:'電子'
    s.connect=Mock(side_effect=[conn,OSError('auxiliary-only database unavailable')])
    s.light=Mock(return_value={'2330':light});s.quote.return_value=quote(bars)
    s.build()
    assert not s.error and s.cached['rows'][0]['entry_ready']
    assert not s.cached['rows'][0]['chip']['available']
    assert s.cached['scope']['sectors']=={'電子':1}

def test_intraday_bar_is_provisional_and_can_disappear_without_rewriting_closed_signals():
    bars,h=market(71);n=now(bars).replace(hour=12,minute=30)
    live=st.analyse('2330','fixture',quote(bars),h,n,intraday=True)
    assert live['provisional'] and live['entry_ready']
    assert live['signals'][-1]['provisional'] and '盤中' in live['state']
    changed=copy.deepcopy(bars);changed[-1].update(close=106.9,high=107.1,open=106.8,low=106.5)
    revised=st.analyse('2330','fixture',quote(changed),h,n,intraday=True)
    assert revised and not revised['entry_ready']
    assert not [e for e in revised['signals'] if e['date']==st.day(n)]
    closed=st.analyse('2330','fixture',quote(bars),h,now(bars))
    assert not closed['provisional'] and not closed['signals'][-1].get('provisional')


def test_intraday_volume_is_time_normalized_and_raw_chart_volume_is_not_changed():
    bars,h=market(71);bars[-1]['volume']=700000
    n=now(bars).replace(hour=11,minute=15)
    live=st.analyse('2330','fixture',quote(bars),h,n,intraday=True)
    assert live['volume_estimated'] and live['volume_ratio']==1.4
    assert live['bars'][-1]['volume']==700000
    assert live['entry_ready']
    assert not st.analyse('2330','fixture',quote(bars),h,now(bars))['entry_ready']


def test_intraday_snapshots_are_separate_and_stale_source_times_suppress_live_signals():
    bars,h=market(71)
    n=now(bars).replace(hour=12,minute=30,tzinfo=st.timezone(timedelta(hours=8)))
    s=service();s.now=lambda:n
    s.inputs=lambda keep:(['2330'],{'2330':'fixture'},{'2330':h})
    s.stock_meta={'2330':dict(sector='電子',market='上市')}
    q=quote(bars);q['market_observed_at']=n.timestamp()-120
    s.quote.return_value=q;s.build()
    assert s.store.save.call_args.args[0]==st.KEY+':intraday'
    assert s.cached['rows'][0]['entry_ready'] and s.cached['intraday']
    assert s.cached['rows'][0]['observed_at']
    # A snapshot with today's date is insufficient if its trade timestamp is old.
    s.loaded=True;s.cache_mode=True
    payload=s.payload()
    assert payload['meta']['signals_current'] and payload['alerts']
    s.now=lambda:n+timedelta(minutes=25)
    payload=s.payload()
    assert not payload['alerts'] and payload['rows'][0]['current_action']=='unconfirmed'
    assert not payload['meta']['signals_current']
    # Source timestamp missing: a freshly fetched yesterday quote cannot be a live entry.
    s=service();s.now=lambda:n;s.inputs=lambda keep:(['2330'],{'2330':'fixture'},{'2330':h})
    s.quote.return_value=quote(bars);s.build()
    assert s.cached['date']!=st.day(n)
    assert not s.cached['rows'][0]['entry_ready']


def test_radar_trigger_queues_one_job_without_a_page_and_keeps_correlation():
    s=service();s.now=lambda:datetime(2026,11,18,10,tzinfo=st.timezone(timedelta(hours=8)))
    for _ in range(3):s.refresh_from_radar('2026-11-18T10:00:00+08:00')
    s.executor.submit.assert_called_once()
    assert s.trigger=='雷達快照排程' and s.radar_at=='2026-11-18T10:00:00+08:00'
    assert s.store.load.call_args.args[0]==st.KEY+':intraday'


def test_sector_filter_reads_all_qualified_rows_without_another_market_scan():
    s=service();rows=[dict(code=str(i),date='2026-11-17',sector='傳產',signals=[]) for i in range(6)]
    rows.append(dict(code='2330',date='2026-11-17',sector='電子',signals=[]))
    s.store.load.return_value={'payload':dict(date='2026-11-17',generated_at=s.now().isoformat(),
        selection_version=st.SELECTION_VERSION,rows=rows[:6],all_rows=rows)}
    assert len(s.payload()['rows'])==7
    assert [r['code'] for r in s.payload(sector='電子')['rows']]==['2330']
    s.executor.submit.assert_not_called()
    assert 'all_rows' not in s.payload()  # bounded response, no 80 charts sent per poll


def test_technical_deep_coverage_includes_each_sector_without_forcing_entries():
    metrics={str(i):dict(bucket='breakout',rank=(False,-i,0)) for i in range(100)}
    sectors={str(i):'電子' if i<10 else '傳產' for i in range(100)}
    chosen=st.technical_pool(metrics,[],limit=20,sectors=sectors)
    assert len(chosen)==20 and any(sectors[c]=='電子' for c in chosen)
    assert any(sectors[c]=='傳產' for c in chosen)


def test_radar_producer_is_independent_from_short_term_refresh():
    import ast,threading
    from pathlib import Path
    source=Path('main.py').read_text(encoding='utf-8')
    block=source.split('def _do_scheduled_radar_live_snapshot():',1)[1].split('\n@app.route',1)[0]
    node=ast.parse('def _do_scheduled_radar_live_snapshot():'+block).body[0]
    svc=Mock();ns=dict(_is_taiwan_intraday_window=lambda:True,
        _SCREENER_REFRESH_LOCK=threading.Lock(),_SCREENER_REFRESH_RUNNING={},
        compute_screener_rows=Mock(return_value=([{}],0,None)),
        RADAR_SCHEDULED_DEEP_SCAN_LIMIT=4,RADAR_LIVE_SNAPSHOT_INTERVAL_SECONDS=600,
        _short_term_service=svc,taiwan_now=lambda:datetime(2026,11,18,10))
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual-radar-producer>','exec'),ns)
    assert '已完成' in ns['_do_scheduled_radar_live_snapshot']()
    svc.refresh_from_radar.assert_not_called()
    svc.payload.assert_not_called()
    assert not ns['_SCREENER_REFRESH_RUNNING']['radar']

@pytest.mark.parametrize('events,active,provisional,expected',[
 ([],False,False,'waiting'),([],True,False,'held'),
 ([dict(type='entry')],True,False,'held'),
 ([dict(type='entry'),dict(type='exit')],False,False,'closed'),
 ([dict(type='entry',provisional=True)],True,True,'waiting'),
 ([dict(type='entry'),dict(type='exit',provisional=True)],False,True,'held'),
 ([dict(type='entry'),dict(type='exit'),dict(type='entry',provisional=True)],True,True,'closed')])
def test_position_groups_do_not_confuse_previews_with_confirmed_positions(events,active,provisional,expected):
    assert st.position_group(dict(signals=events,active=active,provisional=provisional))==expected


def test_position_filter_can_show_held_stock_beyond_the_default_six():
    s=service();rows=[dict(code=str(i),date='2026-11-17',signals=[],active=False) for i in range(6)]
    rows.append(dict(code='2330',date='2026-11-17',signals=[dict(type='entry',date='2026-11-14',price=1500)],active=True))
    s.store.load.return_value={'payload':dict(date='2026-11-17',generated_at=s.now().isoformat(),
        selection_version=st.SELECTION_VERSION,rows=rows[:6],all_rows=rows)}
    result=s.payload(position='held')
    assert [r['code'] for r in result['rows']]==['2330']
    assert result['meta']['position_counts']['waiting']==6
    assert result['rows'][0]['position_group']=='held'
    s.executor.submit.assert_not_called()


def test_nominal_price_scaling_does_not_change_technical_preselection_or_entry():
    bars,h=market(71)
    def light(bs):return dict(closes=[b['close'] for b in bs],timestamps=[int(datetime.fromisoformat(b['date']+'T05:30:00').replace(tzinfo=st.timezone.utc).timestamp()) for b in bs])
    small=st.light_technical(light(bars),now(bars))
    large=copy.deepcopy(bars)
    for b in large:
        for key in ('open','high','low','close'):b[key]*=20
    big=st.light_technical(light(large),now(bars))
    assert small['bucket']==big['bucket'] and small['rank']==pytest.approx(big['rank'])
    assert st.analyse('2330','fixture',quote(large),[],now(bars))['entry_ready']
    assert st.price_band(large[-1]['close'])=='1000以上'


def test_price_diagnostics_record_high_price_exclusion_as_technical_not_institutional():
    bars,_=market(71)
    for b in bars:
        for k in ('open','high','low','close'):b[k]*=20
    bars[-1]['close']*=1.10
    q=dict(closes=[b['close'] for b in bars],timestamps=[int(datetime.fromisoformat(b['date']+'T05:30:00').replace(tzinfo=st.timezone.utc).timestamp()) for b in bars])
    audit={}
    assert st.light_technical(q,now(bars),diagnostics=audit) is None
    assert audit['reason']=='單日漲跌超過9.5%'

def test_detailed_cap_does_not_erase_high_price_candidates_behind_many_low_price_names():
    metrics={str(i):dict(bucket='breakout',rank=(False,-(100-i),0)) for i in range(100)}
    metrics.update({'high1':dict(bucket='recovery',rank=(True,0,0)),
                    'high2':dict(bucket='pullback',rank=(False,0,0)),
                    'high3':dict(bucket='breakout',rank=(False,0,0))})
    sectors={c:'電子' for c in metrics};bands={c:'低於100' for c in metrics}
    bands.update(high1='1000以上',high2='500–999',high3='500–999')
    old=st.technical_pool(metrics,[],sectors=sectors)
    assert not all(c in old for c in ('high1','high2','high3'))
    chosen=st.technical_pool(metrics,[],sectors=sectors,price_bands=bands)
    assert len(chosen)==80 and all(c in chosen for c in ('high1','high2','high3'))
    # Research inclusion does not declare any of these stocks to be an entry.
    assert all('entry' not in metrics[c] for c in chosen)


def test_institutional_data_cannot_change_any_entry_add_reduce_exit_or_stop():
    bars,h=market(100)
    # One close below MA10 must not become a chip-driven early reduction.
    bars[80].update(open=108,close=107.5,low=107.4,high=108.1)
    buying=[dict(r,foreign=100,trust=50) for r in h]
    selling=[dict(r,foreign=-100,trust=-50) for r in h]
    results=[st.analyse('2330','fixture',quote(bars),data,now(bars)) for data in (buying,selling,[])]
    assert all(r for r in results)
    assert results[0]['signals']==results[1]['signals']==results[2]['signals']
    assert results[0]['trailing']==results[1]['trailing']==results[2]['trailing']
    assert results[0]['institutional_buying'] is True
    assert results[1]['institutional_buying'] is False
    assert results[2]['institutional_buying'] is None


def test_four_independent_pattern_families_detect_breakout_pullback_continuation_and_reversal():
    bars,h=market(75)
    initial=st.setup(market(71)[0],st.chip_at([],bars[-1]['date'],[]))
    assert initial['strategy']=='breakout' and initial['entry']
    pullback=copy.deepcopy(bars);pullback[-1].update(close=109,open=108.7,high=109.2,low=106.5,volume=1000000)
    plan=st.setup(pullback,st.chip_at([],bars[-1]['date'],[]))
    assert plan['strategy']=='pullback' and plan['entry']
    continuation=copy.deepcopy(bars)
    for j,b in enumerate(continuation[-21:-6]):
        c=95+j*2;b.update(open=c-.2,close=c,high=c+.2,low=c-.5)
    for b in continuation[-6:-1]:b.update(open=124.8,close=125,high=125.5,low=124.5,volume=1000000)
    continuation[-1].update(open=126.5,close=127,high=127.2,low=125.2,volume=1400000)
    plan=st.setup(continuation,st.chip_at([],bars[-1]['date'],[]))
    assert plan['strategy']=='continuation' and plan['entry']
    assert continuation[-1]['close']/plan['ma20']>1.08  # an old shared veto no longer kills the setup
    import math
    reversal=copy.deepcopy(bars)
    for i,b in enumerate(reversal):
        c=130-i*.25+math.sin(i*math.pi/5)
        b.update(open=c-.15,close=c,high=c+.25,low=c-.4,volume=1000000)
    x=st.levels(reversal);shapes=st.technical_shapes(reversal,x,1.3)
    line=shapes['line'];assert line and line['confirmed_date']<reversal[-1]['date']
    c=line['end_price']+1
    reversal[-1].update(open=c-.3,close=c,high=c+.1,low=c-.5,volume=1400000)
    plan=st.setup(reversal,st.chip_at([],bars[-1]['date'],[]))
    assert plan['strategy']=='reversal' and plan['trigger'] and not plan['trend']
    # Observation versus entry still depends on risk/reward; no fabricated fill.
    assert not plan['entry']  # a single cross in a downtrend is only observation


def test_pattern_v3_uses_separate_snapshot_namespace_and_no_price_ceiling():
    assert st.VERSION=='short-wave-v5' and st.KEY=='short_term:short-wave-v5'
    bars,h=market(71)
    for b in bars:
        for k in ('open','high','low','close'):b[k]*=100
    a=st.analyse('2330','fixture',quote(bars),[],now(bars))
    assert a['entry_ready'] and a['price']>10000


def test_7791_real_downtrend_tiny_cross_is_not_a_confirmed_entry():
    import json
    from pathlib import Path
    fixture=json.loads(Path('tests/fixtures/7791-20261118.json').read_text(encoding='utf8'))
    bars=fixture['bars']
    plan=st.setup(bars,{'available':False})
    assert plan['ma20']>bars[-1]['close'] and plan['trend_slope']<0
    assert plan['pattern_line']['end_price']==pytest.approx(45.78947,abs=.001)
    assert not plan['entry'] and not plan['entry_confirmed']
    result=st.analyse('7791','皇家可口',quote(bars),[],now(bars))
    assert result and not result['entry_ready'] and not result['active']
    assert not [e for e in result['signals'] if e['date']=='2026-11-18' and e['type']=='entry']
    assert '等待連續2日站穩突破線並站回20日線' in result['pending']
    light=dict(closes=[b['close'] for b in bars],timestamps=[int(datetime.fromisoformat(b['date']+'T05:30:00').replace(tzinfo=st.timezone.utc).timestamp()) for b in bars])
    assert st.light_technical(light,now(bars)) is not None
    # The catalogue's close-only path cannot claim traded two-close evidence.
    pseudo=[{k:v for k,v in b.items() if k!='volume'} for b in bars]
    assert not st.standing_confirmation(pseudo,45.8)['confirmed']


def test_standing_confirmation_requires_two_closes_and_recent_cross():
    bars,_=market(75)
    assert not st.standing_confirmation(bars[:-1],108.1)['confirmed']
    assert st.standing_confirmation(bars,108.1)['confirmed']
    assert not st.standing_confirmation(bars,100)['confirmed']  # weeks-old break


def test_two_closes_above_line_without_reclaiming_ma20_remain_unconfirmed():
    import json
    from pathlib import Path
    bars=json.loads(Path('tests/fixtures/7791-20261118.json').read_text(encoding='utf8'))['bars']
    bars[-2]['close']=46.2;bars[-1]['close']=46.4
    check=st.standing_confirmation(bars,45.8)
    assert check['standing'] and not check['reclaimed'] and not check['confirmed']


def test_single_support_break_reduces_but_does_not_exit_and_recovery_retains_position():
    bars,h=market(87)
    early=st.analyse('2330','fixture',quote(bars[:84]),h,now(bars[:84]))
    hard=early['stop'];support=early['soft_support'];assert hard<support
    c=(hard+support)/2
    bars[84].update(open=c+.05,close=c,high=c+.15,low=max(hard+.01,c-.05))
    out=st.analyse('2330','fixture',quote(bars),h,now(bars))
    assert out['active'] and not [e for e in out['signals'] if e['type']=='exit']
    reductions=[e for e in out['signals'] if e['type']=='reduce']
    assert len(reductions)==1 and reductions[0]['units']==.5


def test_two_confirmed_support_breaks_and_falling_ma20_can_close_wave_above_hard_stop():
    import math
    bars,h=market(104)
    for i,b in enumerate(bars[83:102]):
        c=110.9+.1*math.sin(i)
        b.update(open=c-.1,close=c,high=c+.2,low=c-.4,volume=1000000)
    early=st.analyse('2330','fixture',quote(bars[:102]),h,now(bars[:102]))
    assert early['active']
    hard=early['stop'];support=early['soft_support'];c=(hard+support)/2
    for j in (102,103):
        v=c-.02*(j-102)
        bars[j].update(open=v+.02,close=v,high=v+.1,low=max(hard+.01,v-.03),volume=1000000)
    first=st.analyse('2330','fixture',quote(bars[:103]),h,now(bars[:103]))
    assert first['active'] and any(e['type']=='reduce' for e in first['signals'])
    out=st.analyse('2330','fixture',quote(bars),h,now(bars))
    exits=[e for e in out['signals'] if e['type']=='exit']
    assert exits and '趨勢反轉確認' in exits[-1]['reason'] and exits[-1]['price']>hard
    assert not out['active']


def test_pagination_exposes_every_qualified_stock_sorted_by_clarity():
    s=service();rows=[dict(code=str(1000+i),name='fixture',date='2026-11-17',signals=[],signal_stage='watch',trend=False) for i in range(47)]
    rows[-1].update(signal_stage='triggered',trend=True)
    s.store.load.return_value={'payload':dict(date='2026-11-17',generated_at=s.now().isoformat(),
        selection_version=st.SELECTION_VERSION,all_rows=rows,rows=rows[:20])}
    pages=[s.payload(offset=i) for i in (0,20,40)]
    assert [len(p['rows']) for p in pages]==[20,20,7]
    assert pages[0]['rows'][0]['code']=='1046' and pages[0]['meta']['total']==47
    assert len({r['code'] for p in pages for r in p['rows']})==47
    assert pages[0]['meta']['has_more'] and not pages[2]['meta']['has_more']
    s.executor.submit.assert_not_called()

def test_all_market_batches_accumulate_and_resume_beyond_eighty_without_rescanning_light_quotes():
    from unittest.mock import MagicMock
    bars,h=market(71)
    light=dict(closes=[b['close'] for b in bars],timestamps=[int(datetime.fromisoformat(b['date']+'T05:30:00').replace(tzinfo=st.timezone.utc).timestamp()) for b in bars])
    catalogue=[(str(1000+i),'fixture','24','上市') for i in range(170)]
    connections=[]
    for data in (catalogue,[],[],[]):
        c=MagicMock();cur=c.cursor.return_value;cur.__enter__.return_value=cur;cur.fetchall.return_value=data;connections.append(c)
    s=service();s.now=lambda:now(bars);s.connect=Mock(side_effect=connections)
    s.light=Mock(side_effect=lambda codes,markets:{c:light for c in codes});s.quote.return_value=quote(bars)
    s.build()
    first=copy.deepcopy(s.cached)
    assert first['cycle']['cursor']==80 and first['scope']['deep_total']==170 and not first['scope']['deep_complete']
    assert len(first['all_rows'])==80 and s.running
    s.build();assert s.cached['cycle']['cursor']==144
    s.build();assert s.cached['cycle']['cursor']==170 and not s.running
    assert s.cached['scope']['deep_complete'] and len(s.cached['all_rows'])==170
    assert len({c.args[0] for c in s.quote.call_args_list})==170 and s.quote.call_count<=240 and s.light.call_count==3
    assert first['cycle']['cursor']==80  # committed checkpoint did not mutate
    # A restarted process reads the persisted cursor and does not restart at 0.
    restarted=service();restarted.now=lambda:now(bars);restarted.cached=first
    restarted.inputs=Mock(side_effect=AssertionError('must resume existing full-market cycle'))
    restarted.histories=Mock(side_effect=lambda codes:{c:[] for c in codes});restarted.quote.return_value=quote(bars)
    restarted.build()
    assert restarted.cached['cycle']['cursor']==144 and restarted.quote.call_count==80
    restarted.inputs.assert_not_called()


def test_partial_time_budget_saves_completed_symbols_and_never_claims_full_coverage(monkeypatch):
    bars,h=market(71);s=service();s.now=lambda:now(bars)
    codes=[str(1000+i) for i in range(10)]
    s.inputs=lambda keep:(codes,{c:c for c in codes},{c:[] for c in codes})
    s.quote.return_value=quote(bars)
    ticks=iter([0,1,2,121]);monkeypatch.setattr(st.time,'monotonic',lambda:next(ticks))
    s.build()
    assert s.cached['cycle']['cursor']==2 and s.cached['scanned']==2
    assert not s.cached['scope']['deep_complete'] and '逾時' in s.cached['scope']['scan_warning']
    assert not s.running and s.quote.call_count==2


def test_wave_support_recovery_can_restore_reduced_units_without_exceeding_risk_budget():
    bars,h=market(100)
    early=st.analyse('2330','fixture',quote(bars[:84]),h,now(bars[:84]))
    hard=early['stop'];support=early['soft_support'];c=(hard+support)/2
    bars[84].update(open=c+.05,close=c,high=c+.15,low=max(hard+.01,c-.05))
    bars[90]['volume']=2200000
    out=st.analyse('2330','fixture',quote(bars),h,now(bars))
    restored=[e for e in out['signals'] if e['type']=='add' and '回補' in e['reason']]
    assert len(restored)==1 and out['active']
    entry=out['signals'][0];event=restored[0]
    cost=.5*entry['price']+.5*event['price']
    assert cost-event['stop']<=entry['price']-entry['stop']+.03
