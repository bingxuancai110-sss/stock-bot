import unittest
from contextlib import closing
from datetime import datetime, timedelta, date
from tempfile import TemporaryDirectory
from pathlib import Path
from v3_fund import Ledger,checkpoint,encode,valuation,TZ,D
from v3_accounting import due_date,balances,settle,corporate_batch,pay_claim
from v3_release_host import attach_staging,cached_quotes

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.l=Ledger(Path(self.tmp.name)/'fund.db');self.l.initialize()
        self.now=datetime(2026,11,26,10,15,tzinfo=TZ)
        self.calendar=lambda d:d.weekday()<5 and d!=date(2026,11,27)
    def ready(self,positions=None,cash='10000000'):
        s=checkpoint();s.update(ready=True,history_complete=True,positions=positions or {},cash=cash,fees='0',accounting_mode='t2')
        with closing(self.l.connect()) as db,db:db.execute('UPDATE state SET payload=? WHERE id=1',(encode(s),))
    def q(self,price=100):
        return {'2330':dict(code='2330',source='Fugle shared snapshot',price=price,trade_at=self.now.isoformat(),observed_at=self.now.isoformat(),status='fresh_intraday',liquidity_verified=True,session='regular',halted=False,bid=price,ask=price,limit_down=90,limit_up=110,bid_shares=10000,ask_shares=10000)}
    def fill(self,**kw):
        return self.l.fill(**dict(dict(event_id='t1',revision=0,code='2330',side='buy',shares=1000,sleeve='core',quotes=self.q(),now=self.now,listed={'2330'},is_trading_day=self.calendar),**kw))
    def test_holiday_t2(self):self.assertEqual(due_date(self.now.date(),self.calendar),date(2026,12,1))
    def test_bad_calendar(self):
        with self.assertRaises(ValueError):due_date(self.now.date(),lambda d:d==self.now.date())
    def test_buy_settlement_and_nav(self):
        self.ready();r=self.fill();s=self.l.read();self.assertEqual(r['settlement_due'],'2026-12-01');self.assertEqual(s['cash'],'10000000');self.assertEqual(balances(s)['available_cash'],D('9899857.50'));self.assertEqual(valuation(s,self.q(),self.now)['equity'],D('9999857.50'))
        self.now=self.now.replace(month=12,day=1);settle(self.l,ids=['t1'],now=self.now,is_trading_day=self.calendar,evidence_ref='fixture');s=self.l.read();self.assertEqual(s['cash'],'9899857.50');self.assertEqual(s['settlements'],{});self.assertEqual(valuation(s,self.q(),self.now)['equity'],D('9999857.50'))
        with self.assertRaises(ValueError):settle(self.l,ids=['t1'],now=self.now+timedelta(seconds=1),is_trading_day=self.calendar,evidence_ref='fixture')
    def test_early_settlement_rollback(self):
        self.ready();self.fill();s=self.l.read()
        with self.assertRaises(ValueError):settle(self.l,ids=['t1'],now=self.now+timedelta(seconds=1),is_trading_day=self.calendar,evidence_ref='fixture')
        self.assertEqual(self.l.read(),s)
    def test_unsettled_shares(self):
        self.ready();self.fill();self.now+=timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError,'unsettled shares'):self.fill(event_id='t2',revision=1,side='sell')
    def test_sale_receivable(self):
        self.ready({'2330':dict(shares=1000,basis='100000',sleeve='core')},'9900000');self.fill(side='sell');s=self.l.read();self.assertEqual(s['cash'],'9900000');self.assertEqual(balances(s)['receivable'],D('99557.50'));self.assertEqual(balances(s)['available_cash'],D('9900000'));self.assertEqual(valuation(s,{},self.now)['equity'],D('9999557.50'))
    def test_overdue_blocks_new_trade(self):
        self.ready();self.fill();self.now=self.now.replace(month=12,day=1)
        with self.assertRaisesRegex(ValueError,'unsettled due'):self.fill(event_id='t2',revision=1)
    def action(self,kind,**kw):return dict(dict(issuer_id='event1',code='2330',kind=kind,effective_date='2026-11-26',entitled_shares=1000,pay_date='2026-12-01'),**kw)
    def premarket(self):self.ready({'2330':dict(shares=1000,basis='100000',sleeve='core')},'9900000');return self.now.replace(hour=8)
    def test_batch_cash_and_stock(self):
        at=self.premarket();corporate_batch(self.l,actions=[self.action('cash_dividend',per_share=2),self.action('stock_dividend',issuer_id='event2',ratio='.1')],now=at,evidence_ref='fixture');s=self.l.read();self.assertEqual(s['receivables']['event1']['amount'],'2000.00');self.assertEqual(s['share_claims']['event2']['shares'],100);self.assertEqual(D(s['positions']['2330']['basis'])+D(s['share_claims']['event2']['basis']),100000)
        self.now=self.now.replace(month=12,day=1);before=valuation(s,self.q(),self.now)['equity'];pay_claim(self.l,claim_id='event2',now=self.now,evidence_ref='fixture');pay_claim(self.l,claim_id='event1',now=self.now+timedelta(seconds=1),evidence_ref='fixture');s=self.l.read();self.assertEqual(s['positions']['2330']['shares'],1100);self.assertEqual(valuation(s,self.q(),self.now)['equity'],before)
    def test_reduction(self):
        at=self.premarket();corporate_batch(self.l,actions=[self.action('cash_reduction',ratio='.8',refund_per_old_share=20)],now=at,evidence_ref='fixture');s=self.l.read();self.assertEqual(s['positions']['2330']['shares'],800);self.assertEqual(D(s['positions']['2330']['basis']),80000);self.assertEqual(s['receivables']['event1']['amount'],'20000.00')
    def test_batch_atomic(self):
        at=self.premarket();before=self.l.read()
        with self.assertRaises(ValueError):corporate_batch(self.l,actions=[self.action('cash_dividend',per_share=2),self.action('rights_issue',issuer_id='e2')],now=at,evidence_ref='fixture')
        self.assertEqual(self.l.read(),before)
    def test_locked(self):
        with self.assertRaises(ValueError):corporate_batch(self.l,actions=[self.action('cash_dividend',per_share=2)],now=self.now.replace(hour=8),evidence_ref='fixture')
        self.assertFalse(self.l.read()['ready'])
    def test_legacy_corporate_disabled(self):
        from v3_management import corporate_action
        self.premarket()
        with self.assertRaisesRegex(ValueError,'verified corporate batch'):corporate_action(self.l,dict(kind='cash_entitlement',code='2330',event_id='e',evidence_ref='fixture'),self.now)
    def test_host_default_off(self):self.assertFalse(attach_staging(None));self.assertFalse(attach_staging(None,mode='off'))
    def test_host_rejects_production_and_weak_token(self):
        with self.assertRaises(ValueError):attach_staging(None,mode='production')
        with self.assertRaises(ValueError):attach_staging(None,mode='staging-readonly',market=object(),ledger=self.l,token='x'*43)
    def test_cache_incomplete(self):
        class Market:
            def read(self,codes):return {'complete':False,'quotes':{}},503
        with self.assertRaises(ValueError):cached_quotes(Market(),['2330'])
    def test_cache_format(self):
        class Market:
            def read(self,codes):return {'complete':True,'quotes':{'2330':{'price':100}}},200
        self.assertEqual(cached_quotes(Market(),['2330'])['2330']['code'],'2330')
    def test_host_http(self):
        try:from flask import Flask
        except ImportError:self.skipTest('Flask unavailable')
        from v3_shared_market import DynamicMarket
        class Market:
            codes={'2330'}
            def read(self,codes):return {'complete':False,'quotes':{},'missing':codes},503
        app=Flask(__name__);token='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        self.assertTrue(attach_staging(app,mode='staging-readonly',market=Market(),ledger=self.l,token=token))
        with self.assertRaises(ValueError):attach_staging(app,mode='staging-readonly',market=Market(),ledger=self.l,token=token)
        client=app.test_client();self.assertEqual(client.get('/api/v3/fund/performance').status_code,401);self.assertEqual(client.post('/api/v3/fund/performance').status_code,405);r=client.get('/api/v3/fund/performance',headers={'Authorization':'Bearer '+token});self.assertEqual(r.status_code,200);self.assertFalse(r.json['reconciled']);self.assertEqual(client.get('/v3/fund').status_code,200)
