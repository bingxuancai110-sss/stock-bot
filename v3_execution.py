"""Pure, fail-closed Fugle book adapter and paper execution estimates."""
from datetime import datetime
from v3_fund import D, TZ, dec, ts, money, encode, checkpoint
import hashlib


def micros(v):
    if type(v) is not int or v <= 0:
        raise ValueError('invalid provider timestamp')
    return datetime.fromtimestamp(v / 1000000, TZ)


def book(payload, ticker, *, code, session, observed_at, now):
    """REST /intraday/quote, type=oddlot separately; ticker limits are required.
    All inputs must come from a trusted host; no provider calls here.
    """
    now, seen = ts(now), ts(observed_at)
    minute = now.hour * 60 + now.minute
    if session not in ('regular', 'oddlot') or not (550 if session == 'oddlot' else 540) <= minute < 805:
        raise ValueError('outside conservative execution window')
    day = now.date().isoformat()
    if (payload.get('symbol') != code or ticker.get('symbol') != code or
        payload.get('date') != day or ticker.get('date') != day or
        payload.get('type') != ('ODDLOT' if session == 'oddlot' else 'EQUITY') or
        ticker.get('securityType') != '01' or ticker.get('securityStatus') != 'NORMAL' or
        ticker.get('tradingCurrency') != 'TWD' or ticker.get('boardLot') != 1000 or
        ticker.get('isDisposition') is not False or ticker.get('matchingInterval') != 0 or
        ticker.get('market') not in ('TSE', 'OTC') or payload.get('market') != ticker.get('market')):
        raise ValueError('unverified security/session')
    if any(payload.get(k) is True for k in ('isTrial','isClose','isLimitDownHalt','isLimitUpHalt','isDelayedOpen','isDelayedClose')):
        raise ValueError('trial/closed/halted book')
    if session == 'regular' and payload.get('isContinuous') is not True:
        raise ValueError('continuous session not confirmed')
    updated = micros(payload['lastUpdated'])
    if updated.date() != now.date() or not updated <= seen <= now or (now-updated).total_seconds()>5 or (now-seen).total_seconds()>5:
        raise ValueError('stale book')
    lo, hi = dec(ticker['limitDownPrice']), dec(ticker['limitUpPrice'])
    if not 0 < lo < hi:
        raise ValueError('invalid limits')
    def levels(key):
        raw = payload.get(key)
        if not isinstance(raw, list) or not 1 <= len(raw) <= 5:
            raise ValueError('empty/invalid book')
        out = []
        for r in raw:
            price, size = dec(r['price']), r['size']
            if not lo < price < hi or type(size) is not int or size <= 0:
                raise ValueError('unpriced, limit or invalid level')
            out.append([str(price),size * (1000 if session=='regular' else 1)])
        prices = [dec(r[0]) for r in out]
        if len(set(prices)) != len(prices) or prices != sorted(prices,reverse=key=='bids'):
            raise ValueError('unsorted book')
        return out
    bids, asks = levels('bids'), levels('asks')
    if dec(bids[0][0]) >= dec(asks[0][0]):
        raise ValueError('crossed/locked book')
    ident = hashlib.sha256(encode([code,session,payload['lastUpdated']]).encode()).hexdigest()
    return dict(code=code,session=session,book_id=ident,book_at=updated.isoformat(),observed_at=seen.isoformat(),
                bids=bids,asks=asks,limit_down=str(lo),limit_up=str(hi),paper_only=True)


