"""Small, transparent daily-bar technical/institutional strategy. No orders or LINE.

All replay decisions use an expanding prefix and institutional observations strictly
before the bar date (daily reports are unavailable at that day's closing auction).
These are model signals, never asserted fills or measured investment performance.
"""
from datetime import date, datetime, timedelta, timezone
import math
import threading
import time
from flask import Blueprint, jsonify, request

VERSION = 'short-tech-v2'
KEY = 'short_term:' + VERSION
MAX_POOL = 80
MAX_PICKS = 6
SELECTION_VERSION = 7
LIGHT_BATCH = 64


def position_group(row):
    """Confirmed model state; an unfinished entry/exit cannot change this state."""
    if row.get('current_action')=='unconfirmed':return 'unknown'
    confirmed=[e for e in row.get('signals',[]) if not e.get('provisional') and e.get('type') in ('entry','exit')]
    if confirmed:return 'held' if confirmed[-1]['type']=='entry' else 'closed'
    return 'held' if row.get('active') and not row.get('provisional') else 'waiting'


def price_band(price):
    return '1000以上' if price>=1000 else '500–999' if price>=500 else '100–499' if price>=100 else '低於100'


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def day(value):
    return str(value.isoformat() if hasattr(value, 'isoformat') else value)[:10]


def current_status(signals, active, asof, pending):
    """A historical entry is never a new-entry instruction today."""
    latest=signals[-1] if signals else None
    event=latest if latest and latest['date']==asof else None
    last_exit=next((s for s in reversed(signals) if s['type']=='exit'),None)
    last_entry=next((s for s in reversed(signals) if s['type']=='entry'),None)
    labels={'entry':'本次新進場','add':'本次加碼訊號','reduce':'本次減碼訊號','exit':'本次出場訊號'}
    if event:
        message=(asof+' 收盤資料確認'+{'entry':'進場條件成立；下一交易日需重新確認價格與停損距離，不能直接按歷史訊號價成交。',
            'add':'加碼條件成立；僅適用於仍在追蹤該策略部位的情境，不是首次進場訊號。',
            'reduce':'減碼條件成立；適用於仍持有該策略部位的情境。',
            'exit':'已觸發出場；目前不是新進場訊號。'}[event['type']])
        return dict(state=labels[event['type']],action=event['type'],action_note=message,
                    event=event,last_exit=last_exit,last_entry=last_entry)
    if active:
        return dict(state='策略持有中，非新進場',action='hold',event=None,
            action_note='原策略於 '+(last_entry['date'] if last_entry else '歷史日期')+' 進場，尚未出場；目前沒有新的進場訊號，不代表現在補買。',
            last_exit=last_exit,last_entry=last_entry)
    if last_exit:
        return dict(state='已出場，等待新訊號',action='wait',event=None,
            action_note=last_exit['date']+' 已出場，目前沒有新的進場訊號。'+
            ((last_entry['date']+' 的進場已是歷史紀錄，不是現在買進提示。') if last_entry else ''),
            last_exit=last_exit,last_entry=last_entry)
    return dict(state='等待進場，尚未觸發',action='wait',event=None,
                action_note='尚未觸發進場。'+('、'.join(pending) or '等待新的確認條件')+'。',
                last_exit=None,last_entry=None)


def trade_spreads(signals, close=None, asof=None):
    """User-requested raw exit minus initial entry, not position/account P&L."""
    completed=[]; opened=None
    for signal in signals:
        price=number(signal.get('price'))
        if price is None:
            continue
        if signal['type']=='entry':
            opened=dict(entry_date=signal['date'],entry_price=price)
        elif signal['type']=='exit' and opened:
            completed.append(dict(opened,exit_date=signal['date'],exit_price=price,
                                  spread=round(price-opened['entry_price'],2)))
            opened=None
        # Adds/reductions intentionally do not alter this simple price difference.
    if opened and number(close) is not None:
        opened=dict(opened,date=asof,price=number(close),spread=round(number(close)-opened['entry_price'],2))
    return dict(completed=completed,open=opened,
                total_closed_spread=round(sum(t['spread'] for t in completed),2))


