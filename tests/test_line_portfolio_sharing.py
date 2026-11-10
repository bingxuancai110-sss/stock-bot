import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from line_portfolio_sharing import shared_owner


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
    ns = dict(os=SimpleNamespace(environ={"ADMIN_USER_ID": "owner"}),
              normalize_code=lambda text: "", public_web_base_url=lambda url: url,
              request=SimpleNamespace(url_root="https://example.test/"),
              _line_async_query=query,
              _line_hidden_combined_portfolio_flex=lambda uid: {"read_user": uid})
    exec(functions, ns)
    event = SimpleNamespace(source=SimpleNamespace(user_id="visitor"),
                            message=SimpleNamespace(text=command), reply_token="visitor-token")
    ns["handle_message"](event)
    assert calls == [("visitor", "positions_export", {"read_user": "owner"}, "visitor-token")]


def test_normal_holdings_command_still_reads_requester(selected_tree):
    handler = next(node for node in selected_tree.body if node.name == "handle_message")
    private = [node for node in ast.walk(handler) if isinstance(node, ast.Lambda)
               and isinstance(node.body, ast.Call) and isinstance(node.body.func, ast.Name)
               and node.body.func.id == "build_holdings_text"]
    assert private and all(ast.unparse(node.body.args[0]) == "user_id" for node in private)
