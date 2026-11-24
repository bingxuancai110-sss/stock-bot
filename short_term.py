"""Pattern/technical strategies with optional institution annotations. No orders or LINE.

All replay decisions use an expanding prefix and institutional observations strictly
before the bar date (daily reports are unavailable at that day's closing auction).
These are model signals, never asserted fills or measured investment performance.
"""
from datetime import date, datetime, timedelta, timezone
import math
import copy
import threading
import time
from flask import Blueprint, jsonify, request
from technical_space import long_space

VERSION = 'short-wave-v10'
KEY = 'short_term:' + VERSION
MAX_POOL = 24
MAX_PICKS = 20
SELECTION_VERSION = 15
LIGHT_BATCH = 64


def position_group(row):
    """Confirmed model state; an unfinished entry/exit cannot change this state."""
    if row.get('current_action')=='unconfirmed':return 'unknown'
    confirmed=[e for e in row.get('signals',[]) if not e.get('provisional') and e.get('type') in ('entry','exit')]
    if confirmed:return 'held' if confirmed[-1]['type']=='entry' else 'closed'
    return 'held' if row.get('active') and not row.get('provisional') else 'waiting'


def price_band(price):
    return '1000以上' if price>=1000 else '500–999' if price>=500 else '100–499' if price>=100 else '低於100'


def clarity_rank(row):
    stage=row.get('signal_stage','watch')
    # JSON persistence turns tuples into lists. A partial market pass mixes
    # restored rows with newly analysed rows, so use one comparable key type.
    priority=tuple(row.get('technical_priority') or (True,0,0))
    action=row.get('current_action')
    urgency={'exit':0,'reduce':1,'entry':2,'add':3}.get(action,4 if row.get('active') else 5)
    return (action=='unconfirmed',urgency,bool(row.get('provisional')),
            -{'entry':3,'triggered':2,'watch':1}.get(stage,0),
            any('風險' in str(p) for p in row.get('pending',[])),
            not row.get('trend',False),len(row.get('pending',[])),
            priority[1:],row['code'])


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def signal_allocations(signals):
    """Explain recorded model units relative to each entry, never account assets."""
    result=[];initial=None;balance=None;reduced=0
    for source in signals:
        prior=(initial,balance,reduced)
        event=dict(source);event.pop('allocation',None)
        units=number(event.get('units'));kind=event.get('type')
        if kind=='entry':
            initial=units if units and units>0 else None
            balance=0 if initial else None;reduced=0
        before=balance
        if units and units>0 and initial and balance is not None:
            if kind in ('entry','add'):
                after=balance+units
                restoring=kind=='add' and reduced>0 and units<=reduced+1e-8
                label='首次進場 100%' if kind=='entry' else '回補前次減碼股數（首次股數的 %g%%）'%(units/initial*100) if restoring else '加碼首次股數的 %g%%'%(units/initial*100)
                if restoring:reduced=max(0,reduced-units)
            elif kind in ('reduce','exit') and balance>0 and units<=balance+1e-8 and (kind!='exit' or abs(units-balance)<1e-8):
                after=max(0,balance-units)
                label='出清剩餘全部' if kind=='exit' else '減碼當時股數的 %g%%'%(units/balance*100)
                if kind=='reduce':reduced+=units
            else:
                after=None
            if after is not None:
                event['allocation']=dict(label=label,before_pct=round(before/initial*100,4),
                    after_pct=round(after/initial*100,4),change_pct=round(units/initial*100,4),
                    basis='首次進場股數為100%，不是總資產比例')
                if not event.get('provisional'):balance=after
        if event.get('allocation') is None:balance=None
        if event.get('provisional'):initial,balance,reduced=prior
        result.append(event)
    return result


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


def trade_spreads(signals, close=None, asof=None, direction="long"):
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
                                  spread=round((opened['entry_price']-price) if direction=='short' else (price-opened['entry_price']),2)))
            opened=None
        # Adds/reductions intentionally do not alter this simple price difference.
    if opened and number(close) is not None:
        opened=dict(opened,date=asof,price=number(close),spread=round((opened['entry_price']-number(close)) if direction=='short' else (number(close)-opened['entry_price']),2))
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
                    trades=trade_spreads(row.get('signals',[]),direction=row.get('direction','long'))['completed']
                    spread=next((t['spread'] for t in reversed(trades) if t['exit_date']==asof),None)
                alerts.append(dict(event,code=row['code'],name=row['name'],spread=spread,direction=row.get('direction','long')))
    order={'entry':0,'exit':1,'reduce':2,'add':3}
    return sorted(alerts,key=lambda a:(order.get(a['type'],4),a['code']))


def normalize_bars(quote, now, include_current=False):
    """Reject broken OHLC, duplicates and unfinished current daily bars."""
    keys = ('close_dates', 'opens', 'highs', 'lows', 'closes', 'volumes')
    arrays = [quote.get(k) or [] for k in keys]
    if not arrays[0] or len({len(a) for a in arrays}) != 1:
        return []
    bars = [];aligned_adjs=[]
    adjs=quote.get('adj_closes') or []
    for source_index,values in enumerate(zip(*arrays)):
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
        if v==0:continue  # no transaction: not a daily trading bar or confirmation session
        bars.append(dict(date=d, open=o, high=h, low=l, close=c, volume=v))
        aligned_adjs.append(number(adjs[source_index]) if len(adjs)==len(arrays[0]) else None)
    # Do not interpret corporate-action price scaling as a stop or breakout.
    # Retain only a fresh segment after a large raw-price discontinuity. There
    # must still be 65 complete bars before a candidate can be published.
    cut = 0
    for i in range(1, len(bars)):
        if abs(bars[i]['close']/bars[i-1]['close']-1) > .25:
            cut = i
        if len(aligned_adjs)==len(bars):
            a,b=aligned_adjs[i-1],aligned_adjs[i]
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
    resistance = min(pivots) if pivots else (resistances[0] if resistances else None)
    avg_vol = sum(b['volume'] for b in prev[-20:])/20
    avg_turnover = sum(b['close']*b['volume'] for b in prev[-20:])/20
    low_pivots=[i for i in range(max(2,len(prev)-20),len(prev)-2)
                if prev[i]['low']<=min(z['low'] for z in prev[i-2:i+3])
                and prev[i]['low']<max(z['low'] for z in prev[i-2:i+3])]
    swing_floor=prev[low_pivots[-1]]['low'] if low_pivots else min(z['low'] for z in prev[-10:])
    return dict(ma10=ma10, ma20=ma20, ma60=ma60, atr=atr,
                ma20_previous=sum(z['close'] for z in prev[-20:])/20,swing_floor=swing_floor,
                trend_slope=ma20/old20-1,
                trend=last['close'] > ma20 > ma60 and ma20 > old20,
                recovering=last['close'] > ma20 or last['close'] > ma60,
                pressure=pressure, support=support, resistance=resistance,
                volume_ratio=last['volume']/avg_vol if avg_vol else 0,
                avg_volume=avg_vol, avg_turnover=avg_turnover,
                change=(last['close']/prev[-1]['close']-1)*100)


