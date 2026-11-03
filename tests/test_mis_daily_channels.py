"""Request daily snapshot channels, not undated/incremental quote subscriptions."""
import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
from pathlib import Path
import threading
import time
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from market_quotes import prefer_quote


@lru_cache(maxsize=1)
def functions():
    names={'_twse_mis_quote_symbols','_fetch_twse_mis_quotes'}
    tree=ast.parse((Path(__file__).parents[1]/'main.py').read_text())
    return compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]), '<mis-production>','exec')


def context(rows):
    now=datetime(2026,11,3,9,40,tzinfo=ZoneInfo('Asia/Taipei'))
    response=Mock();response.json.return_value={'rtcode':'0000','msgArray':rows}
    session=Mock();session.get.return_value=response
    ns=dict(taiwan_today=lambda:now.date(),taiwan_now=lambda:now,datetime=datetime,time=time,
            TW_TZ=ZoneInfo('Asia/Taipei'),_realtime_cache_lock=threading.Lock(),
            _TWSE_MIS_CACHE={'day':now.date().isoformat(),'at':0,'data':{}},
            _TWSE_MIS_CACHE_SECONDS=5,_TWSE_MIS_BATCH_SIZE=20,_twse_mis_session=lambda:session,
            _taiwan_post_close=lambda:False,prefer_quote=prefer_quote)
    exec(functions(),ns)
    return ns,session


def test_daily_channels_cover_listed_otc_and_alpha_etfs_and_unknown_markets():
    ns,_=context([])
    assert ns['_twse_mis_quote_symbols'](['2308','6223','00981A'],{'2308':'.TW','6223':'.TWO'})==[
        'tse_2308.tw_20261103','otc_6223.tw_20261103','tse_00981A.tw_20261103','otc_00981A.tw_20261103']
    ns['taiwan_today']=lambda:datetime(2026,11,4).date()
    assert ns['_twse_mis_quote_symbols'](['2308'],'.TW')==['tse_2308.tw_20261104']


def test_full_ten_stock_snapshot_respects_default_feed_delay_and_replaces_mixed_format_cache():
    codes=['2308','009816','00981A','6442','6669','8996','6223','2368','2383','3026']
    suffixes={c:'.TW' for c in codes};suffixes['6223']='.TWO'
    rows=[dict(c=c,d='20261103',t='09:39:59',ex='otc' if c=='6223' else 'tse',z='100',y='99') for c in codes]
    ns,session=context(rows)
    ns['_TWSE_MIS_CACHE']['data']={'2308':dict(close=98,source='TWSE MIS',close_date='20261103',updated_at='20261103 09:20:00')}
    with ThreadPoolExecutor(max_workers=1) as executor:
        ns['_TWSE_MIS_EXECUTOR']=executor
        result=ns['_fetch_twse_mis_quotes'](codes,suffixes,force_refresh=True)
    assert set(result)==set(codes) and result['2308']['close']==100
    params=session.get.call_args.kwargs['params']
    assert 'delay' not in params  # retain MIS userDelay contract rather than forcing zero
    assert params['ex_ch']=='|'.join(('otc' if c=='6223' else 'tse')+'_'+c+'.tw_20261103' for c in codes)
    assert all(q['has_live_trade_price'] and q['source']=='TWSE MIS 盤中最後成交' for q in result.values())


def test_no_yesterday_or_missing_trade_price_is_promoted_to_today():
    rows=[dict(c='2308',d='20261102',ex='tse',z='100',y='99'),
          dict(c='2330',d='20261103',ex='tse',z='-',pz='-',y='99')]
    ns,_=context(rows)
    with ThreadPoolExecutor(max_workers=1) as executor:
        ns['_TWSE_MIS_EXECUTOR']=executor
        assert ns['_fetch_twse_mis_quotes'](['2308','2330'],'.TW',force_refresh=True,cache_result=False)=={}


def test_batch_missing_prices_recover_individually_without_refetching_successes():
    codes=['2308','2330','3026']
    ns,session=context([])
    def get(url, **kwargs):
        channels=kwargs['params']['ex_ch'].split('|')
        rows=[]
        for channel in channels:
            c=channel.split('_')[1].split('.')[0]
            rows.append(dict(c=c,ex='tse',d='20261103',t='09:39:59',y='100',
                             z='101' if len(channels)==1 or c=='3026' else '-',pz='-'))
        response=Mock();response.json.return_value={'rtcode':'0000','msgArray':rows}
        return response
    session.get.side_effect=get
    with ThreadPoolExecutor(max_workers=4) as executor:
        ns['_TWSE_MIS_EXECUTOR']=executor
        result=ns['_fetch_twse_mis_quotes'](codes,'.TW',force_refresh=True,cache_result=False)
    assert set(result)==set(codes)
    assert all(q['close']==101 and q['has_live_trade_price'] for q in result.values())
    calls=[call.kwargs['params']['ex_ch'] for call in session.get.call_args_list]
    assert len(calls)==3
    assert 'tse_3026.tw_20261103' not in calls


def test_failed_single_recovery_is_throttled_across_outer_retries():
    rows=[dict(c=c,d='20261103',ex='tse',z='-',pz='-',y='100') for c in ['2308','2330']]
    ns,session=context(rows)
    with ThreadPoolExecutor(max_workers=4) as executor:
        ns['_TWSE_MIS_EXECUTOR']=executor
        for _ in range(3):
            assert ns['_fetch_twse_mis_quotes'](['2308','2330'],'.TW',force_refresh=True,cache_result=False)=={}
    assert len(session.get.call_args_list)==5  # three batches, two single attempts total
