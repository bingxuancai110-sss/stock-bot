import ast
from pathlib import Path
from unittest.mock import Mock

from flask import Flask, jsonify, request


def test_cash_form_returns_json_even_if_webview_rewrites_accept_header():
    source = Path(__file__).parents[1] / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    route = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "web_positions")
    route.decorator_list = []
    reply_index = next(index for index, node in enumerate(route.body)
                       if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                       and node.test.id == "cash_json")
    route.body = route.body[:reply_index + 1]
    save = Mock(return_value=("3371.00", "", 200))
    clear = Mock()
    scope = dict(request=request, jsonify=jsonify, time=Mock(),
                 valid_web_csrf=lambda uid: request.form.get("csrf_token") == "fixture",
                 save_portfolio_cash=save, _web_fragment_cache_clear=clear)
    exec(compile(ast.Module(body=[route], type_ignores=[]), str(source), "exec"), scope)
    app = Flask(__name__)
    app.add_url_rule("/web/positions", view_func=lambda: scope["web_positions"]("owner"), methods=["POST"])
    client = app.test_client()
    form = dict(action="save_cash", portfolio_cash="3371", response_format="json")
    headers = {"Accept": "text/html,application/xhtml+xml,*/*"}

    rejected = client.post("/web/positions", data=form, headers=headers)
    assert rejected.status_code == 403 and rejected.is_json
    save.assert_not_called()
    clear.assert_not_called()

    response = client.post("/web/positions", data=dict(form, csrf_token="fixture"), headers=headers)
    assert response.status_code == 200 and "Location" not in response.headers
    assert response.get_json() == {"cash": "3371.00", "message": "", "ok": True}
    save.assert_called_once_with("owner", "3371")
    assert clear.call_count == 2
