import copy
from selection_logic import radar_candidates
from selection_logic import radar_state
import pytest


def test_research_slots_ignore_institution_lots_nominal_price_and_input_order():
    rows=[(str(1000+i),dict(spark_pct=5-i*.1,spark_turnover=100-i,total_net_lots=i,price=10**i)) for i in range(8)]
    categories={c:'A' if i<6 else 'B' for i,(c,_) in enumerate(rows)}
    expected=[c for c,_ in radar_candidates(rows,4,categories)]
    changed=copy.deepcopy(rows)
    for c,r in changed:r.update(total_net_lots=-999999 if c in expected else 999999,price=1)
    assert [c for c,_ in radar_candidates(list(reversed(changed)),4,categories)]==expected
    assert any(categories[c]=='B' for c in expected)


def test_complete_coverage_includes_negative_and_flat_names_without_duplicates():
    rows=[('2330',dict(spark_pct=-2,spark_turnover=100)),('2383',dict(spark_pct=0,spark_turnover=0))]
    assert {c for c,_ in radar_candidates(rows+rows,99,{})}=={'2330','2383'}
    assert len(radar_candidates(rows+rows,99,{}))==2
    assert radar_candidates(rows,0,{})==[] and radar_candidates([],5,{})==[]


@pytest.mark.parametrize('pct,ratio,label',[
    (2,2,'帶量上漲'),(-2,2,'帶量下跌'),(0,2,'放量整理'),
    (-2,1,'價格轉弱、量能普通'),(0,1,'價格持平、量能普通'),
    (None,2,'量能放大、漲跌待確認'),(None,None,'漲跌待確認'),
    (2,None,'價格強、量能待確認'),(2,1,'價格強、量能普通'),
])
def test_radar_labels_respect_direction_and_missing_evidence(pct,ratio,label):
    assert radar_state(dict(close=100,pct=pct,vol_ratio=ratio))==label


def test_inconsistent_highs_and_invalid_prices_do_not_create_bullish_states():
    assert radar_state(dict(close=100,pct=-2,vol_ratio=2,high_60d=90))=='帶量下跌'
    assert radar_state(dict(close=100,pct=2,vol_ratio=2,high_20d=99))=='真正帶量突破'
    assert radar_state(dict(close=100,pct=2,vol_ratio=None,high_20d=99))=='突破但量能待確認'
    assert radar_state(dict(close=float('nan'),pct=2,vol_ratio=2))=='行情待確認'
