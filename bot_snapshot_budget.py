"""Opt-in public snapshot reader and cooperative scheduler. No startup or I/O on import."""
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from threading import Lock
import base64
import json
import time
import zlib

PUBLIC = frozenset(('stock_info_map','monthly_revenue','valuation','chips_superman'))
SOURCES = frozenset(('supabase','yahoo','fugle','twse','tpex','other'))
OPS = frozenset(('metadata','body','read','write','request'))


class Deferred(RuntimeError):
    pass


class Meter:
    """Explicit byte measurements only; not TLS/SQL wire or billed bytes."""
    def __init__(self):
        self.lock = Lock(); self.counts = Counter()

    def record(self, source, op, *, sent=0, received=0, ok=True):
        if source not in SOURCES or op not in OPS or any(type(n) is not int or n<0 for n in (sent,received)) or type(ok) is not bool:
            raise ValueError('invalid metric')
        with self.lock:
            for field,n in (('calls',1),('errors',int(not ok)),('sent_payload_bytes',sent),('received_payload_bytes',received)):
                self.counts[(source,op,field)] += n

    def report(self):
        with self.lock:
            return {':'.join(k):v for k,v in self.counts.items()}


def decode_packed(packed, limit):
    if not isinstance(packed,str) or len(packed)>limit*2:
        raise Deferred('compressed envelope exceeds limit')
    try:
        obj=zlib.decompressobj(); raw=obj.decompress(base64.b64decode(packed,validate=True),limit+1)
        if len(raw)>limit or not obj.eof or obj.unused_data or obj.unconsumed_tail:
            raise Deferred('invalid or oversized decompressed snapshot')
        data=json.loads(raw)
        if not isinstance(data,dict): raise Deferred('invalid snapshot object')
        return data
    except (ValueError,zlib.error,RecursionError) as e: raise Deferred('invalid compressed snapshot') from e


class SQLReader:
    """Read-only PostgreSQL adapter; exact row version and bounded body transfer.
    The server may still detoast/serialize JSONB to enforce the byte cap.
    Caller supplies isolated/test or explicitly authorized read connection.
    """
    def __init__(self, connect, release, meter):
        self.connect,self.release,self.meter=connect,release,meter

    def _query(self, sql, args, op):
        conn=self.connect(); ok=False; size=0
        try:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION READ ONLY')
                cur.execute('SET LOCAL statement_timeout=5000')
                cur.execute(sql,args); row=cur.fetchone()
            if row: size=sum(len(str(x).encode()) for x in row if x is not None)
            conn.commit(); ok=True; return row
        except Exception:
            conn.rollback(); raise
        finally:
            try:self.release(conn)
            finally:self.meter.record('supabase',op,received=size,ok=ok)

    def metadata(self,key):
        return self._query('SELECT data_date,computed_at,xmin::text FROM shared_data_snapshots WHERE snapshot_key=%s',(key,),'metadata')

    def body(self,key,version,projection,limit):
        if projection=='full': expr='payload'
        elif projection=='cycle': expr="jsonb_build_object('cycle',payload->'cycle','scope',payload->'scope','selection_version',payload->'selection_version')"
        else: raise ValueError('unknown projection')
        return self._query(f'''SELECT CASE WHEN octet_length(body)<=%s THEN body ELSE NULL END
            FROM (SELECT jsonb_build_object('payload',({expr}),'source_meta',source_meta)::text AS body FROM shared_data_snapshots
              WHERE snapshot_key=%s AND data_date IS NOT DISTINCT FROM %s AND computed_at=%s AND xmin::text=%s) q''',
            (limit,key,*version),'body')


