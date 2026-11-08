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
                "web_admin_taiex_reduction", "cron_taiex_reduction", "_run_taiex_reduction_alerts"}:
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
              "run_in_background": Mock()}
        with pytest.raises(PermissionError):
            load(functions, "cron_taiex_reduction", ns)()
        ns["run_in_background"].assert_not_called()


def test_reminder_failure_isolated_from_existing_background_jobs(functions):
    ns = {"_taiex_reduction_service": Mock(side_effect=TimeoutError())}
    assert "未完成" in load(functions, "_run_taiex_reduction_alerts", ns)()
