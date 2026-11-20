"""Causal short-side research. No order routing or borrow-availability claims."""
from datetime import date


def setup(bars, fraction=1):
    from short_term import levels
    x=levels(bars);b=bars[-1];prev=bars[:-1];c=b['close'];atr=x['atr']
    ratio=x['volume_ratio']/max(15/270,min(1,fraction))
    buffer=max(atr*.25,c*.003)
    floor=min(z['low'] for z in bars[-22:-2])
    below=all(z['close']<floor-buffer for z in bars[-2:])
    fresh=any(z['close']>=floor-buffer for z in bars[-7:-2])
    weak=c<x['ma20'] and prev[-1]['close']<x['ma20_previous']
    down=x['ma20']<x['ma60'] and x['trend_slope']<0
    # Two confirmed rising pivots; no future bars or retrospective line fitting.
    lows=[i for i in range(max(2,len(prev)-60),len(prev)-2)
          if prev[i]['low']<=min(z['low'] for z in prev[i-2:i+3])
          and prev[i]['low']<max(z['low'] for z in prev[i-2:i+3])]
    line=None;line_break=False
    if lows:
        j=lows[-1];i=next((i for i in reversed(lows[:-1]) if j-i>=3 and prev[j]['low']>prev[i]['low']*1.005),None)
        if i is not None:
            slope=(prev[j]['low']-prev[i]['low'])/(j-i)
            level=prev[j]['low']+slope*(len(bars)-1-j)
            valid=all(prev[k]['low']>=prev[i]['low']+slope*(k-i)-buffer for k in lows if i<k<j)
            line_break=valid and all(bars[k]['close']<level+slope*(k-(len(bars)-1))-buffer for k in range(len(bars)-2,len(bars))) and any(bars[k]['close']>=level+slope*(k-(len(bars)-1))-buffer for k in range(len(bars)-7,len(bars)-2))
            line=dict(start_date=prev[i]['date'],end_date=b['date'],start_price=prev[i]['low'],end_price=level,confirmed_date=bars[j+2]['date'])
    rejection=down and prev[-1]['high']>=x['ma20_previous']*.985 and prev[-1]['close']<x['ma20_previous'] and c<prev[-1]['low']-buffer
    strong=c<=b['low']+.4*(b['high']-b['low'])
    kind='reversal' if line_break else 'breakout' if below and fresh else 'pullback' if rejection else None
    trigger=bool(kind and weak and strong and ratio>=1.05)
    stop=max(z['high'] for z in prev[-5:])+.5*atr
    risk=stop-c
    supports=sorted({z['low'] for z in prev[-60:] if z['low']<c},reverse=True)
    target=supports[0] if supports else c-2*risk
    rr=(c-target)/risk if risk>0 else 0
    hot=abs(x['change'])>9.5 or atr/c>.06
    risk_ok=0<risk/c<=.06 and rr>=1.5 and target>0
    watch=(weak or down or line_break) and not hot and b['volume']>0
    label={'reversal':'上升線跌破','breakout':'支撐跌破確認','pullback':'反彈碰壓轉弱'}.get(kind,'空方型態觀察')
    return dict(x,volume_ratio=ratio,entry=trigger and risk_ok and not hot and b['volume']>0,
                trigger=trigger,watchable=watch,stop=stop,risk=risk,target=target,rr=rr,risk_ok=risk_ok,
                soft_resistance=max(z['high'] for z in prev[-10:])+.25*atr,
                strategy=kind or ('pullback' if down else 'reversal'),pattern=label,
                pattern_level=(line['end_price'] if kind=='reversal' and line else floor),
                pattern_line=line if kind=='reversal' else None,buffer=buffer)


