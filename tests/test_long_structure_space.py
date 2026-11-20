import copy
import pytest
from technical_space import long_space
from test_short_term import market
import short_term as st


def structure():
    bars,h=market(80)
    for b in bars:b.update(open=100,high=101,low=99,close=100)
    bars[45]['high']=115;bars[60]['high']=130
    bars[-1].update(open=104,high=106,low=104,close=105)
    return bars


def test_nearest_confirmed_pressure_cannot_be_skipped_for_better_reward_risk():
    bars=structure()
    result=long_space(bars,105,2)
    assert result['resistance']==115 and result['kind']=='已確認波段壓力'
    assert result['confirmed_date']==bars[47]['date']
    assert long_space(bars,114,2)['resistance']==115
    assert long_space(bars,120,2)['resistance']==130


def test_unconfirmed_recent_wick_does_not_replace_confirmed_swing_zone():
    bars=structure();bars[-2]['high']=106
    assert long_space(bars,105,2)['resistance']==115


def test_range_ceiling_and_model_reference_have_different_labels():
    bars=structure()
    for b in bars:b['high']=max(b['open'],b['close'])+1
    bars[-2]['high']=108
    p=long_space(bars,105,3)
    assert p['resistance']==108 and p['kind']=='60日區間上緣'
    p=long_space(bars,140,3)
    assert p['resistance'] is None and '模型參考' in p['kind']


def test_high_and_low_nominal_prices_use_the_same_structural_space():
    bars=structure()
    for factor in (.1,10,50):
        scaled=[dict(b,**{k:b[k]*factor for k in ('open','high','low','close')}) for b in bars]
        assert long_space(scaled,105*factor,2*factor)['resistance']==pytest.approx(115*factor)


def test_engine_and_observation_use_the_same_entry_based_space():
    bars=structure();p=st.setup(bars,dict(available=False,foreign=0,trust=0))
    assert p['target']==115
    assert p['rr']==pytest.approx((115-105)/(105-p['stop']))
    assert p['target_kind']==p['space']['kind']


def test_peak_cannot_be_confirmed_without_two_completed_right_bars():
    bars=structure();bars[-3]['high']=112
    # The peak one session before the current bar cannot be a confirmed pivot.
    bars[-2]['high']=111
    p=long_space(bars,105,2)
    assert p['pressure_date']!=bars[-2]['date']
