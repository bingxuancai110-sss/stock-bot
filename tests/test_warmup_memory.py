"""Budget actual warm-up and ETF producers without external services."""
import ast
import datetime
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock


class WarmupMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8'))
        names = {'_guarded_background_stage', '_guarded_history_quotes',
                 'build_etf_product_rankings', '_build_etf_product_rankings',
                 '_start_etf_product_ranking_refresh', 'run_in_background', '_do_warmup'}
        cls.code = {n.name: compile(ast.Module(body=[n], type_ignores=[]), '<actual-warmup>', 'exec')
                    for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names}

    def test_history_batches_download_every_symbol_once_without_parallel_spike(self):
        fetch = Mock(side_effect=lambda codes, **kw: {c: {'close': 10} for c in codes})
        ns = dict(_background_work_memory_safe=Mock(return_value=True), get_realtime_stocks_bulk=fetch)
        exec(self.code['_guarded_history_quotes'], ns)
        codes = [str(i) for i in range(103)]
        self.assertEqual(set(ns['_guarded_history_quotes'](codes + ['0'], '1y', '.TW')), set(codes))
        self.assertEqual([c for call in fetch.call_args_list for c in call.args[0]], codes)
        for call in fetch.call_args_list:
            self.assertLessEqual(len(call.args[0]), 4)
            self.assertEqual(call.kwargs, dict(workers=1, rng='1y', market_suffix='.TW'))

    def test_growth_during_download_stops_next_batch_and_publishes_nothing(self):
        budget = Mock(side_effect=[True, False])
        ns = dict(_background_work_memory_safe=budget,
                  get_realtime_stocks_bulk=Mock(return_value={'0': {'close': 10}}))
        exec(self.code['_guarded_history_quotes'], ns)
        with self.assertRaisesRegex(RuntimeError, '未發布部分排名'):
            ns['_guarded_history_quotes']([str(i) for i in range(10)], '2y', '.TW')
        ns['get_realtime_stocks_bulk'].assert_called_once()

    def test_last_batch_is_checked_before_returning_statistics(self):
        ns = dict(_background_work_memory_safe=Mock(side_effect=[True, False]),
                  get_realtime_stocks_bulk=Mock(return_value={'A': {}}))
        exec(self.code['_guarded_history_quotes'], ns)
        with self.assertRaises(RuntimeError):
            ns['_guarded_history_quotes'](['A'], '1y')

    def test_refused_stage_does_not_run_or_touch_existing_data(self):
        ns = dict(_background_work_memory_safe=Mock(return_value=False),
                  _current_process_rss_bytes=lambda: 400 * 1024**2)
        exec(self.code['_guarded_background_stage'], ns)
        fn = Mock()
        with self.assertRaisesRegex(RuntimeError, '上次完整快照'):
            ns['_guarded_background_stage']('ETF排名', fn, reserve_mb=240)
        fn.assert_not_called()
        ns['_background_work_memory_safe'].assert_called_once_with(reserve_mb=240)

    def test_ranking_aborts_before_replacing_old_cache_or_shared_snapshot(self):
        old = {'categories': {'old': True}}
        ns = dict(time=Mock(time=lambda: 1000), _realtime_cache_lock=threading.Lock(),
                  _etf_product_ranking_cache={'at': 0, 'data': old},
                  _background_work_memory_safe=Mock(return_value=True),
                  get_etf_catalog_products=lambda: {'A': {'category': '主動式'}},
                  _fetch_taiex_history=lambda _: [],
                  _guarded_history_quotes=Mock(side_effect=RuntimeError('budget exhausted')),
                  _save_shared_data_snapshot=Mock())
        exec(self.code['_build_etf_product_rankings'], ns)
        with self.assertRaisesRegex(RuntimeError, 'budget exhausted'):
            ns['_build_etf_product_rankings'](force_refresh=True)
        self.assertIs(ns['_etf_product_ranking_cache']['data'], old)
        ns['_save_shared_data_snapshot'].assert_not_called()

    def test_warmup_and_page_refresh_cannot_build_rankings_at_same_time(self):
        lock = threading.Lock()
        ns = dict(_ETF_RANKING_BUILD_LOCK=lock,
                  _build_etf_product_rankings=Mock(side_effect=RuntimeError('download failed')))
        exec(self.code['build_etf_product_rankings'], ns)
        lock.acquire()
        with self.assertRaisesRegex(RuntimeError, '已在建立中'):
            ns['build_etf_product_rankings'](True)
        ns['_build_etf_product_rankings'].assert_not_called()
        lock.release()
        with self.assertRaisesRegex(RuntimeError, 'download failed'):
            ns['build_etf_product_rankings'](True)
        self.assertFalse(lock.locked())

    def test_page_refresh_is_serialized_and_releases_flag_after_queue_failure(self):
        executor = Mock()
        jobs = []
        executor.submit.side_effect = jobs.append
        ns = dict(_ETF_RANKING_REFRESH_RUNNING=False, _ETF_RANKING_REFRESH_LOCK=threading.Lock(),
                  _BACKGROUND_JOB_EXECUTOR=executor, build_etf_product_rankings=Mock())
        exec(self.code['_start_etf_product_ranking_refresh'], ns)
        start = ns['_start_etf_product_ranking_refresh']
        self.assertTrue(start())
        self.assertFalse(start())
        ns['build_etf_product_rankings'].assert_not_called()
        jobs.pop()()
        self.assertFalse(ns['_ETF_RANKING_REFRESH_RUNNING'])
        executor.submit.side_effect = RuntimeError('queue unavailable')
        self.assertFalse(start())
        self.assertFalse(ns['_ETF_RANKING_REFRESH_RUNNING'])

    def test_actual_warmup_refuses_all_expensive_stages_under_pressure(self):
        now = datetime.datetime(2026, 11, 9, 16, 0)
        names = ['fetch_institutional_data', 'fetch_monthly_revenue', 'fetch_valuation',
                 'get_industry_map', 'get_name_map', 'build_chips_payload',
                 'fetch_twse_etf_product_metrics', 'fetch_twse_etf_distribution_history',
                 'build_etf_product_rankings', 'build_turning_observation',
                 '_warm_current_position_quotes', 'compute_screener_rows',
                 'build_leaderboard', '_build_strategy_lab_payload', 'refresh_us_indices',
                 '_save_shared_data_snapshot', '_save_strategy_lab_shared_cache']
        ns = {name: Mock() for name in names}
        research = Mock()
        ns.update(taiwan_now=lambda: now, taiwan_today=lambda: now.date(),
                  _run_taiex_reduction_alerts=lambda: 'signals checked',
                  _background_work_memory_safe=Mock(return_value=False),
                  _current_process_rss_bytes=lambda: 400 * 1024**2,
                  _revenue_cache={}, _valuation_cache={},
                  _load_etf_product_ranking_snapshot=lambda: ({'old': True}, False, 'old'),
                  _taiwan_post_close=lambda: True,
                  app=Mock(blueprints={'us_rotation': Mock(us_rotation_research_job=research)}))
        exec(self.code['_guarded_background_stage'], ns)
        exec(self.code['_do_warmup'], ns)
        result = ns['_do_warmup']()
        self.assertIn('保留網站記憶體', result)
        for name in names:
            ns[name].assert_not_called()
        research.assert_not_called()

    def test_warmup_nested_bulk_downloads_inherit_serial_budget(self):
        context = threading.local()
        jobs = []
        executor = Mock()
        executor.submit.side_effect = jobs.append
        ns = dict(time=Mock(time=lambda: 1), log_trigger_source=Mock(),
                  _BACKGROUND_JOB_SUBMISSION_LOCK=threading.RLock(), _BACKGROUND_JOB_QUEUED=set(),
                  _job_mark_start=lambda _: True, _job_mark_done=Mock(),
                  _BACKGROUND_JOB_EXECUTOR=executor, _DAILY_SNAPSHOT_DOWNLOAD_CONTEXT=context)
        exec(self.code['run_in_background'], ns)
        seen = []
        ns['run_in_background']('預熱快取', lambda: seen.append(context.serial))
        jobs.pop()()
        self.assertEqual(seen, [True])
        self.assertFalse(context.serial)


if __name__ == '__main__':
    unittest.main()
