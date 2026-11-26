import unittest
from datetime import datetime,timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
from contextlib import closing
from v3_fund import Ledger,checkpoint,encode,valuation,TZ,D
from v3_management import costs,oddlot_estimate,corporate_action,dashboard,research_tick

class Management(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.l=Ledger(Path(self.tmp.name)/'test.db');self.l.initialize()
        self.now=datetime(2026,11,26,10,15,tzinfo=TZ)
        self.order=dict(code='2330',side='buy',shares=25,limit=101,submitted_at=(self.now-timedelta(seconds=3)).isoformat(),expires_at=(self.now+timedelta(seconds=20)).isoformat())
        self.payload=dict(symbol='2330',type='ODDLOT',date='2026-11-26',data=[dict(serial=1,time=int(self.now.timestamp()*1e6),size=300,price=100)])
    def ready(self):
        s=checkpoint();s.update(ready=True,positions={'2330':dict(shares=1000,basis='100000',sleeve='core')},cash='9900000',history_complete=True)
        with closing(self.l.connect()) as db,db:db.execute('UPDATE state SET payload=? WHERE id=1',(encode(s),))
    def action(self,kind='cash_entitlement',**kw):return dict(dict(kind=kind,code='2330',event_id='e1',evidence_ref='fixture',effective_date='2026-11-26',entitled_shares=1000,per_share=2,pay_date='2026-11-27'),**kw)
    def test_costs(self):
        self.assertEqual(costs(1000,'buy',session='oddlot',minimum=1,discount='.6')['fee'],'1');self.assertEqual(costs(100000,'sell')['tax'],'300');self.assertEqual(costs(100000,'sell',kind='equity_etf')['tax'],'100')
    def test_cost_bad(self):
        with self.assertRaises(ValueError):costs(100,'sell',kind='unknown')
    def test_odd_estimate(self):
        r=oddlot_estimate(self.order,self.payload,self.now);self.assertEqual(r['gross'],'2500.00');self.assertFalse(r['actual_fill_verified'])
    def test_odd_equal(self):
        self.payload['data'][0]['price']=101;self.assertEqual(oddlot_estimate(self.order,self.payload,self.now)['status'],'unfilled')
    def test_odd_before(self):
        self.payload['data'][0]['time']-=5_000_000;self.assertEqual(oddlot_estimate(self.order,self.payload,self.now)['status'],'unfilled')
    def test_odd_depth(self):
        self.payload['data'][0]['size']=249;self.assertEqual(oddlot_estimate(self.order,self.payload,self.now)['status'],'unfilled')
    def test_odd_wrong_type(self):
        self.payload['type']='EQUITY'
        with self.assertRaises(ValueError):oddlot_estimate(self.order,self.payload,self.now)
    def test_odd_duplicate(self):
        self.payload['data']*=2
        with self.assertRaises(ValueError):oddlot_estimate(self.order,self.payload,self.now)
    def test_corporate_locked(self):
        with self.assertRaises(ValueError):corporate_action(self.l,self.action(),self.now)
    def test_dividend_and_payment(self):
        self.ready();a=self.action();r=corporate_action(self.l,a,self.now);self.assertEqual(corporate_action(self.l,a,self.now),r);s=self.l.read();self.assertEqual(s['receivables']['e1']['amount'],'2000.00');self.assertEqual(s['cash'],'9900000')
        q={'2330':dict(code='2330',source='Fugle shared snapshot',price=98,trade_at=self.now.isoformat(),observed_at=self.now.isoformat(),status='fresh_intraday')};self.assertEqual(valuation(s,q,self.now)['equity'],D('10000000'))
        p=self.action('cash_payment',event_id='e2',claim_id='e1')
        with self.assertRaises(ValueError):corporate_action(self.l,p,self.now+timedelta(seconds=1))
        corporate_action(self.l,p,self.now+timedelta(days=1));s=self.l.read();self.assertEqual(s['cash'],'9902000.00');self.assertEqual(s['receivables'],{})
    def test_split_basis(self):
        self.ready();corporate_action(self.l,self.action('share_change',ratio=2),self.now);self.assertEqual(self.l.read()['positions']['2330'],dict(shares=2000,basis='100000',sleeve='core'))
    def test_fraction_block(self):
        self.ready()
        with self.assertRaises(ValueError):corporate_action(self.l,self.action('share_change',ratio='1.0001'),self.now)
    def test_wrong_entitlement(self):
        self.ready()
        with self.assertRaises(ValueError):corporate_action(self.l,self.action(entitled_shares=999),self.now)
    def test_dashboard_locked(self):
        d=dashboard(self.l);self.assertFalse(d['reconciled']);self.assertEqual(d['marks'],[]);self.assertEqual(d['capital'],'10000000')
    def test_schedule_locked(self):
        def no():raise AssertionError('should not call')
        self.assertEqual(research_tick(self.l,now=self.now,is_trading_day=lambda d:True,research_reader=no,quote_reader=no)['status'],'history_unreconciled')
    def test_schedule_proposal_only(self):
        self.ready();s=self.l.read();q={'2330':dict(code='2330',source='Fugle shared snapshot',price=100,trade_at=self.now.isoformat(),observed_at=self.now.isoformat(),status='fresh_intraday')};r=research_tick(self.l,now=self.now,is_trading_day=lambda d:True,research_reader=lambda:[dict(code='2330',conviction='.2',sleeve='core')],quote_reader=lambda codes:q);self.assertFalse(r['execution_authorized']);self.assertEqual(self.l.read(),s)
    def test_schedule_closed(self):
        self.assertEqual(research_tick(self.l,now=self.now,is_trading_day=lambda d:False,research_reader=None,quote_reader=None)['status'],'outside_schedule')
    def test_persisted_oddlot_fill(self):
        from v3_management import submit_oddlot
        self.ready(); submitted=self.now-timedelta(seconds=3)
        raw=dict(symbol='2330',date='2026-11-26',type='ODDLOT',market='TSE',lastUpdated=int(submitted.timestamp()*1e6),bids=[dict(price=99,size=500)],asks=[dict(price=100,size=500)])
        ticker=dict(symbol='2330',date='2026-11-26',market='TSE',securityType='01',securityStatus='NORMAL',tradingCurrency='TWD',boardLot=1000,isDisposition=False,matchingInterval=0,limitDownPrice=90,limitUpPrice=110)
        args=dict(event_id='odd1',revision=0,code='2330',side='buy',shares=25,sleeve='core',listed={'2330'},is_trading_day=lambda d:True)
        quotes={'2330':dict(code='2330',source='Fugle shared snapshot',price=100,trade_at=self.now.isoformat(),observed_at=self.now.isoformat(),status='fresh_intraday')}
        fill=dict(args,quotes=quotes,now=self.now,oddlot_payload=self.payload,fee_policy={'minimum':'1'})
        with self.assertRaisesRegex(ValueError,'prior paper order'):self.l.fill(**fill)
        submit_oddlot(self.l,**args,limit=101,now=submitted,expires_at=self.now+timedelta(seconds=20),book_payload=raw,ticker_payload=ticker,observed_at=submitted)
        r=self.l.fill(**fill);self.assertEqual(r['shares'],25);self.assertEqual(r['fee'],'4');self.assertEqual(self.l.fill(**fill),r);self.assertFalse(r['actual_fill_verified']);self.assertEqual(self.l.read()['positions']['2330']['shares'],1025)
    def test_dashboard_route(self):
        try:from flask import Flask
        except ImportError:self.skipTest('Flask unavailable')
        from v3_management import register_management
        app=Flask(__name__);register_management(app,self.l,'x'*40);c=app.test_client();self.assertEqual(c.get('/api/v3/fund/performance').status_code,401);r=c.get('/api/v3/fund/performance',headers={'Authorization':'Bearer '+'x'*40});self.assertEqual(r.status_code,200);self.assertFalse(r.json['reconciled']);self.assertEqual(r.headers['Cache-Control'],'no-store')
