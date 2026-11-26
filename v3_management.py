"""Opt-in paper management. No startup, network, scheduler or broker side effects."""
from contextlib import closing
from datetime import date, timedelta
from decimal import ROUND_HALF_UP
import hmac
import json
from v3_fund import D, dec, money, ts, encode, performance, proposal
from v3_execution import micros


def costs(gross, side, *, session='regular', discount='1', minimum='20', kind='stock'):
    """Explicit configurable paper fee policy, not broker-specific billing."""
    gross, discount, minimum = map(dec, (gross, discount, minimum))
    if gross <= 0 or not 0 < discount <= 1 or minimum < 0 or side not in ('buy','sell') or session not in ('regular','oddlot') or kind not in ('stock','equity_etf'):
        raise ValueError('unsupported fee policy')
    fee = max(minimum, gross * D('.001425') * discount).quantize(D('1'), rounding=ROUND_HALF_UP)
    tax = (gross * (D('.003') if kind=='stock' else D('.001'))).quantize(D('1'), rounding=ROUND_HALF_UP) if side=='sell' else D(0)
    return dict(fee=str(fee),tax=str(tax),net_cash=str(money(-gross-fee if side=='buy' else gross-fee-tax)))


def oddlot_estimate(order, payload, now):
    """Strictly later actual market print, not proof of our queue position.
    Host must persist order before the print and authenticate provider origin.
    Only strict price improvement and <=10% of a single print; no partial fill.
    """
    now, submitted = ts(now), ts(order['submitted_at'])
    expiry=ts(order['expires_at']); code=order['code']; side=order['side']; qty=order['shares']; limit=dec(order['limit'])
    if (not submitted < expiry or expiry-submitted>timedelta(minutes=5) or submitted.date()!=expiry.date() or now.date()!=submitted.date() or now<submitted or side not in ('buy','sell') or type(qty) is not int or not 1<=qty<1000 or limit<=0 or payload.get('symbol')!=code or payload.get('type')!='ODDLOT' or payload.get('date')!=now.date().isoformat()):
        raise ValueError('invalid oddlot intent/session')
    # Conservative window unchanged even after future exchange expansions.
    if not 550<=submitted.hour*60+submitted.minute<805: raise ValueError('outside paper window')
    if not isinstance(payload.get('data'),list): raise ValueError('missing actual prints')
    seen=set(); eligible=[]
    for row in payload['data']:
        serial=row['serial']; at=micros(row['time']); size=row['size']; price=dec(row['price'])
        if type(serial) is not int or not 0<serial<99999999 or serial in seen or type(size) is not int or size<=0 or price<=0 or row.get('isTrial') is True:
            raise ValueError('invalid/duplicate print')
        seen.add(serial)
        if submitted<at<=min(now,expiry) and at.date()==submitted.date() and 550<=at.hour*60+at.minute<805 and (now-at).total_seconds()<=5 and (price<limit if side=='buy' else price>limit) and size//10>=qty:
            eligible.append((at,serial,price))
    if not eligible: return dict(status='unfilled',paper_only=True)
    at,serial,price=min(eligible)
    return dict(status='paper_estimate',code=code,side=side,shares=qty,price=str(price),gross=str(money(price*qty)),print_id=f'{code}:{at.date()}:{serial}',at=at.isoformat(),paper_only=True,actual_fill_verified=False)