def standing_confirmation(bars, level, slope=0):
    """Two closes above the same known boundary and MA20, with a noise buffer.

    Do not keep calling a weeks-old break a fresh reversal. The current boundary
    is projected back, never refitted separately to make yesterday pass.
    """
    ranges=[max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close']))
            for a,b in zip(bars[-15:-1],bars[-14:])]
    buffer=max(bars[-1]['close']*.005, sum(ranges)/14*.25)
    above=[]
    for lag in range(5, -1, -1):
        end=len(bars)-lag
        close=bars[end-1]['close']
        above.append(close > level-slope*lag+buffer)
    recent_cross=any(not above[i-1] and above[i] for i in range(1,len(above)))
    ma_now=sum(b['close'] for b in bars[-20:])/20
    ma_prev=sum(b['close'] for b in bars[-21:-1])/20
    reclaimed=bars[-1]['close']>ma_now and bars[-2]['close']>ma_prev
    # Close-only preselection must remain a watch, never fake volume evidence.
    traded=all((number(b.get('volume')) or 0)>0 for b in bars[-2:])
    return dict(confirmed=bool(recent_cross and all(above[-2:]) and reclaimed and traded),
                buffer=buffer, reclaimed=reclaimed, standing=all(above[-2:]),
                recent_cross=recent_cross, crossed=above[-1] and recent_cross)


def technical_shapes(bars, x, ratio):
    """Independent causal pattern families, using only confirmed prior pivots."""
    n=len(bars);b=bars[-1];prev=bars[:-1];c=b['close']
    highs=[i for i in range(max(2,n-60),n-3)
           if prev[i]['high']>=max(z['high'] for z in prev[i-2:i+3])
           and prev[i]['high']>min(z['high'] for z in prev[i-2:i+3])]
    lows=[i for i in range(max(2,n-60),n-3)
          if prev[i]['low']<=min(z['low'] for z in prev[i-2:i+3])
          and prev[i]['low']<max(z['low'] for z in prev[i-2:i+3])]
    line=None;reversal=False;near_line=False;neckline=None;confirmation=None
    if highs:
        j=highs[-1]
        i=next((i for i in reversed(highs[:-1]) if j-i>=3 and prev[j]['high']<prev[i]['high']*.995),None)
        if i is not None:
            slope=(prev[j]['high']-prev[i]['high'])/(j-i)
            level=prev[j]['high']+slope*(n-1-j)
            if level>0:
                line=dict(start_date=prev[i]['date'],end_date=b['date'],start_price=prev[i]['high'],end_price=level,
                          confirmed_date=bars[j+2]['date'])
                near_line=abs(c/level-1)<=max(.05,1.5*x.get('atr',0)/c)
                confirmation=standing_confirmation(bars,level,slope)
                # A resistance line must not cut through intervening swing highs.
                valid_line=all(prev[k]['high']<=prev[i]['high']+slope*(k-i)+max(x.get('atr',0)*.25,prev[k]['high']*.005)
                               for k in highs if i<k<j)
                reversal=valid_line and confirmation['crossed'] and b['low']>=min(z['low'] for z in prev[-10:])*.995
    bottom=False
    if len(lows)>=2:
        i,j=lows[-2:]
        if j-i>=4 and prev[j]['low']>prev[i]['low']*1.005 and (x['ma20']<=x['ma60'] or x['trend_slope']<=0):
            neckline=max(z['high'] for z in prev[i:j+1])
            bottom_check=standing_confirmation(bars,neckline)
            bottom=bottom_check['crossed']
            if bottom:confirmation=bottom_check
    strong=c>=b['low']+.6*(b['high']-b['low'])
    recovered=c>x['ma20'] and (x['trend_slope']>0 or c>max(z['high'] for z in prev[-10:]))
    consolidation=(max(z['high'] for z in prev[-20:])-min(z['low'] for z in prev[-20:]))/c<=max(.16,min(.24,4*x.get('atr',0)/c))
    fixed_pressure=max(z['high'] for z in bars[-22:-2])
    price_confirmation=standing_confirmation(bars,fixed_pressure)
    confirmed_breakout=bool(price_confirmation['confirmed'])
    breakout=consolidation and recovered and strong and ((c>x['pressure'] and ratio>=1.2) or confirmed_breakout)
    breakout_level=fixed_pressure if confirmed_breakout else x['pressure']
    pullback=x['trend'] and b['low']<=x['ma20']+max(x['ma20']*.015,.5*x.get('atr',0)) and c>prev[-1]['high'] and c>x['ma20']
    base_high=max(z['high'] for z in prev[-5:]);base_low=min(z['low'] for z in prev[-5:])
    preceding_run=prev[-6]['close']/min(z['low'] for z in prev[-21:-5])-1
    base=(base_high-base_low)/c<=max(.08,min(.16,2*x.get('atr',0)/c)) and base_low>=x['ma20']*.97
    continuation=preceding_run>=.08 and base and recovered and c>base_high and strong and (ratio>=1.1 or c>base_high+max(.5*x.get('atr',0),c*.005))
    reversal=(reversal or bottom) and strong and (ratio>=1.05 or bool(confirmation and confirmation['confirmed']))
    triggers={'reversal':reversal,'breakout':breakout,'pullback':pullback,'continuation':continuation}
    watches={'reversal':near_line or (neckline is not None and abs(c/neckline-1)<=.05),
             'breakout':recovered and 0<=x['pressure']/c-1<=.05,
             'pullback':x['trend'] and abs(c/x['ma20']-1)<=.06,
             'continuation':preceding_run>=.08 and base and recovered}
    labels={'reversal':'底部轉強' if bottom else '下降線突破','breakout':'突破確認','pullback':'回測轉強','continuation':'強勢整理再突破'}
    families=('reversal','continuation','breakout','pullback')
    # Each family stands on its own. A tentative reversal must not veto an
    # independently confirmed continuation/breakout/pullback on the same bar.
    kind=next((k for k in families if triggers[k] and
        (k!='reversal' or confirmation and confirmation['confirmed'])),None)
    if kind is None:kind=next((k for k in families if triggers[k]),None)
    watching=[k for k,v in watches.items() if v or triggers[k]]
    if x['trend'] and not watching:watching=['continuation']
    if not watching and c<x['ma20'] and any(sum(z['close'] for z in bars[k-19:k+1])/20>sum(z['close'] for z in bars[k-59:k+1])/60 for k in range(max(59,n-20),n-1)):
        watching=['repair']
    chosen=kind or (watching[0] if watching else None)
    candidates=[]
    for family in families:
        if not triggers[family]:continue
        level=(neckline if bottom else line['end_price'] if line else prev[-1]['high']) if family=='reversal' else base_high if family=='continuation' else breakout_level if family=='breakout' else prev[-1]['high']
        invalidation=prev[lows[-1]]['low'] if family=='reversal' and lows else base_low if family=='continuation' else min(z['low'] for z in prev[-3:]) if family=='pullback' else min(z['low'] for z in prev[-10:])
        candidates.append(dict(kind=family,trigger=True,pattern=labels[family],confirmed=family!='reversal' or bool(confirmation and confirmation['confirmed']),
                               confirmation=confirmation if family=='reversal' else None,confirmation_method=('連2日站穩同一壓力與20日線' if family=='breakout' and confirmed_breakout else '收盤放量突破20日壓力' if family=='breakout' else '連2日站穩下降線與20日線' if family=='reversal' else '回測支撐後收復前日高點' if family=='pullback' else '整理後有效突破箱頂'),line=line if family=='reversal' and not bottom else None,level=level,invalidation=invalidation))
    return dict(candidates=candidates,kind=kind,watching=watching,trigger=bool(kind),pattern=labels.get(kind,'等待型態'),
                confirmed=chosen!='reversal' or bool(confirmation and confirmation['confirmed']),
                confirmation=confirmation if chosen=='reversal' else None,
                invalidation=(prev[lows[-1]]['low'] if chosen=='reversal' and lows else min(z['low'] for z in prev[-3:]) if chosen=='pullback' else base_low if chosen=='continuation' else min(z['low'] for z in prev[-10:])),
                line=line,level=(neckline if bottom or (chosen=='reversal' and not line and neckline) else
                      line['end_price'] if chosen=='reversal' and line else base_high if chosen=='continuation' else
                      breakout_level if chosen=='breakout' else prev[-1]['high']))


