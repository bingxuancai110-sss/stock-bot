"""Exercise actual producer/API functions without importing the Flask app."""
import ast
from pathlib import Path
import re
import threading
import time
from unittest.mock import Mock
import pytest

SOURCE=Path('main.py').read_text(encoding='utf-8-sig')
def install(ns,name):
    fragment=re.search(r'(?ms)^def '+name+r'\b.*?(?=^def |^@|\Z)',SOURCE)[0]
    node=next(n for n in ast.parse(fragment).body if isinstance(n,ast.FunctionDef))
    exec(compile(ast.Module(body=[node],type_ignores=[]),'actual-radar','exec'),ns)

def producer(admission=True):
    ns={'_SCREENER_REFRESH_LOCK':threading.Lock(),'_SCREENER_REFRESH_RUNNING':{},'_SCREENER_REFRESH_ERRORS':{},
        'RADAR_SCHEDULED_DEEP_SCAN_LIMIT':240,'RADAR_LIGHT_SCAN_RESERVE_MB':200,'RADAR_DEEP_SCAN_LIMIT':240,
        '_background_work_memory_safe':Mock(return_value=admission),'_BACKGROUND_JOB_EXECUTOR':Mock(),
        'compute_screener_rows':Mock(),'_screener_cache':{'radar':{'rows':[{'code':'2330'}]}},
        '_screener_empty_result_has_scan_evidence':Mock(return_value=False),'time':time}
    install(ns,'_start_screener_background_refresh');install(ns,'_screener_refresh_status')
    return ns

def test_default_radar_background_is_bounded_even_outside_market_hours():
    ns=producer();assert ns['_start_screener_background_refresh']('radar')
    ns['_background_work_memory_safe'].assert_called_once_with(reserve_mb=200)
    ns['_BACKGROUND_JOB_EXECUTOR'].submit.call_args.args[0]()
    ns['compute_screener_rows'].assert_called_once_with('radar',persist=True,force_refresh=True,persist_live=False,radar_deep_limit=240,price_workers=2)
    assert not ns['_SCREENER_REFRESH_RUNNING']['radar']

@pytest.mark.parametrize('mode,limit',[('blackhorse',None)])
def test_non_radar_scans_keep_the_original_headroom(mode,limit):
    ns=producer();assert ns['_start_screener_background_refresh'](mode,radar_deep_limit=limit)
    ns['_background_work_memory_safe'].assert_called_once_with(reserve_mb=280)

def test_memory_rejection_is_not_reported_as_a_running_scan():
    ns=producer(False);assert not ns['_start_screener_background_refresh']('radar')
    ns['_BACKGROUND_JOB_EXECUTOR'].submit.assert_not_called()
    status=ns['_screener_refresh_status']('radar',False)
    assert not status['building'] and status['deferred'] and '記憶體' in status['note']

def test_recheck_after_queueing_prevents_compute_when_memory_has_changed():
    ns=producer();ns['_background_work_memory_safe'].side_effect=[True,False]
    ns['_start_screener_background_refresh']('radar')
    ns['_BACKGROUND_JOB_EXECUTOR'].submit.call_args.args[0]()
    ns['compute_screener_rows'].assert_not_called()
    assert not ns['_SCREENER_REFRESH_RUNNING']['radar']
    assert '失敗' in ns['_screener_refresh_status']('radar')['note']

def test_failed_scan_has_a_bounded_retry_cooldown_and_clears_running_flag():
    ns=producer();ns['compute_screener_rows'].side_effect=RuntimeError('fixture failure')
    ns['_start_screener_background_refresh']('radar');ns['_BACKGROUND_JOB_EXECUTOR'].submit.call_args.args[0]()
    assert not ns['_start_screener_background_refresh']('radar')
    assert ns['_BACKGROUND_JOB_EXECUTOR'].submit.call_count==1

def test_empty_quotes_are_a_failure_not_a_successful_zero_result():
    ns=producer();ns['_screener_cache']['radar']={'rows':[]}
    completed=Mock();failed=Mock()
    ns['_start_screener_background_refresh']('radar',on_complete=completed,on_failure=failed)
    ns['_BACKGROUND_JOB_EXECUTOR'].submit.call_args.args[0]()
    completed.assert_not_called();failed.assert_called_once()

def test_valid_zero_candidates_with_scan_evidence_are_accepted():
    ns=producer();ns['_screener_cache']['radar']={'rows':[]}
    ns['_screener_empty_result_has_scan_evidence'].return_value=True
    completed=Mock();ns['_start_screener_background_refresh']('radar',on_complete=completed)
    ns['_BACKGROUND_JOB_EXECUTOR'].submit.call_args.args[0]();completed.assert_called_once()

def test_repeated_requests_do_not_queue_duplicate_scans():
    ns=producer();assert ns['_start_screener_background_refresh']('radar')
    assert not ns['_start_screener_background_refresh']('radar')
    assert ns['_screener_refresh_status']('radar')['building']
    assert ns['_BACKGROUND_JOB_EXECUTOR'].submit.call_count==1

@pytest.mark.parametrize('limit',[0,-1,4])
def test_explicit_small_depth_normalizes_and_caps_history_workers(limit):
    ns=producer();ns['_start_screener_background_refresh']('radar',radar_deep_limit=limit,price_workers=20)
    ns['_BACKGROUND_JOB_EXECUTOR'].submit.call_args.args[0]()
    assert ns['compute_screener_rows'].call_args.kwargs['radar_deep_limit']==4
    assert ns['compute_screener_rows'].call_args.kwargs['price_workers']==2

def test_workbench_api_exposes_actual_deferred_status_and_limited_scope():
    ns=producer(False)
    ns.update(_is_taiwan_intraday_window=lambda:False,_screener_recent_snapshot=lambda mode:(None,''),
              _workbench_screener_rows=lambda mode,snapshot:[])
    install(ns,'_workbench_source_payload')
    payload=ns['_workbench_source_payload']('fixture-user','雷達')
    assert payload['ok'] and not payload['meta']['available'] and not payload['meta']['building'] and payload['meta']['deferred']
    ns['_screener_recent_snapshot']=lambda mode:({'rows':[{}],'source_meta':{'radar_diagnostics':{'deep_candidate_count':4,'spark_qualified_count':1600}}},'fixture')
    payload=ns['_workbench_source_payload']('fixture-user','雷達')
    assert payload['meta']['scan_limited'] and payload['meta']['deep_scanned']==4
