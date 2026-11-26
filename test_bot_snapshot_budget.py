import base64
from datetime import datetime, timezone, timedelta, date
import json
from threading import Event, Thread
import unittest
from unittest.mock import MagicMock
import zlib
from bot_snapshot_budget import Meter, SQLReader, VersionedSnapshots, BackgroundGate, Deferred, decode_packed

KEY='short_term:fixture'
class Reader:
    def __init__(self, now):
        self.version=(now.date(),now,'1');self.calls=[];self.payload={'rows':[{'code':'2330','chart':[100]*100}]};self.meta={'schema_version':15};self.race=False
    def metadata(self,key):self.calls.append(('meta',key));return self.version
    def body(self,key,version,projection,limit):
        self.calls.append(('body',key,projection));return None if self.race else (json.dumps({'payload':self.payload,'source_meta':self.meta}),)

class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,27,tzinfo=timezone.utc);self.r=Reader(self.now);self.safe=True
        self.s=VersionedSnapshots(self.r,lambda **kw:self.safe,lambda *a:True,short_key=KEY,now=lambda:self.now)
    def test_lazy_metadata(self):
        self.s.metadata(KEY);self.assertEqual(self.r.calls,[('meta',KEY)])
    def test_version_reuse(self):
        a=self.s.load(KEY);a['payload']['rows'][0]['chart'][0]=0
        b=self.s.load(KEY);self.assertEqual(b['payload']['rows'][0]['chart'][0],100)
        self.assertEqual(sum(c[0]=='body' for c in self.r.calls),1)
        self.r.version=(*self.r.version[:2],'2');self.s.load(KEY);self.assertEqual(sum(c[0]=='body' for c in self.r.calls),2)
    def test_race_no_stale(self):
        self.s.load(KEY);self.r.version=(*self.r.version[:2],'2');self.r.race=True
        with self.assertRaisesRegex(Deferred,'changed'):self.s.load(KEY)
        self.assertEqual(self.s.bytes,0)
    def test_expired_future(self):
        self.s.load(KEY);self.now+=timedelta(days=2);self.assertIsNone(self.s.load(KEY));self.now-=timedelta(days=3);self.assertIsNone(self.s.load(KEY));self.assertEqual(self.s.bytes,0)
    def test_scope_and_projection(self):
        for k,p in [('homepage_quote_map:user','full'),('valuation','cycle'),(KEY,'custom')]:
            with self.assertRaises(ValueError):self.s.load(k,projection=p)
        self.assertFalse(self.r.calls);self.s.load(KEY,projection='cycle');self.assertEqual(self.r.calls[-1],('body',KEY,'cycle'))
    def test_safe_and_policy(self):
        self.safe=False
        with self.assertRaises(Deferred):self.s.load(KEY)
        self.assertFalse(self.r.calls);self.safe=True;self.s.verify=lambda *a:False
        with self.assertRaisesRegex(Deferred,'policy'):self.s.load(KEY)
    def test_bounds(self):
        self.s.max_bytes=10
        with self.assertRaisesRegex(Deferred,'oversized'):self.s.load(KEY)
        self.assertEqual(self.s.bytes,0);self.s.max_bytes=2000;self.s.max_items=1;self.s.load(KEY);self.s.load('valuation');self.assertEqual(len(self.s.cache),1)
    def test_compressed(self):
        packed=base64.b64encode(zlib.compress(b'{"rows":[]}')).decode();self.r.payload={'packed':packed}
        self.assertEqual(self.s.load(KEY+':compressed-v1')['payload'],{'rows':[]})
        for value in ('bad!',base64.b64encode(zlib.compress(b' '*2000)).decode(),base64.b64encode(zlib.compress(b'{}')+b'extra').decode()):
            with self.assertRaises(Deferred):decode_packed(value,100)
    def test_metadata_and_body_sql(self):
        conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value;cur.fetchone.side_effect=[self.r.version,('{"payload":{},"source_meta":{}}',)];release=MagicMock();meter=Meter();sql=SQLReader(lambda:conn,release,meter)
        v=sql.metadata(KEY);sql.body(KEY,v,'cycle',1000)
        queries=[a.args[0] for a in cur.execute.call_args_list];self.assertIn('SET TRANSACTION READ ONLY',queries);self.assertIn('xmin::text',queries[-1]);self.assertIn('octet_length(body)',queries[-1]);self.assertIn("payload->'cycle'",queries[-1]);self.assertEqual(cur.execute.call_args.args[1],(1000,KEY,*v));self.assertEqual(release.call_count,2);self.assertEqual(meter.report()['supabase:body:calls'],1)
    def test_sql_failure(self):
        conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value;cur.execute.side_effect=RuntimeError('db');meter=Meter();release=MagicMock()
        with self.assertRaises(RuntimeError):SQLReader(lambda:conn,release,meter).metadata(KEY)
        conn.rollback.assert_called_once();release.assert_called_once();self.assertEqual(meter.report()['supabase:metadata:errors'],1)
    def test_meter_scope(self):
        m=Meter();m.record('fugle','request',sent=10,received=120);m.record('fugle','request',ok=False);self.assertEqual(m.report()['fugle:request:received_payload_bytes'],120);self.assertEqual(m.report()['fugle:request:errors'],1)
        with self.assertRaises(ValueError):m.record('secret-url','request')