def setup(bars, chip, volume_fraction=1):
    x=levels(bars);b=bars[-1];c=b['close']
    x['volume_ratio']/=max(15/270,min(1,volume_fraction))
    hot=abs(x['change'])>9.5
    high_volatility=x['atr']/c>.06
    shapes=technical_shapes(bars,x,x['volume_ratio'])
    space=long_space(bars,c,x['atr'])
    risk_limit=max(.06,min(.12,2*x['atr']/c))
    vol5=sum(z['volume'] for z in bars[-6:-1])/1000
    ratio=((chip.get('foreign',0)+chip.get('trust',0))/vol5) if vol5 else 0
    def plan_for(shape):
        base_days=5 if shape['kind']=='continuation' else 10
        anchor=shape['invalidation'] if shape['kind'] in ('reversal','pullback','continuation') else min(z['low'] for z in bars[:-1][-base_days:])
        soft=anchor-.25*x['atr']
        floor=anchor if shape['kind'] in ('reversal','pullback','continuation') else x['swing_floor']
        stop=min(soft-.5*x['atr'],floor-.75*x['atr']);risk=c-stop
        target=space['resistance'] or c+2*risk
        rr=(target-c)/risk if risk>0 else 0
        trend_following=shape['kind'] in ('breakout','pullback','continuation') and c>x['ma20'] and x['ma20']>=x['ma20_previous']
        distance_ok=0<risk/c<=risk_limit
        risk_ok=distance_ok and (trend_following or rr>=1.5)
        valid=not hot and bool(shape['watching'] or shape['trigger']) and b['volume']>0
        return dict(x,resistance=space['resistance'],stop=stop,soft_support=soft,target=target,target_kind=space['kind'],space=space,
                    risk=risk,rr=rr,risk_policy='trend_following' if trend_following else 'turning_space',distance_ok=distance_ok,hot=hot,high_volatility=high_volatility,liquid=x['avg_turnover']>=10_000_000,
                    risk_limit=risk_limit,chip_ratio=ratio*100,eligible=valid and risk_ok,risk_ok=risk_ok,
                    trigger=shape['trigger'],watchable=b['volume']>0 and bool(shape['watching']),
                    entry=shape['trigger'] and shape['confirmed'] and valid and risk_ok,pattern=shape['pattern'],
                    entry_confirmed=shape['confirmed'],confirmation=shape['confirmation'],confirmation_method=shape.get('confirmation_method','型態尚待確認'),
                    strategy=shape['kind'] or (shape['watching'][0] if shape['watching'] else None),
                    patterns=shape['watching'],pattern_line=shape['line'],pattern_level=shape['level'])
    plans=[plan_for(dict(shapes,**candidate)) for candidate in shapes.get('candidates',[])] or [plan_for(shapes)]
    selected=next((p for p in plans if p['entry']),None)
    if selected is None:selected=next((p for p in plans if p['trigger'] and p['entry_confirmed']),plans[0])
    selected['family_checks']=[dict(strategy=p['strategy'],pattern=p['pattern'],trigger=p['trigger'],entry=p['entry'],
                                    confirmed=p['entry_confirmed'],risk_policy=p['risk_policy'],stop=round(p['stop'],2),rr=round(p['rr'],2),risk_pct=round(p['risk']/c*100,2)) for p in plans]
    return selected