def latest_alerts(rows, asof):
    alerts=[]
    for row in rows:
        if row['date']!=asof:
            continue
        for event in row.get('signals',[]):
            if event['date']==asof:
                spread=None
                if event['type']=='exit':
                    trades=trade_spreads(row.get('signals',[]))['completed']
                    spread=next((t['spread'] for t in reversed(trades) if t['exit_date']==asof),None)
                alerts.append(dict(event,code=row['code'],name=row['name'],spread=spread))
    order={'entry':0,'exit':1,'reduce':2,'add':3}
    return sorted(alerts,key=lambda a:(order.get(a['type'],4),a['code']))


def normalize_bars(quote, now, include_current=False):
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
        if parsed > now.date() or (parsed == now.date() and now.hour < 14 and not include_current):
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
                trend_slope=ma20/old20-1,
                trend=last['close'] > ma20 > ma60 and ma20 > old20,
                recovering=last['close'] > ma20 or last['close'] > ma60,
                pressure=pressure, support=support, resistance=resistance,
                volume_ratio=last['volume']/avg_vol if avg_vol else 0,
                avg_volume=avg_vol, avg_turnover=avg_turnover,
                change=(last['close']/prev[-1]['close']-1)*100)


def setup(bars, chip, volume_fraction=1):
    x = levels(bars); b = bars[-1]; c = b['close']
    x['volume_ratio']/=max(15/270,min(1,volume_fraction))
    hot = (abs(x['change']) > 5 or c/bars[-6]['close'] > 1.15 or
           c/bars[-21]['close'] > 1.30 or c/x['ma20'] > 1.08 or
           x['atr']/c > .045 or x['volume_ratio'] > 3)
    liquid = x['avg_turnover'] >= 10_000_000  # advisory only, never an entry/selection gate
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
    valid = not hot and x['trend'] and b['volume']>0
    risk_ok = 0 < risk/c <= .06 and rr >= 1.5
    return dict(x, stop=stop, target=target, target_kind='壓力區' if x['resistance'] else '2R 參考',
                risk=risk, rr=rr, hot=hot, liquid=liquid, chips_ok=chips_ok,
                chip_ratio=ratio*100, eligible=valid and risk_ok, risk_ok=risk_ok,
                trigger=breakout or pullback,
                watchable=(not hot and b['volume']>0 and x['recovering'] and
                           (abs(c/x['ma20']-1)<=.06 or 0<=x['pressure']/c-1<=.05)),
                entry=(breakout or pullback) and valid and risk_ok,
                pattern='突破確認' if breakout else '回測轉強' if pullback else '等待突破／回測')


