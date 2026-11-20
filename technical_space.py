"""Price-scale-independent, causal structural space for a proposed long entry."""


def long_space(bars,entry,atr):
    prior=bars[:-1][-60:]
    threshold=max(atr,entry*.005)
    zones=[]
    for i in range(2,len(prior)-2):
        b=prior[i];window=prior[i-2:i+3]
        # A peak requires two completed bars to its right and a meaningful swing,
        # not merely a historical high a few ticks above today's quote.
        prominence=b['high']-max(min(z['low'] for z in prior[i-2:i+1]),
                                 min(z['low'] for z in prior[i:i+3]))
        if b['high']>=max(z['high'] for z in window) and prominence>=threshold:
            zones.append(dict(price=b['high'],date=b['date'],confirmed_date=prior[i+2]['date']))
    overhead=sorted((z for z in zones if z['price']>entry),key=lambda z:z['price'])
    if overhead:
        return dict(resistance=overhead[0]['price'],kind='已確認波段壓力',
                    pressure_date=overhead[0]['date'],confirmed_date=overhead[0]['confirmed_date'])
    ceiling=max((b['high'] for b in prior),default=0)
    if ceiling>entry:
        return dict(resistance=ceiling,kind='60日區間上緣',pressure_date=None,confirmed_date=None)
    return dict(resistance=None,kind='2R模型參考（非已知壓力）',pressure_date=None,confirmed_date=None)
