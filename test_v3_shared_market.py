from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from v3_shared_market import Engine, DynamicMarket, sqlite_store, TZ, TOTAL


class Response:
    def __init__(self, code, status=200, retry='60'):
        self.code, self.status_code = code, status
        self.headers={'Retry-After':retry}
    def json(self):
        return {'code':self.code}


class Session:
    def __init__(self):
        self.calls=[]
        self.status=200
        self.started,self.release=Event(),Event()
        self.block=False
    def get(self,url,**kwargs):
        code=url.split('/')[-1]
        self.calls.append(code)
        if self.block:
            self.started.set()
            if not self.release.wait(3):
                raise TimeoutError()
        return Response(code,self.status)


class SharedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'market.db')
        self.store=sqlite_store(self.path)
        self.store.init_schema()
        self.now=datetime(2026,11,26,10,15,tzinfo=TZ)
        self.session=Session()
        self.codes=[str(i) for i in range(2000,2100)]
        self.bot,self.v3=self.engine(),self.engine()
        self.pool()
    def engine(self):
        return Engine(sqlite_store(self.path),self.session,'fake',now=lambda:self.now,
            is_trading_day=lambda d:d.weekday()<5 and d.day!=30,parser=self.parse)
    def parse(self,code,payload,now):
        return dict(code=code,price=100,previous_close=98,time=now.timestamp()*1e6,
            observed_at=now.timestamp()*1000,closed=now.hour*60+now.minute>=810,
            type='INDEX' if code=='IX0001' else 'EQUITY')
    def pool(self, candidates=None, holdings=None):
        return self.v3.set_pool(listed=self.codes,holdings=holdings or self.codes[:2],
            candidates=self.codes if candidates is None else candidates,as_of=self.now.date().isoformat())
    def test_shared_cache(self):
        self.assertEqual(self.bot.fetch('bot','2000'),'updated')
        self.assertEqual(self.v3.fetch('v3','2000'),'cache_hit')
        self.assertEqual(len(self.session.calls),1)
        self.assertEqual(self.v3.read(['2000'])['2000']['price'],100)
    def test_budget_across_instances(self):
        for c in self.codes[:30]:
            self.assertEqual(self.bot.fetch('bot',c),'updated')
        self.assertEqual(self.bot.fetch('bot','2030'),'rate_limited')
        for c in self.codes[30:50]:
            self.assertEqual(self.v3.fetch('v3',c),'updated')
        self.assertEqual(self.v3.fetch('v3','2050'),'rate_limited')
        self.assertEqual(len(self.session.calls),TOTAL)
        self.now+=timedelta(seconds=60)
        self.assertEqual(self.v3.fetch('v3','2050'),'updated')
    def test_concurrent_budget(self):
        with ThreadPoolExecutor(max_workers=12) as ex:
            out=list(ex.map(lambda c:self.engine().fetch('v3',c),self.codes[:50]))
        self.assertEqual(out.count('updated'),20)
        self.assertEqual(len(self.session.calls),20)
    def test_duplicate_inflight(self):
        self.session.block=True
        with ThreadPoolExecutor(max_workers=2) as ex:
            f=ex.submit(self.bot.fetch,'bot','2000')
            try:
                self.assertTrue(self.session.started.wait(2))
                self.assertEqual(self.v3.fetch('v3','2000'),'in_flight')
            finally:
                self.session.release.set()
            self.assertEqual(f.result(),'updated')
        self.assertEqual(len(self.session.calls),1)
    def test_429_cooldown_global(self):
        self.session.status=429
        self.assertEqual(self.bot.fetch('bot','2000'),'provider_rate_limited')
        self.assertEqual(self.v3.fetch('v3','2001'),'rate_limited')
        self.assertEqual(len(self.session.calls),1)
        self.now+=timedelta(seconds=60)
        self.session.status=200
        self.assertEqual(self.v3.fetch('v3','2001'),'updated')
    def test_auth_cooldown(self):
        self.session.status=401
        self.assertEqual(self.bot.fetch('bot','2000'),'credential_error')
        self.assertEqual(self.v3.fetch('v3','2001'),'rate_limited')
    def test_invalid_symbol_and_unlisted(self):
        with self.assertRaises(ValueError):self.v3.fetch('v3','../bad')
        self.assertEqual(self.v3.fetch('v3','9999'),'not_allowed')
        self.assertNotIn('9999',self.pool(['9999','2003']))
        with self.assertRaises(ValueError):self.pool(holdings=['9999'])
        self.assertEqual(len(self.session.calls),0)
    def test_stale_listing(self):
        with self.assertRaises(ValueError):
            self.v3.set_pool(listed=self.codes,holdings=[],candidates=[],as_of='2026-11-25')
    def test_rotate(self):
        first=self.v3.collector('v3').refresh_pool()
        self.assertEqual(first['updated'],20)
        self.now+=timedelta(seconds=60)
        second=self.v3.collector('v3').refresh_pool()
        self.assertIn('2000',second['quotes'])
        self.assertIn('2030',second['quotes'])
        self.assertNotIn('2003',second['quotes'])
    def test_stale_and_day_reset(self):
        self.bot.fetch('bot','2000')
        self.now+=timedelta(seconds=181)
        self.assertEqual(self.v3.read(['2000']),{})
        self.now+=timedelta(days=1)
        self.assertEqual(self.v3.pool(),())
        self.assertEqual(self.v3.fetch('v3','2000'),'not_allowed')
    def test_final_close(self):
        self.now=self.now.replace(hour=13,minute=30)
        self.assertEqual(self.bot.fetch('bot','2000'),'updated')
        self.now=self.now.replace(hour=17)
        self.assertEqual(self.v3.read(['2000'])['2000']['closed'],True)
        self.assertEqual(self.v3.fetch('v3','2000'),'outside_session')
    def test_unconfirmed_close(self):
        self.now=self.now.replace(hour=13,minute=29)
        self.bot.fetch('bot','2000')
        self.now+=timedelta(minutes=1)
        self.assertEqual(self.v3.read(['2000']),{})
    def test_holiday(self):
        self.now+=timedelta(days=4)
        self.assertEqual(self.bot.fetch('bot','2000'),'outside_session')
        self.assertEqual(self.session.calls,[])
    def test_bad_provider_row(self):
        self.bot.parse=lambda c,p,n:dict(self.parse(c,p,n),price=float('nan'))
        self.assertEqual(self.bot.fetch('bot','2000'),'invalid_quote')
        self.assertEqual(self.v3.read(['2000']),{})
    def test_dynamic_read_scope(self):
        self.bot.fetch('bot','2000')
        self.bot.fetch('bot','2003')
        market=DynamicMarket(self.v3)
        self.pool(['2002'])
        self.assertNotIn('2003',market.codes)
        data,status=market.read(['2000','2003'])
        self.assertEqual(status,200)
        self.assertFalse(data['complete'])
        self.assertFalse(data['execution_authorized'])
        self.assertEqual(data['missing'],['2003'])
        self.assertEqual(len(self.session.calls),2)
    def test_expired_lease_not_reported_as_update(self):
        def delayed_parse(c,p,n):
            self.now+=timedelta(seconds=31)
            return self.parse(c,p,n)
        self.bot.parse=delayed_parse
        self.assertEqual(self.bot.fetch('bot','2000'),'lease_expired')
        self.assertEqual(self.v3.read(['2000']),{})
    def test_refresh_after_cache_ttl(self):
        self.bot.fetch('bot','2000')
        self.now+=timedelta(seconds=44)
        self.assertEqual(self.v3.fetch('v3','2000'),'cache_hit')
        self.now+=timedelta(seconds=1)
        self.assertEqual(self.v3.fetch('v3','2000'),'updated')
        self.assertEqual(len(self.session.calls),2)
    def test_db_failure_no_fetch(self):
        self.bot.store=sqlite_store(str(Path(self.tmp.name)/'missing.db'))
        with self.assertRaises(Exception):self.bot.fetch('bot','2000')
        self.assertEqual(self.session.calls,[])

if __name__=='__main__':unittest.main()
