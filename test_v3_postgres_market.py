"""CI-only PostgreSQL tests; never use a production database."""
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from v3_shared_market import StateStore, Engine, TZ
from test_v3_shared_market import Session


@unittest.skipUnless(os.environ.get('V3_TEST_DATABASE_URL'), 'isolated PostgreSQL not configured')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg2
        self.connect=lambda:psycopg2.connect(os.environ['V3_TEST_DATABASE_URL'])
        self.namespace='ci-'+uuid.uuid4().hex
        self.store=StateStore(self.connect,self.namespace,postgres=True)
        self.store.init_schema()
        self.addCleanup(self.cleanup)
        self.now=datetime(2026,11,26,10,15,tzinfo=TZ)
        self.session=Session()
        self.codes=[str(i) for i in range(2000,2060)]
        self.engine().set_pool(listed=self.codes,holdings=self.codes[:2],
            candidates=self.codes,as_of=self.now.date().isoformat())
    def cleanup(self):
        conn=self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM v3_market_state WHERE namespace=%s',(self.namespace,))
            conn.commit()
        finally:
            conn.close()
    def engine(self):
        return Engine(StateStore(self.connect,self.namespace,postgres=True),self.session,'fake',
            now=lambda:self.now,is_trading_day=lambda d:True,parser=self.parse)
    def parse(self,code,payload,now):
        return dict(code=code,price=100,previous_close=98,time=now.timestamp()*1e6,
            observed_at=now.timestamp()*1000,closed=False,type='EQUITY')
    def test_shared_postgres_budget(self):
        with ThreadPoolExecutor(max_workers=12) as ex:
            out=list(ex.map(lambda c:self.engine().fetch('v3',c),self.codes))
        self.assertEqual(out.count('updated'),20)
        self.assertEqual(len(self.session.calls),20)
    def test_shared_postgres_cache(self):
        self.assertEqual(self.engine().fetch('bot','2000'),'updated')
        self.assertEqual(self.engine().fetch('v3','2000'),'cache_hit')
        self.assertEqual(len(self.session.calls),1)


class ProviderContractTests(unittest.TestCase):
    def test_existing_fugle_parser(self):
        from fugle_background import quote_row
        now=datetime(2026,11,26,10,15,tzinfo=TZ)
        payload={'symbol':'2382','date':'2026-11-26','type':'EQUITY',
                 'lastTrade':{'price':100,'time':int(now.timestamp()*1e6)},
                 'referencePrice':98,'isClose':False}
        row=quote_row('2382',payload,now)
        self.assertEqual(row['code'],'2382')
        self.assertEqual(row['price'],100)
        self.assertEqual(row['previous_close'],98)
        self.assertIsNone(quote_row('2330',payload,now))
