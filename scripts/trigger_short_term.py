"""Trigger only; a queue acknowledgment never claims a completed market scan."""
import json
import os
from urllib.parse import quote
from urllib.request import Request, urlopen

ENDPOINT = 'https://stock-bot-6xct.onrender.com/cron/short-term-live'


def main():
    url = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL']
    url += ('&' if '?' in url else '?') + 'audience=' + quote(ENDPOINT, safe='')
    req = Request(url, headers={'Authorization': 'Bearer ' + os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']})
    with urlopen(req, timeout=20) as response:
        token = json.load(response)['value']
    req = Request(ENDPOINT, data=b'', headers={'Authorization': 'Bearer ' + token}, method='POST')
    with urlopen(req, timeout=45) as response:
        result = json.load(response)
    if not result.get('ok') or result.get('state') not in {'queued', 'outside_session'}:
        raise RuntimeError('伺服器未接收盤中研究工作')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
