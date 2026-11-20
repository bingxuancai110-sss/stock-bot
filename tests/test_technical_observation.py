import copy
from test_short_term import market,quote,now
import short_term as st
import short_side as ss


def test_extreme_high_price_decline_is_visible_without_becoming_new_short():
    bars,h=market(85)
    for i,b in enumerate(bars):
        c=110-i*.1;b.update(open=c+.2,high=c+.4,low=c-.4,close=c,volume=1000000)
    b=bars[-1];c=bars[-2]['close']*.9
    b.update(open=c,high=c+.1,low=c-.2,close=c,volume=1500000)
    for factor in (1,10,50):
        scaled=[dict(b,**{k:b[k]*factor for k in ('open','high','low','close')}) for b in bars]
        result=ss.analyse('2330','isolated',quote(scaled),h,now(scaled))
        assert result and not result['entry_ready']
        assert result['current_action']!='entry'
        assert any('9.5%' in x for x in result['pending'])


def test_breakdown_stop_uses_broken_structure_not_distant_high():
    bars,h=market(80)
    for i,b in enumerate(bars):
        c=110 if i<50 else 104
        b.update(open=c+.2,high=c+.5,low=90 if i==40 else c-1,close=c,volume=1000000)
    bars[-4]['high']=125
    bars[-2].update(open=101,high=101,low=99,close=99.5,volume=1400000)
    bars[-1].update(open=100,high=100.2,low=98.8,close=99,volume=1400000)
    plan=ss.setup(bars)
    assert plan['trigger'] and plan['strategy']=='breakout'
    assert bars[-1]['close']<plan['stop']<110
    assert plan['stop']<max(b['high'] for b in bars[-6:-1])
    assert 0<plan['risk_limit']<=.12


def test_repair_is_watch_only_and_does_not_invent_a_buy():
    bars,h=market(85)
    bars[-1].update(open=105,high=106,low=98,close=99,volume=2000000)
    row=st.analyse('2330','isolated repair',quote(bars),h,now(bars))
    assert row and row['current_action']!='entry'
    assert 'repair' in row['patterns'] and row['pattern_label']=='多方修復觀察'
    assert any('不是買點' in x for x in row['pending'])


def test_scale_invariance_and_price_independent_risk_limit():
    bars,h=market(80);base=st.setup(bars,dict(available=False,foreign=0,trust=0))
    for factor in (10,50):
        scaled=[dict(b,**{k:b[k]*factor for k in ('open','high','low','close')}) for b in bars]
        p=st.setup(scaled,dict(available=False,foreign=0,trust=0))
        assert p['entry']==base['entry'] and p['watchable']==base['watchable']
        assert abs(p['risk_limit']-base['risk_limit'])<1e-10


def test_deep_audit_has_numeric_evidence_for_both_directions():
    bars,h=market(80)
    audit=st.technical_audit('2330','isolated',bars)
    assert audit['deep'] and audit['bars']==80 and audit['date']==bars[-1]['date']
    for key in ('long','short'):
        assert 6<=audit[key]['risk_limit_pct']<=12
        assert isinstance(audit[key]['rr'],float) and audit[key]['reasons']
    assert st.technical_audit('2330','isolated',[])['bars']==0
