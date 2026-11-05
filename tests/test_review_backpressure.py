"""Exercise actual review job admission without importing production main."""
import ast
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock


class ReviewBackpressureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8'))
        cls.compiled = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in {
                '_start_workbench_review_background_refresh', 'evaluate_picks'
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
        ns = dict(get_picks_since=lambda *args: picks, get_realtime_stocks_bulk=bulk)
        exec(self.compiled['evaluate_picks'], ns)
        with self.assertRaises(Captured):
            ns['evaluate_picks']('radar')
        self.assertEqual(set(bulk.call_args.args[0]), {p['code'] for p in picks})
        self.assertEqual(bulk.call_args.kwargs, {'workers': 1, 'rng': '6mo'})
