import unittest
from v3_staging import FixtureMarket, fixture_state, create_app


class StagingTests(unittest.TestCase):
    def test_fixture_scope(self):
        result, status = FixtureMarket().read(['2330','2345'])
        self.assertEqual(status,200)
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing'],['2345'])
        self.assertFalse(result['execution_authorized'])
        self.assertEqual(result['quotes']['2330']['status'],'synthetic_fixture')
        self.assertEqual(FixtureMarket().read(['2345'])[1],503)

    def test_locked_clean_state(self):
        state=fixture_state()
        self.assertFalse(state['ready'])
        self.assertFalse(state['history_complete'])
        self.assertEqual(state['positions'],{})
        self.assertEqual(state['data_mode'],'synthetic_only')

    def test_invalid_token_before_init(self):
        for token in ('','x'*43):
            with self.assertRaises(ValueError): create_app(token)

    def test_http(self):
        try: import flask
        except ImportError: self.skipTest('Flask unavailable')
        token='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        app=create_app(token); self.addCleanup(app.extensions['v3_staging_tmp'].cleanup)
        c=app.test_client(); auth={'Authorization':'Bearer '+token}
        self.assertEqual(c.get('/healthz').json['data_mode'],'synthetic_only')
        path='/api/v3/market/quotes?symbols=2330'
        self.assertEqual(c.get(path).status_code,401)
        self.assertEqual(c.get(path,headers={'Authorization':'Bearer wrong'}).status_code,401)
        r=c.get(path,headers=auth);self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['quotes']['2330']['source'],'synthetic_test_fixture')
        self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.assertEqual(c.get('/api/v3/market/quotes?symbols=9999',headers=auth).status_code,403)
        self.assertEqual(c.get('/api/v3/market/quotes?symbols=2345',headers=auth).status_code,503)
        self.assertEqual(c.get('/api/v3/fund/performance').status_code,401)
        r=c.get('/api/v3/fund/performance',headers=auth);self.assertEqual(r.status_code,200)
        self.assertFalse(r.json['reconciled']);self.assertEqual(r.json['positions'],{})
        self.assertEqual(r.json['data_mode'],'synthetic_only')
        self.assertEqual(c.post('/api/v3/fund/performance',headers=auth).status_code,405)
        self.assertEqual(c.get('/api/v3/fill',headers=auth).status_code,404)
        self.assertIn(b'STAGING',c.get('/v3/fund').data)

    def test_gunicorn_loopback_smoke(self):
        """Real HTTP against the staging start command, no Render or provider calls."""
        import importlib.util
        import json
        import os
        from pathlib import Path
        import secrets
        import socket
        import subprocess
        import sys
        import tempfile
        import time
        import urllib.error
        import urllib.request
        if any(importlib.util.find_spec(n) is None for n in ('flask','gunicorn')):
            self.skipTest('Flask/Gunicorn unavailable')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        token=secrets.token_urlsafe(48)
        env={k:os.environ[k] for k in ('PATH','LANG','LC_ALL') if k in os.environ}
        env.update(V3_STAGING_TOKEN=token,PYTHONUNBUFFERED='1')
        base=f'http://127.0.0.1:{port}'
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(path,auth=None,method='GET'):
            headers={'Authorization':'Bearer '+auth} if auth is not None else {}
            req=urllib.request.Request(base+path,headers=headers,method=method)
            try:
                with opener.open(req,timeout=2) as r:
                    return r.status,r.headers,r.read()
            except urllib.error.HTTPError as e:
                with e:
                    return e.code,e.headers,e.read()
        with tempfile.TemporaryFile() as log:
            proc=subprocess.Popen([sys.executable,'-m','gunicorn','v3_staging:create_app()',
                '--bind',f'127.0.0.1:{port}','--workers','1','--threads','2','--timeout','30'],
                cwd=Path(__file__).resolve().parent,env=env,stdout=log,stderr=log)
            try:
                deadline=time.monotonic()+20
                while True:
                    if proc.poll() is not None or time.monotonic()>deadline:
                        self.fail('isolated Gunicorn server failed startup')
                    try:
                        status,headers,body=get('/healthz')
                        if status==200:break
                    except (OSError,urllib.error.URLError):pass
                    time.sleep(.1)
                self.assertFalse(json.loads(body)['execution_authorized'])
                self.assertEqual(headers['X-V3-Data-Mode'],'synthetic-only')
                for path in ('/api/v3/fund/performance','/api/v3/market/quotes?symbols=2330'):
                    self.assertEqual(get(path)[0],401)
                    self.assertEqual(get(path,'wrong')[0],401)
                    status,h,b=get(path,token)
                    self.assertEqual(status,200);self.assertEqual(h['Cache-Control'],'no-store')
                    self.assertEqual(json.loads(b)['data_mode'],'synthetic_only')
                    self.assertEqual(get(path,token,'POST')[0],405)
                self.assertEqual(get('/api/v3/fill',token)[0],404)
                status,h,b=get('/v3/fund')
                self.assertEqual(status,200)
                self.assertIn('frame-ancestors',h['Content-Security-Policy'])
                self.assertNotIn(token.encode(),b)
            finally:
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill();proc.wait(timeout=5)