def analyse(code, name, quote, history, now, intraday=False):
    bars = normalize_bars(quote or {}, now, include_current=intraday)
    if len(bars) < 65 or (now.date()-date.fromisoformat(bars[-1]['date'])).days > 7:
        return None
    signals = []; trails = []; position = None; latest = None; chip = None
    dates = [b['date'] for b in bars]
    for i in range(64, len(bars)):
        prefix = bars[:i+1]; b = bars[i]
        chip = chip_at(history, b['date'], dates[:i+1])
        fraction=1
        if intraday and b['date']==day(now):
            fraction=((now.hour-9)*60+now.minute)/270
        plan = setup(prefix, chip, volume_fraction=fraction); latest = plan
        def emit(kind, price, reason, stop=None, target=None, units=None):
            if kind=='entry':
                reason+=('；價格 %.2f，MA20 %.2f／MA60 %.2f；%s %.2f，量比 %.2f，初始停損 %.2f，報酬風險比 %.2fR' %
                         (b['close'],plan['ma20'],plan['ma60'],
                          '突破前20日高點' if plan['pattern']=='突破確認' else '回測月線後站回前一日高點',
                          plan['pressure'] if plan['pattern']=='突破確認' else prefix[-2]['high'],
                          plan['volume_ratio'],plan['stop'],plan['rr']))
            elif kind=='add':
                reason+='；原進場 %.2f，1R %.2f，目前 %.2f，前10日高點 %.2f，量比 %.2f' % (
                    position['entry'],position['risk'],b['close'],max(z['high'] for z in prefix[-11:-1]),plan['volume_ratio'])
            elif kind=='reduce':
                reason+='；目前 %.2f，MA10 %.2f，停利／壓力參考 %.2f' % (b['close'],plan['ma10'],target)
            elif kind=='exit':
                reason+='；當日最低 %.2f，前日設定停損 %.2f，當日開盤 %.2f' % (b['low'],stop,b['open'])
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
            previous_ma10=sum(z['close'] for z in prefix[-11:-1])/10
            technical_weak=b['close']<plan['ma10'] and prefix[-2]['close']<previous_ma10
            chip_weak=chip['available'] and chip['weak'] and b['close']<plan['ma10']
            weak=technical_weak or chip_weak
            if not p['reduced'] and (rejection or weak):
                units = p['units']/2; p['units'] -= units; p['reduced'] = True
                reason=('壓力區受阻，減少策略部位一半' if rejection else
                        '連續兩日收盤跌破十日線，減少策略部位一半' if technical_weak else
                        '收盤跌破十日線且法人轉賣，減少策略部位一半')
                emit('reduce',b['close'],reason,p['stop'],p['target'],units)
            elif (not p['added'] and not p['reduced'] and plan['eligible'] and
                  b['close'] >= p['entry']+p['risk'] and
                  b['close'] > max(z['high'] for z in prefix[-11:-1]) and
                  b['close'] > p['entry']*1.01 and plan['volume_ratio'] >= 1.25):
                added_risk = (b['close']-p['stop'])*.5
                existing_risk = p['cost']-p['units']*p['stop']
                if existing_risk+added_risk <= p['risk']:
                    p['units'] += .5; p['cost'] += .5*b['close']; p['added'] = True
                    emit('add',b['close'],'獲利達 1R 後再次放量突破；技術與風險成立，法人僅供參考，加碼初始部位的一半',p['stop'],p['target'],.5)
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
            emit('entry',b['close'],plan['pattern']+'；趨勢、量價與風險條件成立，法人僅供參考（收盤訊號，非成交保證）',plan['stop'],plan['target'],1)
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
    pending=[]
    if not latest['trend']:pending.append('等待趨勢確認')
    if not (latest['risk_ok'] if position else preview_risk_ok):pending.append('等待風險空間')
    if not latest['trigger']:pending.append('等待突破／回測')
    provisional=intraday and bars[-1]['date']==day(now)
    if provisional:
        for signal in signals:
            if signal['date']==day(now):
                signal['provisional']=True
                signal['reason']='盤中暫定，收盤前可能消失：'+signal['reason']
    status=current_status(signals,bool(position),bars[-1]['date'],pending)
    if provisional:
        labels={'entry':'盤中進場條件成立（暫定）','add':'盤中加碼條件成立（暫定）',
                'reduce':'盤中減碼條件成立（暫定）','exit':'盤中出場條件成立（暫定）'}
        status['state']=labels.get(status['action'],status['state'])
        status['action_note']='盤中未收棒，條件可能改變；'+status['action_note'].replace('收盤資料確認','盤中暫定').replace('收盤','本次')
    event=status['event']
    checks=[dict(label='趨勢',passed=latest['trend']),
            dict(label='風險空間',passed=latest['risk_ok'] if position else preview_risk_ok),dict(label='進場型態',passed=latest['trigger'])]
    # Exit candidates remain for three sessions, even though position is now flat.
    return dict(code=code,name=name,date=bars[-1]['date'],price=bars[-1]['close'],provisional=provisional,
                change_pct=round(latest['change'],2),state=status['state'],active=bool(position),
                current_action=status['action'],action_note=status['action_note'],
                last_exit_date=(status['last_exit'] or {}).get('date'),
                last_entry_date=(status['last_entry'] or {}).get('date'),
                watch_only=not position and not recent, pending=pending, checks=checks,
                entry_ready=bool(event and event['type']=='entry'),
                latest_event_date=event['date'] if event else None,
                entry=round(observation if not position else position['entry'],2),
                stop=round(position['stop'] if position else latest['stop'],2),
                target=round(position['target'] if position else preview_target,2),
                target_kind=position['target_kind'] if position else '壓力區' if preview_pressure else '2R 參考',support=round(latest['support'],2),
                resistance=round(latest['resistance'],2) if latest['resistance'] else None,
                rr=round(latest['rr'] if position else preview_rr,2), chip=chip,volume_ratio=round(latest['volume_ratio'],2),
                technical_priority=(not latest['trend'],-latest['trend_slope'],
                    min(abs(bars[-1]['close']/latest['ma20']-1),abs(latest['pressure']/bars[-1]['close']-1))),
                chip_ratio=round(latest['chip_ratio'],2) if chip['available'] else None,
                chip_support='資料待確認' if not chip['available'] else '買盤支持' if latest['chips_ok'] else '買盤分歧／未延續',
                liquidity_warning=not latest['liquid'],
                avg_volume=round(latest['avg_volume']),
                avg_turnover=round(latest['avg_turnover']),
                trend=latest['trend'], bars=bars[-120:],signals=signals,trailing=trails,
                source=str(quote.get('source') or 'Yahoo 日線'),volume_estimated=provisional,
                latest_event=event['type'] if event else None)