def estimate(b, *, side, shares, limit, now):
    """All-or-none conservative visible-depth estimate (not a real fill).
    Oddlot always remains indicative: auction outcome is unknown.
    """
    now=ts(now); updated,seen=ts(b['book_at']),ts(b['observed_at'])
    if not updated<=seen<=now or (now-updated).total_seconds()>5:
        raise ValueError('stale execution book')
    session=b['session']
    if side not in ('buy','sell') or type(shares) is not int or shares<=0 or session not in ('regular','oddlot'):
        raise ValueError('invalid intent')
    if (session=='regular' and shares%1000) or (session=='oddlot' and shares>=1000):
        raise ValueError('wrong lot size')
    limit=dec(limit); lo,hi=dec(b['limit_down']),dec(b['limit_up'])
    if not lo<limit<hi: raise ValueError('invalid limit')
    left, legs=shares,[]
    for p,n in b['asks' if side=='buy' else 'bids']:
        price=dec(p)
        if (side=='buy' and price>limit) or (side=='sell' and price<limit): break
        # Assume at most 10% visible size (rounded down to trade units).
        unit=1000 if session=='regular' else 1
        qty=min(left,(n//(10*unit))*unit)
        if qty: legs.append(dict(price=str(price),shares=qty));left-=qty
        if not left: break
    if left: return dict(status='insufficient_depth',shares=0,book_id=b['book_id'],paper_only=True)
    gross=money(sum(dec(x['price'])*x['shares'] for x in legs))
    return dict(status='indicative_auction' if session=='oddlot' else 'estimated_regular',shares=shares,
        gross=str(gross),vwap=str(gross/shares),legs=legs,book_id=b['book_id'],paper_only=True)


def reconcile_preview(*, events, expected_cash, expected_positions, cutoff, evidence_ref):
    """Replay documented paper trades after the reported 11/13 checkpoint.
    Never unlock or mutate a ledger; a match is arithmetic, not proof of completeness.
    """
    s=checkpoint(); last=ts(s['last_at']); cutoff=ts(cutoff)
    if cutoff<last or not isinstance(evidence_ref,str) or not evidence_ref.strip(): raise ValueError('missing evidence/cutoff')
    ids=set(); cash=dec(s['cash']); pos={c:p['shares'] for c,p in s['positions'].items()}
    for e in events:
        ident=e['event_id']; at=ts(e['at']); code=e['code']; qty=e['shares']; side=e['side']
        if (not isinstance(ident,str) or not ident or ident in ids or not last<at<=cutoff or
            type(qty) is not int or qty<=0 or side not in ('buy','sell') or
            not isinstance(code,str) or not code.isascii() or not code.isdigit() or len(code)!=4 or
            not isinstance(e.get('evidence_ref'),str) or not e['evidence_ref'].strip()):
            raise ValueError('invalid/duplicate historical event')
        ids.add(ident);last=at
        price,fee,tax=map(dec,(e['price'],e['fee'],e['tax']))
        if price<=0 or fee<0 or tax<0 or (side=='buy' and tax!=0): raise ValueError('invalid historical amounts')
        gross=money(price*qty)
        if side=='buy': cash-=gross+fee;pos[code]=pos.get(code,0)+qty
        else:
            if pos.get(code,0)<qty: raise ValueError('historical oversell')
            cash+=gross-fee-tax;pos[code]-=qty
            if not pos[code]: del pos[code]
        if cash<0 or cash!=dec(e['cash_after']): raise ValueError('historical cash mismatch')
    match=cash==dec(expected_cash) and pos==expected_positions
    return dict(arithmetic_match=match,events=len(ids),cash=str(cash),positions=pos,cutoff=cutoff.isoformat(),
                evidence_ref=evidence_ref,history_complete=False,ready=False)


def fetch_book(engine, code, session):
    """Opt-in shared quota acquisition. No HTTP route, no separate credentials.
    Reserve each REST call before network I/O; failures still consume budget.
    """
    from v3_shared_market import BUDGET, TOTAL, valid_code
    now=engine._now(); minute=now.hour*60+now.minute
    if (session not in ('regular','oddlot') or not valid_code(code) or code=='IX0001' or
        not engine.is_trading_day(now.date()) or not (550 if session=='oddlot' else 540)<=minute<805):
        raise ValueError('invalid book request')
    base='https://api.fugle.tw/marketdata/v1.0/stock/intraday/'
    def get(path,params):
        with engine.store.transaction() as state:
            t=engine._clean(state,engine._now())
            if code not in state.get('pool',[]): raise ValueError('outside V3 universe')
            if t<state.get('cooldown',0) or len(state['calls'])>=TOTAL or sum(x[1]=='v3' for x in state['calls'])>=BUDGET['v3']:
                raise ValueError('shared budget unavailable')
            state['calls'].append([t,'v3'])
        response=engine.session.get(base+path+'/'+code,params=params,headers={'X-API-KEY':engine._key},timeout=(2,3),allow_redirects=False)
        if response.status_code in (401,403,429):
            seconds=300
            if response.status_code==429:
                try: seconds=max(60,float(dec(response.headers.get('Retry-After','60'))))
                except (ValueError,ArithmeticError,OverflowError): seconds=60
            with engine.store.transaction() as state:
                t=engine._clean(state,engine._now());state['cooldown']=max(state.get('cooldown',0),t+seconds)
            raise ValueError('provider refused request')
        if response.status_code!=200: raise ValueError('provider unavailable')
        return response.json()
    ticker=get('ticker',{})
    payload=get('quote',{'type':'oddlot'} if session=='oddlot' else {})
    seen=engine._now()
    normalized=book(payload,ticker,code=code,session=session,observed_at=seen,now=seen)
    return dict(book=normalized,book_payload=payload,ticker_payload=ticker,book_observed_at=seen.isoformat())
