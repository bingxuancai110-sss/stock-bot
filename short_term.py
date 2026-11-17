"""Small, transparent daily-bar technical/institutional strategy. No orders or LINE.

All replay decisions use an expanding prefix and institutional observations strictly
before the bar date (daily reports are unavailable at that day's closing auction).
These are model signals, never asserted fills or measured investment performance.
"""
from datetime import date, datetime, timedelta
import math
import threading
import time
from flask import Blueprint, jsonify, request

VERSION = 'short-daily-v1'
KEY = 'short_term:' + VERSION
MAX_POOL = 80
MAX_PICKS = 6
SELECTION_VERSION = 2


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def day(value):
    return str(value.isoformat() if hasattr(value, 'isoformat') else value)[:10]


def normalize_bars(quote, now):
    """Reject broken OHLC, duplicates and unfinished current daily bars."""
    keys = ('close_dates', 'opens', 'highs', 'lows', 'closes', 'volumes')
    arrays = [quote.get(k) or [] for k in keys]
    if not arrays[0] or len({len(a) for a in arrays}) != 1:
        return []
    bars = []
    for values in zip(*arrays):
        d = day(values[0]); nums = [number(v) for v in values[1:]]
        try:
            parsed = date.fromisoformat(d)
        except ValueError:
            return []
        if any(v is None for v in nums):
            return []
        o, h, l, c, v = nums
        if min(o, h, l, c) <= 0 or v < 0 or not (l <= min(o, c) <= max(o, c) <= h):
            return []
        if bars and d <= bars[-1]['date']:
            return []
        if parsed > now.date() or (parsed == now.date() and now.hour < 14):
            continue
        bars.append(dict(date=d, open=o, high=h, low=l, close=c, volume=v))
    # Do not interpret corporate-action price scaling as a stop or breakout.
    # Retain only a fresh segment after a large raw-price discontinuity. There
    # must still be 65 complete bars before a candidate can be published.
    cut = 0
    adjs = quote.get('adj_closes') or []
    for i in range(1, len(bars)):
        if abs(bars[i]['close']/bars[i-1]['close']-1) > .25:
            cut = i
        if len(adjs) == len(arrays[0]):
            a, b = number(adjs[i-1]), number(adjs[i])
            if a and b:
                ratio = (a/bars[i-1]['close'])/(b/bars[i]['close'])
                if abs(ratio-1) > .02:
                    cut = i
    return bars[cut:]


def chip_at(history, before, price_dates):
    """Require the five immediately preceding price sessions, not five arbitrary rows."""
    dates = [d for d in price_dates if d < before][-5:]
    by_date = {day(x['date']):x for x in history}
    if len(dates) != 5 or any(d not in by_date for d in dates):
        return dict(available=False, supportive=False, weak=False, date=None)
    rows = [by_date[d] for d in dates]
    if any(number(r.get(k)) is None for r in rows for k in ('foreign', 'trust')):
        return dict(available=False, supportive=False, weak=False, date=None)
    f = sum(float(r['foreign']) for r in rows)
    t = sum(float(r['trust']) for r in rows)
    foreign_days = sum(r['foreign'] > 0 for r in rows)
    trust_days = sum(r['trust'] > 0 for r in rows)
    # Ratios are calculated by analyse() from the exactly aligned five bars.
    supportive = ((f > 0 and foreign_days >= 3 and rows[-1]['foreign'] >= 0) or
                  (t > 0 and trust_days >= 3 and rows[-1]['trust'] >= 0))
    return dict(available=True, supportive=supportive,
                weak=(sum(float(r['foreign'])+float(r['trust']) for r in rows[-2:]) < 0),
                foreign=round(f, 2), trust=round(t, 2), foreign_days=foreign_days,
                trust_days=trust_days, date=dates[-1])