def exclusion_reason(bars, history, now):
    if len(bars)<65:
        return '完整日 K 不足'
    if (now.date()-date.fromisoformat(bars[-1]['date'])).days>7:
        return '行情日期過舊'
    chip=chip_at(history,bars[-1]['date'],[b['date'] for b in bars])
    plan=setup(bars,chip)
    if plan['hot']:
        return '過熱或波動過高'
    if not plan['trend']:
        return '上升趨勢未成立'
    return '停損距離或壓力空間不合適'


def light_technical(quote, now, intraday=False, diagnostics=None):
    """Price-only streamed preselection; no institution, price or lot-size bias."""
    points=[]
    for ts,px in zip(quote.get('timestamps') or [],quote.get('closes') or []):
        p=number(px)
        if not p or not ts:
            continue
        d=datetime.fromtimestamp(int(ts),timezone(timedelta(hours=8))).date()
        if d>now.date() or (d==now.date() and now.hour<14 and not intraday):continue
        points.append((d,p))
    def reject(reason):
        if diagnostics is not None:diagnostics['reason']=reason
        return None
    if len(points)<60:return reject('歷史資料不足60日')
    if (now.date()-points[-1][0]).days>7:return reject('行情日期過舊')
    closes=[p[1] for p in points];c=closes[-1]
    ma20=sum(closes[-20:])/20;ma60=sum(closes[-60:])/60
    old20=sum(closes[-25:-5])/20;pressure=max(closes[-21:-1])
    if abs(c/closes[-2]-1)>.05:return reject('單日漲跌超過5%')
    if c/ma20>1.08:return reject('月線乖離超過8%')
    if c/closes[-6]>1.15:return reject('五日漲幅超過15%')
    if c/closes[-21]>1.30:return reject('二十日漲幅超過30%')
    trend=c>ma20>ma60 and ma20>old20
    if not (c>ma20 or c>ma60):return reject('尚未站回均線')
    if not (abs(c/ma20-1)<=.06 or 0<=pressure/c-1<=.05):return reject('距月線或突破位置過遠')
    if diagnostics is not None:diagnostics['reason']='通過技術初篩'
    bucket='breakout' if trend and c>=pressure*.99 else 'pullback' if trend and abs(c/ma20-1)<=.03 else 'recovery'
    return dict(bucket=bucket,rank=(not trend,-(ma20/old20-1),abs(c/pressure-1)))


