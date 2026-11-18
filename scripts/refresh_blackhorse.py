"""An accepted job is not success: require an actual current saved snapshot."""
import json
import os
import time
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError

ENDPOINT='https://stock-bot-6xct.onrender.com/cron/blackhorse-refresh'


def call(method):
    url=os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    url+=('&' if '?' in url else '?')+'audience='+quote(ENDPOINT,safe='')
    with urlopen(Request(url,headers={'Authorization':'Bearer '+os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}),timeout=20) as r:
        token=json.load(r)['value']
    with urlopen(Request(ENDPOINT,data=b'' if method=='POST' else None,
        headers={'Authorization':'Bearer '+token},method=method),timeout=40) as r:
        return json.load(r)


def main():
    deadline=time.monotonic()+240
    result={};next_post=0
    while True:
        stamp=time.monotonic()
        method='POST' if stamp>=next_post and not result.get('building') else 'GET'
        if method=='POST':next_post=stamp+60
        try:result=call(method)
        except (URLError,TimeoutError,ValueError) as exc:
            result={'ok':False,'state':'unavailable','error':type(exc).__name__}
        print(json.dumps(result,ensure_ascii=False),flush=True)
        if result.get('ok') and result.get('state')=='fresh' and result.get('computed_at') and result.get('quote_date'):
            return
        if time.monotonic()>deadline:raise TimeoutError('黑馬未確認保存最新快照，不能將排程接收當成更新完成')
        time.sleep(10)


if __name__=='__main__':main()
