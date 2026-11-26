from datetime import date, datetime, timedelta
import unittest
from v3_research_pool import build_pool, load_and_refresh, TZ


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,26,10,15,tzinfo=TZ)
        self.prev=date(2026,11,25)
        self.day=self.now.date().isoformat()
        self.ts=self.now.isoformat()
        self.active=['2382','2345','3017','2308','2330','2885']
        self.b=dict(source_date=self.prev,computed_at=self.ts,rows=[
            dict(code='2382',quote_date=self.day,score=90,rev=18,val=20,mom=26,streak_score=5,chip=21),
            dict(code='2345',quote_date=self.prev.isoformat(),score=85),
            dict(code='9999',quote_date=self.day,score=99)])
        self.r=dict(source_date=self.prev,scan_finished_at=self.ts,rows=[dict(code='2382',quote_date=self.day,breakout='季線新高',vol_ratio=2),dict(code='3017',quote_date=self.day,vol_ratio=1.5)])
        self.c=dict(data_date=self.prev.isoformat(),computed_at=self.ts,payload=dict(available=True,groups={'both_buy':[dict(code='2382',lots=100,hit_days=7)],'trust_buy':[dict(code='2382',lots=20)],'both_sell':[dict(code='2308',lots=-50)]}))
        self.s=dict(selection_version=15,intraday=True,date=self.day,generated_at=self.ts,all_rows=[self.short('3017','long')],short_rows=[self.short('2382','short'),self.short('2308','short','exit')])
    def short(self,code,side,action='entry'):
        return dict(code=code,direction=side,date=self.day,observed_at=self.ts,current_action=action,latest_event_date=self.day,provisional=True,stop=100,rr=2)
    def build(self,**kw):
        args=dict(now=self.now,last_close=self.prev,last_chips=self.prev,intraday=True,listed=self.active,holdings=['2330'],blackhorse=self.b,radar_live=self.r,chips=self.c,short_live=self.s,short_version=15)
        args.update(kw)
        return build_pool(**args)
    def item(self,result,code):
        return next(r for r in result['ranked'] if r['code']==code)
    def test_all_sources_and_factor_dedup(self):
        a=self.build();self.assertEqual(a['codes'][0],'2330');self.assertEqual(a['codes'][1],'2382')
        r=self.item(a,'2382');self.assertEqual(r['source_count'],4);self.assertTrue(r['conflict']);self.assertFalse(a['execution_authorized'])
        e=next(e for e in r['evidence'] if e['source']=='blackhorse_factors');self.assertEqual(e['score'],90);self.assertEqual(e['factors']['rev'],18)
    def test_unlisted_rejected_and_holdings_kept(self):
        self.assertNotIn('9999',self.build()['codes'])
        with self.assertRaises(ValueError):self.build(holdings=['9999'])
    def test_old_radar_falls_back(self):
        self.r['scan_finished_at']=(self.now-timedelta(minutes=11)).isoformat()
        a=self.build(radar_close=dict(computed_at=self.ts,rows=[dict(code='2885',quote_date=self.prev.isoformat())]))
        self.assertEqual(a['sources']['radar'],'prior_close');self.assertFalse(self.item(a,'2885')['evidence'][0]['intraday'])
    def test_radar_institution_date_not_quote_date(self):
        self.assertEqual(self.build()['sources']['radar'],'live')
        self.r['rows'][0]['quote_date']=self.prev.isoformat();self.r['rows'][1]['quote_date']=self.prev.isoformat()
        self.assertEqual(self.build()['sources']['radar'],'unavailable')
    def test_future_snapshot_rejected(self):
        self.b['computed_at']=(self.now+timedelta(seconds=1)).isoformat()
        self.assertEqual(self.build()['sources']['blackhorse_factors'],'unavailable')
    def test_unknown_and_nonfinite_scores(self):
        self.b['rows'][0]['score']=float('nan');self.b['rows'][1]['quote_date']=None
        self.assertFalse(any(e['source']=='blackhorse_factors' for e in self.item(self.build(),'2382')['evidence']))
    def test_unconfirmed_source_date(self):
        self.b['source_meta']={'source_date_confirmed':False}
        self.assertEqual(self.build()['sources']['blackhorse_factors'],'unavailable')
    def test_chips_stale_and_buy_sell_distinct(self):
        a=self.build();self.assertTrue(any(e['direction']=='bearish_research' for e in self.item(a,'2308')['evidence']))
        self.c['data_date']='2026-11-24';self.assertEqual(self.build()['sources']['chips'],'unavailable')
    def test_short_exit_not_entry(self):
        e=next(e for e in self.item(self.build(),'2308')['evidence'] if e['source']=='short_term')
        self.assertEqual((e['side'],e['action'],e['direction']),('short','exit','neutral_research'))
    def test_short_stale_row_and_version(self):
        self.s['all_rows'][0]['observed_at']=(self.now-timedelta(minutes=21)).isoformat()
        self.assertFalse(any(e['source']=='short_term' for e in self.item(self.build(),'3017')['evidence']))
        self.s['selection_version']=14;self.assertEqual(self.build()['sources']['short_term'],'unavailable')
    def test_historical_entry_not_new(self):
        self.s['short_rows'][0]['latest_event_date']=self.prev.isoformat()
        self.assertFalse(any(e['source']=='short_term' for e in self.item(self.build(),'2382')['evidence']))
    def test_cap(self):
        a=self.build(cap=2);self.assertEqual(a['codes'],['2330','2382'])
    def test_no_research(self):
        a=self.build(blackhorse=None,radar_live=None,chips=None,short_live=None)
        self.assertFalse(a['usable']);self.assertEqual(a['codes'],['2330'])
    def test_bad_reference_date(self):
        with self.assertRaises(ValueError):self.build(last_close=self.now.date())
    def test_prior_close_short_is_not_live(self):
        old=self.prev.isoformat(); row=dict(self.short('2885','long'),date=old,latest_event_date=old,provisional=False)
        snap=dict(selection_version=15,intraday=False,date=old,generated_at=self.ts,all_rows=[row],short_rows=[])
        a=self.build(short_live=None,short_close=snap)
        e=self.item(a,'2885')['evidence'][0];self.assertFalse(e['intraday']);self.assertEqual(e['data_date'],old)
    def test_adapter_saved_only_and_engine(self):
        from v3_shared_market import Engine, sqlite_store
        from tempfile import TemporaryDirectory
        from pathlib import Path
        class Store:
            def load(_,key,**kwargs):return {'payload':self.s} if key.endswith(':intraday') else None
        class Session:
            def get(*args,**kwargs):raise AssertionError('must not request quotes')
        with TemporaryDirectory() as tmp:
            st=sqlite_store(str(Path(tmp)/'db'));st.init_schema()
            engine=Engine(st,Session(),'test',now=lambda:self.now,is_trading_day=lambda d:True)
            def chips_reader(**kwargs):
                self.assertEqual(kwargs,{'allow_compute':False,'persist':False});return self.c
            kw=dict(now=self.now,listed=self.active,holdings=['2330'],last_close=self.prev,last_chips=self.prev,intraday=True,screener_reader=lambda k:self.b if k=='blackhorse' else None,live_radar_reader=lambda:self.r,chips_reader=chips_reader,short_store=Store(),short_key='short_term:test',short_version=15)
            a=load_and_refresh(engine,**kw);self.assertTrue(a['applied']);self.assertEqual(tuple(a['codes']),engine.pool())
            class Broken:
                def load(*a,**kw):raise RuntimeError('do not leak')
            def fail(*a,**kw):raise RuntimeError('do not leak')
            kw.update(screener_reader=fail,live_radar_reader=fail,chips_reader=fail,short_store=Broken())
            b=load_and_refresh(engine,**kw);self.assertFalse(b['applied']);self.assertEqual(b['errors']['blackhorse'],'RuntimeError');self.assertEqual(tuple(a['codes']),engine.pool())
            kw['listed']=[c for c in self.active if c!='2382']
            b=load_and_refresh(engine,**kw);self.assertFalse(b['applied']);self.assertNotIn('2382',engine.pool())

if __name__=='__main__':unittest.main()
