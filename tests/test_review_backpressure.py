"""Exercise actual review job admission without importing production main."""
import ast
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock
from unittest.mock import patch, mock_open


class ReviewBackpressureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8'))
        cls.compiled = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in {
                '_start_workbench_review_background_refresh', 'evaluate_picks',
                '_review_price_map', '_background_work_memory_safe'
            }:
                cls.compiled[node.name] = compile(ast.Module(body=[node], type_ignores=[]), '<actual-review>', 'exec')

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
        self.assertEqual(bulk.call_args.kwargs, {'workers': 1, 'rng': '6mo'})

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
