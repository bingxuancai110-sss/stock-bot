"""Run without importing main (which starts production background jobs).

SQLite executes the actual helper's INSERT/ON CONFLICT/transaction logic;
this does not replace a PostgreSQL concurrency or production verification.
"""
import ast
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sqlite3
import unittest
import textwrap


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''
          CREATE TABLE positions(user_id TEXT, code TEXT, shares INTEGER, cost REAL,
            bought_on TEXT, note TEXT);
          CREATE TABLE position_change_logs(user_id TEXT, code TEXT, action TEXT,
            shares_delta INTEGER, trade_price REAL, trade_date TEXT, note TEXT, source TEXT);
        ''')
        self.lose_ack = False
        self.fail_log = False
        outer = self

        class Cursor:
            def __init__(self): self.raw = outer.db.cursor()
            def execute(self, sql, params):
                if outer.fail_log and 'INSERT INTO position_change_logs' in sql:
                    raise RuntimeError('log write failed')
                return self.raw.execute(sql.replace('%s', '?'), params)
            def fetchone(self): return self.raw.fetchone()
            def close(self): self.raw.close()

        class Connection:
            def cursor(self): return Cursor()
            def commit(self):
                outer.db.commit()
                if outer.lose_ack:
                    outer.lose_ack = False
                    raise RuntimeError('commit succeeded, reply lost')
            def rollback(self): outer.db.rollback()

        source = Path(__file__).resolve().parents[1].joinpath('main.py').read_text(encoding='utf-8')
        schema = re.search(r'CREATE TABLE IF NOT EXISTS position_submission_dedup \([\s\S]+?\n            \)', source)
        self.assertIsNotNone(schema, 'startup must create the dedup table')
        self.db.execute(schema.group().replace('DEFAULT NOW()', 'DEFAULT CURRENT_TIMESTAMP'))
        start = source.index('\ndef add_position(')
        end = source.index('\ndef _void_same_day_correction_logs(', start)
        tree = ast.parse(source[start:end])
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'add_position')
        scope = dict(math=math, re=re, hashlib=hashlib, json=json,
                     get_db_connection=lambda: Connection(), release_db_connection=lambda c: None,
                     taiwan_today=lambda: '2026-11-05', clear_leaderboard_cache=lambda **k: None,
                     _invalidate_home_intraday_payload_cache=lambda u: None)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), '<actual-add-position>', 'exec'), scope)
        self.add = scope['add_position']

    def tearDown(self): self.db.close()

    def submit(self, user='u1', qty=4, key='request_key_123456789'):
        return self.add(user, '8996', qty, 1300.75, '2026-11-05', note='record', request_key=key)

    def count(self, table): return self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def test_replay_records_exactly_one_lot_and_log(self):
        self.assertIs(self.submit(), True)
        self.assertEqual(self.submit(), 'duplicate')
        self.assertEqual([self.count(t) for t in ('positions', 'position_change_logs')], [1, 1])

    def test_lost_commit_reply_can_be_safely_replayed(self):
        self.lose_ack = True
        self.assertIs(self.submit(), False)
        self.assertEqual(self.submit(), 'duplicate')
        self.assertEqual(self.count('positions'), 1)

    def test_log_failure_rolls_back_key_and_position(self):
        self.fail_log = True
        self.assertIs(self.submit(), False)
        self.assertEqual(self.count('positions'), 0)
        self.assertEqual(self.count('position_submission_dedup'), 0)
        self.fail_log = False
        self.assertIs(self.submit(), True)

    def test_changed_payload_does_not_masquerade_as_success(self):
        self.assertIs(self.submit(), True)
        self.assertEqual(self.submit(qty=8), 'conflict')
        self.assertEqual(self.db.execute('SELECT SUM(shares) FROM positions').fetchone()[0], 4)

    def test_user_and_new_submission_are_independent(self):
        self.assertIs(self.submit(), True)
        self.assertIs(self.submit(user='u2'), True)
        self.assertIs(self.submit(key='another_request_123456'), True)
        self.assertEqual(self.count('positions'), 3)


class ResultTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1].joinpath('main.py').read_text(encoding='utf-8')
        start = source.index('    def trade_result_response(')
        end = source.index('\n    if request.method == "POST":', start)
        scope = dict(html=html, respond_page=lambda title, body, nav: body)
        exec(compile(textwrap.dedent(source[start:end]), '<actual-trade-result>', 'exec'), scope)
        self.render = scope['trade_result_response']

    def test_sale_success_without_decorative_symbol(self):
        self.assertIn('p2-trade-result success', self.render('已賣出 4 股', succeeded=True))

    def test_failed_sale_is_explicit_error(self):
        self.assertIn('p2-trade-result error', self.render('賣出失敗', succeeded=False))

    def test_buy_success_uses_actual_response_contract(self):
        result = self.render('✅ 已新增 8996。')
        self.assertIn('p2-trade-result success', result)
        self.assertNotIn('class="msg"', result)

    def test_uncertain_commit_does_not_claim_failure(self):
        result = self.render('資料可能已入帳', pending=True)
        self.assertIn('p2-trade-result waiting', result)
        self.assertIn('結果待確認', result)
        self.assertNotIn('操作未完成', result)


if __name__ == '__main__': unittest.main()
