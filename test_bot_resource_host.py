import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
import unittest
from bot_resource_host import ResourceHost, SnapshotWriter, MeteredRequests, provider, dumps
from bot_snapshot_budget import Meter, Deferred

class WriterTests(unittest.TestCase):
    def setUp(self):
        self.conn=MagicMock();self.cur=self.conn.cursor.return_value.__enter__.return_value
        self.m=Meter();self.release=MagicMock();self.w=SnapshotWriter(lambda:self.conn,self.release,self.m)
        self.day=date(2026,11,27);self.t=datetime(2026,11,27,tzinfo=timezone.utc)
        self.v=(self.day,self.t,'1');self.v2=(self.day,self.t+timedelta(seconds=1),'2');self.p={'a':[100]*5000}
    def save(self,**kw):return self.w.save('valuation',self.p,self.day,{'source':'fixture'},**kw)
    def test_refresh_and_replay(self):
        self.cur.fetchone.side_effect=[None,self.v,self.v,self.v2,self.v2]
        self.assertEqual(self.save()['kind'],'body_written');size=self.m.report()['supabase:write:sent_payload_bytes']
        self.assertEqual(self.save()['kind'],'metadata_refresh');self.assertEqual(self.save(replay=True)['kind'],'replay_skipped')
        self.assertEqual(self.m.report()['supabase:write:sent_payload_bytes'],size);self.assertEqual(self.release.call_count,3)
    def test_external_change_forces_full_write(self):
        self.cur.fetchone.side_effect=[None,self.v,self.v2,self.v2]
        self.save();self.assertEqual(self.save()['kind'],'body_written')
    def test_unknown_replay_rejected(self):
        self.cur.fetchone.return_value=self.v
        with self.assertRaises(Deferred):self.save(replay=True)
        self.conn.rollback.assert_called_once();self.assertNotIn('valuation',self.w.last)
    def test_private_scope(self):
        with self.assertRaises(ValueError):self.w.save('fugle_private_quotes:owner',self.p)
        self.conn.cursor.assert_not_called()
    def test_failed_commit_no_cache(self):
        self.cur.fetchone.side_effect=[None,self.v];self.conn.commit.side_effect=RuntimeError('db')
        with self.assertRaises(RuntimeError):self.save()
        self.assertEqual(self.w.last,{});self.conn.rollback.assert_called_once()
    def test_repeated_body_stress(self):
        n=100; self.cur.fetchone.side_effect=[None,self.v]+[x for _ in range(n-1) for x in (self.v,self.v)]
        for _ in range(n):self.save()
        sql=[a.args[0] for a in self.cur.execute.call_args_list]
        self.assertEqual(sum('INSERT INTO' in s for s in sql),1);self.assertEqual(sum(s.startswith('UPDATE ') for s in sql),99)
        self.assertEqual(self.m.report()['supabase:write:sent_payload_bytes'],len(dumps(self.p).encode())+len(dumps({'source':'fixture'}).encode()))

class MeterTests(unittest.TestCase):
    def test_domains(self):
        for url,expected in [('https://query1.finance.yahoo.com/x','yahoo'),('https://api.fugle.tw/a','fugle'),('https://mis.twse.com.tw','twse'),('https://www.tpex.org.tw','tpex'),('https://x.supabase.co','supabase'),('https://fugle.tw.attacker.example','other')]:self.assertEqual(provider(url),expected)
    def test_requests_parity(self):
        mod=MagicMock();m=Meter();proxy=MeteredRequests(mod,m);r=SimpleNamespace(status_code=200,_content=b'abc',request=SimpleNamespace(body='abc'))
        mod.get.return_value=r;self.assertIs(proxy.get('https://api.fugle.tw',headers={'X-API-KEY':'secret'},timeout=3),r);mod.get.assert_called_once_with('https://api.fugle.tw',headers={'X-API-KEY':'secret'},timeout=3)
        self.assertEqual(m.report()['fugle:request:received_payload_bytes'],3);self.assertNotIn('secret',str(m.report()));self.assertIs(proxy.Session,mod.Session)
    def test_stream_and_failure(self):
        mod=MagicMock();m=Meter();proxy=MeteredRequests(mod,m);mod.post.return_value=SimpleNamespace(status_code=500,_content=False,request=SimpleNamespace(body=None));proxy.post('https://www.twse.com.tw',stream=True)
        mod.request.side_effect=TimeoutError('private url');
        with self.assertRaises(TimeoutError):proxy.request('GET','https://query1.finance.yahoo.com')
        self.assertEqual(m.report()['twse:request:received_payload_bytes'],0);self.assertEqual(m.report()['yahoo:request:errors'],1);self.assertEqual(m.report()['twse:request:errors'],1)

