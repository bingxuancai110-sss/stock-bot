"""Exercise actual review job admission without importing production main."""
import ast
import re
from pathlib import Path
import threading
from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock
from unittest.mock import patch, mock_open


class ReviewBackpressureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8')
        cls.compiled = {}
        for name in {
                '_start_workbench_review_background_refresh', 'evaluate_picks',
                '_review_price_map', '_background_work_memory_safe'
                ,'build_workbench_review_payload','_load_workbench_review_snapshot','_save_workbench_review_snapshot',
                '_get_workbench_review_snapshot','_workbench_review_snapshot_complete'
            }:
            fragment=re.search(r'(?ms)^def '+name+r'\b.*?(?=^def |^@|\Z)',source)[0]
            node=next(n for n in ast.parse(fragment).body if isinstance(n,ast.FunctionDef))
            cls.compiled[name] = compile(ast.Module(body=[node], type_ignores=[]), '<actual-review>', 'exec')

    def setUp(self):
        self.jobs = []
        self.executor = Mock()
        self.executor.submit.side_effect = self.jobs.append
        self.ns = dict(threading=threading, time=Mock(time=lambda: 123),
                       _WORKBENCH_REVIEW_REFRESH_LOCK=threading.Lock(),
                       _WORKBENCH_REVIEW_REFRESH_RUNNING=False,
                       _workbench_review_lock=threading.Lock(),
                       _workbench_review_cache={'at': 10, 'evaluations': {'old': True}},
                       _BACKGROUND_JOB_EXECUTOR=self.executor,
                       _background_work_memory_safe=Mock(return_value=True),
                       taiwan_today=lambda:datetime(2026,11,19).date(),
                       taiwan_now=lambda:datetime(2026,11,19,19),
                       get_picks_since=Mock(return_value=[]),
                       _review_price_map=Mock(return_value={}),_jsonable=lambda x:x,
                       _save_workbench_review_snapshot=Mock(return_value=True),
                       _sync_today_pick_factors_from_screener_snapshot=Mock(),
                       analyze_pick_factors=Mock(return_value={'n':0,'groups':[]}),
                       _workbench_merge_evaluations=Mock(return_value={'samples': []}))
        exec(self.compiled['_start_workbench_review_background_refresh'], self.ns)
        self.start = self.ns['_start_workbench_review_background_refresh']

    def test_only_one_review_job_can_be_queued(self):
        self.assertTrue(self.start())
        self.assertFalse(self.start())
        self.assertEqual(len(self.jobs), 1)
        self.ns['_workbench_merge_evaluations'].assert_not_called()
        self.jobs[0]()
        self.assertFalse(self.ns['_WORKBENCH_REVIEW_REFRESH_RUNNING'])
        self.assertEqual(self.ns['_workbench_review_cache']['at'], 123)

    def test_low_headroom_does_not_enqueue_or_discard_old_statistics(self):
        self.ns['_background_work_memory_safe'].return_value = False
        self.assertFalse(self.start())
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.ns['_workbench_review_cache']['evaluations'], {'old': True})

    def test_headroom_is_rechecked_after_waiting_in_queue(self):
        self.start()
        self.ns['_background_work_memory_safe'].return_value = False
        self.jobs[0]()
        self.ns['_workbench_merge_evaluations'].assert_not_called()
        self.assertFalse(self.ns['_WORKBENCH_REVIEW_REFRESH_RUNNING'])
        self.assertEqual(self.ns['_workbench_review_cache']['evaluations'], {'old': True})

    def test_failed_submission_releases_single_flight_flag(self):
        self.executor.submit.side_effect = RuntimeError('queue unavailable')
        with self.assertRaises(RuntimeError):
            self.start()
        self.assertFalse(self.ns['_WORKBENCH_REVIEW_REFRESH_RUNNING'])

    def test_history_download_is_serial_and_keeps_every_recommended_symbol(self):
        class Captured(Exception):
            pass
        picks = [{'code': str(i)} for i in range(200)]
        bulk = Mock(side_effect=Captured)
        ns = dict(get_picks_since=lambda *args: picks, get_realtime_stocks_bulk=bulk,
                  _background_work_memory_safe=Mock(return_value=True))
        exec(self.compiled['_review_price_map'], ns)
        exec(self.compiled['evaluate_picks'], ns)
        with self.assertRaises(Captured):
            ns['evaluate_picks']('radar')
        self.assertEqual(len(bulk.call_args.args[0]), 4)
        self.assertEqual(bulk.call_args.kwargs, {'workers': 1, 'rng': '6mo','skip_mis':True,'request_timeout':5})

    def test_bounded_batches_keep_all_symbols_when_memory_is_available(self):
        codes = [str(i) for i in range(200)]
        bulk = Mock(side_effect=lambda batch, **kwargs: {code: {'close': 10} for code in batch})
        ns = dict(get_realtime_stocks_bulk=bulk, _background_work_memory_safe=Mock(return_value=True))
        exec(self.compiled['_review_price_map'], ns)
        result = ns['_review_price_map'](codes, '6mo')
        self.assertEqual(set(result), set(codes))
        self.assertEqual(bulk.call_count, 50)
        self.assertTrue(all(len(call.args[0]) <= 4 for call in bulk.call_args_list))

    def test_memory_pressure_mid_download_does_not_return_partial_statistics(self):
        bulk = Mock(return_value={'2330': {'close': 10}})
        ns = dict(get_realtime_stocks_bulk=bulk,
                  _background_work_memory_safe=Mock(side_effect=[True, False]))
        exec(self.compiled['_review_price_map'], ns)
        with self.assertRaisesRegex(RuntimeError, '未發布部分統計'):
            ns['_review_price_map']([str(i) for i in range(8)], '6mo')
        self.assertEqual(bulk.call_count, 1)

    def test_container_memory_is_checked_even_when_worker_rss_is_low(self):
        ns = dict(_current_process_rss_bytes=lambda: 180 * 1024**2, RENDER_MEMORY_LIMIT_MB=512)
        exec(self.compiled['_background_work_memory_safe'], ns)
        with patch('builtins.open', mock_open(read_data=str(420 * 1024**2))):
            self.assertFalse(ns['_background_work_memory_safe']())

    def test_unavailable_cgroup_falls_back_to_worker_rss(self):
        ns = dict(_current_process_rss_bytes=lambda: 200 * 1024**2, RENDER_MEMORY_LIMIT_MB=512)
        exec(self.compiled['_background_work_memory_safe'], ns)
        with patch('builtins.open', side_effect=FileNotFoundError):
            self.assertTrue(ns['_background_work_memory_safe']())

    def test_one_mature_quote_batch_is_shared_with_all_three_review_sources(self):
        mature=datetime(2026,11,1).date();young=datetime(2026,11,18).date()
        self.ns['get_picks_since'].side_effect=lambda mode,days:[{'code':'2330','date':mature},{'code':'2383','date':young}]
        prices={'2330':{'close':100}};self.ns['_review_price_map'].return_value=prices
        self.start();self.jobs[0]()
        self.ns['_review_price_map'].assert_called_once_with(['2330'],'6mo')
        calls=self.ns['_workbench_merge_evaluations'].call_args_list
        self.assertEqual([c.args[0] for c in calls],[('blackhorse',),('radar','radar_live'),('turning',)])
        self.assertTrue(all(c.kwargs['price_map'] is prices for c in calls))
        factor_calls=self.ns['analyze_pick_factors'].call_args_list
        self.assertEqual([c.args[0] for c in factor_calls],['blackhorse','radar','turning'])
        self.assertTrue(all(c.kwargs['price_map'] is prices and not c.kwargs['sync_factors'] for c in factor_calls))
        self.ns['_save_workbench_review_snapshot'].assert_called_once()

    def test_persistence_failure_preserves_previous_statistics(self):
        self.ns['_save_workbench_review_snapshot'].return_value=False
        self.start();self.jobs[0]()
        self.assertEqual(self.ns['_workbench_review_cache']['evaluations'],{'old':True})

    def test_all_missing_mature_quotes_cannot_publish_empty_statistics(self):
        self.ns['get_picks_since'].return_value=[{'code':'2330','date':datetime(2026,11,1).date()}]
        self.ns['_review_price_map'].return_value={}
        self.start();self.jobs[0]()
        self.ns['_save_workbench_review_snapshot'].assert_not_called()
        self.assertEqual(self.ns['_workbench_review_cache']['evaluations'],{'old':True})

    def test_stale_saved_statistics_survive_worker_restart_while_refresh_is_pending(self):
        stats={'horizons':{'5–19 日':{'n':1,'avg':10,'market':2,'samples':[]}}}
        saved={'at':1,'computed_at':'2026-11-18T18:00:00+08:00','evaluations':{'blackhorse':stats,'radar':stats,'turning':stats}}
        self.ns.update(_WORKBENCH_REVIEW_SNAPSHOT_LOADED=False,_WORKBENCH_REVIEW_CACHE_SECONDS=10,
            _load_workbench_review_snapshot=Mock(return_value=saved),
            _workbench_review_cache={'at':0,'evaluations':{}},
            _start_workbench_review_background_refresh=Mock(return_value=False),
            _workbench_review_mode_picks=lambda *a:[],
            _workbench_number=lambda v,default=None:default if v is None else float(v))
        for name in ('_workbench_review_snapshot_complete','_get_workbench_review_snapshot','build_workbench_review_payload'):exec(self.compiled[name],self.ns)
        payload=self.ns['build_workbench_review_payload']()
        self.assertTrue(payload['statistics_stale'] and payload['statistics_deferred'])
        self.assertTrue(all(m['statistics_ready'] and m['horizons'][0]['average_pct']==10 for m in payload['modes']))
        self.assertEqual(payload['computed_at'],saved['computed_at'])

    def test_saved_statistics_use_the_existing_snapshot_store_and_keep_actual_timestamp(self):
        store=Mock();store.load.return_value={'payload':{'at':1,'computed_at':'saved','evaluations':{'turning':{}}}}
        ns=dict(_taiex_reduction_store=lambda:store,taiwan_today=lambda:datetime(2026,11,19).date())
        for name in ('_load_workbench_review_snapshot','_save_workbench_review_snapshot'):exec(self.compiled[name],ns)
        saved=ns['_load_workbench_review_snapshot']()
        self.assertEqual(saved['computed_at'],'saved')
        self.assertTrue(ns['_save_workbench_review_snapshot'](saved))
        self.assertEqual(store.save.call_args.args[0],'workbench_review:v1')
        self.assertEqual(store.save.call_args.kwargs['data_date'],datetime(2026,11,19).date())

    def test_legacy_review_without_factor_groups_requires_refresh_but_keeps_evaluations(self):
        ns={};exec(self.compiled['_workbench_review_snapshot_complete'],ns)
        snapshot={'evaluations':{m:None for m in ('blackhorse','radar','turning')}}
        assert not ns['_workbench_review_snapshot_complete'](snapshot)
        snapshot['factor_data']={m:None for m in ('blackhorse','radar','turning')}
        assert ns['_workbench_review_snapshot_complete'](snapshot)