def corporate_action(ledger, action, now):
    """Explicit event, only reconciled state. Cash entitlement then payment;
    exact share split/stock dividend with verified entitlement, no fractions.
    Do not infer events or entitlement from adjusted historical prices.
    """
    now=ts(now); kind=action['kind']; code=action['code']; ident=action['event_id']; evidence=action.get('evidence_ref')
    if kind not in ('cash_entitlement','cash_payment','share_change') or not isinstance(ident,str) or not ident or not isinstance(evidence,str) or not evidence.strip(): raise ValueError('unverified action')
    digest=encode(action)
    with closing(ledger.connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS corporate_events (event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL)')
        old=db.execute('SELECT digest,result FROM corporate_events WHERE event_id=?',(ident,)).fetchone()
        if old:
            if old[0]!=digest: raise ValueError('action ID conflict')
            return json.loads(old[1])
        s=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        if not s['ready'] or now<=ts(s['last_at']): raise ValueError('unreconciled or stale action')
        if s.get('accounting_mode')=='t2': raise ValueError('use verified corporate batch and claim payment')
        receivables=s.setdefault('receivables',{}); p=s['positions'].get(code)
        if kind=='cash_payment':
            claim=receivables.get(action.get('claim_id'))
            if not claim or claim['code']!=code or now.date()<date.fromisoformat(claim['pay_date']): raise ValueError('no payable claim')
            amount=dec(claim['amount']); s['cash']=str(money(dec(s['cash'])+amount)); del receivables[action['claim_id']]
        else:
            effective=date.fromisoformat(action['effective_date']); entitled=action['entitled_shares']
            if effective!=now.date() or ts(s['last_at']).date()>=effective or not p or type(entitled) is not int or entitled!=p['shares'] or entitled<=0:
                raise ValueError('entitlement requires pre-event reconciled holdings')
            if kind=='cash_entitlement':
                pay=date.fromisoformat(action['pay_date']); per=dec(action['per_share']); withholding=dec(action.get('withholding','0'))
                if pay<effective or per<=0 or not 0<=withholding<1: raise ValueError('invalid cash action')
                amount=money(entitled*per*(1-withholding)); receivables[ident]=dict(code=code,amount=str(amount),pay_date=pay.isoformat())
            else:
                ratio=dec(action['ratio']); new=entitled*ratio
                if ratio<=0 or new!=new.to_integral_value() or new<=0: raise ValueError('fractional share action unsupported')
                p['shares']=int(new) # unchanged aggregate basis
        s.update(version=s['version']+1,last_at=now.isoformat())
        result=dict(event_id=ident,kind=kind,revision=s['version'],paper_only=True)
        db.execute('UPDATE state SET payload=? WHERE id=1',(encode(s),));db.execute('INSERT INTO corporate_events VALUES(?,?,?)',(ident,digest,encode(result)))
        return result


def dashboard(ledger):
    """Consistent read transaction. No valuation inferred from stale prices."""
    with closing(ledger.connect()) as db, db:
        db.execute('BEGIN')
        s=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        marks=[json.loads(r[0]) for r in db.execute('SELECT payload FROM marks ORDER BY as_of DESC LIMIT 30')]
    from v3_accounting import balances
    return dict(data_mode=s.get('data_mode','historical_or_reconciled'),settlement={k:str(v) for k,v in balances(s).items()},share_claims=s.get('share_claims',{}),paper_only=True,reconciled=s['ready'],history_complete=s['history_complete'],capital=s['capital'],cash=s['cash'],positions=s['positions'],fees=s['fees'],tax=s['tax'],receivables=s.get('receivables',{}),marks=marks,as_of=s['last_at'],revision=s['version'])


def register_management(app, ledger, token):
    from flask import jsonify, request, send_file
    from pathlib import Path
    if not isinstance(token,str) or len(token)<32: raise ValueError('weak token')
    @app.get('/v3/fund')
    def v3_fund_shell():
        response=send_file(Path(__file__).with_name('v3_fund_dashboard.html'),mimetype='text/html')
        response.headers['Cache-Control']='no-store'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        return response
    @app.get('/api/v3/fund/performance')
    def v3_fund_performance():
        allowed=hmac.compare_digest(request.headers.get('Authorization','').encode(),('Bearer '+token).encode())
        r=jsonify(dashboard(ledger) if allowed else {'error':'unauthorized'});r.headers['Cache-Control']='no-store'
        return r,200 if allowed else 401


def research_tick(ledger, *, now, is_trading_day, research_reader, quote_reader):
    """Explicit host scheduler hook; proposals only, never mutates ledger.
    Research callback is trusted; its conviction is not generated by this adapter.
    """
    now=ts(now); minute=now.hour*60+now.minute
    if not is_trading_day(now.date()) or minute not in (555,615,675,735,795): return dict(status='outside_schedule',proposals=[])
    state=ledger.read()
    if not state['ready']: return dict(status='history_unreconciled',proposals=[])
    items=research_reader(); quotes=quote_reader(set(state['positions'])|{x['code'] for x in items}); out=[]
    for x in items:
        try: out.append(dict(code=x['code'],**proposal(state,quotes,now,code=x['code'],conviction=x['conviction'],sleeve=x['sleeve'],stop=x.get('stop'))))
        except (ValueError,KeyError,ArithmeticError): out.append(dict(code=x['code'],action='review',reason='invalid/missing evidence'))
    return dict(status='proposals_only',revision=state['version'],proposals=out,execution_authorized=False)


def submit_oddlot(ledger, *, event_id, revision, code, side, shares, sleeve, limit, now, expires_at, listed, is_trading_day, book_payload, ticker_payload, observed_at):
    """Persist a paper intent before a subsequent print can be used. No order routing."""
    import re
    from v3_execution import book
    now,expiry=ts(now),ts(expires_at)
    if not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}',event_id) or code not in listed or not is_trading_day(now.date()) or side not in ('buy','sell') or sleeve not in ('core','tactical','legacy') or type(shares) is not int or not 1<=shares<1000 or not now<expiry<=now+timedelta(minutes=5) or expiry.date()!=now.date(): raise ValueError('invalid paper order')
    b=book(book_payload,ticker_payload,code=code,session='oddlot',observed_at=observed_at,now=now)
    if not dec(b['limit_down'])<dec(limit)<dec(b['limit_up']): raise ValueError('invalid limit')
    order=dict(code=code,side=side,shares=shares,sleeve=sleeve,revision=revision,limit=str(dec(limit)),limit_down=b['limit_down'],limit_up=b['limit_up'],submitted_at=now.isoformat(),expires_at=expiry.isoformat())
    with closing(ledger.connect()) as db,db:
        db.execute('BEGIN IMMEDIATE')
        s=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        if not s['ready'] or s['version']!=revision or now<=ts(s['last_at']): raise ValueError('unreconciled/stale paper order')
        db.execute('CREATE TABLE IF NOT EXISTS oddlot_orders (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        prior=db.execute('SELECT payload FROM oddlot_orders WHERE id=?',(event_id,)).fetchone()
        if prior and prior[0]!=encode(order): raise ValueError('order ID conflict')
        if not prior: db.execute('INSERT INTO oddlot_orders VALUES(?,?)',(event_id,encode(order)))
    return dict(event_id=event_id,**order,paper_only=True)


def register_host(app, *, market, ledger, token):
    """Explicit staging-only host hook; no main.py import or scheduler startup."""
    from v3_market_api import register_v3_market_api
    register_v3_market_api(app,market,token=token)
    register_management(app,ledger,token)
