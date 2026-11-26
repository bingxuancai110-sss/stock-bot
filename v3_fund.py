"""Isolated paper fund: no network, broker, scheduler, or production DB access."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re
import sqlite3
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Taipei')
D = Decimal
CENT = D('0.01')
CAPITAL = D('10000000')
FEE = D('0.001425')
TAX = D('0.003')
BASE = [('2382',4694,'276.5'),('2345',722,'1520'),('3017',328,'3040'),
        ('3037',1017,'785'),('6510',209,'3335'),('3167',727,'824'),('3605',3915,'127.5')]


def money(x):
    return dec(x).quantize(CENT, rounding=ROUND_HALF_UP)


def dec(x):
    if isinstance(x, bool):
        raise ValueError('boolean numeric')
    n = D(str(x))
    if not n.is_finite():
        raise ValueError('nonfinite numeric')
    return n


def ts(x):
    t = datetime.fromisoformat(x) if isinstance(x,str) else x
    if not isinstance(t,datetime) or t.tzinfo is None:
        raise ValueError('aware timestamp required')
    return t.astimezone(TZ)


def encode(x):
    return json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False)


def checkpoint():
    # Historical assistant-reported, hypothetical fills; NOT independently reconciled.
    return dict(version=0,ready=False,capital=str(CAPITAL),cash='4005448.50',
        positions={c:dict(shares=q,basis=str(money(q*D(p))),sleeve='legacy') for c,q,p in BASE},
        realized='0.00',fees='8530.00',tax='0.00',peak=str(CAPITAL),
        last_at='2026-11-13T13:30:00+08:00',history_complete=False,
        checkpoint='11/13 reported hypothetical fills; later ledger unverified')


def quote(q,code,now,*,execution=False,side=None,shares=0):
    if q.get('code')!=code or q.get('source')!='Fugle shared snapshot':
        raise ValueError('unverified quote source or code')
    trade,seen=ts(q['trade_at']),ts(q['observed_at'])
    if trade.date()!=now.date() or seen.date()!=now.date() or not trade<=seen<=now:
        raise ValueError('quote date/time mismatch')
    if not 540<=trade.hour*60+trade.minute<=810 or dec(q['price'])<=0:
        raise ValueError('invalid price/time')
    if q.get('status')=='final_close' and now.hour*60+now.minute>=810 and not execution:
        return dec(q['price'])
    if q.get('status')!='fresh_intraday' or not 540<=now.hour*60+now.minute<810 or (now-trade).total_seconds()>180 or (now-seen).total_seconds()>180:
        raise ValueError('stale or non-executable quote')
    if not execution:
        return dec(q['price'])
    # Current V3 quote endpoint lacks these fields: fail closed until trusted adapter adds them.
    if q.get('liquidity_verified') is not True or q.get('session')!='regular' or q.get('halted') is not False:
        raise ValueError('liquidity/session unknown')
    bid,ask,lo,hi=map(dec,(q['bid'],q['ask'],q['limit_down'],q['limit_up']))
    if not 0<lo<=bid<=ask<=hi or not lo<=dec(q['price'])<=hi:
        raise ValueError('invalid book')
    price=ask if side=='buy' else bid
    available=q['ask_shares'] if side=='buy' else q['bid_shares']
    if type(available) is not int or available<shares or price in (lo,hi):
        raise ValueError('insufficient liquidity or limit price')
    return price


def valuation(state,quotes,now):
    now=ts(now)
    values={c:money(p['shares']*quote(quotes[c],c,now)) for c,p in state['positions'].items()}
    cash=dec(state['cash']); equity=cash+sum(values.values(),D(0))
    if equity<=0: raise ValueError('nonpositive equity')
    peak=max(dec(state['peak']),equity)
    drawdown=(peak-equity)/peak
    return dict(equity=equity,nav=equity/dec(state['capital'])*100,cash_weight=cash/equity,
                drawdown=drawdown,peak=peak,values=values,return_pct=(equity/dec(state['capital'])-1)*100)


def proposal(state,quotes,now,*,code,conviction,sleeve,stop=None):
    """Researcher-supplied conviction (0..1), not source-count-as-alpha. No execution."""
    v=valuation(state,quotes,now); conviction=dec(conviction)
    if not 0<=conviction<=1 or sleeve not in ('core','tactical'):
        raise ValueError('invalid research input')
    p=state['positions'].get(code); price=quote(quotes[code],code,ts(now))
    if p and p['sleeve']!=sleeve: return dict(action='review',reason='legacy/sleeve mismatch')
    if p and sleeve=='tactical' and stop is not None and price<=dec(stop):
        return dict(action='sell',shares=p['shares'],reason='tactical stop review')
    if p and conviction<=D('.35'):
        shares=p['shares']//2000*1000
        return dict(action='sell' if shares else 'review',shares=shares,reason='conviction reduction review')
    cash_floor=D('.50') if v['drawdown']>=D('.15') else D('.35') if v['drawdown']>=D('.10') else D('.20')
    if not state['ready'] or v['cash_weight']<=cash_floor or conviction<D('.65'):
        return dict(action='hold',reason='reconciliation, cash or conviction gate')
    target=(D('.02')+D('.03')*conviction) if sleeve=='tactical' else D('.05')+D('.15')*conviction
    existing=v['values'].get(code,D(0)); budget=min(v['equity']*target-existing,dec(state['cash'])-v['equity']*cash_floor)
    # Core/tactical total caps enforced again at fill. Full lots only in this prototype.
    shares=max(0,int(budget/(price*(1+FEE)))//1000*1000)
    return dict(action='buy' if shares else 'hold',shares=shares,reason='research target; subject to execution guards')


class Ledger:
    """Transactional, idempotent isolated SQLite ledger. No live activation path."""
    def __init__(self,path): self.path=str(path)
    def connect(self): return sqlite3.connect(self.path,timeout=10)
    def initialize(self):
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS marks (as_of TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO state VALUES(1,?)',(encode(checkpoint()),))
    def read(self):
        with closing(self.connect()) as db, db: return json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
    def mark(self,quotes,now,benchmarks=None):
        now=ts(now)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            s=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            if not s['ready'] or now<=ts(s['last_at']): raise ValueError('unreconciled or stale mark')
            v=valuation(s,quotes,now); r=performance(s,quotes,now,benchmarks)
            s.update(peak=str(v['peak']),max_drawdown=r['max_observed_drawdown'],last_at=now.isoformat(),version=s['version']+1)
            db.execute('INSERT INTO marks VALUES(?,?)',(now.isoformat(),encode(r)))
            db.execute('UPDATE state SET payload=? WHERE id=1',(encode(s),))
            return r
    def fill(self,*,event_id,revision,code,side,shares,sleeve,quotes,now,listed,is_trading_day):
        now=ts(now)
        if not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',event_id) or side not in ('buy','sell') or type(shares) is not int or shares<=0 or shares%1000 or sleeve not in ('core','tactical','legacy'):
            raise ValueError('invalid order; regular lots only')
        if code not in listed or not re.fullmatch(r'[0-9]{4}',code) or not is_trading_day(now.date()): raise ValueError('unverified listing/session')
        intent=dict(revision=revision,code=code,side=side,shares=shares,sleeve=sleeve,at=now.isoformat())
        digest=hashlib.sha256(encode(intent).encode()).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT digest,payload FROM events WHERE event_id=?',(event_id,)).fetchone()
            if prior:
                if prior[0]!=digest: raise ValueError('event id conflict')
                return json.loads(prior[1])
            s=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            if not s['ready'] or s['version']!=revision or now<=ts(s['last_at']): raise ValueError('unreconciled, stale revision or time')
            v=valuation(s,quotes,now); p=s['positions'].get(code)
            if p and p['sleeve']!=sleeve: raise ValueError('sleeve mismatch')
            price=quote(quotes[code],code,now,execution=True,side=side,shares=shares)
            gross=money(price*shares); fee=money(gross*FEE); tax=money(gross*TAX) if side=='sell' else D(0)
            cash=dec(s['cash']); positions=deepcopy(s['positions'])
            if side=='buy':
                if sleeve=='legacy': raise ValueError('legacy buys blocked')
                cash-=gross+fee
                basis=dec(p['basis']) if p else D(0)
                positions[code]=dict(shares=shares+(p['shares'] if p else 0),basis=str(basis+gross+fee),sleeve=sleeve)
            else:
                if not p or shares>p['shares']: raise ValueError('short selling prohibited')
                sold=money(dec(p['basis'])*shares/p['shares']); cash+=gross-fee-tax
                s['realized']=str(dec(s['realized'])+gross-fee-tax-sold)
                if shares==p['shares']: del positions[code]
                else: positions[code]=dict(p,shares=p['shares']-shares,basis=str(dec(p['basis'])-sold))
            s.update(cash=str(cash),positions=positions,fees=str(dec(s['fees'])+fee),tax=str(dec(s['tax'])+tax),peak=str(v['peak']))
            post=valuation(s,quotes,now); equity=post['equity']; floor=D('.50') if v['drawdown']>=D('.15') else D('.35') if v['drawdown']>=D('.10') else D('.20')
            if side=='buy':
                if post['cash_weight']<floor or post['values'][code]/equity>D('.20'): raise ValueError('cash or single-stock cap')
                tactical=sum((post['values'][c] for c,p in positions.items() if p['sleeve']=='tactical'),D(0))
                if tactical/equity>D('.20') or (sleeve=='tactical' and gross/equity>D('.08')): raise ValueError('tactical cap')
            if cash<0: raise ValueError('negative cash')
            s.update(version=revision+1,last_at=now.isoformat(),peak=str(post['peak']),max_drawdown=str(max(dec(s.get('max_drawdown','0')),v['drawdown'],post['drawdown'])))
            result=dict(event_id=event_id,revision=s['version'],code=code,side=side,shares=shares,price=str(price),gross=str(gross),fee=str(fee),tax=str(tax),cash=str(cash),nav=str(post['nav']),paper_only=True,at=now.isoformat())
            db.execute('UPDATE state SET payload=? WHERE id=1',(encode(s),))
            db.execute('INSERT INTO events VALUES(?,?,?)',(event_id,digest,encode(result)))
            return result


def performance(state,quotes,now,benchmarks=None):
    """Same-date total return series only; no invented benchmark or historical MDD."""
    now=ts(now); v=valuation(state,quotes,now)
    mdd=max(dec(state.get('max_drawdown','0')),v['drawdown'])
    result=dict(as_of=now.isoformat(),equity=str(v['equity']),nav=str(v['nav']),
        return_pct=str(v['return_pct']),cash_weight=str(v['cash_weight']),drawdown=str(v['drawdown']),
        max_observed_drawdown=str(mdd),history_complete=state['history_complete'],
        reconciled=state['ready'],benchmark_excess_pct={})
    for name,b in (benchmarks or {}).items():
        if name not in ('0050_total_return','TAIEX_total_return') or b.get('date')!=now.date().isoformat() or b.get('base_date')!='2026-11-12' or b.get('total_return') is not True or not b.get('as_of') or ts(b['as_of'])>now or ts(b['as_of']).date()!=now.date():
            continue
        base,current=dec(b['base']),dec(b['value'])
        if min(base,current)<=0: raise ValueError('invalid benchmark')
        result['benchmark_excess_pct'][name]=str(v['return_pct']-(current/base-1)*100)
    return result
