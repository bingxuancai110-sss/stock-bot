"""Standalone synthetic-only staging app; never imports production main."""
import os
import re
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from v3_fund import Ledger, checkpoint, encode
from v3_release_host import attach_staging

FIXTURE_AT = '2026-11-26T10:15:00+08:00'


class FixtureMarket:
    codes = frozenset({'2330', '2382', '2345'})

    def read(self, symbols):
        prices = {'2330': 100, '2382': 200}
        quotes = {c: dict(price=prices[c], previous_close=prices[c], change_pct=0,
                          trade_at=FIXTURE_AT, observed_at=FIXTURE_AT,
                          source='synthetic_test_fixture', status='synthetic_fixture')
                  for c in symbols if c in prices}
        missing = [c for c in symbols if c not in quotes]
        return dict(data_mode='synthetic_only', as_of=FIXTURE_AT,
                    complete=bool(symbols) and not missing, quotes=quotes,
                    missing=missing, execution_authorized=False), 200 if quotes else 503


def fixture_state():
    state = checkpoint()
    state.update(positions={}, cash='10000000', fees='0.00', ready=False,
                 history_complete=False, last_at=FIXTURE_AT,
                 checkpoint='synthetic fixture only; not historical or current fund',
                 data_mode='synthetic_only')
    return state


def create_app(token=None):
    """Gunicorn factory; no production connections, provider key or schedulers."""
    token = os.environ.get('V3_STAGING_TOKEN', '') if token is None else token
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', token) or len(set(token)) < 16:
        raise ValueError('V3 staging token missing or invalid')
    from flask import Flask, jsonify
    app = Flask(__name__, static_folder=None)
    app.config.update(DEBUG=False, TESTING=False, MAX_CONTENT_LENGTH=4096)
    tmp = TemporaryDirectory(prefix='v3-staging-')
    app.extensions['v3_staging_tmp'] = tmp
    ledger = Ledger(Path(tmp.name) / 'fixture.db')
    ledger.initialize()
    with closing(ledger.connect()) as db, db:
        db.execute('UPDATE state SET payload=? WHERE id=1', (encode(fixture_state()),))
    attach_staging(app, mode='staging-readonly', market=FixtureMarket(), ledger=ledger, token=token)

    @app.get('/healthz')
    def health():
        return jsonify(status='ok', data_mode='synthetic_only', execution_authorized=False)

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-V3-Data-Mode'] = 'synthetic-only'
        return response
    return app
