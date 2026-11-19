"""Only a saved review snapshot counts as completion; do not send LINE messages."""
import json
import os
import time
from urllib.parse import quote
from urllib.request import Request,urlopen
from urllib.error import URLError

ENDPOINT='https://stock-bot-6xct.onrender.com/cron/review-refresh'


def call(method):
    url=os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    url+=('&' if '?' in url else '?')+'audience='+quote(ENDPOINT,safe='')
    with urlopen(Request(url,headers={'Authorization':'Bearer '+os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}),timeout=20) as response:
        token=json.load(response)['value']
    with urlopen(Request(ENDPOINT,data=b'' if method=='POST' else None,
        headers={'Authorization':'Bearer '+token},method=method),timeout=40) as response:
        return json.load(response)


def main():
    deadline=time.monotonic()+900;next_post=0;result={}
    while True:
        stamp=time.monotonic()
        method='POST' if stamp>=next_post and not result.get('building') else 'GET'
        if method=='POST':next_post=stamp+60
        try:result=call(method)
        except (URLError,TimeoutError,ValueError) as exc:
            result={'ok':False,'state':'unavailable','error':type(exc).__name__}
        print(json.dumps(result,ensure_ascii=False),flush=True)
        if result.get('ok') and result.get('state')=='fresh' and result.get('computed_at'):return
        if time.monotonic()>deadline:raise TimeoutError('成效統計尚未確認保存完成')
        time.sleep(20)


if __name__=='__main__':main()
