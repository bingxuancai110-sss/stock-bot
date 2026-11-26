"""Opt-in resource adapters for the test branch. No I/O on import."""
from collections import OrderedDict
from datetime import timedelta
from hashlib import sha256
import json
from threading import Lock
from urllib.parse import urlsplit
from bot_snapshot_budget import PUBLIC, Meter, SQLReader, VersionedSnapshots, BackgroundGate, Deferred
from bot_history_budget import HistoryBudget

METER = Meter()


def provider(url):
    try: host = (urlsplit(url).hostname or '').lower()
    except (ValueError, TypeError, AttributeError): return 'other'
    for domain, name in (('finance.yahoo.com','yahoo'), ('fugle.tw','fugle'), ('twse.com.tw','twse'), ('tpex.org.tw','tpex'), ('supabase.co','supabase')):
        if host == domain or host.endswith('.'+domain): return name
    return 'other'


def byte_size(value):
    if isinstance(value, bytes): return len(value)
    if isinstance(value, str): return len(value.encode('utf-8'))
    return 0


class MeteredRequests:
    """Preserve requests module API; measure logical get/post/request calls only.
    Never consume streamed bodies, log content or modify headers/redirects.
    Sessions, urllib, other modules, retries and wire overhead are not covered.
    """
    def __init__(self, module, meter=METER): self.module,self.meter=module,meter
    def __getattr__(self, name): return getattr(self.module,name)
    def _call(self, method, url, *args, **kwargs):
        sent=received=0; ok=False
        try:
            response=getattr(self.module,method)(url,*args,**kwargs)
            sent=byte_size(getattr(getattr(response,'request',None),'body',None))
            received=byte_size(getattr(response,'_content',None))
            ok=200<=response.status_code<400
            return response
        finally:self.meter.record(provider(url),'request',sent=sent,received=received,ok=ok)
    def get(self,url,*args,**kwargs):return self._call('get',url,*args,**kwargs)
    def post(self,url,*args,**kwargs):return self._call('post',url,*args,**kwargs)
    def request(self,method,url,*args,**kwargs):
        # requests.request has method before URL.
        sent=received=0;ok=False
        try:
            response=self.module.request(method,url,*args,**kwargs)
            sent=byte_size(getattr(getattr(response,'request',None),'body',None));received=byte_size(getattr(response,'_content',None));ok=200<=response.status_code<400
            return response
        finally:self.meter.record(provider(url),'request',sent=sent,received=received,ok=ok)


def dumps(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False,default=lambda x:x.isoformat() if hasattr(x,'isoformat') else str(x))


class SnapshotWriter:
    """Per-process committed content/version cache, public keys only.
    Identical confirmed refresh advances computed_at (legacy semantics), but
    sends no JSON body. Explicit replay keeps computed_at unchanged. No schema
    change. External writes invalidate the remembered row version.
    """
    def __init__(self,connect,release,meter=METER):
        self.connect,self.release,self.meter=connect,release,meter;self.lock=Lock();self.last=OrderedDict()
    def save(self,key,payload,data_date=None,source_meta=None,*,replay=False):
        if key not in PUBLIC or not isinstance(payload,dict) or not payload or type(replay) is not bool:raise ValueError('unsupported snapshot write')
        p,m=dumps(payload),dumps(source_meta or {});size=byte_size(p)+byte_size(m)
        if size>8*1024**2:raise Deferred('snapshot write too large')
        digest=sha256(dumps([str(data_date) if data_date is not None else None,p,m]).encode()).hexdigest()
        with self.lock:
            conn=None;sent=0;ok=False
            try:
                conn=self.connect()
                with conn.cursor() as cur:
                    cur.execute('SET LOCAL statement_timeout=5000')
                    cur.execute('SELECT data_date,computed_at,xmin::text FROM shared_data_snapshots WHERE snapshot_key=%s FOR UPDATE',(key,))
                    row=cur.fetchone();previous=self.last.get(key)
                    if row and previous==(digest,tuple(row)):
                        if replay:version=tuple(row);kind='replay_skipped'
                        else:
                            cur.execute('UPDATE shared_data_snapshots SET computed_at=NOW() WHERE snapshot_key=%s RETURNING data_date,computed_at,xmin::text',(key,))
                            version=tuple(cur.fetchone());kind='metadata_refresh'
                    else:
                        if replay:raise Deferred('unverified replay version')
                        sent=size
                        cur.execute('''INSERT INTO shared_data_snapshots(snapshot_key,data_date,computed_at,payload,source_meta)
                            VALUES(%s,%s,NOW(),%s::jsonb,%s::jsonb) ON CONFLICT(snapshot_key) DO UPDATE SET
                            data_date=EXCLUDED.data_date,computed_at=EXCLUDED.computed_at,payload=EXCLUDED.payload,source_meta=EXCLUDED.source_meta
                            RETURNING data_date,computed_at,xmin::text''',(key,data_date,p,m))
                        version=tuple(cur.fetchone());kind='body_written'
                conn.commit();ok=True;self.last[key]=(digest,version)
                return dict(kind=kind,computed_at=version[1])
            except Exception:
                self.last.pop(key,None)
                if conn is not None:conn.rollback()
                raise
            finally:
                try:
                    if conn is not None:self.release(conn)
                finally:self.meter.record('supabase','write',sent=sent,ok=ok)


def snapshot_policy(key,projection,version,payload,meta):
    if projection!='full' or not payload:return False
    if key=='stock_info_map':return all(isinstance(payload.get(k),dict) for k in ('names','industries','markets'))
    return key in PUBLIC


class ResourceHost:
    def __init__(self,connect,release,safe,fetch,*,meter=METER):
        self.safe=safe;self.writer=SnapshotWriter(connect,release,meter)
        self.snapshots=VersionedSnapshots(SQLReader(connect,release,meter),safe,snapshot_policy,short_key='short_term:short-wave-v10')
        self.history=HistoryBudget(fetch,safe,self.verify_history)
        self.gate=BackgroundGate({'預熱快取':(5,160,lambda:None),'短線盤中更新檢查':(3,160,lambda:None)},safe)
    @staticmethod
    def verify_history(code,row,day,market,span):
        if not isinstance(row,dict) or row.get('close_is_final') is not True:return False
        dates=row.get('close_dates') or []
        days={'3mo':90,'6mo':180,'1y':365}[span]
        return bool(dates and dates[0]<=day-timedelta(days=days-21) and dates[-1]==day)
    def review(self,codes,span,*,day,markets):
        return self.history.load(codes,span=span,completed_day=day,markets=markets,source_revision='bot-review-v1')
    def optional_job(self,name,fn):
        """Existing serial executor owns queue/priority; share gate admission only."""
        if name not in self.gate.handlers:return fn()
        with self.gate.lock:
            if self.gate.active is not None:raise Deferred('optional job busy')
            self.gate.active=name
        try:
            if not self.safe(reserve_mb=self.gate.handlers[name][1]):raise Deferred('optional job memory deferred')
            return fn()
        finally:
            with self.gate.lock:self.gate.active=None