def analyse(code,name,quote,history,now,intraday=False):
    from short_term import normalize_bars,chip_at,day
    bars=normalize_bars(quote,now,include_current=intraday)
    if len(bars)<65 or (now.date()-date.fromisoformat(bars[-1]['date'])).days>7:return None
    signals=[];trails=[];p=None;latest=None
    for i in range(64,len(bars)):
        prefix=bars[:i+1];b=bars[i];c=b['close'];fraction=((now.hour-9)*60+now.minute)/270 if intraday and b['date']==day(now) else 1
        plan=setup(prefix,fraction);latest=plan
        def emit(kind,price,reason,units):
            evidence=dict(close=c,ma10=plan['ma10'],ma20=plan['ma20'],ma60=plan['ma60'],atr=plan['atr'],volume_ratio=plan['volume_ratio'],pattern_level=plan['pattern_level'],stop=p['stop'],target=p['target'],initial_risk=p['risk'],entry_price=p['entry'],prior_low10=min(z['low'] for z in prefix[-11:-1]),risk_reward=plan['rr'])
            detail='；收 %.2f，MA10 %.2f／MA20 %.2f／MA60 %.2f，型態線 %.2f，ATR %.2f，量比 %.2f，停損 %.2f，回補參考 %.2f'%(c,plan['ma10'],plan['ma20'],plan['ma60'],plan['pattern_level'],plan['atr'],plan['volume_ratio'],p['stop'],p['target'])
            detail+='；原放空 %.2f，初始1R %.2f，前10日低點 %.2f，進場報酬風險 %.2fR'%(p['entry'],p['risk'],evidence['prior_low10'],plan['rr'])
            signals.append(dict(id='short-v1:%s:%s:%s'%(code,b['date'],kind),date=b['date'],type=kind,price=round(price,2),reason=reason+detail,stop=round(p['stop'],2),target=round(p['target'],2),units=units,direction='short',evidence=evidence,origin='replay',strategy=plan['strategy'],pattern=plan['pattern'],pattern_line=plan['pattern_line']))
        if p:
            # Prior stop always wins, including bars that also touch the target.
            if b['high']>=p['stop']:
                emit('exit',max(b['open'],p['stop']),'觸及事前硬停損，全部回補；跳空按較高開盤價參考，非成交保證',p['units']);p=None;continue
            broken=c>p['soft_resistance'] and c>plan['ma20']
            p['breaks']=p['breaks']+1 if broken else 0
            if p['breaks']>=2 and plan['ma20']>=plan['ma20_previous']:
                emit('exit',c,'連續兩日站回事前趨勢壓力與20日線，20日線轉平或上升；反轉確認，全部回補',p['units']);p=None;continue
            bounced=b['low']<=p['target'] and (c>b['open'] or min(c,b['open'])-b['low']>.45*(b['high']-b['low']))
            if not p['reduced'] and ((bounced and c>plan['ma10']) or (c>plan['ma20'] and prefix[-2]['close']>plan['ma20_previous'])):
                units=p['units']/2;emit('reduce',c,'支撐反彈並站回10日線，或連兩日站回20日線；回補當時空單50%',units);p['units']-=units;p['proceeds']*=.5;p['reduced']=True
            elif not p['added'] and not p['reduced'] and c<=p['entry']-p['risk'] and c<min(z['low'] for z in prefix[-11:-1]) and plan['volume_ratio']>=1.25 and c<plan['ma20']:
                combined=max(0,p['stop']*p['units']-p['proceeds'])+(p['stop']-c)*.5
                if combined<=p['risk']:
                    emit('add',c,'獲利至少1R、再破前10日低點、量比至少1.25，合併停損風險不超過初始1R；加空首次股數50%，最多一次',.5);p['units']+=.5;p['proceeds']+=c*.5;p['added']=True
            new_stop=min(p['stop'],max(z['high'] for z in prefix[-5:])+.5*plan['atr'])
            if new_stop>c:p['stop']=new_stop
            p['soft_resistance']=min(p['soft_resistance'],max(z['high'] for z in prefix[-10:])+.25*plan['atr'])
            if c<p['target'] and not bounced:
                lows=[z['low'] for z in prefix[:-1][-60:] if z['low']<c]
                p['target']=max(lows) if lows else max(.01,c-2*(p['stop']-c))
            trails.append(dict(date=b['date'],value=round(p['stop'],2),target=round(p['target'],2)))
        elif plan['entry']:
            p=dict(entry=c,risk=plan['risk'],stop=plan['stop'],target=plan['target'],soft_resistance=plan['soft_resistance'],units=1.,proceeds=c,added=False,reduced=False,breaks=0,strategy=plan['strategy'])
            emit('entry',c,plan['pattern']+'；價格與均線轉弱，量比至少1.05，停損距離不超過6%、報酬風險至少1.5R；首次放空100%，法人不作門檻',1.)
    if latest is None:return None
    recent=[e for e in signals if e['date']>=bars[-3]['date']]
    if not p and not latest['watchable'] and not recent:return None
    provisional=intraday and bars[-1]['date']==day(now)
    for e in signals:
        if provisional and e['date']==day(now):e['provisional']=True;e['reason']='盤中暫定，收盤前可能消失：'+e['reason']
    event=next((e for e in reversed(signals) if e['date']==bars[-1]['date']),None)
    entries=[e for e in signals if e['type']=='entry'];exits=[e for e in signals if e['type']=='exit']
    action=event['type'] if event else 'hold' if p else 'wait'
    labels=dict(entry='放空條件成立',add='加空條件成立',reduce='回補一半',exit='全部回補',hold='空單續抱',wait='等待放空確認')
    chip=chip_at(history,bars[-1]['date'],[b['date'] for b in bars])
    return dict(code=code,name=name,direction='short',date=bars[-1]['date'],price=bars[-1]['close'],change_pct=round(latest['change'],2),provisional=provisional,active=bool(p),current_action=action,state=('盤中暫定 · ' if provisional and event else '')+labels[action],action_note=event['reason'] if event else '策略空單續抱；不是新的放空指令。' if p else '等待價格與量價確認，歷史訊號不是現在進場指令。',
                entry_ready=bool(event and action=='entry'),latest_event=event['type'] if event else None,latest_event_date=event['date'] if event else None,last_entry_date=entries[-1]['date'] if entries else None,last_exit_date=exits[-1]['date'] if exits else None,
                signal_stage='entry' if event and action=='entry' else 'triggered' if latest['trigger'] else 'watch',strategy=p['strategy'] if p else latest['strategy'],patterns=[p['strategy'] if p else latest['strategy']],pattern_line=latest['pattern_line'],
                entry=round(p['entry'] if p else bars[-1]['close'],2),stop=round(p['stop'] if p else latest['stop'],2),target=round(p['target'] if p else latest['target'],2),target_kind='支撐／2R回補參考',soft_support=round(p['soft_resistance'] if p else latest['soft_resistance'],2),support=round(latest['support'],2),resistance=latest['resistance'],rr=round(latest['rr'],2),
                pending=[] if p else ['等待收盤確認跌破／反彈轉弱']+([] if latest['risk_ok'] else ['等待風險空間']),checks=[dict(label='空方型態',passed=latest['trigger']),dict(label='風險空間',passed=latest['risk_ok'])],technical_priority=(not latest['trend_slope']<0,latest['trend_slope'],0),trend=latest['trend_slope']<0,
                volume_ratio=round(latest['volume_ratio'],2),avg_volume=latest['avg_volume'],avg_turnover=latest['avg_turnover'],chip=chip,chip_ratio=None,institutional_buying=False,institutional_selling=chip['available'] and chip['foreign']+chip['trust']<0,liquidity_warning=latest['avg_turnover']<10_000_000,
                bars=bars[-120:],signals=signals,trailing=trails,source=quote.get('source','Yahoo 日線'),volume_estimated=provisional,short_eligibility='未核實融券／借券資格與券源；僅為技術研究訊號')
