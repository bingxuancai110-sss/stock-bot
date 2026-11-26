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


def refresh(*, request=call, clock=time.monotonic, sleep=time.sleep, budget=900, emit=print):
    """Retry admission conservatively; never call a deferred snapshot complete."""
    deadline=clock()+budget
    next_post=0
    backoff=60
    result={}
    posts=0
    while clock()<deadline:
        stamp=clock()
        method='POST' if stamp>=next_post and not result.get('building') else 'GET'
        if method=='POST':
            posts+=1
            next_post=stamp+backoff
            backoff=min(300,backoff*2)
        try:
            result=request(method)
            if not isinstance(result,dict):
                raise ValueError('invalid response')
        except (URLError,TimeoutError,ValueError) as exc:
            result={'ok':False,'state':'unavailable','error':type(exc).__name__}
        emit(json.dumps({'method':method,'posts':posts,'result':result},ensure_ascii=False))
        if result.get('ok') is True and result.get('state')=='fresh' and result.get('computed_at') and not result.get('building'):
            return result
        # Status reads are cheap and never enqueue; retry POST with bounded backoff.
        pause=20 if result.get('building') else 30
        remaining=deadline-clock()
        if remaining>0:sleep(min(pause,remaining))
    state=result.get('state','unknown')
    raise TimeoutError('成效統計尚未確認保存完成；最後狀態=%s；更新請求=%d' % (state,posts))


def main():
    refresh(emit=lambda line:print(line,flush=True))


if __name__=='__main__':main()