def technical_pool(metrics, keep, limit=MAX_POOL, sectors=None, price_bands=None):
    buckets=[sorted(((c,m) for c,m in metrics.items() if m['bucket']==b),key=lambda x:(x[1]['rank'],x[0]))
             for b in ('breakout','pullback','recovery')]
    result=list(dict.fromkeys(c for c in keep))[:limit]
    if sectors:
        # Inspect each technical setup across sectors before consuming the cap.
        # This affects research coverage, never forces a buy or sector quota in picks.
        buckets=[[(c,m) for c,m in bucket if sectors.get(c,'未分類')==sector]
                 for bucket in buckets for sector in sorted(set(sectors.values()))]
    if price_bands:
        # The detailed-research cap must not silently erase a nominal-price band.
        # Interleave coverage only; entry/risk and final technical ranking stay unchanged.
        buckets=[[(c,m) for c,m in bucket if price_bands.get(c)==band]
                 for bucket in buckets for band in sorted(set(price_bands.values()))]
    for i in range(max((len(b) for b in buckets),default=0)):
        for bucket in buckets:
            if i<len(bucket) and bucket[i][0] not in result:
                result.append(bucket[i][0])
                if len(result)>=limit:return result
    return result


class ShortTermService:
    def __init__(self, *, connect, store, quote, executor, safe, now, equity, calendar=None,light=None,category=None):
        self.connect=connect; self.store=store; self.quote=quote; self.executor=executor
        self.safe=safe; self.now=now; self.equity=equity
        self.calendar=calendar or (lambda d:d.weekday()<5)
        self.light=light;self.category=category or (lambda code,industry:'未分類')
        self.stock_meta={};self.scan_meta={};self.phase='等待排程';self.progress_total=MAX_POOL
        self.lock=threading.RLock(); self.cached=None; self.loaded=False
        self.running=False; self.started=0.; self.progress=0; self.error=''; self.last_try=0.
        self.cache_mode=None;self.radar_at=None;self.trigger='頁面補檢'

    def intraday(self):
        n=self.now()
        return self.calendar(n.date()) and (9,0)<=(n.hour,n.minute)<(13,35)

    def refresh_from_radar(self, observed_at=None):
        """Called from the existing Radar producer, independent of any viewer."""
        self.radar_at=observed_at or self.now().isoformat()
        self.trigger='雷達快照排程'
        return self.payload(refresh=True)

    def inputs(self, keep):
        # The catalogue defines the universe. Institution activity never chooses
        # which symbols get a technical look. Do not hold a DB connection over HTTP.
        conn=self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SET LOCAL statement_timeout=12000')
                cur.execute("SELECT code,name,industry,market FROM stock_info WHERE code ~ '^[0-9]{4}$' ORDER BY code")
                raw=cur.fetchall()
            conn.commit()
        finally:
            conn.close()
        self.stock_meta={str(c):dict(name=str(n or c),industry=i,market=str(m or ''),sector=self.category(str(c),i))
                         for c,n,i,m in raw if self.equity(str(c)) and str(m or '') not in ('興櫃','兴柜','emerging')}
        for row in keep:
            if self.equity(str(row['code'])):
                self.stock_meta.setdefault(row['code'],dict(name=row['name'],industry='',market='',sector='未分類'))
        universe=sorted(self.stock_meta)
        if not universe or not self.light:
            raise RuntimeError('全市場股票清單或技術行情來源尚未就緒')
        markets={};sectors={}
        for info in self.stock_meta.values():
            m=info['market'] or '未確認';markets[m]=markets.get(m,0)+1
            c=info['sector'];sectors[c]=sectors.get(c,0)+1
        metrics={};quote_count=0;dates=[];coverage={};price_by_code={};high_examples=[]
        with self.lock:
            self.phase='全市場技術初篩';self.progress=0;self.progress_total=len(universe)
            self.scan_meta=dict(universe_count=len(universe),markets=markets,sectors=sectors)
        for offset in range(0,len(universe),LIGHT_BATCH):
            if time.monotonic()>self.deadline:
                raise RuntimeError('全市場技術行情回應逾時；保留原結果，稍後可重試')
            if not self.safe(reserve_mb=96):
                raise RuntimeError('全市場初篩因記憶體餘裕不足暫停')
            batch=universe[offset:offset+LIGHT_BATCH]
            quotes=self.light(batch,{c:self.stock_meta[c]['market'] for c in batch}) or {}
            for code in batch:
                q=quotes.get(code) or {}
                if q:
                    quote_count+=1
                    for ts in reversed(q.get('timestamps') or []):
                        if not ts:continue
                        d=datetime.fromtimestamp(int(ts),timezone(timedelta(hours=8))).date()
                        if d<=self.now().date() and not(d==self.now().date() and self.now().hour<14 and not self.intraday()):
                            dates.append(day(d));break
                diagnostic={}
                metric=light_technical(q,self.now(),intraday=self.intraday(),diagnostics=diagnostic)
                if metric:metrics[code]=metric
                px=next((number(v) for v in reversed(q.get('closes') or []) if number(v) and number(v)>0),None)
                if px:
                    band=price_band(px);price_by_code[code]=band
                    counter=coverage.setdefault(band,dict(quoted=0,technical=0,deep=0,qualified=0))
                    counter['quoted']+=1;counter['technical']+=bool(metric)
                    if px>=500 and len(high_examples)<20:
                        high_examples.append(dict(code=code,name=self.stock_meta[code]['name'],price=round(px,2),reason=diagnostic.get('reason','資料待確認')))
            with self.lock:self.progress=min(offset+len(batch),len(universe))
            print('短線全市場技術初篩 %s/%s'%(self.progress,len(universe)),flush=True)
        if not quote_count:raise RuntimeError('全市場技術行情暫時無法取得，沒有使用法人買超名單替代')
        codes=technical_pool(metrics,[r['code'] for r in keep if r['code'] in self.stock_meta],
                             sectors={c:i['sector'] for c,i in self.stock_meta.items()},price_bands=price_by_code)
        names={c:self.stock_meta[c]['name'] for c in codes};history={c:[] for c in codes}
        with self.lock:
            self.scan_meta.update(light_scanned=len(universe),light_quotes=quote_count,
                light_missing=len(universe)-quote_count,technical_candidates=len(metrics),
                market_date=max(dates,default=None),deep_total=len(codes))
            self.scan_meta['deep_sectors']={s:sum(self.stock_meta[c]['sector']==s for c in codes)
                                           for s in sectors}
            for code in codes:
                if code in price_by_code:coverage[price_by_code[code]]['deep']+=1
            self.scan_meta.update(price_coverage=coverage,high_price_examples=high_examples)
            self.phase='技術詳查';self.progress=0;self.progress_total=len(codes)
        conn=None
        try:
            if codes:
                conn=self.connect()
                with conn.cursor() as cur:
                    cur.execute('SET LOCAL statement_timeout=12000')
                    cur.execute('SELECT code,trade_date,foreign_net_lots,trust_net_lots\n                        FROM inst_history WHERE code=ANY(%s) AND trade_date BETWEEN %s AND %s\n                        ORDER BY code,trade_date',(codes,self.now().date()-timedelta(days=240),self.now().date()))
                    for c,d,f,t in cur.fetchall():
                        history[str(c)].append(dict(date=day(d),foreign=number(f),trust=number(t)))
                conn.commit()
        except Exception as exc:
            print('短線法人輔助資料暫缺：'+type(exc).__name__,flush=True)
            history={c:[] for c in codes}
        finally:
            if conn:conn.close()
        return codes,names,history

    def build(self):
        try:
            intraday=self.intraday()
            snapshot_key=KEY+(':intraday' if intraday else '')
            deadline=time.monotonic()+120
            self.deadline=deadline
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
                    market=(self.stock_meta.get(code) or {}).get('market','').lower()
                    suffix='.TW' if market in ('上市','twse','listed','tw') else '.TWO' if market in ('上櫃','上柜','tpex','otc','two') else None
                    q=self.quote(code,rng='6mo',include_mis=False,request_timeout=5,market_suffix=suffix,
                                 force_refresh=intraday)
                    if not q:
                        failures+=1
                        excluded['行情來源暫缺']=excluded.get('行情來源暫缺',0)+1
                        continue
                    observed=number(q.get('market_observed_at'))
                    # A recent fetch is not proof of a recent trade. Require the
                    # source's market timestamp before exposing a live signal.
                    live_fresh=bool(observed and -60<=(self.now().timestamp()-observed)<=1200 and
                        datetime.fromtimestamp(observed,timezone(timedelta(hours=8))).date()==self.now().date())
                    bars=normalize_bars(q,self.now(),include_current=intraday and live_fresh)
                    if bars:
                        quote_dates.append(bars[-1]['date'])
                    row=analyse(code,names[code],q,history[code],self.now(),intraday=intraday and live_fresh)
                    if row:
                        row['sector']=(self.stock_meta.get(code) or {}).get('sector','未分類')
                        row['market']=(self.stock_meta.get(code) or {}).get('market','未確認')
                        row['observed_at']=datetime.fromtimestamp(observed,timezone(timedelta(hours=8))).isoformat() if observed else None
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
                                    len(r.get('pending',[])),r.get('technical_priority',(True,0,0)),r['code']))
            # Preserve observed events in stored snapshots instead of retrospectively
            # replacing their reasons/prices when upstream history is revised.
            old={s['id']:s for r in previous.get('rows',[]) for s in r.get('signals',[])}
            for row in rows:
                row['signals']=[(old.get(s['id']) if not s.get('provisional') and not (old.get(s['id']) or {}).get('provisional') else None)
                                or dict(s,observed_at=self.now().isoformat()) for s in row['signals']]
            self.scan_meta['qualified_sectors']={s:sum(r.get('sector')==s for r in rows)
                                                for s in {r.get('sector') for r in rows}}
            for row in rows:
                band=price_band(number(row.get('price')) or 0)
                counter=self.scan_meta.get('price_coverage',{}).get(band)
                if counter is not None:counter['qualified']+=1
            payload=dict(version=VERSION,selection_version=SELECTION_VERSION,
                         rows=rows[:MAX_PICKS],all_rows=rows,scanned=len(codes),failed=failures,
                         intraday=intraday,trigger=self.trigger,radar_at=self.radar_at,
                         date=max(quote_dates,default=self.scan_meta.get('market_date')),excluded=excluded,scope=dict(self.scan_meta),
                         alerts=latest_alerts(rows,max(quote_dates,default=None)),
                         generated_at=self.now().isoformat(),
                         note='日 K 規則試行・歷史箭頭為規則重播，尚未宣稱回測績效；不含基本面、不送出交易。')
            if not self.store.save(snapshot_key,payload,data_date=payload['date'],source_meta={'version':VERSION,'intraday':intraday}):
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

    def payload(self, refresh=False, sector=None, position=None):
        with self.lock:
            intraday=self.intraday()
            if not self.loaded or (self.cache_mode is not None and self.cache_mode!=intraday and not self.running):
                saved=self.store.load(KEY+(':intraday' if intraday else ''),max_age_seconds=30*86400)
                self.cached=saved.get('payload') if saved else None
                self.loaded=True;self.cache_mode=intraday
            cached=self.cached
            generated=(cached or {}).get('generated_at','')
            try:
                age=(self.now()-datetime.fromisoformat(generated)).total_seconds()
            except (ValueError,TypeError):
                age=999999
            stale=(age>(600 if intraday else 1800) or generated[:10]!=day(self.now()) or
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
            expected=self.now().date()
            if self.now().hour<14 and not intraday:
                expected-=timedelta(days=1)
            for _ in range(20):
                if self.calendar(expected):break
                expected-=timedelta(days=1)
            signals_current=bool((cached or {}).get('date')==day(expected) and
                (cached or {}).get('selection_version')==SELECTION_VERSION and not self.error and
                (not intraday or ((cached or {}).get('intraday') and age<=1200)))
            rows=[]
            for row in (cached or {}).get('all_rows',(cached or {}).get('rows',[])):
                row=dict(row,spreads=trade_spreads(row.get('signals',[]),row.get('price'),row.get('date')))
                try:quote_age=(self.now()-datetime.fromisoformat(row.get('observed_at') or '')).total_seconds()
                except (ValueError,TypeError):quote_age=999999
                if signals_current and row.get('date')==day(expected) and (not intraday or -60<=quote_age<=1200):
                    rows.append(row)
                else:
                    rows.append(dict(row,state='資料待更新，非最新訊號',current_action='unconfirmed',
                        action_note='行情截至 '+str(row.get('date') or '未確認')+'；尚未確認最新交易日的訊號，不沿用舊買點。',
                        entry_ready=False,latest_event=None,latest_event_date=None))
            alerts=[]
            for alert in (cached or {}).get('alerts',[]) if signals_current else []:
                row=next((r for r in rows if r['code']==alert['code']),None)
                spread=alert.get('spread')
                if spread is None and row and alert['type']=='exit':
                    spread=next((t['spread'] for t in reversed(row['spreads']['completed']) if t['exit_date']==alert['date']),None)
                alerts.append(dict(alert,spread=spread))
            alerts=[a for a in alerts if any(r['code']==a['code'] and r.get('current_action')!='unconfirmed' for r in rows)]
            if sector=='電子':rows=[r for r in rows if r.get('sector')=='電子']
            elif sector=='非電子':rows=[r for r in rows if r.get('sector')!='電子']
            position_counts={g:sum(position_group(r)==g for r in rows) for g in ('held','waiting','closed','unknown')}
            for row in rows:row['position_group']=position_group(row)
            if position in position_counts:rows=[r for r in rows if r['position_group']==position]
            if sector in ('電子','非電子'):alerts=[a for a in alerts if any(r['code']==a['code'] for r in rows)]
            return dict(ok=True,rows=rows[:MAX_PICKS],alerts=alerts,meta=dict(
                position_counts=position_counts,
                intraday=intraday,refresh_interval_seconds=600,
                next_refresh_at=(datetime.fromisoformat(generated)+timedelta(seconds=600)).isoformat() if generated and intraday else None,
                trigger=(cached or {}).get('trigger'),radar_at=(cached or {}).get('radar_at'),
                version=VERSION,building=self.running,progress=self.progress,pool_limit=MAX_POOL,
                phase=self.phase,progress_total=self.progress_total,
                scope=dict(self.scan_meta) if self.running else (cached or {}).get('scope',{}),
                signals_current=signals_current,expected_date=day(expected),
                date=(cached or {}).get('date'),generated_at=generated,stale=stale,
                scanned=(cached or {}).get('scanned',0),failed=(cached or {}).get('failed',0),
                excluded=(cached or {}).get('excluded',{}),
                error=self.error,note=(cached or {}).get('note','全市場技術初篩・量價與風險決定進場，法人僅供參考；最多顯示 6 檔。')))


def create_blueprint(*, login, service):
    bp=Blueprint('short_term',__name__)
    @bp.route('/web/api/workbench/short-term')
    @login
    def short_term(uid):
        try:
            result=service.payload(request.args.get('refresh')=='1',sector=request.args.get('sector'),position=request.args.get('position'))
            response=jsonify(result)
        except Exception:
            response=jsonify(ok=False,error='短線研究暫時無法載入，請稍後重試')
            response.status_code=503
        response.headers['Cache-Control']='no-store'
        return response
    return bp
