"""Exercise the production MIS request and session functions without importing app startup."""
import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo


def test_official_polls_reuse_session_without_rebootstrapping_each_time():
    tree=ast.parse((Path(__file__).parents[1]/'main.py').read_text())
    now=datetime(2026,11,2,10,45,tzinfo=ZoneInfo('Asia/Taipei'))
    session=Mock()
    response=Mock()
    response.json.return_value={'rtcode':'0000','msgArray':[dict(c='2330',d='20261102',ex='tse',t='10:44:59',z='100',y='99',h='101',l='98',v='10')]}
    session.get.return_value=response
    factory=Mock(return_value=session)
    ns=dict(requests=SimpleNamespace(Session=factory),HTTPAdapter=Mock(),
            _TWSE_MIS_SESSION_LOCAL=threading.local(),time=time,
            taiwan_now=lambda:now,taiwan_today=lambda:now.date(),
            _realtime_cache_lock=threading.Lock(),_TWSE_MIS_CACHE={'day':None,'data':{}},
            _TWSE_MIS_CACHE_SECONDS=5,_TWSE_MIS_BATCH_SIZE=20,
            datetime=datetime,TW_TZ=ZoneInfo('Asia/Taipei'),_taiwan_post_close=lambda:False)
    names={'_twse_mis_session','_twse_mis_quote_symbols','_fetch_twse_mis_quotes'}
    for n in tree.body:
        if isinstance(n,ast.FunctionDef) and n.name in names:
            exec(compile(ast.Module(body=[n],type_ignores=[]),'<production-mis>','exec'),ns)
    with ThreadPoolExecutor(max_workers=1) as executor:
        ns['_TWSE_MIS_EXECUTOR']=executor
        for _ in range(2):
            result=ns['_fetch_twse_mis_quotes'](['2330'],market_suffix='.TW',force_refresh=True,cache_result=False)
            assert result['2330']['close']==100
            assert result['2330']['has_live_trade_price'] is True
            assert result['2330']['source']=='TWSE MIS 盤中最後成交'
        factory.assert_called_once()
        urls=[call.args[0] for call in session.get.call_args_list]
        assert urls.count('https://mis.twse.com.tw/stock/index.jsp')==1
        assert urls.count('https://mis.twse.com.tw/stock/api/getStockInfo.jsp')==2