def analyse(code, name, quote, history, now, intraday=False):
    bars = normalize_bars(quote or {}, now, include_current=intraday)
    if len(bars) < 65 or (now.date()-date.fromisoformat(bars[-1]['date'])).days > 7:
        return None
    signals = []; trails = []; position = None; latest = None; chip = None
    watch_dates={};trigger_dates={};previous_patterns=set();previous_trigger=None
    dates = [b['date'] for b in bars]
    for i in range(64, len(bars)):
        prefix = bars[:i+1]; b = bars[i]
        chip = chip_at(history, b['date'], dates[:i+1])
        fraction=1
        if intraday and b['date']==day(now):
            fraction=((now.hour-9)*60+now.minute)/270
        plan = setup(prefix, chip, volume_fraction=fraction); latest = plan
        current_patterns=set(plan['patterns'])
        for k in current_patterns-previous_patterns:
            watch_dates[k]=b['date'] if i>64 else None
        if plan['trigger'] and plan['strategy']!=previous_trigger:
            trigger_dates[plan['strategy']]=b['date'] if i>64 else None
        previous_patterns=current_patterns
        previous_trigger=plan['strategy'] if plan['trigger'] else None
        def emit(kind, price, reason, stop=None, target=None, units=None):
            if kind=='entry':
                reason+='；確認方式 '+str(plan.get('confirmation_method') or '依事前型態')
                reason+='；波段追蹤：首道壓力作減碼觀察，不是固定賣出終點' if plan.get('risk_policy')=='trend_following' else '；轉折初期須有至少1.5R空間'
                reason+=('；價格 %.2f，MA20 %.2f／MA60 %.2f；%s %.2f，量比 %.2f，初始停損 %.2f，報酬風險比 %.2fR' %
                         (b['close'],plan['ma20'],plan['ma60'],
                          plan['pattern'],plan['pattern_level'],
                          plan['volume_ratio'],plan['stop'],plan['rr']))
                if plan['strategy']=='reversal':
                    reason+='；連續2日收在同一突破線＋緩衝之上，且兩日均站回20日線；緩衝 %.2f' % plan['confirmation']['buffer']
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
                         units=units, origin='replay',strategy=(position or {}).get('strategy') or plan['strategy'],pattern=plan['pattern'],
                         pattern_line=plan['pattern_line'])
            signals.append(event)
        if position:
            p = position
            # The line used for today's touch was known after yesterday's close.
            if b['low'] <= p['stop']:
                emit('exit', min(b['open'],p['stop']), '觸及事前設定的硬停損；跳空按較低開盤價標示參考，非成交保證',
                     p['stop'],p['target'],p['units'])
                trails.append(dict(date=b['date'],value=round(p['stop'],2)))
                position = None
                continue  # no same-bar re-entry or optimistic stop/target sequencing
            broken=b['close']<p['soft_support'] and b['close']<plan['ma20']
            p['trend_breaks']=p['trend_breaks']+1 if broken else 0
            if p['trend_breaks']>=2 and plan['ma20']<=plan['ma20_previous']:
                emit('exit',b['close'],'趨勢反轉確認：連續兩日收盤跌破事前趨勢支撐與20日線，且20日線不再上升',p['stop'],p['target'],p['units'])
                trails.append(dict(date=b['date'],value=round(p['stop'],2),support=round(p['soft_support'],2)))
                position=None;continue
            upper = b['high']-max(b['open'],b['close'])
            rejection = (b['high'] >= p['target'] and
                         (b['close'] < b['open'] or upper > .45*(b['high']-b['low'])))
            previous_ma10=sum(z['close'] for z in prefix[-11:-1])/10
            technical_weak=(b['close']<p['soft_support'] and b['close']<plan['ma10']) or (
                b['close']<plan['ma20'] and prefix[-2]['close']<plan['ma20_previous'])
            weak=technical_weak
            if not p['reduced'] and ((rejection and b['close']<plan['ma10']) or weak):
                units = p['units']/2;p['cost']*=.5;p['units']-=units;p['reduced']=True;p['reduced_units']=units
                reason=('壓力受阻且跌破十日線，先減碼一半，保留其餘部位追蹤趨勢' if rejection else
                        '短期轉弱／收盤跌破趨勢支撐，先減碼一半；尚未確認反轉，不直接出清')
                emit('reduce',b['close'],reason,p['stop'],p['target'],units)
            elif (((not p['added'] and not p['reduced'] and b['close']>=p['entry']+p['risk']) or
                   (p['reduced'] and not p['restored'] and b['close']>p['soft_support'])) and plan['eligible'] and
                  b['close'] > max(z['high'] for z in prefix[-11:-1]) and
                  b['close'] > p['entry']*1.01 and plan['volume_ratio'] >= 1.25):
                restoring=p['reduced'];add_units=p['reduced_units'] if restoring else .5
                added_risk = (b['close']-p['stop'])*add_units
                existing_risk = max(0,p['cost']-p['units']*p['stop'])
                if existing_risk+added_risk <= p['risk']:
                    p['units']+=add_units;p['cost']+=add_units*b['close']
                    if restoring:p['restored']=True;p['reduced']=False
                    else:p['added']=True
                    emit('add',b['close'],'減碼後收復趨勢支撐、再次突破且風險受控，回補減碼部位' if restoring else
                         '獲利達1R後再次放量突破；技術與風險成立，加碼初始部位一半',p['stop'],p['target'],add_units)
            # Structural trailing stop only ratchets upward, effective next session.
            new_stop = max(p['stop'],min(plan['swing_floor']-.75*plan['atr'],p['soft_support']-.5*plan['atr']))
            if new_stop < b['close']:
                p['stop'] = new_stop
            p['soft_support']=max(p['soft_support'],min(z['low'] for z in prefix[-10:])-.25*plan['atr'])
            if b['close'] > p['target'] and not rejection:
                p['target'] = plan['resistance'] or b['close']+2*(b['close']-p['stop'])
                p['target_kind'] = plan['target_kind']
            trails.append(dict(date=b['date'],value=round(p['stop'],2),support=round(p['soft_support'],2),target=round(p['target'],2)))
        elif plan['entry']:
            position = dict(entry=b['close'],risk=plan['risk'],stop=plan['stop'],
                            target=plan['target'],units=1.,cost=b['close'],added=False,
                            reduced=False,restored=False,reduced_units=0,trend_breaks=0,soft_support=plan['soft_support'],
                            entry_date=b['date'],target_kind=plan['target_kind'],strategy=plan['strategy'])
            emit('entry',b['close'],plan['pattern']+'；趨勢、量價與風險條件成立，法人僅供參考（收盤訊號，非成交保證）',plan['stop'],plan['target'],1)
            trails.append(dict(date=b['date'],value=round(plan['stop'],2)))
    if latest is None:
        return None
    # Existing modeled positions remain visible for exits even if entry filters
    # subsequently fail. Flat overextended/illiquid names never become picks.
    recent = [s for s in signals if s['date'] >= bars[-3]['date']]
    if not position and not latest['watchable'] and not any(s['type']=='exit' for s in recent):
        return None
    observation=max(latest['ma20'] if 'repair' in latest['patterns'] else latest['pattern_level'],bars[-1]['close'])
    preview_risk=observation-latest['stop']
    preview_space=long_space(bars,observation,latest['atr'])
    preview_pressure=preview_space['resistance']
    preview_target=preview_pressure or observation+2*preview_risk
    preview_rr=(preview_target-observation)/preview_risk if preview_risk>0 else 0
    preview_risk_ok=0<preview_risk/observation<=latest['risk_limit'] and (latest.get('risk_policy')=='trend_following' or preview_rr>=1.5)
    pending=[]
    if latest['hot']:pending.append('單日漲跌逾9.5%，等待下一交易日可交易性確認，不新增部位')
    if 'repair' in latest['patterns']:pending.append('多方修復觀察：尚未收復20日線，不是買點')
    if not latest['patterns']:pending.append('等待型態確認')
    if not (latest['risk_ok'] if position else preview_risk_ok):pending.append('等待風險空間：停損距離 %.1f%%／上限 %.1f%%；至%s %.2fR／%s'%((latest['risk']/bars[-1]['close'] if position else preview_risk/observation)*100,latest['risk_limit']*100,(latest['space'] if position else preview_space)['kind'],latest['rr'] if position else preview_rr,'波段追蹤關卡' if latest.get('risk_policy')=='trend_following' else '至少1.5R'))
    if not latest['trigger']:pending.append('等待突破／回測')
    if latest['strategy']=='reversal' and not latest['entry_confirmed']:
        pending.append('等待連續2日站穩突破線並站回20日線')
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
    checks=[dict(label='追價／波動風險',passed=not latest['hot']),dict(label='型態',passed=bool(latest['patterns'])),
            dict(label='結構風險',passed=latest['risk_ok'] if position else preview_risk_ok),
            dict(label='站穩確認' if latest['strategy']=='reversal' else '進場型態',passed=latest['trigger'] and latest['entry_confirmed'])]
    # Exit candidates remain for three sessions, even though position is now flat.
    return dict(code=code,name=name,observation_price=round(observation,2) if not position else None,family_checks=latest.get('family_checks',[]),space=latest['space'] if position else preview_space,pattern_label=('多方修復觀察' if 'repair' in latest['patterns'] else '多方趨勢觀察' if not latest['trigger'] and not position else None),risk_policy=latest.get('risk_policy'),risk_limit=round(latest['risk_limit']*100,2),date=bars[-1]['date'],price=bars[-1]['close'],provisional=provisional,
                strategy=position['strategy'] if position else latest['strategy'],
                patterns=list(dict.fromkeys(latest['patterns']+([position['strategy']] if position else []))),
                pattern_line=latest['pattern_line'],
                signal_stage='entry' if event and event['type']=='entry' else 'triggered' if latest['trigger'] else 'watch',
                watch_date=max((watch_dates.get(k) or '' for k in latest['patterns']),default='') or None,
                trigger_date=trigger_dates.get(latest['strategy']),
                soft_support=round(position['soft_support'] if position else latest['soft_support'],2),
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
                target_kind=position['target_kind'] if position else preview_space['kind'],support=round(latest['support'],2),
                resistance=round(latest['resistance'],2) if latest['resistance'] else None,
                rr=round(latest['rr'] if position else preview_rr,2), chip=chip,volume_ratio=round(latest['volume_ratio'],2),
                technical_priority=(not latest['trend'],-latest['trend_slope'],
                    min(abs(bars[-1]['close']/latest['ma20']-1),abs(latest['pressure']/bars[-1]['close']-1))),
                chip_ratio=round(latest['chip_ratio'],2) if chip['available'] else None,
                institutional_buying=(chip['foreign']+chip['trust']>0) if chip['available'] else None,
                chip_support='法人同買' if chip['available'] and chip['foreign']+chip['trust']>0 else '',
                liquidity_warning=not latest['liquid'],
                avg_volume=round(latest['avg_volume']),
                avg_turnover=round(latest['avg_turnover']),
                trend=latest['trend'], bars=bars[-120:],signals=signals,trailing=trails,
                source=str(quote.get('source') or 'Yahoo 日線'),volume_estimated=provisional,
                latest_event=event['type'] if event else None)


