from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
from contextlib import closing
import unittest
from v3_execution import book, estimate, reconcile_preview, fetch_book
from v3_fund import Ledger, checkpoint, encode, TZ, D
from v3_shared_market import Engine, DynamicMarket, sqlite_store
from v3_research_pool import build_pool

class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,26,10,15,tzinfo=TZ)
        self.stamp=int(self.now.timestamp()*1e6)
        self.raw=dict(symbol='2330',date='2026-11-26',type='EQUITY',market='TSE',isContinuous=True,lastUpdated=self.stamp,
                      bids=[dict(price=99,size=40),dict(price=98,size=40)],asks=[dict(price=100,size=10),dict(price=101,size=20)])
        self.ticker=dict(symbol='2330',date='2026-11-26',market='TSE',securityType='01',securityStatus='NORMAL',tradingCurrency='TWD',boardLot=1000,isDisposition=False,matchingInterval=0,limitDownPrice=90,limitUpPrice=110)
    def book(self,session='regular',**kw):
        return book(self.raw,self.ticker,code='2330',session=session,observed_at=self.now,now=kw.get('now',self.now))
    def plan(self,**kw):
        return estimate(self.book(),**dict(side='buy',shares=2000,limit=101,now=self.now,**kw))
    def test_depth_vwap(self):
        p=self.plan();self.assertEqual(D(p['gross']),201000);self.assertEqual(D(p['vwap']),D('100.5'));self.assertEqual(len(p['legs']),2)
    def test_insufficient_limit(self):
        p=estimate(self.book(),side='buy',shares=2000,limit=100,now=self.now);self.assertEqual(p['status'],'insufficient_depth');self.assertEqual(p['shares'],0)
    def test_sell_depth(self):
        p=estimate(self.book(),side='sell',shares=5000,limit=98,now=self.now);self.assertEqual(D(p['gross']),494000)
    def test_oddlot_units(self):
        self.raw['type']='ODDLOT';self.raw['asks']=[dict(price=100,size=500)]
        b=self.book('oddlot');self.assertEqual(b['asks'][0][1],500)
        p=estimate(b,side='buy',shares=50,limit=100,now=self.now);self.assertEqual(p['status'],'indicative_auction')
    def test_oddlot_early(self):
        self.now=self.now.replace(hour=9,minute=5);self.raw['type']='ODDLOT'
        with self.assertRaises(ValueError):self.book('oddlot')
    def test_stale(self):
        with self.assertRaises(ValueError):self.book(now=self.now+timedelta(seconds=6))
    def test_trial(self):
        self.raw['isTrial']=True
        with self.assertRaises(ValueError):self.book()
    def test_halt(self):
        self.ticker['securityStatus']='SUSPENDED'
        with self.assertRaises(ValueError):self.book()
    def test_market_order(self):
        self.raw['asks'][0]['price']=0
        with self.assertRaises(ValueError):self.book()
    def test_crossed(self):
        self.raw['bids'][0]['price']=100
        with self.assertRaises(ValueError):self.book()
    def test_bad_unit(self):
        self.ticker['boardLot']=100
        with self.assertRaises(ValueError):self.book()
    def test_wrong_session(self):
        with self.assertRaises(ValueError):self.book('oddlot')
    def test_limit_price(self):
        self.raw['asks']=[dict(price=110,size=100)]
        with self.assertRaises(ValueError):self.book()
    def test_reconcile_no_unlock(self):
        s=checkpoint();r=reconcile_preview(events=[],expected_cash=s['cash'],expected_positions={c:p['shares'] for c,p in s['positions'].items()},cutoff=self.now,evidence_ref='fixture')
        self.assertTrue(r['arithmetic_match']);self.assertFalse(r['ready']);self.assertFalse(r['history_complete'])
    def test_reconcile_trade_and_mismatch(self):
        s=checkpoint();pos={c:p['shares'] for c,p in s['positions'].items()};pos['2330']=3
        e=dict(event_id='e1',at=self.now.isoformat(),code='2330',shares=3,side='buy',price=100,fee=1,tax=0,cash_after='4005147.50',evidence_ref='fixture')
        kw=dict(events=[e],expected_cash='4005147.50',expected_positions=pos,cutoff=self.now,evidence_ref='fixture')
        self.assertTrue(reconcile_preview(**kw)['arithmetic_match']);self.assertFalse(reconcile_preview(**dict(kw,expected_cash=0))['arithmetic_match'])
        with self.assertRaises(ValueError):reconcile_preview(**dict(kw,events=[e,e]))
        with self.assertRaises(ValueError):reconcile_preview(**dict(kw,events=[dict(e,cash_after=0)]))
    def test_end_to_end(self):
        raw,ticker,now=self.raw,self.ticker,self.now
        class Response:
            status_code=200
            def __init__(self,data):self.data=data
            def json(self):return self.data
        class Session:
            calls=[]
            def get(self,url,**kw):
                self.calls.append((url,kw.get('params')));return Response(ticker if '/ticker/' in url else raw)
        def parse(c,p,n):return dict(code=c,price=100,previous_close=98,time=self.stamp,observed_at=self.stamp/1000,closed=False,type='EQUITY')
        with TemporaryDirectory() as tmp:
            st=sqlite_store(str(Path(tmp)/'market.db'));st.init_schema();session=Session()
            engine=Engine(st,session,'fake',now=lambda:now,is_trading_day=lambda d:True,parser=parse)
            research=build_pool(now=now,last_close=(now-timedelta(days=1)).date(),last_chips=(now-timedelta(days=1)).date(),intraday=True,listed=['2330'],holdings=[],short_version=15,blackhorse=dict(source_date='2026-11-25',computed_at=now.isoformat(),rows=[dict(code='2330',quote_date='2026-11-26',score=90)]))
            self.assertEqual(research['codes'],['2330']);engine.set_pool(listed=['2330'],holdings=[],candidates=research['codes'],as_of='2026-11-26');self.assertEqual(engine.fetch('v3','2330'),'updated')
            market=DynamicMarket(engine);data,status=market.read(['2330']);self.assertEqual(status,200)
            quotes={c:dict(q,code=c) for c,q in data['quotes'].items()}
            fetched=fetch_book(engine,'2330','regular');self.assertEqual(len(session.calls),3)
            with st.transaction() as state:self.assertEqual(len(state['calls']),3)
            ledger=Ledger(Path(tmp)/'fund.db');ledger.initialize();s=checkpoint();s.update(ready=True,history_complete=True,positions={},cash='10000000',fees='0')
            with closing(ledger.connect()) as db,db:db.execute('UPDATE state SET payload=? WHERE id=1',(encode(s),))
            kw=dict(event_id='test',revision=0,code='2330',side='buy',shares=2000,sleeve='core',quotes=quotes,now=now,listed={'2330'},is_trading_day=lambda d:True,limit=101,**{k:v for k,v in fetched.items() if k!='book'})
            r=ledger.fill(**kw);self.assertEqual(r['gross'],'201000.00');self.assertEqual(r['execution_model'],'visible_depth_estimate');self.assertEqual(ledger.fill(**kw),r)
            with self.assertRaisesRegex(ValueError,'consumed'):ledger.fill(**dict(kw,event_id='test2',revision=1,now=now+timedelta(seconds=1)))
            self.assertEqual(ledger.read()['version'],1)
            self.assertEqual(ledger.read()['positions']['2330']['shares'],2000)
            # All requests share the quota. No additional network call when exhausted.
            with st.transaction() as state:state['calls']=[[now.timestamp(),'v3']]*20
            with self.assertRaisesRegex(ValueError,'budget'):fetch_book(engine,'2330','oddlot')
            self.assertEqual(len(session.calls),3)
if __name__=='__main__':unittest.main()
