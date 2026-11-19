import copy
from selection_logic import radar_candidates


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
