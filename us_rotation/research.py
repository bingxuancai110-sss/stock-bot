"""Transparent, dated US-sector/Taiwan observation screens; never trade recommendations."""
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import json
import math

VERSION = 'us-tw-observation-v1'
UNIVERSE = json.loads(Path(__file__).with_name('watch_universe.json').read_text(encoding='utf-8'))


def codes():
    return sorted({c['code'] for sector in UNIVERSE.values() for c in sector['companies']})


def numeric(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def day(value):
    raw = str(value or '')[:10]
    try:
        if len(raw) == 8 and raw.isdigit():
            return datetime.strptime(raw, '%Y%m%d').date().isoformat()
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


def aligned_returns(stock_dates, stock_closes, benchmark, price_date):
    """Use identical start/end dates and price returns for both stocks and TAIEX."""
    values = {day(d): numeric(v) for d,v in zip(stock_dates,stock_closes) if day(d)}
    market = {day(r.get('trade_date')): numeric(r.get('close')) for r in (benchmark or []) if day(r.get('trade_date'))}
    market = {d:v for d,v in market.items() if v is not None and v>0}
    end = day(price_date)
    out = {'benchmark_date': max(market) if market else None}
    for n in (5,20,60):
        prefix = f'price_return_{n}d_pct'
        out[prefix]=out[f'market_return_{n}d_pct']=out[f'relative_market_{n}d_pp']=None
        out[f'comparison_start_{n}d']=None
        local=sorted(d for d,v in values.items() if d<=str(end or '') and v is not None and v>0)
        if len(local)>n and local[-1]==end:
            out[prefix]=round((values[end]/values[local[-n-1]]-1)*100,4)
            out[f'comparison_start_{n}d']=local[-n-1]
        if not end or end != out['benchmark_date']:
            continue
        dates=sorted(d for d in market if d<=end)
        if len(dates)<n+1:
            continue
        start=dates[-n-1];v0,v1=values.get(start),values.get(end)
        if v0 is None or v1 is None or v0<=0 or v1<=0:
            continue
        own=(v1/v0-1)*100;index=(market[end]/market[start]-1)*100
        out[prefix]=round(own,4);out[f'market_return_{n}d_pct']=round(index,4)
        out[f'relative_market_{n}d_pp']=round(own-index,4);out[f'comparison_start_{n}d']=start
    return out


def revenue_period(value):
    raw=str(value or '').replace('-','').replace('/','')
    if len(raw)==5 and raw.isdigit():raw=str(int(raw[:3])+1911)+raw[3:]
    if len(raw)!=6 or not raw.isdigit() or not 1<=int(raw[4:])<=12:return None
    return raw[:4]+'/'+raw[4:]


def build_research(snapshot, companies, today=None):
    today=today or datetime.now(ZoneInfo('Asia/Taipei')).date()
    by_code={str(c.get('code')):c for c in companies}
    sectors={r.get('id'):r for r in (snapshot or {}).get('industries',[])}
    result={}
    for key,config in UNIVERSE.items():
        us=sectors.get(key,{})
        relative=numeric(us.get('relative_20d_pct'));ret=numeric(us.get('return_20d_pct'))
        us_day=day(us.get('as_of') or (snapshot or {}).get('as_of'))
        us_recent=bool(us_day and 0<=(today-date.fromisoformat(us_day)).days<=7)
        strong=relative>0 and ret>0 if us_recent and relative is not None and ret is not None else None
        rows=[]
        for issuer in config['companies']:
            data=by_code.get(issuer['code'],{});price_day=day(data.get('price_date'))
            rel=numeric(data.get('relative_market_20d_pp'));own=numeric(data.get('price_return_20d_pct'))
            close=numeric(data.get('close'));ma20=numeric(data.get('ma20'))
            recent=bool(price_day and 0<=(today-date.fromisoformat(price_day)).days<=7)
            valid=recent and price_day==day(data.get('benchmark_date')) and rel is not None and own is not None and close is not None and close>0 and ma20 is not None and ma20>0
            above=close>ma20 if close is not None and ma20 is not None else None
            if not valid or strong is None:
                state,label='check','資料待核對'
                next_step='先補齊同一資料日的大盤比較與均線，不以缺漏資料判定強弱。'
                if price_day and not recent:next_step='股價資料較早，先等待最新完成收盤資料。'
            elif strong and rel>0 and own>0 and above:
                state,label='aligned','美台同向'
                next_step='台股跑贏加權且站上 20 日線；接著核對下一次營收與法人買盤能否延續。'
            elif strong:
                state,label='waiting','台股尚未跟上'
                next_step='先等台股跑贏加權並站上 20 日線；落後不代表一定會補漲。'
            elif rel>0 and own>0 and above:
                state,label='local','台股較強'
                next_step='台股表現較強，但美股產業未同步領先，需另查台灣本身的需求與消息。'
            else:
                state,label='weak','暫無同向優勢'
                next_step='美台尚未形成同向優勢，先看股價能否站回 20 日線與相對大盤轉強。'
            period=revenue_period(data.get('revenue_period'));yoy=numeric(data.get('revenue_yoy_pct'))
            age=(today.year*12+today.month)-(int(period[:4])*12+int(period[5:])) if period else None
            revenue_state='金融另查' if key=='financials' else '較早營收' if age is not None and not 0<=age<=2 else '月份待核對' if not period else '年增正' if yoy is not None and yoy>0 else '年增負' if yoy is not None and yoy<0 else '持平' if yoy==0 else '待公告'
            rows.append({**data,**issuer,'name':data.get('name') if data.get('name') and data.get('name')!=issuer['code'] else issuer['name'],
                'state':state,'state_label':label,'next_step':next_step,'above_ma20':above,
                'price_return_20d_pct':own,'relative_market_20d_pp':rel,'price_date':price_day,
                'revenue_period_label':period,'revenue_state':revenue_state,
                'institutional_aligned':bool(price_day and price_day==day(data.get('institutional_source_date'))),
                'has_price':close is not None and close>0 and price_day is not None,'comparison_valid':valid})
        rows.sort(key=lambda r:(not r['comparison_valid'], -(r['relative_market_20d_pp'] if r['relative_market_20d_pp'] is not None else -1e9),r['code']))
        result[key]={'scope':config['scope'],'rows':rows,'total':len(rows),'priced':sum(r['has_price'] for r in rows),
                     'aligned':sum(r['state']=='aligned' for r in rows),'us_strong':strong,'us_date':us_day,
                     'source_note':'固定研究樣本；官方公司代碼／名稱已核對，產業子題不代表特定美國客戶供貨。'}
    return {'version':VERSION,'sectors':result}
