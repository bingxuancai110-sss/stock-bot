import unittest
from contextlib import closing
from datetime import datetime,timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
from v3_fund import Ledger,checkpoint,valuation,proposal,quote,encode,D,TZ,performance

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.l=Ledger(Path(self.tmp.name)/'test.db');self.l.initialize()
        self.now=datetime(2026,11,26,10,15,tzinfo=TZ)
        self.q={'2330':self.row('2330')}
        self.kw=dict(event_id='test-1',revision=0,code='2330',side='buy',shares=1000,sleeve='core',quotes=self.q,now=self.now,listed={'2330'},is_trading_day=lambda d:True)
    def row(self,c,p=100):
        return dict(code=c,source='Fugle shared snapshot',trade_at=self.now.isoformat(),observed_at=self.now.isoformat(),price=p,status='fresh_intraday',liquidity_verified=True,session='regular',halted=False,bid=p,ask=p,limit_down=str(D(str(p))*D('.9')),limit_up=str(D(str(p))*D('1.1')),bid_shares=20000,ask_shares=20000)
    def ready(self,positions=None,cash='10000000',peak='10000000'):
        # Synthetic fixture ONLY. Production reconciliation not implemented.
        s=checkpoint();s.update(ready=True,positions=positions or {},cash=cash,peak=peak,fees='0',history_complete=True)
        with closing(self.l.connect()) as db, db:db.execute('UPDATE state SET payload=? WHERE id=1',(encode(s),))
        return s
    def fill(self,**kw):return self.l.fill(**dict(self.kw,**kw))
    def test_checkpoint(self):
        s=self.l.read();self.assertFalse(s['ready']);self.assertEqual(sum(D(p['basis']) for p in s['positions'].values())+D(s['cash']),D('9991470'))
        self.l.initialize();self.assertEqual(self.l.read(),s)
    def test_unreconciled(self):
        with self.assertRaisesRegex(ValueError,'unreconciled'):self.fill()
    def test_buy_and_idempotency(self):
        self.ready();r=self.fill();self.assertEqual(r['fee'],'142.50');self.assertEqual(r['cash'],'9899857.50');self.assertEqual(self.fill(),r);self.assertEqual(self.l.read()['version'],1)
        with self.assertRaisesRegex(ValueError,'conflict'):self.fill(shares=2000)
    def test_revision(self):
        self.ready();self.fill()
        with self.assertRaisesRegex(ValueError,'revision'):self.fill(event_id='other')
    def test_sell_fee_tax_basis(self):
        self.ready();self.fill();self.now+=timedelta(seconds=1);self.q['2330']=self.row('2330',110)
        r=self.fill(event_id='sell',revision=1,side='sell',now=self.now)
        self.assertEqual(r['tax'],'330.00');self.assertEqual(self.l.read()['realized'],'9370.75');self.assertEqual(self.l.read()['positions'],{})
    def test_no_short(self):
        self.ready()
        with self.assertRaisesRegex(ValueError,'short selling'):self.fill(side='sell')
    def test_limit_and_liquidity(self):
        self.ready();self.q['2330']['ask_shares']=999
        with self.assertRaisesRegex(ValueError,'liquidity'):self.fill()
        self.q['2330']['ask_shares']=2000;self.q['2330']['ask']=110
        with self.assertRaisesRegex(ValueError,'limit price'):self.fill()
    def test_missing_order_book(self):
        self.ready();del self.q['2330']['liquidity_verified']
        with self.assertRaisesRegex(ValueError,'liquidity'):self.fill()
    def test_stale_quote(self):
        self.ready()
        with self.assertRaisesRegex(ValueError,'stale'):self.fill(now=self.now+timedelta(seconds=181))
    def test_holiday_and_odd_lot(self):
        self.ready()
        for kw in (dict(is_trading_day=lambda d:False),dict(shares=1),dict(listed=set())):
            with self.assertRaises(ValueError):self.fill(**kw)
    def test_single_cap(self):
        self.ready();self.q['2330']=self.row('2330',200)
        with self.assertRaisesRegex(ValueError,'cap'):self.fill(shares=11000)
        self.assertEqual(self.l.read()['version'],0)
    def test_cash_floor_drawdown(self):
        self.ready(cash='8000000',peak='10000000');self.q['2330']=self.row('2330',400)
        with self.assertRaisesRegex(ValueError,'cap'):self.fill(shares=10000)
    def test_tactical_cap(self):
        self.ready();self.q['2330']=self.row('2330',100)
        with self.assertRaisesRegex(ValueError,'tactical cap'):self.fill(shares=9000,sleeve='tactical')
    def test_missing_position_quote(self):
        s=self.ready(positions={'2382':dict(shares=1000,basis='100000',sleeve='core')})
        with self.assertRaises(KeyError):valuation(s,self.q,self.now)
    def test_proposal_stop(self):
        s=self.ready(positions={'2330':dict(shares=1000,basis='110000',sleeve='tactical')})
        self.assertEqual(proposal(s,self.q,self.now,code='2330',conviction='.9',sleeve='tactical',stop=105)['action'],'sell')
    def test_proposal_conviction(self):
        s=self.ready();self.assertEqual(proposal(s,self.q,self.now,code='2330',conviction='.4',sleeve='core')['action'],'hold')
    def test_mark_close_no_execution(self):
        self.now=self.now.replace(hour=13,minute=30);q=self.row('2330');q['status']='final_close'
        self.assertEqual(quote(q,'2330',self.now),D(100))
        with self.assertRaises(ValueError):quote(q,'2330',self.now,execution=True,side='buy',shares=1000)
    def test_unconfirmed_legacy_add(self):
        self.ready(positions={'2330':dict(shares=1000,basis='100000',sleeve='legacy')})
        with self.assertRaisesRegex(ValueError,'legacy buys'):self.fill(sleeve='legacy')
    def test_performance_benchmark(self):
        s=self.ready(cash='9000000'); b={'0050_total_return':dict(date='2026-11-26',base_date='2026-11-12',total_return=True,base=100,value=95,as_of=self.now.isoformat())}
        r=performance(s,{},self.now,b);self.assertEqual(D(r['nav']),90);self.assertEqual(D(r['benchmark_excess_pct']['0050_total_return']),-5)
        b['0050_total_return']['date']='2026-11-25';self.assertEqual(performance(s,{},self.now,b)['benchmark_excess_pct'],{})
    def test_observed_drawdown(self):
        s=self.ready(cash='9000000');s['max_drawdown']='.2';s['history_complete']=False
        r=performance(s,{},self.now);self.assertEqual(D(r['max_observed_drawdown']),D('.2'));self.assertFalse(r['history_complete'])
    def test_mark_persists(self):
        self.ready(cash='9000000');r=self.l.mark({},self.now)
        self.assertEqual(D(r['nav']),90);self.assertEqual(D(self.l.read()['max_drawdown']),D('.1'))
        with self.assertRaises(ValueError):self.l.mark({},self.now)
    def test_mark_no_unreconciled(self):
        with self.assertRaises(ValueError):self.l.mark({},self.now)
    def test_conviction_reduce(self):
        s=self.ready(positions={'2330':dict(shares=2000,basis='200000',sleeve='core')})
        r=proposal(s,self.q,self.now,code='2330',conviction='.2',sleeve='core');self.assertEqual((r['action'],r['shares']),('sell',1000))
if __name__=='__main__':unittest.main()
