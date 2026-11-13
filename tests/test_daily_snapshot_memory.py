"""Exercise production memory controls without starting the app or market jobs."""
import ast
import datetime
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, MagicMock


class DailySnapshotMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8'))
        names = {'get_consecutive_days_batch', 'get_realtime_stocks_bulk',
                 'run_in_background', '_do_daily_snapshot',
                 'compute_industry_recent_performance', 'save_industry_recent_performance'}
        cls.code = {node.name: compile(ast.Module(body=[node], type_ignores=[]), '<actual-memory>', 'exec')
                    for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names}

    def snapshot(self, stage, admission):
        cursor = Mock()
        cursor.fetchone.return_value = (0,)
        ns = dict(taiwan_today=lambda: datetime.date(2026, 11, 9),
                  _job_get_progress=lambda _: {'stage': stage, 'index': 0, 'total': 3},
                  get_all_position_user_ids=lambda: ['owner'],
                  get_missing_portfolio_snapshot_user_ids=lambda _: [],
                  get_db_connection=lambda: Mock(cursor=lambda: cursor),
                  release_db_connection=Mock(), is_twse_trading_day=lambda _: True,
                  _taiwan_post_close=lambda: True, _run_taiex_reduction_alerts=lambda: 'signals checked',
                  _job_mark_progress=Mock(), _current_process_rss_bytes=lambda: 300 * 1024**2,
                  _background_work_memory_safe=Mock(side_effect=admission),
                  fetch_taiex_summary=Mock(),
                  get_industry_momentum=Mock(return_value={}), fetch_monthly_revenue=lambda: {},
                  get_industry_map=lambda: {}, save_industry_momentum=Mock(),
                  save_industry_recent_performance=Mock(return_value=1),
                  get_all_watchlist_user_ids=lambda: ['owner'], get_user_watchlist=Mock(return_value=['2330']),
                  compute_watchlist_scores=Mock(), compute_screener_rows=Mock(return_value=([], [], {})),
                  save_picks=Mock(return_value=0), save_turning_picks=Mock())
        exec(self.code['_do_daily_snapshot'], ns)
        return ns, ns['_do_daily_snapshot']

    def test_industry_defers_without_discarding_core_or_marking_done(self):
        ns, run = self.snapshot('industry', [False])
        self.assertIn('暫緩 industry', run())
        ns['get_industry_momentum'].assert_not_called()
        ns['save_industry_recent_performance'].assert_not_called()
        ns['_job_mark_progress'].assert_not_called()

    def test_core_snapshot_defers_before_market_fetch_when_memory_is_low(self):
        ns, run = self.snapshot('portfolio', [False])
        self.assertIn('暫緩 portfolio', run())
        ns['fetch_taiex_summary'].assert_not_called()
        ns['_job_mark_progress'].assert_not_called()

    def test_industry_rechecks_budget_before_history_statistics(self):
        ns, run = self.snapshot('industry', [True, False])
        self.assertIn('暫緩 industry', run())
        ns['save_industry_momentum'].assert_called_once()
        ns['save_industry_recent_performance'].assert_not_called()
        ns['_job_mark_progress'].assert_not_called()

    def test_watchlist_does_not_start_another_user_when_memory_is_low(self):
        ns, run = self.snapshot('watchlist', [False])
        self.assertIn('暫緩 watchlist', run())
        ns['get_user_watchlist'].assert_not_called()
        ns['compute_watchlist_scores'].assert_not_called()
        ns['_job_mark_progress'].assert_called_once_with('每日快照', 'watchlist', 0, 1)

    def test_picks_preserve_completed_mode_checkpoint_when_next_mode_defers(self):
        ns, run = self.snapshot('picks', [True, False])
        self.assertIn('暫緩 picks', run())
        ns['compute_screener_rows'].assert_called_once_with('blackhorse')
        self.assertEqual(ns['_job_mark_progress'].call_args.args, ('每日快照', 'picks', 1, 3))
        ns['_background_work_memory_safe'].assert_called_with(reserve_mb=280)

    def test_latest_n_query_deduplicates_codes_and_keeps_streak_semantics(self):
        history = {'A': [3, 2, 0, 7] * 1000, 'B': [-2, -3, None, -4] * 1000}
        cursor = Mock()
        def execute(sql, params):
            self.assertIn('CROSS JOIN LATERAL', sql)
            self.assertIn('ORDER BY trade_date DESC LIMIT %s', sql)
            codes, limit = params
            self.assertEqual(codes, ['A', 'B'])
            self.assertEqual(limit, 3)
            cursor.fetchall.return_value = [(c, i, v) for c in codes for i, v in enumerate(history[c][:limit])]
        cursor.execute.side_effect = execute
        ns = dict(get_db_connection=lambda: Mock(cursor=lambda: cursor), release_db_connection=Mock())
        exec(self.code['get_consecutive_days_batch'], ns)
        self.assertEqual(ns['get_consecutive_days_batch'](['A', 'B', 'A'], max_days=3), {'A': 2, 'B': 0})
        self.assertEqual(ns['get_consecutive_days_batch'](['A', 'B'], 'sell', 3), {'A': 0, 'B': 2})
        self.assertEqual(ns['get_consecutive_days_batch'](['A'], max_days=0), {'A': 0})
        self.assertEqual(cursor.execute.call_count, 2)

    def test_serial_download_cap_is_thread_local_and_restored_after_failure(self):
        context = threading.local()
        executor = Mock()
        queued = []
        executor.submit.side_effect = queued.append
        ns = dict(threading=threading, time=Mock(time=lambda: 1),
                  log_trigger_source=Mock(), _BACKGROUND_JOB_SUBMISSION_LOCK=threading.RLock(),
                  _BACKGROUND_JOB_QUEUED=set(), _job_mark_start=lambda _: True,
                  _job_mark_done=Mock(), _BACKGROUND_JOB_EXECUTOR=executor,
                  _DAILY_SNAPSHOT_DOWNLOAD_CONTEXT=context)
        exec(self.code['run_in_background'], ns)
        observed = []
        def job():
            observed.append(context.serial)
            other = threading.Thread(target=lambda: observed.append(getattr(context, 'serial', False)))
            other.start(); other.join()
            raise RuntimeError('download failed')
        ns['run_in_background']('每日快照', job)
        queued.pop()()
        self.assertEqual(observed, [True, False])
        self.assertFalse(context.serial)
        self.assertEqual(ns['_BACKGROUND_JOB_QUEUED'], set())

    def test_industry_uses_bounded_history_and_never_saves_partial_statistics(self):
        history = Mock(side_effect=RuntimeError('memory budget exhausted'))
        save = Mock()
        picks = [{'code': str(i)} for i in range(200)]
        ns = dict(get_picks_since=lambda *args: picks, _review_price_map=history,
                  _save_shared_data_snapshot=save)
        exec(self.code['compute_industry_recent_performance'], ns)
        exec(self.code['save_industry_recent_performance'], ns)
        self.assertEqual(ns['save_industry_recent_performance'](), 0)
        self.assertEqual(set(history.call_args.args[0]), {str(i) for i in range(200)})
        self.assertEqual(history.call_args.args[1], '6mo')
        save.assert_not_called()

    def test_bulk_fetch_honors_daily_cap_and_keeps_all_symbols(self):
        context = threading.local()
        pool = MagicMock()
        pool.return_value.__enter__.return_value.map.side_effect = lambda fn, codes: map(fn, codes)
        ns = dict(ThreadPoolExecutor=pool, _DAILY_SNAPSHOT_DOWNLOAD_CONTEXT=context,
                  _fetch_twse_mis_quotes=Mock(return_value={}), _load_stock_info_file_cache=Mock(),
                  _market_cache={'map': {}}, _suffix_cache={},
                  get_realtime_stock=Mock(side_effect=lambda code, *args, **kwargs: {'code': code}))
        exec(self.code['get_realtime_stocks_bulk'], ns)
        for serial, expected in [(True, 1), (False, 4)]:
            context.serial = serial
            result = ns['get_realtime_stocks_bulk'](['A', 'B', 'C', 'D'], workers=16)
            self.assertEqual(set(result), {'A', 'B', 'C', 'D'})
            pool.assert_called_with(max_workers=expected)


if __name__ == '__main__':
    unittest.main()
