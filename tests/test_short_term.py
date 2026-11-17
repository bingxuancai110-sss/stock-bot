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
def test_no_entry_for_illiquid_overheated_missing_or_unsupportive_chips(kind):
    bars,h=market(71)
    if kind=='thin':
        for b in bars:b['volume']=10000
    elif kind=='hot':
        bars[-1].update(open=115,low=114,high=121,close=120)
    elif kind=='missing':h=[]
    elif kind=='selling':
        for r in h:r.update(foreign=-40,trust=-10)
    else:h[-3]['foreign']=None
    assert st.analyse('2330','fixture',quote(bars),h,now(bars)) is None


def test_today_chip_report_is_not_used_for_today_entry():
    bars,h=market(71)
    for r in h:r.update(foreign=-40,trust=-10)
    h[-1].update(foreign=999999,trust=999999)
    assert st.analyse('2330','fixture',quote(bars),h,now(bars)) is None


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
    assert not out['active'] and out['state']=='出場訊號'


def test_additions_are_limited_and_cannot_expand_initial_downside_budget():
    bars,h=market(100)
    bars[82]['volume']=2_200_000  # a second distinct volume expansion after 1R
    out=st.analyse('2330','fixture',quote(bars),h,now(bars))
    adds=[s for s in out['signals'] if s['type']=='add']
    assert len(adds)==1
    entry=out['signals'][0]; add=adds[0]
    assert add['price']>=entry['price']+(entry['price']-entry['stop'])-.02
    cost=entry['price']+.5*add['price']
    assert cost-1.5*add['stop']<=entry['price']-entry['stop']+.03


def test_pressure_rejection_reduces_once_and_preserves_the_stop():
    bars,h=market(78)
    initial=st.analyse('2330','fixture',quote(bars[:77]),h,now(bars[:77]))
    target=initial['target']; stop=initial['stop']
    bars[-1].update(open=target-.1,high=target+.5,low=max(stop+.1,target-1),close=target-.8)
    out=st.analyse('2330','fixture',quote(bars),h,now(bars))
    reductions=[s for s in out['signals'] if s['type']=='reduce']
    assert len(reductions)==1 and reductions[0]['stop']>=initial['stop']


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
    assert p['rows']==[{'code':'2330'}] and p['meta']['stale'] and not p['meta']['building']
    assert p['meta']['error']
    s.executor.submit.assert_not_called()


def test_publish_caps_six_rows_and_preserves_observed_events(monkeypatch):
    s=service();s.running=True
    bars,_=market();s.quote.return_value=quote(bars)
    codes=[str(i) for i in range(24)]
    s.inputs=lambda keep:(codes,{c:c for c in codes},{c:[] for c in codes})
    old_event={'id':'saved','date':'2026-11-17','type':'entry','price':123,'reason':'original'}
    s.cached={'rows':[{'active':True,'code':'0','name':'0','signals':[old_event]}]}
    monkeypatch.setattr(st,'analyse',lambda c,*args:dict(code=c,name=c,active=True,
        latest_event='entry',avg_turnover=100,date='2026-11-17',signals=[dict(old_event,price=999)]))
    s.build()
    assert not s.running and len(s.cached['rows'])==6 and s.cached['scanned']==24
    assert s.cached['rows'][0]['signals'][0]['price']==123
    s.store.save.assert_called_once()


def test_empty_result_retains_real_quote_date_and_exclusion_counts():
    s=service();bars,h=market()
    for b in bars:b['volume']=10000
    s.inputs=lambda keep:(['2330'],{'2330':'fixture'},{'2330':h})
    s.quote.return_value=quote(bars);s.now=lambda:now(bars)
    s.build()
    assert s.cached['rows']==[] and s.cached['scanned']==1
    assert s.cached['date']==bars[-1]['date']
    assert s.cached['excluded']=={'流動性不足':1}


def test_all_missing_quotes_do_not_publish_a_false_empty_scan():
    s=service();s.cached={'rows':[],'date':'2026-11-16'};previous=s.cached
    s.inputs=lambda keep:(['2330'],{'2330':'fixture'},{'2330':[]})
    s.quote.return_value=None;s.build()
    assert s.cached is previous and '行情來源' in s.error
    s.store.save.assert_not_called()


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
