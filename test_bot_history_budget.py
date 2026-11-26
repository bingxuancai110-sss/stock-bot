import unittest
from datetime import date,timedelta
from threading import Event,Thread
from bot_history_budget import HistoryBudget,Deferred


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.day=date(2026,11,26); self.t=0; self.calls=[]; self.safe=True
        self.codes=[str(2000+i) for i in range(12)]; self.markets={c:'.TW' for c in self.codes}
        self.b=HistoryBudget(self.fetch,lambda **k:self.safe,lambda *a:True,clock=lambda:self.t)
    def row(self,c):return dict(code=c,close=100,close_dates=[self.day-timedelta(days=1),self.day],closes=[99,100],adj_closes=[98,100],close_is_final=True,private='not retained')
    def fetch(self,codes,**kw):self.calls.append((codes,kw));return {c:self.row(c) for c in codes}
    def load(self,codes=None,**kw):return self.b.load(self.codes if codes is None else codes,**dict(dict(span='6mo',completed_day=self.day,markets=self.markets,source_revision='yahoo-v1'),**kw))
    def test_overlap(self):
        self.load(self.codes[:8]); r=self.load(self.codes[4:]); self.assertEqual(len(r),8);self.assertEqual(len(self.calls),3);self.assertEqual(self.b.stats()['hits'],4)
        self.assertTrue(all(len(c)<=4 and k['workers']==1 and k['skip_mis'] for c,k in self.calls))
    def test_exact_data_and_private_excluded(self):
        c=self.codes[0];r=self.load([c]); self.assertEqual(r[c]['adj_closes'],[98,100]);self.assertEqual(r[c]['close_dates'][-1],self.day);self.assertNotIn('private',r[c]);r[c]['closes'][0]=1;self.assertEqual(self.load([c])[c]['closes'][0],99)
    def test_scope_and_expiry(self):
        c=self.codes[0];self.load([c]);self.load([c],span='1y');self.load([c],source_revision='yahoo-v2');self.load([c],markets={c:'.TWO'});self.t=901;self.load([c]);self.assertEqual(len(self.calls),5)
    def test_next_day(self):
        c=self.codes[0];self.load([c]);self.day+=timedelta(days=1);self.load([c]);self.assertEqual(len(self.calls),2)
    def test_pressure(self):
        self.safe=False
        with self.assertRaises(Deferred):self.load()
        self.assertEqual(self.calls,[])
    def test_partial_failure_resume(self):
        def fetch(cs,**kw):
            r=self.fetch(cs,**kw)
            if cs[0]=='2004' and self.t<60:r.pop('2004')
            return r
        self.b.fetch=fetch
        with self.assertRaises(Deferred):self.load()
        with self.assertRaisesRegex(Deferred,'cooldown'):self.load()
        self.assertEqual(len(self.calls),2);self.t=60;self.load();self.assertEqual(self.calls[2][0][0],'2004')
    def test_final_and_coverage(self):
        self.b.fetch=lambda cs,**kw:{c:dict(self.row(c),close_is_final=False) for c in cs}
        with self.assertRaises(Deferred):self.load([self.codes[0]])
        self.assertEqual(self.b.stats()['cache_items'],0)
        self.t=60;self.b.fetch=self.fetch;self.b.verify=lambda *a:False
        with self.assertRaises(Deferred):self.load([self.codes[0]])
    def test_bounds_and_clear(self):
        self.b.max_items=2;self.load(self.codes[:4]);self.assertEqual(self.b.stats()['cache_items'],2);self.assertTrue(self.b.clear());self.assertEqual(self.b.stats()['cache_bytes'],0)
    def test_result_bound(self):
        self.b.max_bytes=200
        with self.assertRaises(Deferred):self.load(self.codes[:4])
        self.assertLessEqual(self.b.stats()['cache_bytes'],200)
    def test_concurrent_job(self):
        started=Event();release=Event();errors=[]
        def fetch(cs,**kw):started.set();release.wait(2);return self.fetch(cs,**kw)
        def run():
            try:self.load([self.codes[0]])
            except Exception as e:errors.append(e)
        self.b.fetch=fetch;t=Thread(target=run);t.start()
        try:
            self.assertTrue(started.wait(1))
            with self.assertRaisesRegex(Deferred,'active'):self.load([self.codes[0]])
            self.assertFalse(self.b.clear())
        finally:release.set();t.join(3)
        self.assertEqual(errors,[]);self.load([self.codes[0]]);self.assertEqual(len(self.calls),1)
    def test_invalid_input(self):
        for kw in ({'span':'1d'},{'markets':{}},{'source_revision':''}):
            with self.assertRaises(ValueError):self.load(**kw)
        self.assertEqual(self.calls,[])
if __name__=='__main__':unittest.main()
