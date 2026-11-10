import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest


@pytest.fixture(scope="module")
def functions():
    tree = ast.parse((Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8"))
    selected = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
                "web_admin_taiex_reduction", "cron_taiex_reduction", "_run_taiex_reduction_alerts",
                "handle_taiex_admin_postback", "_reply_taiex_line_card", "cron_taiex_reduction_status", "_taiex_reduction_connect"}:
            node.decorator_list = []
            selected[node.name] = compile(ast.Module(body=[node], type_ignores=[]), "<main-route>", "exec")
    return selected


def load(functions, name, ns):
    exec(functions[name], ns)
    return ns[name]


def admin_env(form=None):
    service = Mock()
    service.send_test.return_value = "LINE已接受測試"
    return {
        "is_admin": lambda uid: uid == "admin-self",
        "make_response": lambda text, code: (text, code),
        "_admin_user_rows": Mock(return_value=[("admin-self", "owner"), ("other", "user")]),
        "_taiex_reduction_service": Mock(return_value=service),
        "request": SimpleNamespace(method="POST", form=form or {}, args={}),
        "valid_web_csrf": Mock(return_value=True),
        "redirect": lambda url: url, "quote": quote,
        "run_in_background": Mock(), "_run_taiex_reduction_alerts": Mock(),
    }, service


def test_non_admin_and_bad_csrf_cannot_send_or_change_settings(functions):
    for uid, csrf in (("other", True), ("admin-self", False)):
        ns, service = admin_env({"action": "test"})
        ns["valid_web_csrf"].return_value = csrf
        reply = load(functions, "web_admin_taiex_reduction", ns)(uid)
        assert reply[1] == 403
        service.send_test.assert_not_called()
        service.admin_settings.assert_not_called()


def test_test_recipient_is_forced_to_verified_admin_not_forged_form(functions):
    ns, service = admin_env({"action": "test", "target": "other"})
    load(functions, "web_admin_taiex_reduction", ns)("admin-self")
    service.send_test.assert_called_once_with("admin-self")


def test_admin_permission_target_must_exist(functions):
    ns, service = admin_env({"action": "settings", "target": "unknown", "mode": "on"})
    assert load(functions, "web_admin_taiex_reduction", ns)("admin-self")[1] == 400
    service.admin_settings.assert_not_called()


def test_admin_can_enable_existing_user_and_queue_check(functions):
    ns, service = admin_env({"action": "settings", "target": "other", "mode": "on"})
    load(functions, "web_admin_taiex_reduction", ns)("admin-self")
    service.admin_settings.assert_called_once_with("admin-self", target="other", mode="on")
    ns["run_in_background"].assert_called_once()


def test_cron_empty_or_wrong_secret_rejected_before_work(functions):
    def abort(code):
        raise PermissionError(code)
    for configured, token in ((None, None), ("secret", None), ("secret", "wrong")):
        ns = {"os": SimpleNamespace(environ={"CRON_SECRET": configured}),
              "request": SimpleNamespace(args={"token": token}), "abort": abort,
              "run_in_background": Mock(), "_require_taiex_cron_auth": lambda: abort(403)}
        with pytest.raises(PermissionError):
            load(functions, "cron_taiex_reduction", ns)()
        ns["run_in_background"].assert_not_called()


def test_reminder_failure_isolated_from_existing_background_jobs(functions):
    ns = {"_taiex_reduction_service": Mock(side_effect=TimeoutError())}
    assert "未完成" in load(functions, "_run_taiex_reduction_alerts", ns)()


def test_postback_only_handles_its_own_feature(functions):
    ns = {"_reply_taiex_line_card": Mock(), "_taiex_admin_line_action": Mock(return_value={"type": "flex"})}
    handler = load(functions, "handle_taiex_admin_postback", ns)
    handler(SimpleNamespace(postback=SimpleNamespace(data="other-feature=value")))
    ns["_reply_taiex_line_card"].assert_not_called()
    event = SimpleNamespace(postback=SimpleNamespace(data="tr=1&action=list"))
    handler(event)
    ns["_taiex_admin_line_action"].assert_called_once_with(event, "tr=1&action=list")


def test_flex_reply_uses_existing_line_reply_mechanism(functions):
    ns = {"FlexSendMessage": Mock(return_value="sdk-flex"), "TextSendMessage": Mock(),
          "line_bot_api": SimpleNamespace(reply_message=Mock())}
    load(functions, "_reply_taiex_line_card", ns)(SimpleNamespace(reply_token="existing-token"),
        {"type": "flex", "altText": "title", "contents": {"type": "bubble"}})
    ns["line_bot_api"].reply_message.assert_called_once_with("existing-token", "sdk-flex")


def test_cron_reports_correlated_queue_id_and_connection_failure_as_503(functions):
    jobs = Mock()
    jobs.submit.return_value = "run-id"
    ns = {"_require_taiex_cron_auth": Mock(), "_taiex_reminder_jobs": lambda: jobs,
          "jsonify": lambda value: value}
    route = load(functions, "cron_taiex_reduction", ns)
    assert route() == ({"id": "run-id", "state": "queued"}, 202)
    jobs.submit.side_effect = TimeoutError()
    assert route()[1] == 503


def test_task_connection_is_fresh_ssl_not_global_pool(functions):
    connect = Mock(side_effect=["fresh-1", "fresh-2"])
    ns = {"psycopg2": SimpleNamespace(connect=connect), "_ipv4_addr": "127.0.0.1",
          "_url": SimpleNamespace(path="/testdb", username="test", password="dummy", port=6543)}
    get = load(functions, "_taiex_reduction_connect", ns)
    assert get() == "fresh-1" and get() == "fresh-2"
    assert connect.call_args.kwargs["sslmode"] == "require"
