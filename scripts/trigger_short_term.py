"""Check scheduled freshness or verify a manually triggered scan to completion."""
import json
import os
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError

ENDPOINT = 'https://stock-bot-6xct.onrender.com/cron/short-term-live'


def call(method):
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    url += ('&' if '?' in url else '?') + 'audience=' + quote(ENDPOINT, safe='')
    req = Request(url, headers={'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    with urlopen(req, timeout=20) as response:
        token = json.load(response)['value']
    headers={'Authorization':'Bearer '+token}
    if method=='POST' and os.environ.get('VERIFY_COMPLETE')=='1':headers['X-Stockbot-Manual-Close']='1'
    elif method=='POST':headers['X-Stockbot-Monitor']='1'
    req = Request(ENDPOINT, data=b'' if method=='POST' else None, headers=headers, method=method)
    with urlopen(req, timeout=45) as response:
        return json.load(response)


def main():
    verify=os.environ.get('VERIFY_COMPLETE')=='1'
    deadline=time.monotonic()+1800
    result={};next_post=0
    while True:
        stamp=time.monotonic()
        method='POST' if stamp>=next_post and not result.get('building') else 'GET'
        if method=='POST':next_post=stamp+60
        try:result=call(method)
        except (URLError,TimeoutError,ValueError) as exc:
            if not verify:raise
            result={'ok':False,'state':'unavailable','error':type(exc).__name__}
        print(json.dumps(result,ensure_ascii=False),flush=True)
        if not verify:
            monitor = result.get('monitor')
            if monitor:
                if monitor['state'] in {'stalled', 'unavailable'}:
                    raise RuntimeError('正式站資料更新停滯或無法驗證；請查看上述資料日期和最後成功時間')
                return
            if not result.get('ok') or result.get('state') not in {'queued','fresh','outside_session'}:
                raise RuntimeError('伺服器未接收背景研究工作，稍後排程將重試')
            return
        if result.get('state')=='outside_session':return
        if result.get('ok') and result.get('complete') and result.get('state')=='fresh' and result.get('generated_at'):
            return
        if time.monotonic()>deadline:raise TimeoutError('尚未確認全市場掃描保存完成')
        time.sleep(15)


if __name__ == '__main__':
    main()
