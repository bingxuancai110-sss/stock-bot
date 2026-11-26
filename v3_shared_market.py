"""Opt-in common market engine. No imports start a worker or access secrets.
All callers sharing a provider key must use the SAME state store and namespace.
SQLite is for isolated testing/single host; PostgreSQL is for multi-worker use.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
import math
import re
import sqlite3
import uuid
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Taipei')
SYMBOL = re.compile(r'(?:[0-9]{4,6}[A-Z]?|IX0001)\Z')
BUDGET = {'bot': 30, 'v3': 20}
TOTAL = 50
URL = 'https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/'


class StateStore:
    """Serialized JSON state in one database row; fail closed on DB errors.
    Postgres connection factory must supply dedicated non-autocommit connections.
    init_schema is an explicit test/migration step, never called on import.
    """
    def __init__(self, connect, namespace, *, postgres=False):
        if not namespace or len(namespace) > 100:
            raise ValueError('invalid namespace')
        self.connect, self.namespace, self.pg = connect, namespace, postgres

    def init_schema(self):
        con = self.connect()
        try:
            cur = con.cursor()
            cur.execute('CREATE TABLE IF NOT EXISTS v3_market_state (namespace VARCHAR(100) PRIMARY KEY, payload TEXT NOT NULL)')
            con.commit()
        finally:
            con.close()

    @contextmanager
    def transaction(self):
        con = self.connect()
        p = '%s' if self.pg else '?'
        try:
            cur = con.cursor()
            if not self.pg:
                cur.execute('BEGIN IMMEDIATE')
            cur.execute(f'INSERT INTO v3_market_state (namespace,payload) VALUES ({p},{p}) ON CONFLICT(namespace) DO NOTHING', (self.namespace,'{}'))
            cur.execute(f'SELECT payload FROM v3_market_state WHERE namespace={p}' + (' FOR UPDATE' if self.pg else ''), (self.namespace,))
            state = json.loads(cur.fetchone()[0])
            yield state
            cur.execute(f'UPDATE v3_market_state SET payload={p} WHERE namespace={p}', (json.dumps(state, allow_nan=False),self.namespace))
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()


def sqlite_store(path, namespace='fugle-shared'):
    return StateStore(lambda: sqlite3.connect(path, timeout=5), namespace)


def valid_code(code):
    return isinstance(code,str) and SYMBOL.fullmatch(code) is not None


def quote_row(code, payload, now):
    # The existing BOT parser is injected by default at engine construction.
    from fugle_background import quote_row as parse
    return parse(code, payload, now)


class Engine:
    def __init__(self, store, session, api_key, *, now, is_trading_day, parser=quote_row):
        if not isinstance(api_key,str) or not api_key.strip():
            raise ValueError('missing provider key')
        self.store, self.session, self._key = store, session, api_key
        self.now, self.is_trading_day, self.parse = now, is_trading_day, parser

    def _now(self):
        now = self.now()
        if now.tzinfo is None:
            raise ValueError('timezone required')
        return now.astimezone(TZ)

    def _clean(self, s, now):
        t, day = now.timestamp(), now.date().isoformat()
        # Clock moving backwards must not silently reset the rolling budget.
        s['calls'] = [x for x in s.get('calls',[]) if x[0] > t-60]
        if s.get('day') != day:
            s.update(day=day, quotes={}, leases={}, pool=[], listed=[], held=[], cursor=0)
        s['leases'] = {k:v for k,v in s.get('leases',{}).items() if v[1] > t}
        return t

    def set_pool(self, *, listed, holdings, candidates, as_of):
        """Server-only: listed must be a verified active exchange universe for this date.
        Never accept arbitrary public HTTP symbols as a pool update.
        """
        now = self._now()
        if as_of != now.date().isoformat() or not self.is_trading_day(now.date()):
            raise ValueError('stale listing universe')
        active = set(listed)
        if not active or len(active)>5000 or any(not valid_code(c) or c=='IX0001' for c in active):
            raise ValueError('invalid listing universe')
        held = list(dict.fromkeys(holdings))
        if len(held)>200 or any(c not in active for c in held):
            raise ValueError('unverified holding')
        pool = list(dict.fromkeys(held+[c for c in candidates if c in active]))[:200]
        with self.store.transaction() as s:
            self._clean(s,now)
            old_priority = set(s.get('held', [])[:10])
            old_rest = [c for c in s.get('pool', []) if c not in old_priority]
            new_rest = [c for c in pool if c not in set(held[:10])]
            cursor = 0
            if old_rest and new_rest:
                start = s.get('cursor', 0) % len(old_rest)
                for c in old_rest[start:] + old_rest[:start]:
                    if c in new_rest:
                        cursor = new_rest.index(c)
                        break
            s.update(listed=sorted(active), pool=pool, held=held, cursor=cursor)
        return tuple(pool)

    def pool(self):
        now=self._now()
        with self.store.transaction() as s:
            self._clean(s,now)
            return tuple(s.get('pool',[]))

    @staticmethod
    def _fresh(row, now):
        if not isinstance(row,dict):
            return False
        try:
            price,prior,trade,seen = [row[k] for k in ('price','previous_close','time','observed_at')]
            if any(isinstance(x,bool) or not math.isfinite(float(x)) or float(x)<=0 for x in (price,prior,trade,seen)):
                return False
            trade=datetime.fromtimestamp(float(trade)/1e6,TZ)
            seen=datetime.fromtimestamp(float(seen)/1000,TZ)
            if trade.date()!=now.date() or seen.date()!=now.date() or not 540<=trade.hour*60+trade.minute<=810:
                return False
            age, obs = (now-trade).total_seconds(), (now-seen).total_seconds()
            if min(age,obs)<-5 or (trade-seen).total_seconds()>5 or row.get('is_trial') or row.get('type')=='ODDLOT':
                return False
            if now.hour*60+now.minute>=810:
                return row.get('closed') is True
            return row.get('closed') is not True and max(age,obs)<=180
        except (KeyError,ValueError,TypeError,OverflowError,OSError):
            return False

    def read(self, codes, *, role='bot'):
        now=self._now()
        with self.store.transaction() as s:
            self._clean(s,now)
            if not self.is_trading_day(now.date()) or now.hour*60+now.minute<540:
                return {}
            allowed = set(s.get('pool',[])) | {'IX0001'} if role=='v3' else set(codes)
            return {c:deepcopy(s['quotes'][c]) for c in codes if c in allowed and self._fresh(s.get('quotes',{}).get(c),now)}

    def fetch(self, role, code):
        if role not in BUDGET or not valid_code(code):
            raise ValueError('invalid request')
        now=self._now()
        if not self.is_trading_day(now.date()) or not 540<=now.hour*60+now.minute<=850:
            return 'outside_session'
        token=uuid.uuid4().hex
        with self.store.transaction() as s:
            t=self._clean(s,now)
            if role=='v3' and code not in s.get('pool',[]) and code!='IX0001':
                return 'not_allowed'
            cached=s['quotes'].get(code)
            if self._fresh(cached,now) and (cached.get('closed') is True or t-float(cached['observed_at'])/1000 <45):
                return 'cache_hit'
            if code in s['leases']:
                return 'in_flight'
            if t<s.get('cooldown',0) or len(s['calls'])>=TOTAL or sum(x[1]==role for x in s['calls'])>=BUDGET[role]:
                return 'rate_limited'
            s['calls'].append([t,role])
            s['leases'][code]=[token,t+30]
        status, row, cooldown = 'provider_error', None, 0
        try:
            response=self.session.get(URL+code,headers={'X-API-KEY':self._key},timeout=(2,3),allow_redirects=False)
            if response.status_code==429:
                try:
                    retry=float(response.headers.get('Retry-After',60))
                    cooldown=max(60,retry) if math.isfinite(retry) else 60
                except (TypeError,ValueError):
                    cooldown=60
                status='provider_rate_limited'
            elif response.status_code==403 and code=='IX0001':
                status='index_permission_error'
            elif response.status_code in (401,403):
                cooldown,status=300,'credential_error'
            elif response.status_code==200:
                observed=self._now()
                row=self.parse(code,response.json(),observed)
                if isinstance(row,dict) and row.get('code')==code and self._fresh(row,observed) and (code!='IX0001' or row.get('type')=='INDEX'):
                    json.dumps(row,allow_nan=False)
                    status='updated'
                else:
                    row,status=None,'invalid_quote'
        except Exception:
            row,status=None,'provider_error'
        finally:
            done=self._now()
            with self.store.transaction() as s:
                t=self._clean(s,done)
                if cooldown:
                    s['cooldown']=max(s.get('cooldown',0),t+cooldown)
                if s['leases'].get(code,[None])[0]==token:
                    del s['leases'][code]
                    if status=='updated' and row is not None and now.date()==done.date() and self._fresh(row,done):
                        s['quotes'][code]=row
                    elif status=='updated':
                        status='invalid_quote'
                elif status=='updated':
                    status='lease_expired'
        return status

    def collector(self,role):
        if role not in BUDGET:
            raise ValueError('invalid role')
        return RoleCollector(self,role)


class RoleCollector:
    """Compatible with existing BackgroundService and V3Market.collect contract."""
    def __init__(self,engine,role):
        self.engine,self.role=engine,role

    def collect(self,codes,now,previous=None):
        codes=list(dict.fromkeys(codes))
        if self.role=='v3':
            pool=self.engine.pool()
            codes=[c for c in codes if c in pool or c=='IX0001']
        results=[self.engine.fetch(self.role,c) for c in codes]
        quotes=self.engine.read(codes,role=self.role)
        return dict(trading_date=now.astimezone(TZ).date().isoformat(),quotes=quotes,
                    checked_at=self.engine._now().isoformat(),status='ready' if len(quotes)==len(codes) and codes else 'partial',
                    requested=sum(x not in ('cache_hit','in_flight','rate_limited','outside_session','not_allowed') for x in results),
                    updated=results.count('updated'))

    def refresh_pool(self):
        """Explicit scheduler only. Rotate batches with holdings first on pool setup."""
        if self.role!='v3':
            raise PermissionError('V3 pool only')
        now=self.engine._now()
        with self.engine.store.transaction() as s:
            self.engine._clean(s,now)
            pool=s.get('pool',[])
            if not pool:
                return {'status':'empty_pool','quotes':{}}
            held=[c for c in s.get('held',[]) if c in pool][:10]
            rest=[c for c in pool if c not in held]
            start=s.get('cursor',0)%max(1,len(rest))
            batch=held+(rest[start:]+rest[:start])[:20-len(held)]
            s['cursor']=(start+20-len(held))%max(1,len(rest))
        return self.collect(batch,now)


class DynamicMarket:
    """Drop-in for register_v3_market_api; HTTP reads never fetch upstream."""
    def __init__(self, engine):
        self.engine=engine
        self.collector=engine.collector('v3')

    @property
    def codes(self):
        return frozenset(self.engine.pool()) | {'IX0001'}

    def refresh(self):
        return self.collector.refresh_pool()

    def read(self, symbols):
        now=self.engine._now()
        rows=self.engine.read(symbols,role='v3')
        quotes={}
        for code,row in rows.items():
            price,prior=float(row['price']),float(row['previous_close'])
            pct=(price/prior-1)*100
            if not math.isfinite(pct):
                continue
            quotes[code]=dict(price=price,previous_close=prior,change_pct=pct,
                trade_at=datetime.fromtimestamp(float(row['time'])/1e6,TZ).isoformat(),
                observed_at=datetime.fromtimestamp(float(row['observed_at'])/1000,TZ).isoformat(),
                status='final_close' if row.get('closed') is True else 'fresh_intraday',source='Fugle shared snapshot')
        missing=[c for c in symbols if c not in quotes]
        return dict(as_of=now.isoformat(),complete=bool(symbols) and not missing,quotes=quotes,
                    missing=missing,execution_authorized=False),200 if quotes else 503