def technical_audit(code,name,bars):
    """Explain real deep OHLC decisions; neither an order nor a shortlist quota."""
    result=dict(code=code,name=name,price=bars[-1]['close'] if bars else None,
                date=bars[-1]['date'] if bars else None,deep=True,bars=len(bars))
    if len(bars)<65:
        return dict(result,reason='完整日K不足65根／OHLC未通過驗證，未產生訊號')
    from short_side import setup as short_setup
    plans={'long':setup(bars,dict(available=False,foreign=0,trust=0)), 'short':short_setup(bars)}
    messages=[]
    for side,p in plans.items():
        reasons=[]
        if p['hot']:reasons.append('急漲跌或高波動，只觀察')
        if not p['trigger']:reasons.append('進場型態尚未觸發')
        if side=='long' and not p['entry_confirmed']:reasons.append('尚未連2日站穩')
        risk=p['risk']/bars[-1]['close']*100
        if not (0<risk<=p['risk_limit']*100):reasons.append('停損距離不合適')
        if p['rr']<1.5 and p.get('risk_policy')!='trend_following':reasons.append('至支撐壓力不足1.5R')
        if p['entry']:reasons=['本次進場條件成立（重播部位另行判斷）']
        result[side]=dict(watch=p['watchable'],trigger=p['trigger'],entry=p['entry'],
                          risk_policy=p.get('risk_policy'),risk_pct=round(risk,2),risk_limit_pct=round(p['risk_limit']*100,2),
                          rr=round(p['rr'],2),stop=round(p['stop'],2),target=round(p['target'],2),reasons=reasons)
        messages.append(('多方' if side=='long' else '空方')+'：'+'、'.join(reasons)+
                        '；停損距離 %.1f%%／上限 %.1f%%，空間 %.2fR'%(risk,p['risk_limit']*100,p['rr']))
    result['reason']='；'.join(messages)
    return result


def exclusion_reason(bars, history, now):
    if len(bars)<65:
        return '完整日 K 不足'
    if (now.date()-date.fromisoformat(bars[-1]['date'])).days>7:
        return '行情日期過舊'
    chip=chip_at(history,bars[-1]['date'],[b['date'] for b in bars])
    plan=setup(bars,chip)
    if plan['hot']:
        return '過熱或波動過高'
    if not plan['patterns']:
        return '型態尚未成立'
    return '停損距離或壓力空間不合適'


def light_technical(quote, now, intraday=False, diagnostics=None):
    """Price-only streamed preselection; no institution, price or lot-size bias."""
    points=[]
    for ts,px in zip(quote.get('timestamps') or [],quote.get('closes') or []):
        p=number(px)
        if not p or not ts:
            continue
        try:d=datetime.fromtimestamp(int(ts),timezone(timedelta(hours=8))).date()
        except (ValueError,TypeError,OverflowError,OSError):continue
        if d>now.date() or (d==now.date() and now.hour<14 and not intraday):continue
        if points and d<=points[-1][0]:
            if diagnostics is not None:diagnostics['reason']='行情日期重複或順序異常'
            return None
        points.append((d,p))
    def reject(reason):
        if diagnostics is not None:diagnostics['reason']=reason
        return None
    if len(points)<60:return reject('歷史資料不足60日')
    if (now.date()-points[-1][0]).days>7:return reject('行情日期過舊')
    closes=[p[1] for p in points];c=closes[-1]
    ma20=sum(closes[-20:])/20;ma60=sum(closes[-60:])/60
    old20=sum(closes[-25:-5])/20;pressure=max(closes[-21:-1])
    extreme_move=abs(c/closes[-2]-1)>.095  # entry guard lives in the OHLC engine, not observation coverage
    trend=c>ma20>ma60 and ma20>old20
    # Preliminary closes locate shapes; actual OHLC/volume/risk is verified later.
    pseudo=[dict(date=day(d),open=p,high=p,low=p,close=p) for d,p in points]
    shapes=technical_shapes(pseudo,dict(ma20=ma20,ma60=ma60,trend=trend,trend_slope=ma20/old20-1,pressure=pressure),1.3)
    if not shapes['watching']:
        floor=min(closes[-21:-1])
        if c<ma20 and (ma20<ma60 or c<=floor*1.03):
            if diagnostics is not None:diagnostics['reason']='通過空方觀察初篩'+('（急漲跌，非進場）' if extreme_move else '')
            return dict(bucket='short',rank=(not (ma20<ma60),ma20/old20-1,abs(c/floor-1)))
        return reject('尚未接近型態觀察位置')
    if diagnostics is not None:diagnostics['reason']='通過技術觀察初篩'+('（急漲跌，非進場）' if extreme_move else '')
    bucket=shapes['kind'] or shapes['watching'][0]
    return dict(bucket=bucket,rank=(not trend,-(ma20/old20-1),abs(c/pressure-1)))


