from functools import wraps
from unittest.mock import Mock

from flask import Flask, abort, request
import pytest

from quote_lab import allowed_user, create_blueprint


@pytest.fixture(autouse=True)
def clear_config(monkeypatch):
    monkeypatch.delenv('FUGLE_TEST_USER_ID', raising=False)
    monkeypatch.delenv('FUGLE_TEST_USER_EMAIL', raising=False)


def database(rows):
    from unittest.mock import MagicMock
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=rows
    return conn,cur


def client(monkeypatch, rows=None, **extra):
    conn,cur=database(rows or [])
    def login(fn):
        @wraps(fn)
        def wrapper():
            uid=request.headers.get('X-Fixture-User')
            if not uid:abort(401)
            return fn(uid)
        return wrapper
    app=Flask(__name__)
    app.register_blueprint(create_blueprint(web_login_required=login,
        render_page=lambda title,body,active:body,
        get_db_connection=lambda:conn,release_db_connection=Mock(), **extra))
    return app.test_client(),cur


def test_closed_until_owner_configured(monkeypatch):
    c,cur=client(monkeypatch)
    assert c.get('/web/quote-lab').status_code==401
    assert c.get('/web/quote-lab',headers={'X-Fixture-User':'owner'}).status_code==404
    cur.execute.assert_not_called()


def test_background_snapshot_is_authenticated_owner_only_and_never_downloads(monkeypatch):
    monkeypatch.setenv('FUGLE_TEST_USER_ID', 'owner')
    service = Mock()
    service.enabled_for.side_effect = lambda uid: uid == 'owner'
    service.read.return_value = {'quotes': {'2308': {'code': '2308'}},
                                 'checked_at': '2026-11-04T10:30:00+08:00', 'status': 'ready'}
    apply = Mock(return_value={'home': {}, 'positions': {}})
    c, _ = client(monkeypatch, background_service=service, apply_background=apply)
    assert c.get('/web/quote-lab/background').status_code == 401
    assert c.get('/web/quote-lab/background', headers={'X-Fixture-User': 'other'}).status_code == 403
    service.read.assert_not_called()
    response = c.get('/web/quote-lab/background', headers={'X-Fixture-User': 'owner'})
    assert response.status_code == 200 and 'no-store' in response.headers['Cache-Control']
    assert response.json['codes'] == ['2308']
    apply.assert_called_once_with('owner', [{'code': '2308'}])
    service.tick.assert_not_called()


def test_uid_owner_only_and_no_store(monkeypatch):
    monkeypatch.setenv('FUGLE_TEST_USER_ID','owner')
    c,cur=client(monkeypatch)
    assert c.get('/web/quote-lab',headers={'X-Fixture-User':'someone-else'}).status_code==403
    r=c.get('/web/quote-lab',headers={'X-Fixture-User':'owner'})
    assert r.status_code==200
    assert 'no-store' in r.headers['Cache-Control']
    assert r.headers['Referrer-Policy']=='no-referrer'
    assert r.headers['X-Content-Type-Options']=='nosniff'
    assert 'id="ql-key" type="password"' in r.text
    assert c.post('/web/quote-lab',headers={'X-Fixture-User':'owner'},data={'key':'fixture-secret'}).status_code==405
    cur.execute.assert_not_called()


def test_email_matches_existing_google_identity_and_not_any_signed_in_user(monkeypatch):
    monkeypatch.setenv('FUGLE_TEST_USER_EMAIL',' OWNER@EXAMPLE.TEST ')
    c,cur=client(monkeypatch,[('linked-line-or-google-owner',)])
    assert c.get('/web/quote-lab',headers={'X-Fixture-User':'linked-line-or-google-owner'}).status_code==200
    assert c.get('/web/quote-lab',headers={'X-Fixture-User':'other'}).status_code==403
    assert cur.execute.call_args.args[1]==('owner@example.test',)
    assert "provider='google'" in cur.execute.call_args.args[0]


@pytest.mark.parametrize('rows',[[],[('owner',),('different-owner',)]])
def test_missing_or_ambiguous_email_binding_denies_access(monkeypatch,rows):
    monkeypatch.setenv('FUGLE_TEST_USER_EMAIL','owner@example.test')
    conn,cur=database(rows);release=Mock()
    assert not allowed_user('owner',lambda:conn,release)
    release.assert_called_once_with(conn)


def test_database_failure_denies_access_and_releases(monkeypatch):
    monkeypatch.setenv('FUGLE_TEST_USER_EMAIL','owner@example.test')
    conn,cur=database([]);cur.execute.side_effect=RuntimeError('database unavailable');release=Mock()
    assert not allowed_user('owner',lambda:conn,release)
    conn.rollback.assert_called_once();release.assert_called_once_with(conn)


def test_explicit_uid_precedes_email_without_query(monkeypatch):
    monkeypatch.setenv('FUGLE_TEST_USER_ID','owner')
    monkeypatch.setenv('FUGLE_TEST_USER_EMAIL','different@example.test')
    get=Mock()
    assert allowed_user('owner',get,Mock())
    assert not allowed_user('other',get,Mock())
    get.assert_not_called()


def test_holdings_endpoint_owner_only_and_positive_lots_merged(monkeypatch):
    monkeypatch.setenv('FUGLE_TEST_USER_ID','owner')
    c,cur=client(monkeypatch,[('2308','台達電'),('00981A','測試 ETF')])
    assert c.get('/web/quote-lab/holdings').status_code==401
    assert c.get('/web/quote-lab/holdings',headers={'X-Fixture-User':'other'}).status_code==403
    cur.execute.assert_not_called()
    r=c.get('/web/quote-lab/holdings?uid=someone-else',headers={'X-Fixture-User':'owner'})
    assert r.status_code==200
    assert r.json['holdings']==[{'code':'2308','name':'台達電'},{'code':'00981A','name':'測試 ETF'}]
    query,params=cur.execute.call_args.args
    assert params==('owner',)
    assert 'COALESCE(p.shares,0)>0' in query and 'GROUP BY p.code' in query
    assert 'no-store' in r.headers['Cache-Control']


def test_holdings_query_failure_is_not_an_empty_portfolio(monkeypatch):
    monkeypatch.setenv('FUGLE_TEST_USER_ID','owner')
    c,cur=client(monkeypatch)
    cur.execute.side_effect=RuntimeError('database unavailable')
    r=c.get('/web/quote-lab/holdings',headers={'X-Fixture-User':'owner'})
    assert r.status_code==503
    assert r.json['ok'] is False
    assert 'holdings' not in r.json


def test_empty_holdings_is_valid_and_bad_codes_are_reported(monkeypatch):
    monkeypatch.setenv('FUGLE_TEST_USER_ID','owner')
    c,cur=client(monkeypatch)
    assert c.get('/web/quote-lab/holdings',headers={'X-Fixture-User':'owner'}).json['holdings']==[]
    cur.fetchall.return_value=[('2308','台達電'),('<bad>','invalid')]
    result=c.get('/web/quote-lab/holdings',headers={'X-Fixture-User':'owner'}).json
    assert len(result['holdings'])==1 and result['unsupported_count']==1
    assert c.post('/web/quote-lab/holdings',headers={'X-Fixture-User':'owner'}).status_code==405
