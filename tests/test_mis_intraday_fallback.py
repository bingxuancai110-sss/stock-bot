"""The snapshot may omit z/pz while MIS time-and-sales still contains today's trades."""
import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
import threading
import time
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from market_quotes import mis_intraday_trade, prefer_quote

NOW = datetime(2026, 11, 3, 9, 30, tzinfo=ZoneInfo('Asia/Taipei'))


def bar(price=101, at=NOW-timedelta(minutes=3)):
    return {'c': str(price), 'tlong': str(int(at.timestamp()*1000)), 's': '10'}


@pytest.mark.parametrize('rows', [
    [bar(at=NOW-timedelta(days=1))], [bar(at=NOW+timedelta(minutes=2))],
    [bar(at=NOW.replace(hour=8))], [bar('nan')], [bar('inf')], [bar(0)],
    [{'c': '101', 'tlong': '-'}], [],
])
def test_chart_never_invents_a_trade_from_invalid_or_noncurrent_data(rows):
    assert mis_intraday_trade({'rtcode':'0000','msgArray':rows}, NOW) is None


def test_chart_uses_latest_trade_not_array_order_or_http_time():
    earlier=bar(at=NOW-timedelta(minutes=20))
    latest=bar(102)
    result=mis_intraday_trade({'msgArray':[latest, earlier]}, NOW)
    assert result['close']==102 and result['time']==NOW-timedelta(minutes=3)
    assert mis_intraday_trade({'rtcode':'0001','msgArray':[latest]}, NOW) is None


@lru_cache(maxsize=1)
def production_functions():
    names={'_twse_mis_quote_symbols','_fetch_twse_mis_quotes','_fetch_twse_mis_chart_trade'}
    tree=ast.parse((Path(__file__).parents[1]/'main.py').read_text())
    return compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names], type_ignores=[]), '<production-mis>', 'exec')


def feed(rows, chart_rows=None, postclose=False):
    session=Mock()
    def get(url, **kwargs):
        response=Mock()
        response.json.return_value=({'rtcode':'0000','msgArray':rows} if 'getStockInfo' in url else
                                    {'rtcode':'0000','msgArray':chart_rows if chart_rows is not None else [bar()]})
        return response
    session.get.side_effect=get
    ns=dict(_twse_mis_session=lambda:session, time=time, datetime=datetime,
            taiwan_now=lambda:NOW, taiwan_today=lambda:NOW.date(),
            TW_TZ=ZoneInfo('Asia/Taipei'), _taiwan_post_close=lambda:postclose,
            _is_taiwan_intraday_window=lambda:not postclose,
            _realtime_cache_lock=threading.Lock(), _TWSE_MIS_CACHE={'day':NOW.date().isoformat(),'data':{}},
            _TWSE_MIS_CACHE_SECONDS=5,_TWSE_MIS_BATCH_SIZE=20,
            mis_intraday_trade=mis_intraday_trade, prefer_quote=prefer_quote)
    exec(production_functions(),ns)
    return ns,session


def quote(code='2308', **updates):
    return dict(c=code,ex='tse',d='20261103',t='09:30:00',z='-',pz='-',y='100',**updates)


def test_all_ten_missing_snapshots_can_use_actual_same_day_official_trades():
    codes=[str(2308+i) for i in range(10)]
    ns,session=feed([quote(c) for c in codes])
    with ThreadPoolExecutor(max_workers=4) as executor:
        ns['_TWSE_MIS_EXECUTOR']=executor
        result=ns['_fetch_twse_mis_quotes'](codes,'.TW',force_refresh=True,cache_result=False)
        assert set(result)==set(codes)
        for q in result.values():
            assert q['close']==101 and q['pct']==1 and q['has_live_trade_price'] is True
            assert q['source']=='TWSE MIS 當日分時成交' and q['close_is_final'] is False
            assert q['quote_updated_at']=='2026-11-03 09:27:00'
        calls=len(session.get.call_args_list)
        repeated=ns['_fetch_twse_mis_quotes'](codes,'.TW',force_refresh=True,cache_result=False)
        assert repeated==result
        assert len(session.get.call_args_list)==calls+1  # chart cache, one batch request only


def test_snapshot_live_price_does_not_fetch_chart_and_older_chart_cannot_roll_back_cache():
    row=quote();row['z']='103'
    ns,session=feed([row])
    old=dict(close=102,source='TWSE MIS',quote_updated_at='20261103 09:20:00',fetched_at='2026-11-03 09:20:00')
    ns['_TWSE_MIS_CACHE']['data']={'2308':old}
    with ThreadPoolExecutor(max_workers=1) as executor:
        ns['_TWSE_MIS_EXECUTOR']=executor
        result=ns['_fetch_twse_mis_quotes'](['2308'],'.TW',force_refresh=True)
        assert result['2308']['close']==103 and len(session.get.call_args_list)==1


def test_old_chart_does_not_replace_missing_price_with_yesterday_or_bid_ask():
    row=quote();row.update(a='105_106_',b='104_103_')
    ns,session=feed([row], [bar(at=NOW-timedelta(days=1))])
    with ThreadPoolExecutor(max_workers=1) as executor:
        ns['_TWSE_MIS_EXECUTOR']=executor
        assert ns['_fetch_twse_mis_quotes'](['2308'],'.TW',force_refresh=True,cache_result=False)=={}
        assert ns['_fetch_twse_mis_quotes'](['2308'],'.TW',force_refresh=True,cache_result=False)=={}
        assert len(session.get.call_args_list)==3  # a failed chart is also throttled


def test_after_close_chart_is_not_used_as_official_final_price():
    ns,session=feed([quote()],postclose=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        ns['_TWSE_MIS_EXECUTOR']=executor
        assert ns['_fetch_twse_mis_quotes'](['2308'],'.TW',force_refresh=True,cache_result=False)=={}
        assert len(session.get.call_args_list)==1