def technical_pool(metrics, keep, limit=MAX_POOL, sectors=None, price_bands=None):
    buckets=[sorted(((c,m) for c,m in metrics.items() if m['bucket']==b),key=lambda x:(x[1]['rank'],x[0]))
             for b in ('reversal','breakout','pullback','continuation','repair','recovery','short')]
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
    def __init__(self, *, connect, store, quote, executor, safe, now, equity,
                 calendar=None, light=None, category=None, memory_reserve_mb=40):
        self.connect=connect; self.store=store; self.quote=quote; self.executor=executor
        self.safe=safe; self.now=now; self.equity=equity
        self.memory_reserve_mb=max(24,int(memory_reserve_mb))
        self.calendar=calendar or (lambda d:d.weekday()<5)
        self.light=light;self.category=category or (lambda code,industry:'未分類')
        self.stock_meta={};self.scan_meta={};self.phase='等待排程';self.progress_total=MAX_POOL
        self.lock=threading.RLock(); self.cached=None; self.loaded=False
        self.running=False; self.started=0.; self.progress=0; self.error=''; self.last_try=0.
        self.cache_mode=None;self.radar_at=None;self.trigger='頁面補檢';self.full_queue=[]

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
            if not self.safe(reserve_mb=self.memory_reserve_mb):
                raise RuntimeError('全市場初篩因記憶體餘裕不足暫停')
            batch=universe[offset:offset+LIGHT_BATCH]
            quotes=self.light(batch,{c:self.stock_meta[c]['market'] for c in batch}) or {}
            for code in batch:
                q=quotes.get(code) or {}
                if q:
                    quote_count+=1
                    for ts in reversed(q.get('timestamps') or []):
                        if not ts:continue
                        try:d=datetime.fromtimestamp(int(ts),timezone(timedelta(hours=8))).date()
                        except (ValueError,TypeError,OverflowError,OSError):continue
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
        ordered=technical_pool(metrics,[r['code'] for r in keep if r['code'] in self.stock_meta],limit=len(universe),
                             sectors={c:i['sector'] for c,i in self.stock_meta.items()},price_bands=price_by_code)
        self.full_queue=ordered+[c for c in universe if c not in set(ordered)]
        for c,band in price_by_code.items():self.stock_meta[c]['price_band']=band
        codes=self.full_queue[:MAX_POOL]
        names={c:self.stock_meta[c]['name'] for c in codes};history={c:[] for c in codes}
        with self.lock:
            self.scan_meta.update(light_scanned=len(universe),light_quotes=quote_count,
                light_missing=len(universe)-quote_count,technical_candidates=len(metrics),
                market_date=max(dates,default=None),deep_total=len(self.full_queue),deep_done=0,deep_complete=False)
            self.scan_meta['deep_sectors']={s:sum(self.stock_meta[c]['sector']==s for c in codes)
                                           for s in sectors}
            for code in codes:
                if code in price_by_code:coverage[price_by_code[code]]['deep']+=1
            self.scan_meta.update(price_coverage=coverage,high_price_examples=high_examples)
            self.phase='技術詳查';self.progress=0;self.progress_total=len(codes)
        return codes,names,self.histories(codes)

    def histories(self,codes):
        history={c:[] for c in codes}
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
        return history

    def build(self):
        queued=False
        try:
            with self.lock:self.running=True
            intraday=self.intraday();snapshot_key=KEY+(':intraday' if intraday else '')
            self.deadline=time.monotonic()+120
            if not self.safe(reserve_mb=self.memory_reserve_mb):raise RuntimeError('記憶體餘裕不足，已保留掃描進度')
            with self.lock:previous=self.cached or {}
            saved=previous.get('cycle') or {}
            resume=(saved.get('day')==day(self.now()) and saved.get('intraday')==intraday and
                    previous.get('selection_version')==SELECTION_VERSION and saved.get('cursor',0)<len(saved.get('queue',[])))
            if resume:
                cycle=copy.deepcopy(saved);self.stock_meta=cycle['stocks'];self.scan_meta=copy.deepcopy(previous.get('scope',{}))
                cursor=cycle['cursor'];queue=cycle['queue']
                # Closing bars do not change between batches. Rechecking model
                # positions there wastes requests without improving freshness.
                active=list(dict.fromkeys(r['code'] for r in previous.get('all_rows',[])+previous.get('short_rows',[]) if intraday and r.get('active') and r['code'] in self.stock_meta))
                start=cycle.get('active_cursor',0)%max(1,len(active))
                fast=(active+active)[start:start+min(16,len(active))]
                base=queue[cursor:cursor+MAX_POOL-len(fast)]
                codes=fast+[c for c in base if c not in fast]
                cycle['active_cursor']=start+len(fast)
                names={c:self.stock_meta[c]['name'] for c in codes};history=self.histories(codes)
            else:
                self.full_queue=[]
                keep=[r for r in previous.get('all_rows',previous.get('rows',[]))+previous.get('short_rows',[]) if r.get('active')]
                codes,names,history=self.inputs(keep)
                base=list(codes)
                queue=self.full_queue or codes;cursor=0
                cycle=dict(day=day(self.now()),intraday=intraday,queue=queue,cursor=0,stocks=self.stock_meta,
                           evaluated=[],excluded={},failures=0,started_at=self.now().isoformat())
            covered=set(cycle.get('evaluated',[]));failed_codes=set(cycle.get('failed_codes',[]))
            excluded=dict(cycle.get('excluded',{}));audits=dict(cycle.get('audits',{}));failures=0;processed=0;finished=set();quote_dates=[]
            rows_by_code={r['code']:r for r in previous.get('all_rows',previous.get('rows',[])) if r['code'] in set(queue)}
            short_by_code={r['code']:r for r in previous.get('short_rows',[]) if r['code'] in set(queue)}
            with self.lock:
                self.phase='全市場分批詳查';self.progress=len(covered);self.progress_total=len(queue)
            pause='';time_exhausted=False
            for i,code in enumerate(codes):
                if time.monotonic()>self.deadline:
                    pause='本批行情逾時，進度已保存，稍後接續';time_exhausted=True;break
                if not self.safe(reserve_mb=self.memory_reserve_mb):
                    pause='記憶體餘裕不足，進度已保存';break
                try:
                    market=(self.stock_meta.get(code) or {}).get('market','').lower()
                    suffix='.TW' if market in ('上市','twse','listed','tw') else '.TWO' if market in ('上櫃','上柜','tpex','otc','two') else None
                    q=self.quote(code,rng='6mo',include_mis=False,request_timeout=5,market_suffix=suffix,
                                 force_refresh=intraday,cache_result=False)
                    if not q:
                        failures+=1;failed_codes.add(code)
                        excluded['行情來源暫缺']=excluded.get('行情來源暫缺',0)+1
                        continue
                    failed_codes.discard(code)
                    observed=number(q.get('market_observed_at'))
                    # A recent fetch is not proof of a recent trade. Require the
                    # source's market timestamp before exposing a live signal.
                    live_fresh=bool(observed and -60<=(self.now().timestamp()-observed)<=1200 and
                        datetime.fromtimestamp(observed,timezone(timedelta(hours=8))).date()==self.now().date())
                    bars=normalize_bars(q,self.now(),include_current=intraday and live_fresh)
                    if bars:
                        quote_dates.append(bars[-1]['date'])
                    raw_price=number(q.get('close')) or next((number(v) for v in reversed(q.get('closes') or []) if number(v)),0)
                    if raw_price>=500:audits[code]=technical_audit(code,names[code],bars)
                    from short_side import analyse as analyse_short
                    short_row=analyse_short(code,names[code],q,history[code],self.now(),intraday=intraday and live_fresh)
                    if short_row:
                        short_row.update(sector=(self.stock_meta.get(code) or {}).get('sector','未分類'),market=(self.stock_meta.get(code) or {}).get('market','未確認'),observed_at=datetime.fromtimestamp(observed,timezone(timedelta(hours=8))).isoformat() if observed else None,computed_at=self.now().isoformat())
                        short_by_code[code]=short_row
                    else:short_by_code.pop(code,None)
                    row=analyse(code,names[code],q,history[code],self.now(),intraday=intraday and live_fresh)
                    if row:
                        row['sector']=(self.stock_meta.get(code) or {}).get('sector','未分類')
                        row['market']=(self.stock_meta.get(code) or {}).get('market','未確認')
                        row['observed_at']=datetime.fromtimestamp(observed,timezone(timedelta(hours=8))).isoformat() if observed else None
                        row['computed_at']=self.now().isoformat()
                        rows_by_code[code]=row
                    else:
                        rows_by_code.pop(code,None)
                        reason=exclusion_reason(bars,history[code],self.now())
                        excluded[reason]=excluded.get(reason,0)+1
                except Exception as exc:
                    failures+=1;failed_codes.add(code)
                    excluded['資料讀取或計算失敗']=excluded.get('資料讀取或計算失敗',0)+1
                    print('短線研究單檔略過 %s: %s'%(code,type(exc).__name__),flush=True)
                finally:
                    processed+=1;covered.add(code);finished.add(code)
                    with self.lock:self.progress=len(covered)

            if processed and failures==processed:raise RuntimeError('行情來源暫時無法取得，保留原結果與進度')
            if not processed and codes:raise RuntimeError(pause or '本批尚未取得可確認的資料')
            short_rows=sorted(short_by_code.values(),key=clarity_rank)
            rows=list(rows_by_code.values())
            rows.sort(key=clarity_rank)
            old={e['id']:e for r in previous.get('all_rows',previous.get('rows',[]))+previous.get('short_rows',[]) for e in r.get('signals',[])}
            for row in rows+short_rows:
                row['signals']=[(old.get(e['id']) if not e.get('provisional') and not (old.get(e['id']) or {}).get('provisional') else None)
                                or dict(e,observed_at=self.now().isoformat()) for e in row['signals']]
            advanced=0
            for c in base:
                if c not in finished:break
                advanced+=1
            cycle.update(audits=audits,cursor=cursor+advanced,evaluated=sorted(covered),excluded=excluded,failures=len(failed_codes),failed_codes=sorted(failed_codes))
            complete=cycle['cursor']>=len(queue)
            self.scan_meta.update(deep_total=len(queue),deep_done=len(covered),deep_success=len(covered)-len(failed_codes),deep_complete=complete,scan_warning=pause)
            self.scan_meta['deep_sectors']={sector:sum((self.stock_meta.get(c) or {}).get('sector')==sector for c in covered)
                for sector in {i.get('sector') for i in self.stock_meta.values()}}
            self.scan_meta['qualified_sectors']={g:sum(r.get('sector')==g for r in rows) for g in {r.get('sector') for r in rows}}
            self.scan_meta['short_qualified_sectors']={g:sum(r.get('sector')==g for r in short_rows) for g in {r.get('sector') for r in short_rows}}
            for band,counter in self.scan_meta.get('price_coverage',{}).items():
                counter['deep']=sum((self.stock_meta.get(c) or {}).get('price_band')==band for c in covered)
                counter['qualified']=sum(price_band(number(r.get('price')) or 0)==band for r in rows)
                counter['short_qualified']=sum(price_band(number(r.get('price')) or 0)==band for r in short_rows)
            if audits:self.scan_meta['high_price_examples']=sorted(audits.values(),key=lambda a:-(a.get('price') or 0))
            asof=max(quote_dates+[r.get('date') for r in rows if r.get('date')],default=self.scan_meta.get('market_date'))
            payload=dict(version=VERSION,selection_version=SELECTION_VERSION,rows=rows[:MAX_PICKS],all_rows=rows,short_rows=short_rows,
                scanned=len(covered),failed=cycle['failures'],cycle=cycle,intraday=intraday,trigger=self.trigger,radar_at=self.radar_at,
                date=asof,excluded=excluded,scope=copy.deepcopy(self.scan_meta),alerts=latest_alerts(rows+short_rows,asof),
                generated_at=self.now().isoformat(),note='全市場分批詳查；部分完成會明確顯示進度。規則重播，不送出交易。')
            if not self.store.save(snapshot_key,payload,data_date=asof,source_meta={'version':VERSION,'intraday':intraday}):
                raise RuntimeError('研究結果未確認保存，保留原結果與進度')
            with self.lock:self.cached=payload;self.error=''
            print('✅ 短線分批詳查 %s/%s，本批 %s 檔，候選 %s 檔'%(len(covered),len(queue),processed,len(rows)),flush=True)
            if (not complete and (not pause or time_exhausted) and self.safe(reserve_mb=self.memory_reserve_mb) and self.intraday()==intraday and day(self.now())==cycle['day']):
                queued=True
                try:self.executor.submit(self.build)
                except Exception:
                    queued=False;self.error='背景佇列暫緩，掃描進度已保存'
        except Exception as exc:
            with self.lock:self.error=str(exc)[:160]
            print('⚠️ 短線研究暫緩: '+str(exc)[:160],flush=True)
        finally:
            if not queued:
                with self.lock:self.running=False

    def payload(self, refresh=False, sector=None, position=None, shape=None, offset=0, limit=MAX_PICKS, schedule=False, direction="long", price_range="all", sort="signal"):
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
            expected=self.now().date()
            if self.now().hour<14 and not intraday:expected-=timedelta(days=1)
            for _ in range(20):
                if self.calendar(expected):break
                expected-=timedelta(days=1)
            stale=((age>600 if intraday else (cached or {}).get('date')!=day(expected)) or
                   (cached or {}).get('selection_version')!=SELECTION_VERSION)
            # Viewing, polling and filtering are read-only. Only the independent
            # scheduler or an explicit refresh may start the shared producer.
            cycle=(cached or {}).get('cycle') or {}
            unfinished=cycle.get('day')==day(self.now()) and cycle.get('cursor',0)<len(cycle.get('queue',[]))
            if (refresh or schedule and (not cached or stale or unfinished)) and not self.running and time.monotonic()-self.last_try>60:
                self.last_try=time.monotonic()
                if self.safe(reserve_mb=self.memory_reserve_mb):
                    self.running=True; self.progress=0; self.error=''; self.started=time.monotonic()
                    try:
                        self.executor.submit(self.build)
                    except Exception:
                        self.running=False; self.error='背景佇列暫時無法接收，請稍後重試'
                else:
                    self.error='記憶體餘裕不足，稍後可重試'
            signals_current=bool((cached or {}).get('date')==day(expected) and
                (cached or {}).get('selection_version')==SELECTION_VERSION and
                (not intraday or ((cached or {}).get('intraday') and age<=1200)))
            direction='short' if direction=='short' else 'long'
            rows=[]
            saved_rows=(cached or {}).get('short_rows',[]) if direction=='short' else (cached or {}).get('all_rows',(cached or {}).get('rows',[]))
            for row in saved_rows:
                row=dict(row,signals=signal_allocations(row.get('signals',[])),
                    spreads=trade_spreads(row.get('signals',[]),row.get('price'),row.get('date'),direction=direction))
                if direction=='short':
                    labels={'entry':'首次放空100%','add':'加空首次股數50%','reduce':'回補當時空單50%','exit':'全部回補剩餘空單'}
                    for event in row['signals']:
                        if event.get('allocation'):event['allocation']['label']=labels[event['type']]
                try:quote_age=(self.now()-datetime.fromisoformat(row.get('observed_at') or '')).total_seconds()
                except (ValueError,TypeError):quote_age=999999
                if signals_current and row.get('date')==day(expected) and (not intraday or -60<=quote_age<=1200):
                    rows.append(row)
                else:
                    rows.append(dict(row,state='資料待更新，非最新訊號',current_action='unconfirmed',
                        action_note='行情截至 '+str(row.get('date') or '未確認')+'；尚未確認最新交易日的訊號，不沿用舊買點。',
                        entry_ready=False,latest_event=None,latest_event_date=None,signal_stage='unconfirmed'))
            alerts=[]
            for alert in (cached or {}).get('alerts',[]) if signals_current else []:
                if alert.get('direction','long')!=direction:continue
                row=next((r for r in rows if r['code']==alert['code']),None)
                spread=alert.get('spread')
                if spread is None and row and alert['type']=='exit':
                    spread=next((t['spread'] for t in reversed(row['spreads']['completed']) if t['exit_date']==alert['date']),None)
                event=next((e for e in (row or {}).get('signals',[]) if e.get('id')==alert.get('id')),None)
                alerts.append(dict(alert,spread=spread,allocation=(event or {}).get('allocation')))
            alerts=[a for a in alerts if any(r['code']==a['code'] and r.get('current_action')!='unconfirmed' for r in rows)]
            if sector=='電子':rows=[r for r in rows if r.get('sector')=='電子']
            elif sector=='非電子':rows=[r for r in rows if r.get('sector')!='電子']
            if shape in ('reversal','breakout','pullback','continuation','repair'):
                rows=[r for r in rows if shape in r.get('patterns',[])]
                alerts=[a for a in alerts if any(r['code']==a['code'] for r in rows)]
            ranges={'under100':(0,100),'100to500':(100,500),'500to1000':(500,1000),'1000plus':(1000,float('inf'))}
            price_range=price_range if price_range in ranges else 'all'
            sort=sort if sort in ('price_desc','price_asc') else 'signal'
            if price_range in ranges:
                lo,hi=ranges[price_range]
                rows=[r for r in rows if number(r.get('price')) is not None and lo<=number(r['price'])<hi]
                alerts=[a for a in alerts if any(r['code']==a['code'] for r in rows)]
            position_counts={g:sum(position_group(r)==g for r in rows) for g in ('held','waiting','closed','unknown')}
            for row in rows:row['position_group']=position_group(row)
            if position in position_counts:rows=[r for r in rows if r['position_group']==position]
            if sector in ('電子','非電子'):alerts=[a for a in alerts if any(r['code']==a['code'] for r in rows)]
            rows.sort(key=(lambda r:(number(r.get('price')) is None, (-1 if sort=='price_desc' else 1)*(number(r.get('price')) or 0),r['code'])) if sort!='signal' else clarity_rank)
            total=len(rows)
            recent=dict(entries=0,exits=0,active=sum(position_group(r)=='held' for r in rows),last_entry_date=None)
            for row in rows:
                if row.get('current_action')=='unconfirmed':continue
                dates=[b['date'] for b in row.get('bars',[])][-30:]
                if not dates:continue
                for event in row.get('signals',[]):
                    if event.get('provisional') or event.get('date','')<dates[0]:continue
                    if event['type']=='entry':
                        recent['entries']+=1
                        recent['last_entry_date']=max(recent['last_entry_date'] or '',event['date'])
                    elif event['type']=='exit':recent['exits']+=1
            try:offset=max(0,int(offset));limit=max(1,min(50,int(limit)))
            except (ValueError,TypeError):offset=0;limit=MAX_PICKS
            offset=min(offset,((total-1)//limit)*limit) if total else 0
            stages={k:sum(r.get('signal_stage','watch')==k and r.get('current_action')!='unconfirmed'
                         and (k=='entry' or not r.get('active')) for r in rows)
                    for k in ('watch','triggered','entry')}
            new_watch=[dict(code=r['code'],name=r['name'],date=r['watch_date'],price=r['price'],type='watch')
                       for r in rows if r.get('watch_date')==day(expected) and r.get('current_action')!='unconfirmed' and not r.get('active')]
            return dict(ok=True,rows=rows[offset:offset+limit],alerts=alerts,observations=new_watch,meta=dict(
                price_range=price_range,sort=sort,direction=direction,direction_counts={'long':len((cached or {}).get('all_rows',[])),'short':len((cached or {}).get('short_rows',[]))},total=total,offset=offset,limit=limit,has_more=offset+limit<total,
                recent_signals=recent,stage_counts=stages,
                position_counts=position_counts,
                intraday=intraday,refresh_interval_seconds=600,
                next_refresh_at=(datetime.fromisoformat(generated)+timedelta(seconds=600)).isoformat() if generated and intraday else None,
                trigger=(cached or {}).get('trigger'),radar_at=(cached or {}).get('radar_at'),
                version=VERSION,building=self.running,progress=self.progress,pool_limit=MAX_POOL,
                phase=self.phase,progress_total=self.progress_total,
                scope=dict(self.scan_meta) if self.running else (cached or {}).get('scope',{}),
                signals_current=signals_current,expected_date=day(expected),
                scan_complete=bool(cycle and not unfinished and cycle.get('cursor',0)>=len(cycle.get('queue',[]))),
                date=(cached or {}).get('date'),generated_at=generated,stale=stale,
                scanned=(cached or {}).get('scanned',0),failed=(cached or {}).get('failed',0),
                excluded=(cached or {}).get('excluded',{}),
                error=self.error,note=(cached or {}).get('note','全市場分批詳查・依型態與趨勢排列，可分頁查看全部候選。')))


def create_blueprint(*, login, service):
    bp=Blueprint('short_term',__name__)
    @bp.route('/web/api/workbench/short-term')
    @login
    def short_term(uid):
        try:
            result=service.payload(request.args.get('refresh')=='1',sector=request.args.get('sector'),position=request.args.get('position'),shape=request.args.get('shape'),offset=request.args.get('offset',0),limit=request.args.get('limit',MAX_PICKS),direction=request.args.get('direction','long'),price_range=request.args.get('price_range','all'),sort=request.args.get('sort','signal'))
            response=jsonify(result)
        except Exception:
            response=jsonify(ok=False,error='短線研究暫時無法載入，請稍後重試')
            response.status_code=503
        response.headers['Cache-Control']='no-store'
        return response
    return bp
