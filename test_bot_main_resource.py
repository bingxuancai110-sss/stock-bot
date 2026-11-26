"""Compile the integration copy and run the actual host functions without startup."""
import ast
from datetime import date, datetime, timezone, timedelta
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import MagicMock
from prepare_bot_resource_main import patch_main
from bot_resource_host import ResourceHost
from bot_snapshot_budget import Deferred


class MainContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=Path(os.environ.get('BOT_RESOURCE_TEST_SOURCE',str(Path(__file__).with_name('main.py'))))
        cls.original=path.read_text(encoding='utf-8')
        cls.patched=patch_main(cls.original)
        cls.old=ast.parse(cls.original);cls.new=ast.parse(cls.patched)

    def extract(self,tree,names,ns):
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
        self.assertEqual({n.name for n in nodes},set(names))
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<host-functions>','exec'),ns)
        return ns

    def test_full_compile_and_repeat_rejected(self):
        compile(self.patched,'<integration-copy>','exec')
        with self.assertRaises(ValueError):patch_main(self.patched)
        with self.assertRaises(ValueError):patch_main('def main(): pass')

    def test_default_off_review_parity(self):
        def run(tree):
            fetch=MagicMock(side_effect=lambda cs,**kw:{c:{'close':100} for c in cs})
            safe=MagicMock(return_value=True)
            ns=self.extract(tree,['_review_price_map'],dict(get_realtime_stocks_bulk=fetch,_background_work_memory_safe=safe))
            return ns['_review_price_map'](['2330','2382','2330'],'6mo'),fetch.call_args_list,safe.call_args_list
        self.assertEqual(run(self.old),run(self.new))

    def test_enabled_history_delegation_only_after_close(self):
        day=date(2026,11,27);host=MagicMock();host.review.return_value={'2330':{'close':100}}
        ns=self.extract(self.new,['_review_price_map'],dict(_BOT_RESOURCE_HISTORY_TEST=True,_taiwan_post_close=lambda:True,
            taiwan_today=lambda:day,is_twse_trading_day=lambda d:True,get_market_map=lambda:{'2330':'上市'},
            _get_bot_resource_host=lambda:host,get_realtime_stocks_bulk=MagicMock(),_background_work_memory_safe=lambda **k:True))
        self.assertEqual(ns['_review_price_map'](['2330'],'6mo'),host.review.return_value)
        host.review.assert_called_once_with(['2330'],'6mo',day=day,markets={'2330':'.TW'})
        ns['get_realtime_stocks_bulk'].assert_not_called();ns['_taiwan_post_close']=lambda:False
        ns['get_realtime_stocks_bulk'].return_value={'2330':{'close':99}}
        self.assertEqual(ns['_review_price_map'](['2330'],'6mo'),{'2330':{'close':99}})

    def test_enabled_public_snapshot_paths(self):
        host=MagicMock();value={'data_date':date(2026,11,27),'payload':{'a':1}}
        host.snapshots.load.return_value=value
        ns=self.extract(self.new,['_save_shared_data_snapshot','_load_shared_data_snapshot','_load_shared_data_snapshots_bulk'],
            dict(_BOT_RESOURCE_TEST_MODE=True,_BOT_RESOURCE_PUBLIC={'valuation','monthly_revenue'},_get_bot_resource_host=lambda:host,
                 _SHARED_SNAPSHOT_MAX_AGE={'valuation':123},_SHARED_SNAPSHOT_MEMORY_CACHE={'valuation':{'old':True}},
                 _SHARED_SNAPSHOT_MEMORY_LOCK=threading.RLock()))
        self.assertTrue(ns['_save_shared_data_snapshot']('valuation',{'a':1},value['data_date'],{'source':'fixture'}))
        host.writer.save.assert_called_once_with('valuation',{'a':1},value['data_date'],{'source':'fixture'},replay=False)
        self.assertNotIn('valuation',ns['_SHARED_SNAPSHOT_MEMORY_CACHE']);self.assertEqual(ns['_load_shared_data_snapshot']('valuation'),value)
        host.snapshots.load.assert_called_with('valuation',max_age=123)
        self.assertEqual(ns['_load_shared_data_snapshots_bulk'](['valuation','monthly_revenue','valuation']),{'valuation':value,'monthly_revenue':value})
        host.snapshots.load.side_effect=Deferred('fixture');self.assertIsNone(ns['_load_shared_data_snapshot']('valuation'))
        host.writer.save.side_effect=RuntimeError('fixture');self.assertFalse(ns['_save_shared_data_snapshot']('valuation',{'a':1}))

    def test_disabled_and_private_write_parity(self):
        def run(tree,enabled,key):
            conn=MagicMock();release=MagicMock();host=MagicMock()
            ns=self.extract(tree,['_save_shared_data_snapshot'],dict(_BOT_RESOURCE_TEST_MODE=enabled,_BOT_RESOURCE_PUBLIC={'valuation'},
                _get_bot_resource_host=host,get_db_connection=lambda:conn,release_db_connection=release,json=json,datetime=datetime,timezone=timezone,time=time,
                _SHARED_SNAPSHOT_MEMORY_CACHE={},_SHARED_SNAPSHOT_MEMORY_LOCK=threading.RLock()))
            value=ns['_save_shared_data_snapshot'](key,{'a':1},date(2026,11,27),{'source':'fixture'})
            host.assert_not_called();conn.commit.assert_called_once();release.assert_called_once_with(conn)
            return value,conn.cursor.return_value.execute.call_args
        self.assertEqual(run(self.old,False,'valuation'),run(self.new,False,'valuation'))
        self.assertEqual(run(self.old,False,'private:test'),run(self.new,True,'private:test'))

    def test_background_existing_executor_and_gate(self):
        def run(tree,enabled,name):
            executor=MagicMock();host=MagicMock();host.optional_job.side_effect=lambda n,fn:fn();fn=MagicMock(return_value='ok')
            ns=self.extract(tree,['run_in_background'],dict(_BOT_RESOURCE_TEST_MODE=enabled,_get_bot_resource_host=lambda:host,
                has_request_context=lambda:False,log_trigger_source=MagicMock(),threading=threading,time=time,
                _BACKGROUND_JOB_SUBMISSION_LOCK=threading.RLock(),_BACKGROUND_JOB_QUEUED=set(),_job_mark_start=lambda n:True,
                _job_mark_done=MagicMock(),_BACKGROUND_JOB_EXECUTOR=executor,_DAILY_SNAPSHOT_DOWNLOAD_CONTEXT=threading.local()))
            result=ns['run_in_background'](name,fn);executor.submit.assert_called_once();executor.submit.call_args.args[0]()
            fn.assert_called_once();self.assertEqual(ns['_BACKGROUND_JOB_QUEUED'],set());self.assertEqual(ns['_job_mark_done'].call_args.args[:2],(name,'ok'))
            return result,host.optional_job.call_count
        self.assertEqual(run(self.old,False,'預熱快取'),run(self.new,False,'預熱快取'))
        self.assertEqual(run(self.new,True,'預熱快取')[1],1);self.assertEqual(run(self.new,True,'每日快照')[1],0)

    def test_adjusted_and_split_return_formula_parity(self):
        import math
        old=self.extract(self.old,['_pick_return_split_safe'],dict(datetime=datetime,date=date,math=math))['_pick_return_split_safe']
        new=self.extract(self.new,['_pick_return_split_safe'],dict(datetime=datetime,date=date,math=math))['_pick_return_split_safe']
        day=date(2026,11,27);days=[day-timedelta(days=180),day-timedelta(days=1),day]
        for prices in ([100,101,105],[100,5,6]):
            row=dict(code='2330',close=prices[-1],close_dates=days,closes=prices,adj_closes=prices,close_is_final=True,unneeded={'big':'x'*100})
            h=ResourceHost(MagicMock(),MagicMock(),lambda **k:True,lambda cs,**kw:{'2330':row})
            compact=h.review(['2330'],'6mo',day=day,markets={'2330':'.TW'})['2330']
            self.assertEqual(old(row,days[0],100),new(compact,days[0],100))

if __name__=='__main__':unittest.main()