class HostTests(unittest.TestCase):
    def test_no_init_io_and_gate(self):
        connect=MagicMock();fetch=MagicMock();safe=MagicMock(return_value=False);h=ResourceHost(connect,MagicMock(),safe,fetch)
        connect.assert_not_called();fetch.assert_not_called();fn=MagicMock(return_value='ok')
        with self.assertRaises(Deferred):h.optional_job('預熱快取',fn)
        fn.assert_not_called();self.assertIsNone(h.gate.active);self.assertEqual(h.optional_job('每日快照',fn),'ok')
        safe.return_value=True;self.assertEqual(h.optional_job('預熱快取',fn),'ok')
    def test_history_parity_and_reuse(self):
        day=date(2026,11,27);codes=['2330','2382'];calls=[]
        def fetch(cs,**kw):
            calls.append(cs);return {c:dict(code=c,close=100,close_dates=[day-timedelta(days=180),day],closes=[98,100],adj_closes=[99,100],close_is_final=True) for c in cs}
        h=ResourceHost(MagicMock(),MagicMock(),lambda **k:True,fetch)
        kw=dict(day=day,markets={c:'.TW' for c in codes});a=h.review(codes,'6mo',**kw);self.assertEqual(a,h.review(codes,'6mo',**kw));self.assertEqual(len(calls),1)
        self.assertEqual(a['2330']['adj_closes'],[99,100])
    def test_unverified_close_rejected(self):
        day=date(2026,11,27);h=ResourceHost(MagicMock(),MagicMock(),lambda **k:True,lambda cs,**kw:{c:{'close_is_final':False} for c in cs})
        with self.assertRaises(Exception):h.review(['2330'],'6mo',day=day,markets={'2330':'.TW'})

class PostgresWriterTests(unittest.TestCase):
    def test_real_db_identical_and_external_change(self):
        import os
        from urllib.parse import urlsplit
        url=os.environ.get('V3_TEST_DATABASE_URL')
        if not url:self.skipTest('isolated PostgreSQL unavailable')
        target=urlsplit(url)
        if target.hostname not in ('localhost','127.0.0.1','::1') or target.path!='/v3test':self.fail('refuse non-test database')
        import psycopg2
        conn=psycopg2.connect(url);self.addCleanup(conn.close)
        with conn.cursor() as cur:cur.execute('CREATE TEMP TABLE shared_data_snapshots(snapshot_key text PRIMARY KEY,data_date date,computed_at timestamptz,payload jsonb,source_meta jsonb)')
        conn.commit();w=SnapshotWriter(lambda:conn,lambda c:None,Meter());args=('valuation',{'a':1},date(2026,11,27),{'source':'fixture'})
        self.assertEqual(w.save(*args)['kind'],'body_written');self.assertEqual(w.save(*args)['kind'],'metadata_refresh');self.assertEqual(w.save(*args,replay=True)['kind'],'replay_skipped')
        with conn.cursor() as cur:cur.execute("UPDATE shared_data_snapshots SET payload='{\"a\":2}'::jsonb")
        conn.commit()
        with self.assertRaises(Deferred):w.save(*args,replay=True)
        self.assertEqual(w.save(*args)['kind'],'body_written')
        with conn.cursor() as cur:cur.execute('SELECT payload,source_meta FROM shared_data_snapshots');self.assertEqual(cur.fetchone(),({'a':1},{'source':'fixture'}))

if __name__=='__main__':unittest.main()
