"""Opt-in T+2 paper accounting and verified corporate-event batches.
No feed, broker or migration is started here. Dates come from a trusted calendar.
"""
from contextlib import closing
from copy import deepcopy
from datetime import date, timedelta
import json
from v3_fund import D, dec, money, ts, encode


def due_date(day, is_trading_day):
    if not is_trading_day(day):
        raise ValueError('trade date not verified')
    count = 0
    for i in range(1, 32):
        d = day + timedelta(days=i)
        if is_trading_day(d):
            count += 1
            if count == 2:
                return d
    raise ValueError('calendar incomplete')


def balances(s):
    pending = s.get('settlements', {})
    payable = sum((dec(x['amount']) for x in pending.values() if x['side']=='buy'), D(0))
    receivable = sum((dec(x['amount']) for x in pending.values() if x['side']=='sell'), D(0))
    cash = dec(s['cash'])
    return dict(settled_cash=cash, available_cash=cash-payable, payable=payable, receivable=receivable)


def record_trade(s, *, event_id, code, side, shares, gross, fee, tax, now, is_trading_day):
    """Called within Ledger.fill before changing positions. No cash netting."""
    pending=s.setdefault('settlements', {})
    if event_id in pending or any(date.fromisoformat(x['due'])<=now.date() for x in pending.values()):
        raise ValueError('duplicate or unsettled due trade')
    amount=money(gross+fee if side=='buy' else gross-fee-tax)
    if amount<=0: raise ValueError('invalid settlement amount')
    if side=='buy' and amount>balances(s)['available_cash']:
        raise ValueError('insufficient settled cash')
    if side=='sell':
        pending_buys=sum(x['shares'] for x in pending.values() if x['side']=='buy' and x['code']==code)
        if shares>s['positions'].get(code,{}).get('shares',0)-pending_buys:
            raise ValueError('unsettled shares cannot be sold')
    pending[event_id]=dict(code=code,side=side,shares=shares,amount=str(amount),due=due_date(now.date(),is_trading_day).isoformat())


