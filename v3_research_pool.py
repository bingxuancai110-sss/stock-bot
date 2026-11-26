"""Read saved BOT research snapshots; never scan, order, or request quotes."""
from datetime import date, datetime
import math
from v3_shared_market import TZ, valid_code


def day(value):
    try:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        s = str(value)
        return date.fromisoformat(s if len(s) == 8 else s[:10])
    except (ValueError, TypeError):
        return None


def stamp(value):
    try:
        d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return d.astimezone(TZ) if d.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def num(value):
    try:
        n = float(value)
        return n if not isinstance(value, bool) and math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def rows(value):
    return [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []


def obj(value):
    return value if isinstance(value, dict) else {}


def context(now, last_close, last_chips, intraday):
    if now.tzinfo is None:
        raise ValueError('timezone required')
    now = now.astimezone(TZ)
    if not isinstance(last_close, date) or not isinstance(last_chips, date):
        raise ValueError('verified reference dates required')
    if (last_chips > last_close or last_close > now.date() or
            (last_close == now.date() and now.hour*60+now.minute < 810)):
        raise ValueError('invalid reference dates')
    if intraday and not 540 <= now.hour*60+now.minute < 810:
        raise ValueError('invalid intraday window')
    return now


def build_pool(*, now, last_close, last_chips, intraday, listed, holdings,
               blackhorse=None, radar_live=None, radar_close=None, chips=None,
               short_live=None, short_close=None, short_version, cap=200):
    """All dates and the active list must come from trusted host sources.
    Rank is research priority only; never a return forecast or trade instruction.
    """
    now = context(now, last_close, last_chips, intraday)
    active = set(listed)
    held = list(dict.fromkeys(holdings))
    if not active or len(active)>5000 or any(not valid_code(c) or c=='IX0001' for c in active):
        raise ValueError('invalid active catalogue')
    if not 1 <= cap <= 200 or len(held)>cap or not set(held)<=active:
        raise ValueError('invalid holdings or cap')
    evidence, states = {}, {}
    today = now.date()

    def age_ok(value, limit=None):
        t = stamp(value)
        if not t or t>now:
            return False
        return limit is None or (now-t).total_seconds()<=limit

    def add(raw, source, rank, data_day, direction, **detail):
        code = raw.get('code')
        if not isinstance(code,str) or code not in active:
            return
        e = dict(source=source,rank=rank,data_date=data_day.isoformat(),direction=direction,**detail)
        evidence.setdefault(code,[]).append(e)

    # Blackhorse already contains the five-factor model. Never count it twice.
    b = obj(blackhorse)
    bt = stamp(b.get('computed_at'))
    meta = obj(b.get('source_meta'))
    states['blackhorse_factors'] = 'unavailable'
    if (bt and age_ok(bt) and bt.date()>=last_close and day(b.get('source_date'))==last_chips
            and meta.get('source_date_confirmed') is not False and isinstance(b.get('rows'),list)):
        states['blackhorse_factors'] = 'accepted'
        good = []
        for r in rows(b['rows']):
            qd, score = day(r.get('quote_date')), num(r.get('score'))
            live = intraday and qd==today and bt.date()==today and age_ok(bt,2400)
            closed = qd==last_close and bt.date()>=qd
            if (live or closed) and score is not None and 0<=score<=100:
                good.append((r,qd,score,live))
        good.sort(key=lambda x:(-x[2],str(x[0].get('code'))))
        for i,(r,qd,score,live) in enumerate(good,1):
            add(r,'blackhorse_factors',i,qd,'bullish_research',score=score,
                factors={k:num(r.get(k)) for k in ('rev','val','mom','streak_score','chip')},
                institution_date=last_chips.isoformat(),intraday=live,computed_at=bt.isoformat())

    # A live radar's source_date may be yesterday's institutional data; quote_date
    # and scan completion, not that field, determine its actual price session.
    states['radar']='unavailable'
    for snap, live in ((radar_live,True),(radar_close,False)):
        r = obj(snap); meta=obj(r.get('source_meta'))
        t=stamp(r.get('scan_finished_at') or meta.get('scan_finished_at') or r.get('computed_at'))
        qd=today if live else last_close
        if (live and not intraday) or not t or t.date()<qd or not age_ok(t,600 if live else None) or not isinstance(r.get('rows'),list):
            continue
        if live and t.date()!=today:
            continue
        valid=[x for x in rows(r['rows']) if day(x.get('quote_date'))==qd]
        if r['rows'] and not valid:
            continue
        states['radar']='live' if live else 'prior_close'
        def radar_key(x):
            breakout=2 if x.get('breakout')=='季線新高' else int(bool(x.get('breakout')))
            return (breakout-int((num(x.get('up_streak')) or 0)>=5),num(x.get('vol_ratio')) or 0,num(x.get('streak')) or 0,num(x.get('pct')) or 0)
        for i,x in enumerate(sorted(valid,key=radar_key,reverse=True),1):
            add(x,'radar',i,qd,'bullish_research',intraday=live,computed_at=t.isoformat(),
                breakout=str(x.get('breakout') or ''),volume_ratio=num(x.get('vol_ratio')))
        break

    c=obj(chips); p=obj(c.get('payload')); ct=stamp(c.get('computed_at'))
    states['chips']='unavailable'
    if (p.get('available') is True and day(c.get('data_date') or p.get('data_date'))==last_chips
            and ct and ct.date()>=last_chips and age_ok(ct) and isinstance(p.get('groups'),dict)):
        states['chips']='accepted'
        for group,direction in (('both_buy','bullish_research'),('trust_buy','bullish_research'),('foreign_buy','bullish_research'),('trust_sell','bearish_research'),('both_sell','bearish_research')):
            for i,r in enumerate(rows(p['groups'].get(group)),1):
                add(r,'chips',i,last_chips,direction,group=group,lots=num(r.get('lots')),hit_days=num(r.get('hit_days')))
        for i,r in enumerate(rows(p.get('institutional_shifts')),1):
            add(r,'chips',i,last_chips,'neutral_research',group='institutional_shift',lots=num(r.get('current_total_lots')))

    states['short_term']='unavailable'
    for snap,live in ((short_live,True),(short_close,False)):
        s=obj(snap); qd=today if live else last_close; t=stamp(s.get('generated_at'))
        if ((live and not intraday) or s.get('selection_version')!=short_version or s.get('intraday') is not live
                or day(s.get('date'))!=qd or not t or t.date()<qd or not age_ok(t,1200 if live else None)):
            continue
        if live and t.date()!=today:
            continue
        if not isinstance(s.get('all_rows',s.get('rows')),list) or not isinstance(s.get('short_rows'),list):
            continue
        states['short_term']='live' if live else 'prior_close'
        for side, group in (('long',s.get('all_rows',s.get('rows'))),('short',s['short_rows'])):
            for i,r in enumerate(rows(group),1):
                action=r.get('current_action')
                if day(r.get('date'))!=qd or action not in ('entry','add','reduce','exit','hold','wait') or (live and not age_ok(r.get('observed_at'),1200)):
                    continue
                if r.get('direction',side)!=side or (not live and r.get('provisional')):
                    continue
                if action in ('entry','add','reduce','exit') and day(r.get('latest_event_date'))!=qd:
                    continue
                direction=('bullish_research' if side=='long' else 'bearish_research') if action in ('entry','add','hold') else 'neutral_research'
                add(r,'short_term',i,qd,direction,side=side,action=action,provisional=bool(r.get('provisional') or live),
                    intraday=live,stop=num(r.get('stop')),target=num(r.get('target')),rr=num(r.get('rr')))
        break

    result=[]
    for code,items in evidence.items():
        best={}
        for e in items:
            best[e['source']]=min(e['rank'],best.get(e['source'],e['rank']))
        directions={e['direction'] for e in items}
        result.append(dict(code=code,priority=round(sum(1/(1+r/10) for r in best.values()),6),
                           source_count=len(best),conflict={'bullish_research','bearish_research'}<=directions,evidence=items))
    result.sort(key=lambda r:(-r['source_count'],-r['priority'],r['code']))
    codes=(held+[r['code'] for r in result if r['code'] not in held])[:cap]
    return dict(codes=codes,holdings=held,ranked=result,sources=states,as_of=now.isoformat(),
                execution_authorized=False,usable=bool(evidence))


def load_and_refresh(engine, *, now, listed, holdings, last_close, last_chips,
                     intraday, screener_reader, live_radar_reader, chips_reader,
                     short_store, short_key, short_version):
    """Explicit scheduler adapter; reads saved snapshots only, not service.payload()."""
    listed, holdings = tuple(listed), tuple(holdings)
    errors={}
    def read(name, fn):
        try:
            return fn()
        except Exception as exc:
            errors[name]=type(exc).__name__
            return None
    def short(key):
        return obj(short_store.load(key,max_age_seconds=30*86400)).get('payload')
    data=build_pool(now=now,listed=listed,holdings=holdings,last_close=last_close,last_chips=last_chips,
        intraday=intraday,short_version=short_version,
        blackhorse=read('blackhorse',lambda:screener_reader('blackhorse')),
        radar_live=read('radar_live',live_radar_reader) if intraday else None,
        radar_close=read('radar_close',lambda:screener_reader('radar')),
        chips=read('chips',lambda:chips_reader(allow_compute=False,persist=False)),
        short_live=read('short_live',lambda:short(short_key+':intraday')) if intraday else None,
        short_close=read('short_close',lambda:short(short_key)))
    data['errors']=errors
    # If all research fails, do not overwrite a usable same-day pool.
    if data['usable']:
        engine.set_pool(listed=listed,holdings=holdings,candidates=data['codes'],as_of=now.astimezone(TZ).date().isoformat())
    else:
        # Even if research is unavailable, remove symbols no longer in the verified catalogue.
        active=set(listed)
        old=engine.pool()
        safe=list(dict.fromkeys(list(holdings)+[c for c in old if c in active]))[:200]
        if tuple(safe)!=old:
            engine.set_pool(listed=listed,holdings=holdings,candidates=safe,as_of=now.astimezone(TZ).date().isoformat())
        data['codes']=safe
    data['applied']=data['usable']
    return data
