"""Deterministic research coverage, independent of institution lots or prices."""


def radar_state(price):
    from math import isfinite
    def value(key):
        try:
            n=float((price or {}).get(key))
            return n if isfinite(n) else None
        except (TypeError,ValueError):return None
    close=value('close');pct=value('pct');ratio=value('vol_ratio')
    if ratio is not None and ratio<0:ratio=None
    if close is None or close<=0:return '行情待確認'
    volume=ratio is not None and ratio>=1.5
    # Negative/flat price action cannot be labelled a bullish breakout even
    # when stale or inconsistent high metadata happens to be supplied.
    if pct is not None and pct<0:return '帶量下跌' if volume else '價格轉弱、量能普通'
    if pct==0:return '放量整理' if volume else '價格持平、量能普通'
    highs=[value(k) for k in ('high_60d','high_20d')]
    breakout=any(h is not None and h>0 and close>=h for h in highs)
    if breakout:return '真正帶量突破' if volume else '突破但量能待確認' if ratio is None else '突破但量能普通'
    if pct is None:return '量能放大、漲跌待確認' if volume else '漲跌待確認'
    if volume:return '帶量上漲'
    return '價格強、量能待確認' if ratio is None else '價格強、量能普通'


def radar_candidates(candidates, limit, categories):
    limit=max(0,int(limit))
    if not limit:return []
    by_code={code:info for code,info in candidates}
    price=sorted(by_code,key=lambda c:(-by_code[c].get('spark_pct',0),c))
    turnover=sorted(by_code,key=lambda c:(-by_code[c].get('spark_turnover',0),c))
    groups={}
    for code in price:groups.setdefault(categories.get(code,'未分類'),[]).append(code)
    balanced=[group[i] for i in range(max((len(g) for g in groups.values()),default=0))
              for _,group in sorted(groups.items()) if i<len(group)]
    result=[];seen=set()
    for i in range(len(by_code)):
        for bucket in (price,turnover,balanced):
            code=bucket[i]
            if code in seen:continue
            seen.add(code);result.append((code,by_code[code]))
            if len(result)>=limit:return result
    return result
