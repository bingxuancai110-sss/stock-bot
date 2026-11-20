import pytest
import short_term as st
from test_short_term import service


def saved():
    svc=service()
    prices=[None,99.9,100,499.99,500,999.99,1000,4500]+list(range(101,141))
    rows=[dict(code=str(1000+i),name='isolated',date='2026-11-17',price=p,signals=[],signal_stage='watch') for i,p in enumerate(prices)]
    svc.store.load.return_value={'payload':dict(date='2026-11-17',generated_at=svc.now().isoformat(),selection_version=st.SELECTION_VERSION,
        all_rows=rows,short_rows=[dict(r,direction='short') for r in rows])}
    return svc


@pytest.mark.parametrize('direction',['long','short'])
def test_price_sort_precedes_pagination_and_does_not_scan(direction):
    svc=saved()
    result=svc.payload(direction=direction,sort='price_desc',limit=3)
    assert [r['price'] for r in result['rows']]==[4500,1000,999.99]
    assert result['meta']['has_more'] and result['meta']['total']==48
    assert svc.payload(direction=direction,sort='price_desc',offset=47)['rows'][-1]['price'] is None
    ascending=svc.payload(direction=direction,sort='price_asc',limit=2)
    assert [r['price'] for r in ascending['rows']]==[99.9,100]
    svc.executor.submit.assert_not_called()


@pytest.mark.parametrize('band,expected',[
    ('under100',[99.9]),('1000plus',[4500,1000]),
    ('500to1000',[999.99,500]),('100to500',[499.99]+list(reversed(range(101,141)))+[100]),
])
def test_price_boundaries_and_counts(band,expected):
    result=saved().payload(price_range=band,sort='price_desc',limit=50)
    assert [r['price'] for r in result['rows']]==expected
    assert result['meta']['total']==len(expected)
    assert result['meta']['position_counts']['waiting']==len(expected)


def test_unknown_options_fall_back_and_empty_range_resets_page():
    svc=saved()
    result=svc.payload(sort='bad',price_range='bad')
    assert result['meta']['sort']=='signal' and result['meta']['price_range']=='all'
    result=svc.payload(price_range='1000plus',offset=1000)
    assert result['meta']['offset']==0 and result['meta']['total']==2