def levels(bars):
    closes = [b['close'] for b in bars]
    last = bars[-1]; prev = bars[:-1]
    ma10 = sum(closes[-10:])/10
    ma20 = sum(closes[-20:])/20
    ma60 = sum(closes[-60:])/60
    old20 = sum(closes[-25:-5])/20
    tr = [max(b['high']-b['low'], abs(b['high']-a['close']),
              abs(b['low']-a['close'])) for a, b in zip(bars[-15:-1], bars[-14:])]
    atr = sum(tr)/14
    pressure = max(b['high'] for b in prev[-20:])
    support = min(b['low'] for b in bars[-5:])
    resistances = sorted({b['high'] for b in prev[-60:] if b['high'] > last['close']})
    # Prior confirmed swing highs only; a pivot becomes known two sessions later.
    pivots = [prev[i]['high'] for i in range(max(2,len(prev)-60),len(prev)-2)
              if prev[i]['high'] >= max(b['high'] for b in prev[i-2:i+3])
              and prev[i]['high'] > last['close']]
    resistance = min(pivots) if pivots else (resistances[-1] if resistances else None)
    avg_vol = sum(b['volume'] for b in prev[-20:])/20
    avg_turnover = sum(b['close']*b['volume'] for b in prev[-20:])/20
    return dict(ma10=ma10, ma20=ma20, ma60=ma60, atr=atr,
                trend=last['close'] > ma20 > ma60 and ma20 > old20,
                recovering=last['close'] > ma20 or last['close'] > ma60,
                pressure=pressure, support=support, resistance=resistance,
                volume_ratio=last['volume']/avg_vol if avg_vol else 0,
                avg_volume=avg_vol, avg_turnover=avg_turnover,
                change=(last['close']/prev[-1]['close']-1)*100)


def setup(bars, chip):
    x = levels(bars); b = bars[-1]; c = b['close']
    hot = (abs(x['change']) > 5 or c/bars[-6]['close'] > 1.15 or
           c/bars[-21]['close'] > 1.30 or c/x['ma20'] > 1.08 or
           x['atr']/c > .045 or x['volume_ratio'] > 3)
    liquid = x['avg_turnover'] >= 100_000_000 and x['avg_volume'] >= 1_000_000
    stop = x['support']-.25*x['atr']
    risk = c-stop
    target = x['resistance'] or c+2*risk
    rr = (target-c)/risk if risk > 0 else 0
    vol5 = sum(z['volume'] for z in bars[-6:-1])/1000
    ratio = ((chip.get('foreign',0)+chip.get('trust',0))/vol5) if vol5 else 0
    chips_ok = chip['supportive'] and ratio >= .01
    candle_strong = c >= b['low']+.6*(b['high']-b['low'])
    breakout = c > x['pressure'] and x['volume_ratio'] >= 1.25 and candle_strong
    pullback = (b['low'] <= x['ma20']*1.015 and c > bars[-2]['high']
                and c > x['ma20'] and .8 <= x['volume_ratio'] <= 2)
    valid = liquid and not hot and x['trend'] and chip['available'] and chips_ok
    risk_ok = 0 < risk/c <= .06 and rr >= 1.5
    return dict(x, stop=stop, target=target, target_kind='壓力區' if x['resistance'] else '2R 參考',
                risk=risk, rr=rr, hot=hot, liquid=liquid, chips_ok=chips_ok,
                chip_ratio=ratio*100, eligible=valid and risk_ok, risk_ok=risk_ok,
                trigger=breakout or pullback,
                watchable=(liquid and not hot and chip['available'] and x['recovering'] and
                           (abs(c/x['ma20']-1)<=.06 or 0<=x['pressure']/c-1<=.05)),
                entry=(breakout or pullback) and valid and risk_ok,
                pattern='突破確認' if breakout else '回測轉強' if pullback else '等待突破／回測')


