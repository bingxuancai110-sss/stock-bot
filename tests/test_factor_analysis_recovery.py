from datetime import date,datetime
from unittest.mock import Mock
import html,math
import pytest
from flask import Flask
from test_radar_admission import install


@pytest.mark.parametrize('available,fresh,running,status',[
    (False,False,True,202),(True,True,False,200),(True,False,True,200),
])
def test_factor_endpoint_only_reads_saved_groups(available,fresh,running,status):
    factor_data={m:{'n':6,'groups':[]} for m in ('blackhorse','radar','turning')} if available else {}
    render=Mock(side_effect=lambda label,value:'<section>'+label+'已保存分組</section>')
    ns=dict(html=html,_get_workbench_review_snapshot=lambda:(dict(factor_data=factor_data,computed_at='saved'),fresh),
        _WORKBENCH_REVIEW_REFRESH_RUNNING=running,render_pick_factors=render,
        analyze_pick_factors=Mock(side_effect=AssertionError('request must never compute factors')))
    install(ns,'web_pick_factors');app=Flask(__name__)
    app.add_url_rule('/factors',view_func=lambda:ns['web_pick_factors']('fixture-user'))
    response=app.test_client().get('/factors')
    assert response.status_code==status
    assert response.headers['X-Factors-Available']==('1' if available else '0')
    assert response.headers['X-Factors-Status']==('fresh' if fresh else 'building')
    assert response.headers['Cache-Control']=='no-store'
    assert render.call_count==(3 if available else 0)
    ns['analyze_pick_factors'].assert_not_called()


def test_factor_analysis_reuses_shared_prices_and_preserves_existing_group_math():
    picks=[dict(code='2330',date=date(2026,11,1),rank=1,score=80,industry='半導體',price=100) for _ in range(6)]
    prices={'2330':{'close':110}};sync=Mock();network=Mock(side_effect=AssertionError('shared prices must be reused'))
    ns=dict(date=date,datetime=datetime,math=math,taiwan_today=lambda:date(2026,11,19),
        _sync_today_pick_factors_from_screener_snapshot=sync,get_picks_since=lambda *a:picks,
        _review_price_map=network,_pick_return_split_safe=lambda *a:(10,True,False),
        get_industry_map=lambda:{},industry_name=lambda value:value)
    install(ns,'analyze_pick_factors')
    result=ns['analyze_pick_factors']('radar',price_map=prices,sync_factors=False)
    assert result['n']==6 and result['overall']['avg']==10 and result['groups']
    network.assert_not_called();sync.assert_not_called()
