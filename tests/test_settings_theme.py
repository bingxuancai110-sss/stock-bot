import ast
from functools import wraps
import html
from pathlib import Path
import re
from unittest.mock import MagicMock, Mock
from urllib.parse import quote

from flask import Flask, request, abort, make_response
import pytest

from settings_theme import ThemeStore, create_blueprint

SOURCE = Path('main.py').read_text(encoding='utf-8-sig')
THEMES = ast.literal_eval(re.search(r'(?ms)^WEB_UI_THEMES = (\{.*?^\})', SOURCE)[1])


def actual_settings(uid='fixture-user', theme='mist'):
    ns = dict(request=request, html=html, quote=quote, make_response=make_response,
        WEB_UI_THEMES=THEMES, get_web_ui_theme=lambda uid:theme,
        is_admin=lambda uid:False, valid_web_csrf=lambda:True,
        save_web_ui_theme=lambda uid,value:True,
        csrf_hidden_input=lambda:'<input type="hidden" name="csrf_token" value="fixture-csrf">',
        inject_csrf_inputs=lambda body:body, preserve_web_token=lambda body:body,
        render_page=lambda title,body,nav_active:'<header class="app-header"><h1>'+title+'</h1></header><main id="app-page-content">'+body+'</main>')
    for name in ('wants_fragment','respond_page','web_settings'):
        match=re.search(r'(?ms)^def '+name+r'\b.*?(?=^(?:def |class |@app\.|@web_login_required)|\Z)',SOURCE)
        node=ast.parse(match[0]).body[0];node.decorator_list=[]
        exec(compile(ast.Module(body=[node],type_ignores=[]),name,'exec'),ns)
    return ns['web_settings'](uid)


def test_real_settings_fragment_never_includes_a_second_page_header():
    app=Flask(__name__)
    with app.test_request_context('/web/settings?fragment=1'):
        response=actual_settings()
        body=response.get_data(as_text=True)
        assert 'data-settings-theme-form' in body
        assert '<html' not in body and 'class="app-header"' not in body
        assert response.headers['Cache-Control'].startswith('no-store')
    with app.test_request_context('/web/settings'):
        assert actual_settings().count('class="app-header"') == 1


def test_legacy_global_submit_guard_excludes_only_managed_theme_form():
    assert 'form[data-settings-theme-form]' in SOURCE
    assert 'form.add, form.sellpanel, form[data-form-purpose="leaderboard-settlement"], form[data-settings-theme-form]' in SOURCE


def client(store=None):
    store = store or Mock()
    def login(fn):
        @wraps(fn)
        def wrapper():
            uid=request.headers.get('X-Fixture-User')
            if not uid:abort(401)
            return fn(uid)
        return wrapper
    app=Flask(__name__)
    app.register_blueprint(create_blueprint(web_login_required=login,
        valid_csrf=lambda uid:request.form.get('csrf_token') == 'csrf-'+uid,
        store_factory=lambda:store,themes=THEMES))
    return app.test_client(),store


def test_authenticated_user_binding_and_persisted_acknowledgment():
    http,store=client();store.save.return_value='warm';store.read.return_value='warm'
    response=http.post('/web/api/settings/theme',headers={'X-Fixture-User':'owner'},
        data={'ui_theme':'warm','csrf_token':'csrf-owner','user_id':'other'})
    assert response.status_code == 200 and response.json['theme'] == 'warm'
    store.save.assert_called_once_with('owner','warm')
    assert response.json['state'] == 'saved' and response.headers['Cache-Control'] == 'no-store'
    assert http.get('/web/api/settings/theme',headers={'X-Fixture-User':'owner'}).json['theme'] == 'warm'
    store.read.assert_called_once_with('owner')


def test_login_csrf_and_invalid_theme_are_rejected_without_writes():
    http,store=client()
    assert http.post('/web/api/settings/theme',data={'ui_theme':'warm'}).status_code == 401
    assert http.post('/web/api/settings/theme',headers={'X-Fixture-User':'owner'},data={'ui_theme':'warm','csrf_token':'csrf-other'}).status_code == 403
    assert http.post('/web/api/settings/theme',headers={'X-Fixture-User':'owner'},data={'ui_theme':'bad','csrf_token':'csrf-owner'}).status_code == 400
    store.save.assert_not_called()


def test_database_failure_is_unknown_instead_of_false_success_or_mist_default():
    http,store=client();store.save.side_effect=RuntimeError('connection lost');store.read.side_effect=RuntimeError('unavailable')
    response=http.post('/web/api/settings/theme',headers={'X-Fixture-User':'owner'},data={'ui_theme':'warm','csrf_token':'csrf-owner'})
    assert response.status_code == 503 and response.json['state'] == 'unknown'
    response=http.get('/web/api/settings/theme',headers={'X-Fixture-User':'owner'})
    assert response.status_code == 503 and 'theme' not in response.json


def test_store_changes_only_theme_for_requested_user_and_commits_before_return():
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value=('stone',);release=Mock()
    assert ThemeStore(lambda:conn,release,THEMES).save('owner','stone') == 'stone'
    sql,params=cur.execute.call_args.args
    assert params == ('owner','stone') and 'RETURNING ui_theme' in sql
    assert 'positions' not in sql and 'ui_theme=EXCLUDED.ui_theme' in sql
    conn.commit.assert_called_once();release.assert_called_once_with(conn)


def test_commit_failure_or_missing_returning_row_never_acknowledged():
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value=('warm',);conn.commit.side_effect=RuntimeError('lost commit response')
    with pytest.raises(RuntimeError):ThemeStore(lambda:conn,lambda c:None,THEMES).save('owner','warm')
    conn.rollback.assert_called_once()
    conn=MagicMock();conn.cursor.return_value.__enter__.return_value.fetchone.return_value=None
    with pytest.raises(ValueError):ThemeStore(lambda:conn,lambda c:None,THEMES).save('owner','mist')
    conn.commit.assert_not_called()


def test_effective_default_only_when_database_read_succeeds_without_profile():
    conn=MagicMock();conn.cursor.return_value.__enter__.return_value.fetchone.return_value=None
    assert ThemeStore(lambda:conn,lambda c:None,THEMES).read('owner') == 'mist'
