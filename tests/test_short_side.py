import copy
from unittest.mock import patch
import short_side as ss
import short_term as st
from test_short_term import market,quote,now


def controlled(bars,fraction=1):
    x=ss_original(bars,fraction)
    x.update(entry=len(bars)==65,watchable=True,stop=105,risk=5,target=80,rr=4,
             ma10=95,ma20=100,ma20_previous=101,soft_resistance=104,volume_ratio=1.5)
    return x


ss_original=ss.setup


def path():
    b,h=market(69)
    for z in b:z.update(open=100,high=100.2,low=99.8,close=100)
    b[65].update(open=99,high=99.5,low=97.5,close=98)
    b[66].update(open=95,high=95.5,low=93,close=94)
    b[67].update(open=95,high=97,low=79,close=96)
    b[68].update(open=106,high=107,low=103,close=104)
    return b,h


def test_short_add_cover_stop_and_evidence():
    b,h=path()
    with patch.object(ss,'setup',side_effect=controlled):r=ss.analyse('2330','fixture',quote(b),h,now(b))
    assert [e['type'] for e in r['signals']]==['entry','add','reduce','exit']
    assert [e['units'] for e in r['signals']]==[1,.5,.75,.75]
    assert r['signals'][-1]['price']==106  # adverse gap, not optimistic stop
    assert all(e['evidence']['stop'] and 'MA20' in e['reason'] for e in r['signals'])
    assert all(e['direction']=='short' for e in r['signals'])
    assert '未核實' in r['short_eligibility']


def test_stop_wins_when_same_bar_also_hits_profit_target():
    b,h=path();b[65].update(high=106,low=75,open=100)
    with patch.object(ss,'setup',side_effect=controlled):r=ss.analyse('2330','fixture',quote(b[:66]),h,now(b[:66]))
    assert [e['type'] for e in r['signals']]==['entry','exit']
    assert r['signals'][-1]['price']==105


def test_prefix_causality_and_no_future_repainting():
    b,h=path()
    with patch.object(ss,'setup',side_effect=controlled):
        a=ss.analyse('2330','fixture',quote(b[:66]),h,now(b[:66]))
        z=ss.analyse('2330','fixture',quote(b),h,now(b))
    assert a['signals']==z['signals'][:len(a['signals'])]


def test_short_spread_direction_is_correct_without_changing_long():
    signals=[dict(type='entry',price=100,date='2026-11-01'),dict(type='exit',price=80,date='2026-11-03')]
    assert st.trade_spreads(signals,direction='short')['completed'][0]['spread']==20
    assert st.trade_spreads(signals)['completed'][0]['spread']==-20
    assert st.trade_spreads(signals[:1],110,'2026-11-02',direction='short')['open']['spread']==-10


def test_real_breakdown_requires_two_closes_and_volume_risk():
    b,h=market(80)
    for i,z in enumerate(b):
        c=110-i*.1;z.update(open=c+.1,high=c+.4,low=c-.4,close=c,volume=1000000)
    b[-2].update(open=102,high=102.2,low=99,close=99.5,volume=1400000)
    b[-1].update(open=100,high=100.2,low=98.8,close=99,volume=1400000)
    p=ss.setup(b)
    assert p['trigger'] and p['strategy']=='breakout'
    assert p['stop']>b[-1]['close']>p['target']
    single=copy.deepcopy(b);single[-2]['close']=103
    assert not ss.setup(single)['trigger']
    thin=copy.deepcopy(b);thin[-1]['volume']=0
    assert not ss.setup(thin)['entry']


def test_real_uptrend_is_not_short_entry():
    b,h=market(80)
    assert not ss.setup(b)['entry']


def test_intraday_short_events_remain_provisional():
    b,h=path();b=b[:65];n=now(b).replace(hour=12)
    with patch.object(ss,'setup',side_effect=controlled):r=ss.analyse('2330','fixture',quote(b),h,n,intraday=True)
    assert r['signals'][-1]['provisional'] and r['provisional']
    assert st.position_group(r)=='waiting'


def test_direction_filters_saved_rows_alerts_and_spreads():
    from test_short_term import service
    b,h=path()
    with patch.object(ss,'setup',side_effect=controlled):r=ss.analyse('2330','fixture',quote(b),h,now(b))
    svc=service();svc.now=lambda:now(b)
    svc.cached=dict(date=b[-1]['date'],generated_at=now(b).isoformat(),version=st.VERSION,
                    selection_version=st.SELECTION_VERSION,all_rows=[],short_rows=[r],alerts=st.latest_alerts([r],b[-1]['date']))
    svc.store.load.return_value={'payload':svc.cached}
    result=svc.payload(direction='short')
    assert result['meta']['direction']=='short' and len(result['rows'])==1
    row=result['rows'][0]
    assert row['spreads']['completed'][0]['spread']==-6
    assert row['signals'][-2]['allocation']['after_pct']==75
    assert row['signals'][-1]['allocation']['label']=='全部回補剩餘空單'
    assert result['alerts'][0]['direction']=='short'
    assert svc.payload()['rows']==[] and svc.payload()['alerts']==[]
