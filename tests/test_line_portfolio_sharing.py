import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from line_portfolio_sharing import shared_owner, SharingSettings


def test_single_owner_and_explicit_multi_admin_mapping():
    assert shared_owner({"ADMIN_USER_ID": " owner, owner "}) == "owner"
    assert shared_owner({"ADMIN_USER_ID": "owner,other",
                         "LINE_SHARED_PORTFOLIO_USER_ID": "owner"}) == "owner"


@pytest.mark.parametrize("env", [{}, {"ADMIN_USER_ID": "owner,other"},
    {"ADMIN_USER_ID": "owner", "LINE_SHARED_PORTFOLIO_USER_ID": "stranger"}])
def test_ambiguous_or_non_admin_account_is_never_exposed(env):
    assert shared_owner(env) is None


@pytest.fixture(scope="module")
def selected_tree():
    source = Path("main.py").read_text(encoding="utf-8-sig")
    names = {"handle_message", "_line_shared_owner_portfolio_flex"}
    selected = []
    for name in names:
        match = re.search(r"(?ms)^def " + name + r"\(.*?(?=^(?:def |class |@)|\Z)", source)
        assert match
        selected.extend(node for node in ast.parse(match[0]).body
                        if isinstance(node, ast.FunctionDef) and node.name == name)
    return ast.Module(body=selected, type_ignores=[])


@pytest.fixture(scope="module")
def functions(selected_tree):
    nodes = selected_tree.body
    for node in nodes:
        node.decorator_list = []
    return compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec")


@pytest.mark.parametrize("command", ["秉軒持股", "秉軒的持股", "高雄許光漢的持股", "秉軒操作日報"])
def test_shared_command_reads_owner_but_replies_to_requester(functions, command):
    calls = []
    def query(recipient, feature, builder, reply_token, base):
        calls.append((recipient, feature, builder(), reply_token))
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ({"user_id": "owner"},)
    ns = dict(os=SimpleNamespace(environ={"ADMIN_USER_ID": "owner,other"}),
              is_admin=lambda uid: uid in {"owner", "other"},
              get_db_connection=lambda: conn, release_db_connection=lambda conn: None,
              normalize_code=lambda text: "", public_web_base_url=lambda url: url,
              request=SimpleNamespace(url_root="https://example.test/"),
              _line_async_query=query,
              _line_hidden_combined_portfolio_flex=lambda uid: {"read_user": uid})
    exec(functions, ns)
    event = SimpleNamespace(source=SimpleNamespace(user_id="visitor"),
                            message=SimpleNamespace(text=command), reply_token="visitor-token")
    ns["handle_message"](event)
    assert calls == [("visitor", "positions_export", {"read_user": "owner"}, "visitor-token")]


def test_binding_is_admin_only_atomic_and_idempotent():
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("key",)
    settings = SharingSettings(lambda: conn, lambda conn: None, {"ADMIN_USER_ID": "owner,other"})
    assert settings.enable("visitor") is False
    assert conn.cursor.call_count == 0
    assert settings.enable("owner") is True
    sql, params = cursor.execute.call_args.args
    assert "ON CONFLICT" in sql and "WHERE shared_data_snapshots.payload->>'user_id'=EXCLUDED.payload->>'user_id'" in sql
    assert '"user_id": "owner"' in params[1]
    cursor.fetchone.return_value = None
    assert settings.enable("other") is False


def test_removed_admin_binding_is_not_shared():
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ({"user_id": "removed"},)
    assert SharingSettings(lambda: conn, lambda conn: None, {"ADMIN_USER_ID": "owner"}).owner() is None


@pytest.mark.parametrize("requester", ["owner", "visitor", "other"])
def test_everyone_reads_same_authorized_owner_without_manual_binding(functions, requester):
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    replies = []
    ns = dict(os=SimpleNamespace(environ={"ADMIN_USER_ID": "owner,other", "FUGLE_TEST_USER_ID": "owner"}),
              get_db_connection=lambda: conn, release_db_connection=lambda conn: None,
              normalize_code=lambda text: "", public_web_base_url=lambda url: url,
              request=SimpleNamespace(url_root="https://example.test/"),
              _line_hidden_combined_portfolio_flex=lambda uid: {"read_user": uid},
              _line_async_query=lambda uid, feature, builder, token, base: replies.append((uid, builder())))
    exec(functions, ns)
    ns["handle_message"](SimpleNamespace(source=SimpleNamespace(user_id=requester, type="user"),
        message=SimpleNamespace(text="秉軒的持股"), reply_token="token"))
    assert replies == [(requester, {"read_user": "owner"})]


@pytest.mark.parametrize("rows,expected", [([("owner",)], "owner"), ([], None),
    ([("owner",), ("other",)], None), ([("visitor",)], None)])
def test_existing_verified_email_mapping_is_exact_unique_and_admin_only(rows, expected):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = rows
    env = {"ADMIN_USER_ID": "owner,other", "FUGLE_TEST_USER_EMAIL": " OWNER@EXAMPLE.TEST "}
    assert SharingSettings(lambda: conn, lambda conn: None, env).owner() == expected
    sql, params = cursor.execute.call_args.args
    assert "provider='google'" in sql and "LOWER(email)=%s" in sql
    assert params == ("owner@example.test",)


def test_invalid_explicit_sharing_setting_does_not_select_another_account():
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    env = {"ADMIN_USER_ID": "owner,other", "LINE_SHARED_PORTFOLIO_USER_ID": "visitor",
           "FUGLE_TEST_USER_ID": "owner"}
    assert SharingSettings(lambda: conn, lambda conn: None, env).owner() is None


@pytest.mark.parametrize("uid,source", [("visitor", "user"), ("owner", "group")])
def test_public_sharing_setup_rejects_visitors_and_groups(functions, uid, source):
    api = SimpleNamespace(reply_message=MagicMock())
    ns = dict(normalize_code=lambda text: "", is_admin=lambda uid: uid == "owner",
              line_bot_api=api, TextSendMessage=lambda **kw: kw)
    exec(functions, ns)
    ns["handle_message"](SimpleNamespace(source=SimpleNamespace(user_id=uid, type=source),
        message=SimpleNamespace(text="公開我的持股"), reply_token="token"))
    assert "限管理員本人" in api.reply_message.call_args.args[1]["text"]


def test_normal_holdings_command_still_reads_requester(selected_tree):
    handler = next(node for node in selected_tree.body if node.name == "handle_message")
    private = [node for node in ast.walk(handler) if isinstance(node, ast.Lambda)
               and isinstance(node.body, ast.Call) and isinstance(node.body.func, ast.Name)
               and node.body.func.id == "build_holdings_text"]
    assert private and all(ast.unparse(node.body.args[0]) == "user_id" for node in private)