class VersionedSnapshots:
    """Metadata-only on repeated reads, lazy body/projection on demand.
    Exact public allowlist, never per-user keys. Cache limits encoded bytes, not RSS.
    Version is an observation, not a permanent globally unique content ID.
    """
    def __init__(self, reader, safe, verify, *, short_key, max_bytes=2*1024**2, max_items=8, max_decoded=8*1024**2, now=lambda:datetime.now(timezone.utc)):
        if not short_key.startswith('short_term:') or len(short_key)>80 or not 0<max_bytes<=8*1024**2 or not 0<max_decoded<=32*1024**2 or not 0<max_items<=32:
            raise ValueError('invalid snapshot config')
        self.reader,self.safe,self.verify,self.now=reader,safe,verify,now
        self.short=frozenset((short_key,short_key+':intraday',short_key+':compressed-v1',short_key+':intraday:compressed-v1'))
        self.max_bytes,self.max_items,self.max_decoded=max_bytes,max_items,max_decoded
        self.lock=Lock(); self.cache=OrderedDict(); self.bytes=0

    def _validate(self,key,projection):
        if key not in PUBLIC|self.short or projection not in ('full','cycle') or (projection=='cycle' and key not in self.short): raise ValueError('unsupported public snapshot')

    def _meta(self,key,max_age):
        if not 0<max_age<=30*86400: raise ValueError('invalid age')
        row=self.reader.metadata(key)
        if not row: return None
        day,stamp,rev=row
        if not isinstance(stamp,datetime) or stamp.tzinfo is None or not isinstance(rev,str) or not rev:
            raise Deferred('invalid version metadata')
        age=(self.now()-stamp).total_seconds()
        return tuple(row) if 0<=age<=max_age else None

    def metadata(self,key,max_age=86400):
        self._validate(key,'full'); return self._meta(key,max_age)

    def load(self,key,*,projection='full',max_age=86400):
        self._validate(key,projection)
        if not self.lock.acquire(blocking=False): raise Deferred('snapshot reader busy')
        try:
            if not self.safe(reserve_mb=160): raise Deferred('memory headroom')
            version=self._meta(key,max_age)
            slot=(key,projection); old=self.cache.pop(slot,None)
            if old:self.bytes-=len(old[1])
            if version is None:return None
            if old and old[0]==version: raw=old[1]
            else:
                row=self.reader.body(key,version,projection,self.max_bytes)
                if not row or not isinstance(row[0],str): raise Deferred('snapshot changed or oversized; retry later')
                raw=row[0].encode()
                if len(raw)>self.max_bytes: raise Deferred('oversized body')
            if not self.safe(reserve_mb=160): raise Deferred('memory headroom')
            envelope=json.loads(raw)
            if not isinstance(envelope,dict) or not isinstance(envelope.get('payload'),dict) or not isinstance(envelope.get('source_meta'),dict): raise Deferred('invalid body')
            payload=envelope['payload']; source_meta=envelope['source_meta']
            if projection=='full' and key.endswith(':compressed-v1'):
                payload=decode_packed(payload.get('packed'),self.max_decoded)
            if not self.safe(reserve_mb=160): raise Deferred('memory headroom')
            if not self.verify(key,projection,version,payload,source_meta): raise Deferred('snapshot policy mismatch')
            if not 0<=(self.now()-version[1]).total_seconds()<=max_age: raise Deferred('snapshot expired during read')
            self.cache[slot]=(version,raw);self.bytes+=len(raw)
            while self.bytes>self.max_bytes or len(self.cache)>self.max_items:
                _,(_,d)=self.cache.popitem(last=False);self.bytes-=len(d)
            return dict(data_date=version[0],computed_at=version[1],payload=payload,source_meta=source_meta)
        finally:self.lock.release()


class BackgroundGate:
    """One cooperative process-wide instance, injected fixed handlers only.
    No threads/timers/executor created. Host calls tick at safe checkpoints.
    An aging score mitigates starvation; a running task is not preempted.
    """
    def __init__(self, handlers, safe, *, clock=time.monotonic):
        if not handlers or len(handlers)>16: raise ValueError('invalid handlers')
        for name,(priority,reserve,fn) in handlers.items():
            if not isinstance(name,str) or not 0<=priority<=10 or not 160<=reserve<=512 or not callable(fn): raise ValueError('invalid policy')
        self.handlers=dict(handlers); self.safe=safe;self.clock=clock
        self.lock=Lock();self.pending={};self.active=None

    def submit(self,name):
        if name not in self.handlers: raise ValueError('unknown job')
        with self.lock:
            if name==self.active or name in self.pending:return False
            self.pending[name]=self.clock();return True

    def tick(self):
        with self.lock:
            if self.active is not None:return 'busy'
            if not self.pending:return 'idle'
            now=self.clock()
            name=min(self.pending,key=lambda n:(self.handlers[n][0]-(now-self.pending[n])/60,self.pending[n],n))
            queued=self.pending.pop(name);self.active=name
        priority,reserve,fn=self.handlers[name]
        try:
            if not self.safe(reserve_mb=reserve):
                with self.lock:self.pending[name]=queued
                return 'deferred'
            try:fn()
            except Deferred:
                with self.lock:self.pending[name]=queued
                return 'deferred'
            return 'completed'
        finally:
            with self.lock:self.active=None
