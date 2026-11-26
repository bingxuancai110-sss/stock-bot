from datetime import datetime, timedelta
import unittest
try:
    from flask import Flask
except ImportError:
    Flask = None
from v3_market_api import V3Market, register_v3_market_api, TZ, build_fugle_market


class FakeCollector:
    def __init__(self):
        self.calls = 0
        self.rows = {}
    def collect(self, codes, now, previous):
        self.calls += 1
        return dict(trading_date=now.date().isoformat(), quotes=self.rows, status='ready')


@unittest.skipIf(Flask is None, "Flask unavailable")
class MarketTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 26, 10, 15, tzinfo=TZ)
        self.collector = FakeCollector()
        self.market = V3Market(self.collector, ['2382', '2345', 'IX0001'], now=lambda:self.now)
        self.app = Flask(__name__)
        self.token = 'x'*40
        register_v3_market_api(self.app, self.market, token=self.token)
        self.client = self.app.test_client()
        self.collector.rows['2382'] = self.row('2382')
    def row(self, code, **kwargs):
        return dict(dict(code=code, price=100, previous_close=98, time=self.now.timestamp()*1e6,
                         observed_at=self.now.timestamp()*1000, closed=False, type='EQUITY'), **kwargs)
    def get(self, codes='2382', auth=True):
        return self.client.get('/api/v3/market/quotes', query_string={'symbols':codes},
                               headers={'Authorization':'Bearer '+self.token} if auth else {})
    def test_auth(self):
        self.assertEqual(self.get(auth=False).status_code, 401)
        self.assertEqual(self.collector.calls, 0)
    def test_invalid(self):
        self.assertEqual(self.get('2382,').status_code, 400)
    def test_outside_universe(self):
        self.assertEqual(self.get('2330').status_code, 403)
    def test_empty(self):
        self.assertEqual(self.get().status_code, 503)
    def test_refresh_read_no_provider(self):
        self.market.refresh()
        for _ in range(3):
            r = self.get()
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json['quotes']['2382']['price'], 100)
            self.assertFalse(r.json['execution_authorized'])
            self.assertEqual(r.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.collector.calls, 1)
    def test_partial(self):
        self.market.refresh()
        r = self.get('2382,2345')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json['complete'])
        self.assertEqual(r.json['missing'], ['2345'])
    def test_stale(self):
        self.market.refresh()
        self.now += timedelta(minutes=4)
        self.assertEqual(self.get().status_code, 503)
    def test_next_day(self):
        self.market.refresh()
        self.now += timedelta(days=1)
        self.assertEqual(self.get().status_code, 503)
    def test_final_close(self):
        self.now = self.now.replace(hour=13, minute=30)
        self.collector.rows['2382'] = self.row('2382', closed=True)
        self.market.refresh()
        self.now += timedelta(hours=3)
        self.assertEqual(self.get().json['quotes']['2382']['status'], 'final_close')
    def test_not_final(self):
        self.now = self.now.replace(hour=13, minute=30)
        self.collector.rows['2382'] = self.row('2382')
        self.market.refresh()
        self.assertEqual(self.get().status_code, 503)
    def test_trial_and_invalid_values(self):
        for bad in ({'is_trial':True}, {'price':float('nan')}, {'price':True}, {'type':'ODDLOT'}):
            self.collector.rows['2382'] = self.row('2382', **bad)
            self.market.refresh()
            self.assertEqual(self.get().status_code, 503)
    def test_wrong_index_type(self):
        self.collector.rows['IX0001'] = self.row('IX0001')
        self.market.refresh()
        self.assertEqual(self.get('IX0001').status_code, 503)
    def test_weekend(self):
        self.now += timedelta(days=2)
        self.assertEqual(self.market.refresh(), 'outside_session')
        self.assertEqual(self.collector.calls, 0)
    def test_missing_token(self):
        with self.assertRaises(ValueError):
            register_v3_market_api(Flask('other'), self.market, token='short')
    def test_missing_provider_key(self):
        with self.assertRaises(ValueError):
            build_fugle_market('', ['2382'], now=lambda:self.now)

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 26, 10, 15, tzinfo=TZ)
        self.collector = FakeCollector()
        self.market = V3Market(self.collector, ['2382', '2345', 'IX0001'], now=lambda:self.now)
        self.row = dict(code='2382', price=100, previous_close=98, time=self.now.timestamp()*1e6,
                        observed_at=self.now.timestamp()*1000, closed=False, type='EQUITY')
        self.collector.rows['2382'] = self.row
    def test_no_data(self):
        self.assertEqual(self.market.read(['2382'])[1], 503)
    def test_snapshot_and_no_fetch(self):
        self.assertEqual(self.market.refresh(), 'ready')
        for _ in range(3):
            data, status = self.market.read(['2382'])
            self.assertEqual(status, 200)
            self.assertEqual(data['quotes']['2382']['price'], 100)
            self.assertFalse(data['execution_authorized'])
        self.assertEqual(self.collector.calls, 1)
    def test_partial(self):
        self.market.refresh()
        data, status = self.market.read(['2382','2345'])
        self.assertEqual(status, 200)
        self.assertFalse(data['complete'])
        self.assertEqual(data['missing'], ['2345'])
    def test_stale(self):
        self.market.refresh()
        self.now += timedelta(minutes=4)
        self.assertEqual(self.market.read(['2382'])[1], 503)
    def test_next_day(self):
        self.market.refresh()
        self.now += timedelta(days=1)
        self.assertEqual(self.market.read(['2382'])[1], 503)
    def test_final_close(self):
        self.now = self.now.replace(hour=13, minute=30)
        self.row.update(time=self.now.timestamp()*1e6, observed_at=self.now.timestamp()*1000, closed=True)
        self.market.refresh()
        self.now += timedelta(hours=3)
        self.assertEqual(self.market.read(['2382'])[0]['quotes']['2382']['status'], 'final_close')
    def test_unconfirmed_close(self):
        self.now = self.now.replace(hour=13, minute=30)
        self.market.refresh()
        self.assertEqual(self.market.read(['2382'])[1], 503)
    def test_invalid_rows(self):
        for bad in ({'is_trial':True}, {'price':float('nan')}, {'price':True}, {'type':'ODDLOT'}, {'code':'9999'}):
            self.collector.rows['2382'] = dict(self.row, **bad)
            self.market.refresh()
            self.assertEqual(self.market.read(['2382'])[1], 503)
    def test_bad_index(self):
        self.collector.rows['IX0001'] = dict(self.row, code='IX0001')
        self.market.refresh()
        self.assertEqual(self.market.read(['IX0001'])[1], 503)
    def test_weekend(self):
        self.now += timedelta(days=2)
        self.assertEqual(self.market.refresh(), 'outside_session')
        self.assertEqual(self.collector.calls, 0)
    def test_invalid_universe(self):
        for codes in ([], ['abc'], ['2330'] + [str(1000+i) for i in range(20)]):
            with self.assertRaises(ValueError):
                V3Market(self.collector, codes, now=lambda:self.now)
    def test_missing_provider_key(self):
        with self.assertRaises(ValueError):
            build_fugle_market('', ['2382'], now=lambda:self.now)

if __name__ == '__main__':
    unittest.main()
