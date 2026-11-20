import copy
from unittest.mock import patch
import pytest
from technical_space import short_space
from test_short_term import market,quote,now,service
import short_term as st
import short_side as ss


def test_short_uses_confirmed_support_not_an_unfinished_low_tick():
    bars,h=market(80)
    for b in bars:b.update(open=100,high=101,low=99,close=100)
    bars[40]['low']=80;bars[60]['low']=70
    bars[-2].update(open=95,high=96,low=94.9,close=95)
    bars[-1].update(open=95,high=96,low=94,close=95)
    assert short_space(bars,95,2)['support']==80
    assert short_space(bars,75,2)['support']==70


def test_decisive_single_close_break_is_a_causal_entry_not_a_wait_for_tomorrow():
    bars,h=market(80)
    for i,b in enumerate(bars):
        c=110 if i<50 else 104
        b.update(open=c+.2,high=c+.5,low=90 if i==40 else c-1,close=c,volume=1000000)
    bars[-1].update(open=101,high=101,low=98.8,close=99,volume=1500000)
    p=ss.setup(bars)
    assert p['entry'] and p['strategy']=='breakout'
    assert '單日收盤' in p['confirmation']
    assert p['stop']>99>p['target']


def test_high_atr_is_not_a_blanket_entry_veto_when_structural_risk_fits():
    bars,h=market(80)
    for b in bars:b.update(open=104,high=107,low=100,close=104,volume=1000000)
    bars[-2].update(open=104,high=106,low=100,close=102,volume=1000000)
    bars[-1].update(open=100,high=100,low=97.8,close=98,volume=1500000)
    p=ss.setup(bars)
    assert p['high_volatility'] and not p['hot'] and p['entry']
    assert 0<p['risk']/98<=p['risk_limit']<=.12


def test_wave_trend_can_enter_with_nearby_checkpoint_without_fabricating_more_space():
    bars,h=market(80);c=bars[-1]['close']
    bars[70]['high']=c+2
    bars[-1].update(low=c-1.5)
    p=st.setup(bars,dict(available=False))
    assert p['entry'] and p['risk_policy']=='trend_following' and p['rr']<1.5
    assert p['target']==pytest.approx(c+2)


def test_independent_valid_long_family_survives_an_invalid_reversal_risk_plan():
    bars,h=market(80);x=st.levels(bars);shapes=st.technical_shapes(bars,x,1.4)
    common=dict(trigger=True,confirmed=True,confirmation=None,line=None,level=x['pressure'])
    shapes['candidates']=[dict(common,kind='reversal',pattern='invalid reversal',invalidation=1),
                          dict(common,kind='pullback',pattern='valid pullback',invalidation=bars[-2]['low'])]
    with patch.object(st,'technical_shapes',return_value=shapes):p=st.setup(bars,dict(available=False))
    assert p['entry'] and p['strategy']=='pullback'
    assert not p['family_checks'][0]['entry'] and p['family_checks'][1]['entry']


def test_zero_volume_sessions_do_not_count_and_adjusted_prices_stay_date_aligned():
    bars,h=market(140);bars[10]['volume']=0
    for k in ('open','high','low','close'):bars[11][k]*=1.1
    q=quote(bars);q['adj_closes']=[b['close']*.8 for b in bars]
    normalized=st.normalize_bars(q,now(bars))
    assert len(normalized)==139 and bars[10]['date'] not in [b['date'] for b in normalized]
    q['adj_closes']=[b['close']*(.8 if i<40 else 1) for i,b in enumerate(bars)]
    normalized=st.normalize_bars(q,now(bars))
    assert normalized[0]['date']==bars[40]['date'] and len(normalized)==100


def test_recent_entry_count_is_not_the_same_as_today_and_ignores_provisional_events():
    bars,h=market(80);n=now(bars);svc=service();svc.now=lambda:n
    events=[dict(id='1',date=bars[-10]['date'],type='entry',price=100,units=1),
            dict(id='2',date=bars[-5]['date'],type='exit',price=95,units=1),
            dict(id='3',date=bars[-1]['date'],type='entry',price=90,units=1,provisional=True)]
    row=dict(code='2330',name='isolated',date=bars[-1]['date'],price=90,bars=bars[-30:],signals=events,active=False,current_action='wait')
    svc.store.load.return_value={'payload':dict(date=row['date'],generated_at=n.isoformat(),selection_version=st.SELECTION_VERSION,all_rows=[row])}
    response=svc.payload();r=response['meta']['recent_signals']
    assert r['entries']==1 and r['exits']==1 and r['last_entry_date']==bars[-10]['date']
    assert response['meta']['stage_counts']['entry']==0


def test_causal_short_signals_do_not_repaint_when_later_bars_arrive():
    bars,h=market(100)
    for i,b in enumerate(bars):
        c=115-i*.15;b.update(open=c+.2,high=c+.5,low=c-.4,close=c,volume=1200000)
    bars[74].update(open=102,high=103,low=99,close=99.5,volume=2000000)
    early=ss.analyse('2330','isolated',quote(bars[:76]),h,now(bars[:76]))
    late=ss.analyse('2330','isolated',quote(bars),h,now(bars))
    assert early and late
    assert early['signals']==[e for e in late['signals'] if e['date']<=bars[75]['date']]