def analyse(code, name, quote, history, now):
    bars = normalize_bars(quote or {}, now)
    if len(bars) < 65 or (now.date()-date.fromisoformat(bars[-1]['date'])).days > 7:
        return None
    signals = []; trails = []; position = None; latest = None; chip = None
    dates = [b['date'] for b in bars]
    for i in range(64, len(bars)):
        prefix = bars[:i+1]; b = bars[i]
        chip = chip_at(history, b['date'], dates[:i+1])
        plan = setup(prefix, chip); latest = plan
        def emit(kind, price, reason, stop=None, target=None, units=None):
            event = dict(id=f'{VERSION}:{code}:{b["date"]}:{kind}', date=b['date'],
                         type=kind, price=round(price,2), reason=reason,
                         stop=round(stop,2) if stop else None,
                         target=round(target,2) if target else None,
                         units=units, origin='replay')
            signals.append(event)
        if position:
            p = position
            # The line used for today's touch was known after yesterday's close.
            if b['low'] <= p['stop']:
                emit('exit', min(b['open'],p['stop']), '觸及前一日保護價；跳空時以開盤價標示觸發參考，非成交保證',
                     p['stop'],p['target'],p['units'])
                trails.append(dict(date=b['date'],value=round(p['stop'],2)))
                position = None
                continue  # no same-bar re-entry or optimistic stop/target sequencing
            upper = b['high']-max(b['open'],b['close'])
            rejection = (b['high'] >= p['target'] and
                         (b['close'] < b['open'] or upper > .45*(b['high']-b['low'])))
            weak = chip['available'] and chip['weak'] and b['close'] < plan['ma10']
            if not p['reduced'] and (rejection or weak):
                units = p['units']/2; p['units'] -= units; p['reduced'] = True
                emit('reduce',b['close'],'壓力區受阻，減少策略部位一半' if rejection else '收盤跌破十日線且近兩日法人合計轉賣，減少策略部位一半',p['stop'],p['target'],units)
            elif (not p['added'] and not p['reduced'] and plan['eligible'] and
                  b['close'] >= p['entry']+p['risk'] and
                  b['close'] > max(z['high'] for z in prefix[-11:-1]) and
                  b['close'] > p['entry']*1.01 and plan['volume_ratio'] >= 1.25):
                added_risk = (b['close']-p['stop'])*.5
                existing_risk = p['cost']-p['units']*p['stop']
                if existing_risk+added_risk <= p['risk']:
                    p['units'] += .5; p['cost'] += .5*b['close']; p['added'] = True
                    emit('add',b['close'],'獲利達 1R 後再次放量突破；籌碼支持且合併風險未超過初始預算，加碼初始部位的一半',p['stop'],p['target'],.5)
            # Structural trailing stop only ratchets upward, effective next session.
            new_stop = max(p['stop'], min(z['low'] for z in prefix[-5:])-.25*plan['atr'])
            if new_stop < b['close']:
                p['stop'] = new_stop
            if b['close'] > p['target'] and not rejection:
                p['target'] = plan['resistance'] or b['close']+2*(b['close']-p['stop'])
                p['target_kind'] = '壓力區' if plan['resistance'] else '2R 參考'
            trails.append(dict(date=b['date'],value=round(p['stop'],2),target=round(p['target'],2)))
        elif plan['entry']:
            position = dict(entry=b['close'],risk=plan['risk'],stop=plan['stop'],
                            target=plan['target'],units=1.,cost=b['close'],added=False,
                            reduced=False,entry_date=b['date'],target_kind=plan['target_kind'])
            emit('entry',b['close'],plan['pattern']+'；五日法人買盤與量價條件同時成立（收盤訊號，非當根可成交保證）',plan['stop'],plan['target'],1)
            trails.append(dict(date=b['date'],value=round(plan['stop'],2)))
    if latest is None:
        return None
    # Existing modeled positions remain visible for exits even if entry filters
    # subsequently fail. Flat overextended/illiquid names never become picks.
    recent = [s for s in signals if s['date'] >= bars[-3]['date']]
    if not position and not latest['watchable'] and not any(s['type']=='exit' for s in recent):
        return None
    observation=max(latest['pressure'],bars[-1]['close'])
    preview_risk=observation-latest['stop']
    preview_pressure=latest['resistance']
    if not preview_pressure or preview_pressure<=observation:
        above=[b['high'] for b in bars[-61:-1] if b['high']>observation]
        preview_pressure=max(above) if above else None
    preview_target=preview_pressure or observation+2*preview_risk
    preview_rr=(preview_target-observation)/preview_risk if preview_risk>0 else 0
    preview_risk_ok=0<preview_risk/observation<=.06 and preview_rr>=1.5
    label = {'entry':'進場訊號','add':'加碼訊號','reduce':'減碼訊號','exit':'出場訊號'}
    pending=[]
    if not latest['trend']:pending.append('等待趨勢確認')
    if not latest['chips_ok']:pending.append('等待籌碼確認')
    if not (latest['risk_ok'] if position else preview_risk_ok):pending.append('等待風險空間')
    if not latest['trigger']:pending.append('等待突破／回測')
    state = label[recent[-1]['type']] if recent else ('策略追蹤中' if position else pending[0] if pending else '等待進場')
    checks=[dict(label='趨勢',passed=latest['trend']),dict(label='籌碼',passed=latest['chips_ok']),
            dict(label='風險空間',passed=latest['risk_ok'] if position else preview_risk_ok),dict(label='進場型態',passed=latest['trigger'])]
    # Exit candidates remain for three sessions, even though position is now flat.
    return dict(code=code,name=name,date=bars[-1]['date'],price=bars[-1]['close'],
                change_pct=round(latest['change'],2),state=state,active=bool(position),
                watch_only=not position and not recent, pending=pending, checks=checks,
                entry_ready=bool(recent and recent[-1]['type']=='entry' and recent[-1]['date']==bars[-1]['date']),
                latest_event_date=recent[-1]['date'] if recent else None,
                entry=round(observation if not position else position['entry'],2),
                stop=round(position['stop'] if position else latest['stop'],2),
                target=round(position['target'] if position else preview_target,2),
                target_kind=position['target_kind'] if position else '壓力區' if preview_pressure else '2R 參考',support=round(latest['support'],2),
                resistance=round(latest['resistance'],2) if latest['resistance'] else None,
                rr=round(latest['rr'] if position else preview_rr,2), chip=chip,volume_ratio=round(latest['volume_ratio'],2),
                chip_ratio=round(latest['chip_ratio'],2) if chip['available'] else None,
                avg_turnover=round(latest['avg_turnover']),
                trend=latest['trend'], bars=bars[-120:],signals=signals,trailing=trails,
                source=str(quote.get('source') or 'Yahoo 日線'),
                latest_event=recent[-1]['type'] if recent else None)


