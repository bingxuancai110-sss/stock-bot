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
