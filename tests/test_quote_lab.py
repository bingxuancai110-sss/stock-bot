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


def client(monkeypatch, rows=None):
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
        get_db_connection=lambda:conn,release_db_connection=Mock()))
    return app.test_client(),cur


def test_closed_until_owner_configured(monkeypatch):
    c,cur=client(monkeypatch)
    assert c.get('/web/quote-lab').status_code==401
    assert c.get('/web/quote-lab',headers={'X-Fixture-User':'owner'}).status_code==404
    cur.execute.assert_not_called()


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