def exclusion_reason(bars, history, now):
    if len(bars)<65:
        return '完整日 K 不足'
    if (now.date()-date.fromisoformat(bars[-1]['date'])).days>7:
        return '行情日期過舊'
    chip=chip_at(history,bars[-1]['date'],[b['date'] for b in bars])
    plan=setup(bars,chip)
    if not plan['liquid']:
        return '流動性不足'
    if plan['hot']:
        return '過熱或波動過高'
    if not plan['trend']:
        return '上升趨勢未成立'
    if not chip['available']:
        return '法人資料不完整'
    if not plan['chips_ok']:
        return '法人買盤未確認'
    return '停損距離或壓力空間不合適'


class ShortTermService:
    def __init__(self, *, connect, store, quote, executor, safe, now, equity):
        self.connect=connect; self.store=store; self.quote=quote; self.executor=executor
        self.safe=safe; self.now=now; self.equity=equity
        self.lock=threading.RLock(); self.cached=None; self.loaded=False
        self.running=False; self.started=0.; self.progress=0; self.error=''; self.last_try=0.

    def inputs(self, keep):
        conn=self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SET LOCAL statement_timeout=12000')
                cur.execute('''WITH days AS (SELECT DISTINCT trade_date FROM inst_history
                    WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 5)
                    SELECT code,MAX(name),SUM(foreign_net_lots),SUM(trust_net_lots),
                    COUNT(*) FILTER(WHERE foreign_net_lots>0),COUNT(*) FILTER(WHERE trust_net_lots>0)
                    FROM inst_history WHERE trade_date IN (SELECT trade_date FROM days)
                    AND code ~ '^[0-9]{4}$' GROUP BY code HAVING COUNT(*)=5
                    AND ((SUM(foreign_net_lots)>0 AND COUNT(*) FILTER(WHERE foreign_net_lots>0)>=3)
                    OR (SUM(trust_net_lots)>0 AND COUNT(*) FILTER(WHERE trust_net_lots>0)>=3))
                    ORDER BY GREATEST(SUM(foreign_net_lots),SUM(trust_net_lots)) DESC LIMIT 160''',(self.now().date(),))
                raw=cur.fetchall()
                names={str(r[0]):str(r[1] or r[0]) for r in raw if self.equity(str(r[0]))}
                codes=list(dict.fromkeys([str(r['code']) for r in keep if self.equity(str(r['code']))]+list(names)))[:MAX_POOL]
                for r in keep:
                    names.setdefault(r['code'],r['name'])
                cur.execute('''SELECT code,trade_date,foreign_net_lots,trust_net_lots
                    FROM inst_history WHERE code=ANY(%s) AND trade_date BETWEEN %s AND %s
                    ORDER BY code,trade_date''',(codes,self.now().date()-timedelta(days=240),self.now().date()))
                history={c:[] for c in codes}
                for c,d,f,t in cur.fetchall():
                    history[str(c)].append(dict(date=day(d),foreign=number(f),trust=number(t)))
            conn.commit()
            return codes,names,history
        finally:
            conn.close()

    def build(self):
        try:
            deadline=time.monotonic()+120
            if not self.safe(reserve_mb=96):
                raise RuntimeError('記憶體餘裕不足，稍後可重試')
            with self.lock:
                previous=self.cached or {}; keep=[r for r in previous.get('rows',[]) if r.get('active')]
            codes,names,history=self.inputs(keep)
            rows=[]; failures=0; excluded={}; quote_dates=[]
            for i,code in enumerate(codes):
                if time.monotonic()>deadline:
                    raise RuntimeError('行情回應較慢，本輪研究逾時；保留原結果，稍後可重試')
                if not self.safe(reserve_mb=96):
                    raise RuntimeError('背景研究因記憶體餘裕不足暫停，保留原結果')
                try:
                    q=self.quote(code,rng='6mo',include_mis=False,request_timeout=5)
                    if not q:
                        failures+=1
                        excluded['行情來源暫缺']=excluded.get('行情來源暫缺',0)+1
                        continue
                    bars=normalize_bars(q,self.now())
                    if bars:
                        quote_dates.append(bars[-1]['date'])
                    row=analyse(code,names[code],q,history[code],self.now())
                    if row:
                        rows.append(row)
                    else:
                        reason=exclusion_reason(bars,history[code],self.now())
                        excluded[reason]=excluded.get(reason,0)+1
                except Exception as exc:
                    failures+=1
                    excluded['資料讀取或計算失敗']=excluded.get('資料讀取或計算失敗',0)+1
                    print('短線研究單檔略過 %s: %s'%(code,type(exc).__name__),flush=True)
                with self.lock:
                    self.progress=i+1
            if codes and failures==len(codes):
                raise RuntimeError('行情來源暫時無法取得，保留原結果')
            order={'entry':0,'add':1,'reduce':2,'exit':3}
            rows.sort(key=lambda r:(order.get(r['latest_event'],4),0 if r.get('active') else 1,
                                    len(r.get('pending',[])),-r['avg_turnover']))
            # Preserve observed events in stored snapshots instead of retrospectively
            # replacing their reasons/prices when upstream history is revised.
            old={s['id']:s for r in previous.get('rows',[]) for s in r.get('signals',[])}
            for row in rows:
                row['signals']=[old.get(s['id']) or dict(s,observed_at=self.now().isoformat()) for s in row['signals']]
            payload=dict(version=VERSION,selection_version=SELECTION_VERSION,
                         rows=rows[:MAX_PICKS],scanned=len(codes),failed=failures,
                         date=max(quote_dates,default=None),excluded=excluded,
                         generated_at=self.now().isoformat(),
                         note='日 K 規則試行・歷史箭頭為規則重播，尚未宣稱回測績效；不含基本面、不送出交易。')
            if not self.store.save(KEY,payload,data_date=payload['date'],source_meta={'version':VERSION}):
                raise RuntimeError('研究結果未確認保存，保留原結果')
            with self.lock:
                self.cached=payload; self.error=''
            print('✅ 短線研究完成 %s/%s'%(len(payload['rows']),len(codes)),flush=True)
        except Exception as exc:
            with self.lock:
                self.error=str(exc)[:160]
            print('⚠️ 短線研究暫緩: '+str(exc)[:160],flush=True)
        finally:
            with self.lock:
                self.running=False

    def payload(self, refresh=False):
        with self.lock:
            if not self.loaded:
                saved=self.store.load(KEY,max_age_seconds=30*86400)
                self.cached=saved.get('payload') if saved else None
                self.loaded=True
            cached=self.cached
            generated=(cached or {}).get('generated_at','')
            try:
                age=(self.now()-datetime.fromisoformat(generated)).total_seconds()
            except (ValueError,TypeError):
                age=999999
            stale=(age>1800 or generated[:10]!=day(self.now()) or
                   (cached or {}).get('selection_version')!=SELECTION_VERSION)
            # One job shared across all signed-in viewers. Never start work from
            # entering another workbench tab or fan out to extra threads.
            if (not cached or stale or refresh) and not self.running and time.monotonic()-self.last_try>60:
                self.last_try=time.monotonic()
                if self.safe(reserve_mb=96):
                    self.running=True; self.progress=0; self.error=''; self.started=time.monotonic()
                    try:
                        self.executor.submit(self.build)
                    except Exception:
                        self.running=False; self.error='背景佇列暫時無法接收，請稍後重試'
                else:
                    self.error='記憶體餘裕不足，稍後可重試'
            return dict(ok=True,rows=(cached or {}).get('rows',[]),meta=dict(
                version=VERSION,building=self.running,progress=self.progress,pool_limit=MAX_POOL,
                date=(cached or {}).get('date'),generated_at=generated,stale=stale,
                scanned=(cached or {}).get('scanned',0),failed=(cached or {}).get('failed',0),
                excluded=(cached or {}).get('excluded',{}),
                error=self.error,note=(cached or {}).get('note','日 K 規則試行・背景篩選最多 80 檔法人買盤候選，顯示最多 6 檔。')))


def create_blueprint(*, login, service):
    bp=Blueprint('short_term',__name__)
    @bp.route('/web/api/workbench/short-term')
    @login
    def short_term(uid):
        try:
            result=service.payload(request.args.get('refresh')=='1')
            response=jsonify(result)
        except Exception:
            response=jsonify(ok=False,error='短線研究暫時無法載入，請稍後重試')
            response.status_code=503
        response.headers['Cache-Control']='no-store'
        return response
    return bp