def settle(ledger, *, ids, now, is_trading_day, evidence_ref):
    """Apply explicit, verified due obligations as a single atomic transaction."""
    now=ts(now); ids=list(ids)
    if not ids or len(ids)!=len(set(ids)) or not is_trading_day(now.date()) or not isinstance(evidence_ref,str) or not evidence_ref.strip():
        raise ValueError('invalid settlement confirmation')
    with closing(ledger.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        s=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        if not s['ready'] or s.get('accounting_mode')!='t2' or now<=ts(s['last_at']): raise ValueError('locked or stale settlement')
        p=s.get('settlements',{}); cash=dec(s['cash'])
        for key in ids:
            item=p.get(key)
            if not item or date.fromisoformat(item['due'])>now.date(): raise ValueError('unknown or not due')
            cash+=dec(item['amount'])*(1 if item['side']=='sell' else -1)
        if cash<0: raise ValueError('settlement shortfall')
        db.execute('CREATE TABLE IF NOT EXISTS settlement_log (trade_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        for key in ids:
            item=p.pop(key); db.execute('INSERT INTO settlement_log VALUES(?,?)',(key,encode(dict(item,settled_at=now.isoformat(),evidence_ref=evidence_ref))))
        s.update(cash=str(money(cash)),version=s['version']+1,last_at=now.isoformat())
        db.execute('UPDATE state SET payload=? WHERE id=1',(encode(s),))
        return dict(settled=ids,cash=s['cash'],revision=s['version'],paper_only=True)


def share_claim_value(s, quotes, now):
    from v3_fund import quote
    return sum((money(x['shares']*quote(quotes[x['code']],x['code'],now)) for x in s.get('share_claims',{}).values()),D(0))


def corporate_batch(ledger, *, actions, now, evidence_ref):
    """One pre-market batch from prior-day reconciled holdings. Explicit issuer IDs.
    Supports cash dividend, exact split, deferred stock dividend, cash reduction.
    No rights offerings, fractions, reversals or inferred entitlements.
    """
    now=ts(now); actions=list(actions)
    if not actions or not isinstance(evidence_ref,str) or not evidence_ref.strip() or now.hour*60+now.minute>=540:
        raise ValueError('invalid pre-market batch')
    with closing(ledger.connect()) as db,db:
        db.execute('BEGIN IMMEDIATE')
        s=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        if not s['ready'] or s.get('accounting_mode')!='t2' or ts(s['last_at']).date()>=now.date(): raise ValueError('requires prior-day reconciled state')
        original=deepcopy(s['positions']); seen=set(); share_actions=set()
        claims=s.setdefault('receivables',{}); stock_claims=s.setdefault('share_claims',{})
        db.execute('CREATE TABLE IF NOT EXISTS issuer_actions (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        for a in actions:
            key=a['issuer_id']; code=a['code']; kind=a['kind']; p=original.get(code)
            if not isinstance(key,str) or not key or key in seen or key in claims or key in stock_claims or db.execute('SELECT 1 FROM issuer_actions WHERE id=?',(key,)).fetchone() or a['effective_date']!=now.date().isoformat() or not p or type(a['entitled_shares']) is not int or a['entitled_shares']!=p['shares']:
                raise ValueError('unverified or duplicate entitlement')
            seen.add(key); q=p['shares']; basis=dec(p['basis'])
            if kind in ('split','stock_dividend','cash_reduction'):
                if code in share_actions or any(x['code']==code for x in s.get('settlements',{}).values()) or any(x['code']==code for x in stock_claims.values()): raise ValueError('conflicting share action or pending settlement')
                share_actions.add(code)
            if kind in ('cash_dividend','cash_reduction','stock_dividend'):
                pay=date.fromisoformat(a['pay_date'])
                if pay<now.date(): raise ValueError('invalid payment date')
            if kind=='cash_dividend':
                per=dec(a['per_share']); withholding=dec(a.get('withholding','0'))
                if per<=0 or not 0<=withholding<1: raise ValueError('invalid dividend')
                claims[key]=dict(code=code,amount=str(money(q*per*(1-withholding))),pay_date=pay.isoformat())
            elif kind in ('split','cash_reduction'):
                ratio=dec(a['ratio']); new=q*ratio
                if ratio<=0 or new!=new.to_integral_value() or new<=0: raise ValueError('fractional or invalid share change')
                if kind=='cash_reduction':
                    per=dec(a['refund_per_old_share'])
                    if not ratio<1 or per<=0: raise ValueError('invalid reduction')
                    refund=money(q*per); removed=money(basis*(1-ratio))
                    claims[key]=dict(code=code,amount=str(refund),pay_date=pay.isoformat())
                    s['realized']=str(money(dec(s['realized'])+refund-removed)); s['positions'][code]['basis']=str(basis-removed)
                s['positions'][code]['shares']=int(new)
            elif kind=='stock_dividend':
                ratio=dec(a['ratio']); extra=q*ratio
                if ratio<=0 or extra!=extra.to_integral_value() or extra<=0: raise ValueError('fractional stock dividend')
                allocated=money(basis*extra/(q+extra)); s['positions'][code]['basis']=str(basis-allocated)
                stock_claims[key]=dict(code=code,shares=int(extra),basis=str(allocated),sleeve=p['sleeve'],pay_date=pay.isoformat())
            else: raise ValueError('unsupported company action')
            db.execute('INSERT INTO issuer_actions VALUES(?,?)',(key,encode(dict(action=a,evidence_ref=evidence_ref))))
        s.update(version=s['version']+1,last_at=now.isoformat());db.execute('UPDATE state SET payload=? WHERE id=1',(encode(s),))
        return dict(actions=len(seen),revision=s['version'],paper_only=True)


def pay_claim(ledger, *, claim_id, now, evidence_ref):
    now=ts(now)
    if not isinstance(evidence_ref,str) or not evidence_ref.strip(): raise ValueError('missing payment evidence')
    with closing(ledger.connect()) as db,db:
        db.execute('BEGIN IMMEDIATE');s=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        if not s['ready'] or s.get('accounting_mode')!='t2' or now<=ts(s['last_at']): raise ValueError('locked or stale payment')
        cash=s.get('receivables',{}); shares=s.get('share_claims',{}); item=cash.get(claim_id) or shares.get(claim_id)
        if not item or now.date()<date.fromisoformat(item['pay_date']): raise ValueError('claim unknown or not due')
        if claim_id in cash:
            s['cash']=str(money(dec(s['cash'])+dec(item['amount'])));del cash[claim_id]
        else:
            p=s['positions'].get(item['code']);
            if p and p['sleeve']!=item['sleeve']: raise ValueError('sleeve conflict')
            s['positions'][item['code']]=dict(shares=item['shares']+(p['shares'] if p else 0),basis=str(dec(item['basis'])+(dec(p['basis']) if p else D(0))),sleeve=item['sleeve']);del shares[claim_id]
        db.execute('CREATE TABLE IF NOT EXISTS claim_payments (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        db.execute('INSERT INTO claim_payments VALUES(?,?)',(claim_id,encode(dict(item,paid_at=now.isoformat(),evidence_ref=evidence_ref))))
        s.update(version=s['version']+1,last_at=now.isoformat());db.execute('UPDATE state SET payload=? WHERE id=1',(encode(s),))
        return dict(claim_id=claim_id,revision=s['version'],paper_only=True)