class GateTests(unittest.TestCase):
    def setUp(self):
        self.t=0;self.calls=[];self.safe=True
        self.g=BackgroundGate({'review':(5,160,lambda:self.calls.append('review')),'daily':(1,200,lambda:self.calls.append('daily'))},lambda **kw:self.safe,clock=lambda:self.t)
    def test_priority_and_coalescing(self):
        self.assertTrue(self.g.submit('review'));self.assertFalse(self.g.submit('review'));self.g.submit('daily');self.assertEqual(self.g.tick(),'completed');self.assertEqual(self.g.tick(),'completed');self.assertEqual(self.calls,['daily','review']);self.assertEqual(self.g.tick(),'idle')
    def test_age(self):
        self.g.submit('review');self.t=301;self.g.submit('daily');self.g.tick();self.assertEqual(self.calls,['review'])
    def test_defer(self):
        self.g.submit('daily');self.safe=False;self.assertEqual(self.g.tick(),'deferred');self.assertEqual(self.calls,[]);self.safe=True;self.assertEqual(self.g.tick(),'completed')
    def test_busy(self):
        start=Event();end=Event()
        def work():start.set();end.wait(2)
        self.g.handlers['review']=(5,160,work);self.g.submit('review');t=Thread(target=self.g.tick);t.start()
        try:self.assertTrue(start.wait(1));self.assertFalse(self.g.submit('review'));self.g.submit('daily');self.assertEqual(self.g.tick(),'busy')
        finally:end.set();t.join(3)
        self.assertEqual(self.g.tick(),'completed')
    def test_error_release(self):
        def fail():raise RuntimeError('failed')
        self.g.handlers['review']=(5,160,fail);self.g.submit('review')
        with self.assertRaises(RuntimeError):self.g.tick()
        self.g.submit('daily');self.assertEqual(self.g.tick(),'completed')
    def test_callback_deferred(self):
        def fail():raise Deferred('retry')
        self.g.handlers['review']=(5,160,fail);self.g.submit('review');self.assertEqual(self.g.tick(),'deferred');self.assertFalse(self.g.submit('review'))

class PostgresSnapshotTests(unittest.TestCase):
    def test_real_sql_version_projection_and_size_cap(self):
        import os
        url=os.environ.get('V3_TEST_DATABASE_URL')
        if not url:self.skipTest('isolated PostgreSQL unavailable')
        # The only permitted target is the CI loopback service.
        from urllib.parse import urlsplit
        target=urlsplit(url)
        if target.hostname not in ('localhost','127.0.0.1','::1') or target.path!='/v3test':self.fail('refuse non-test database')
        import psycopg2
        conn=psycopg2.connect(url);self.addCleanup(conn.close)
        with conn.cursor() as cur:
            cur.execute('CREATE TEMP TABLE shared_data_snapshots(snapshot_key text PRIMARY KEY, data_date date, computed_at timestamptz, payload jsonb, source_meta jsonb)')
            body=json.dumps({'rows':['x'*2000],'cycle':{'queue':['2330']},'selection_version':15})
            cur.execute('INSERT INTO shared_data_snapshots VALUES(%s,CURRENT_DATE,NOW(),%s::jsonb,%s::jsonb)',(KEY,body,'{}'))
        conn.commit()
        reader=SQLReader(lambda:conn,lambda c:None,Meter());version=reader.metadata(KEY)
        self.assertEqual(reader.body(KEY,version,'full',100),(None,))
        small=reader.body(KEY,version,'cycle',1000)
        self.assertEqual(json.loads(small[0])['payload']['cycle']['queue'],['2330'])
        with conn.cursor() as cur:cur.execute("UPDATE shared_data_snapshots SET payload=payload || '{\"changed\":true}'::jsonb WHERE snapshot_key=%s",(KEY,))
        conn.commit()
        self.assertIsNone(reader.body(KEY,version,'full',10000))
        new=reader.metadata(KEY);self.assertNotEqual(new,version)
        self.assertTrue(json.loads(reader.body(KEY,new,'full',10000)[0])['payload']['changed'])

if __name__=='__main__':unittest.main()
