"""Deterministic research coverage, independent of institution lots or prices."""


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
