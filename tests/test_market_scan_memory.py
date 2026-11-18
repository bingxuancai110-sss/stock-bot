import threading
from unittest.mock import Mock

import market_scan_memory as memory
from test_radar_admission import install


def test_reclaims_rebuildable_caches_preserves_accounts_and_persisted_data(monkeypatch):
    monkeypatch.setattr(memory, '_last_reclaim', float('-inf'))
    monkeypatch.setattr(memory.time, 'time', lambda:1000)
    monkeypatch.setattr(memory.gc, 'collect', Mock())
    quotes={str(i):{'at':999,'data':{'history':[1,2,3]}} for i in range(800)}
    quotes['expired']={'at':0,'data':{'history':[1]}}
    persisted={'snapshot':{'rows':[{'code':'2330'}]}}
    account={'private-user':{'positions':[1]}}
    ns=dict(_realtime_cache=quotes,_realtime_cache_lock=threading.RLock(),
        _SHARED_SNAPSHOT_MEMORY_CACHE={'expired':{'at':0,'value':persisted}},
        _SHARED_SNAPSHOT_MEMORY_LOCK=threading.RLock(),
        _RADAR_SPARK_CACHE={'at':0,'data':{'2330':[1,2,3]}},_RADAR_SPARK_CACHE_LOCK=threading.Lock(),
        _WEB_SESSION_CACHE=account)
    assert memory.reclaim(ns,1000)
    assert len(quotes)==128 and '799' in quotes and 'expired' not in quotes
    assert not ns['_SHARED_SNAPSHOT_MEMORY_CACHE'] and persisted['snapshot']['rows']
    assert not ns['_RADAR_SPARK_CACHE']['data'] and ns['_WEB_SESSION_CACHE'] is account
    memory.gc.collect.assert_called_once()
    assert not memory.reclaim(ns,1001)


def test_admission_remeasures_memory_after_reclaim_without_lowering_reserve(monkeypatch):
    import builtins
    ns=dict(_current_process_rss_bytes=Mock(side_effect=[470*1024**2,350*1024**2]),
            _reclaim_market_scan_memory=Mock(return_value=True),RENDER_MEMORY_LIMIT_MB=512)
    install(ns,'_background_work_memory_safe')
    monkeypatch.setattr(builtins,'open',Mock(side_effect=OSError()))
    assert ns['_background_work_memory_safe'](96)
    ns['_reclaim_market_scan_memory'].assert_called_once()
    assert ns['_current_process_rss_bytes'].call_count==2


def test_admission_still_refuses_when_reclamation_cannot_restore_headroom(monkeypatch):
    import builtins
    ns=dict(_current_process_rss_bytes=lambda:470*1024**2,
            _reclaim_market_scan_memory=lambda:True,RENDER_MEMORY_LIMIT_MB=512)
    install(ns,'_background_work_memory_safe')
    monkeypatch.setattr(builtins,'open',Mock(side_effect=OSError()))
    assert not ns['_background_work_memory_safe'](96)
