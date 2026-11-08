import json
from datetime import date
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest

from taiex_line_cards import admin_action, alert_card, directory
from taiex_reduction import push_text


def action(**params):
    return urlencode({"tr": "1", **params})


def controller(actor="admin", source="user", data=None, rows=None):
    service = Mock()
    service.admin_settings.return_value = {"target": {"approved": True, "enabled": True}}
    users = Mock(return_value=rows or [("target", "甲"), ("other", "乙")])
    queue = Mock()
    result = admin_action(actor, source, data or action(action="list"), is_admin=lambda uid: uid == "admin",
                          users=users, service=service, queue_check=queue)
    return result, service, users, queue


def test_warning_confirm_and_test_are_flex_cards_without_fake_test_quotes():
    result = {"date": "2026-11-09", "streak": 2, "rows": [
        {"date": "2026-11-06", "close": 100, "ma20": 101},
        {"date": "2026-11-09", "close": 99, "ma20": 101}]}
    warning = alert_card(result, date(2026, 11, 10))
    assert warning["type"] == "flex"
    assert "第 2 日" in warning["altText"]
    assert "尚未減碼" in json.dumps(warning, ensure_ascii=False)
    result["streak"] = 3
    confirm = alert_card(result, date(2026, 11, 10))
    assert "2026-11-10" in json.dumps(confirm, ensure_ascii=False)
    assert "50%" in json.dumps(confirm, ensure_ascii=False)
    test = alert_card(test_date="2026-11-09")
    body = json.dumps(test, ensure_ascii=False)
    assert "並非真實減碼訊號" in body and "收盤 100" not in body
    assert test["contents"]["header"]["backgroundColor"] != confirm["contents"]["header"]["backgroundColor"]


def test_directory_is_paginated_and_targets_stable_user_ids():
    users = [(f"stable-{i}", f"user-{i}") for i in range(12)]
    card = directory(users, {}, 5)
    encoded = json.dumps(card, ensure_ascii=False)
    assert "user-5" in encoded and "user-9" in encoded
    assert "user-10" not in encoded
    assert "target=stable-5" in encoded
    assert "offset=10" in encoded and "offset=0" in encoded
    assert "名單詳情" in encoded


@pytest.mark.parametrize("actor,source", [("stranger", "user"), ("admin", "group"), ("admin", "room")])
def test_non_admin_or_group_cannot_read_users_or_change_permissions(actor, source):
    result, service, users, queue = controller(actor, source, action(action="set", target="target", mode="on"))
    assert result["type"] == "text"
    users.assert_not_called()
    service.admin_settings.assert_not_called()
    queue.assert_not_called()


@pytest.mark.parametrize("mode", ["on", "allowed", "off"])
def test_inline_controls_save_explicit_mode_and_redisplay_latest_state(mode):
    card, service, _, queue = controller(data=action(action="set", target="target", mode=mode))
    service.admin_settings.assert_called_once_with("admin", "target", mode)
    assert "設定已儲存" in json.dumps(card, ensure_ascii=False)
    assert queue.call_count == (1 if mode == "on" else 0)


def test_reordering_users_does_not_change_target():
    _, service, _, _ = controller(data=action(action="set", target="target", mode="off"),
                                  rows=[("other", "乙"), ("target", "甲")])
    service.admin_settings.assert_called_once_with("admin", "target", "off")


@pytest.mark.parametrize("data", [
    "tr=1&action=set&target=unknown&mode=on",
    "tr=1&action=set&target=target&mode=invalid",
    "tr=1&action=set&target=target&target=other&mode=on",
    "tr=1&action=delete", "tr=0&action=list", "tr=1&action=list&offset=bad",
])
def test_malformed_or_forged_actions_do_not_apply(data):
    with pytest.raises(ValueError):
        controller(data=data)


def test_test_action_sends_to_actor_even_if_target_is_forged():
    _, service, users, _ = controller(data=action(action="test", target="other"))
    service.send_test.assert_called_once_with("admin")
    users.assert_not_called()


def test_card_transport_keeps_persisted_json_and_retry_key_exact():
    card = alert_card(test_date="2026-11-09")
    stored = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    response = Mock(status_code=200)
    post = Mock(return_value=response)
    push_text("secret", "recipient", stored, "fixed", post)
    assert post.call_args.kwargs["json"]["messages"] == [card]
    assert post.call_args.kwargs["headers"]["X-Line-Retry-Key"] == "fixed"


def test_old_pending_text_payload_keeps_original_format_on_retry():
    post = Mock(return_value=Mock(status_code=200))
    push_text("secret", "recipient", "old notification", "old-key", post)
    assert post.call_args.kwargs["json"]["messages"] == [{"type": "text", "text": "old notification"}]


def test_saved_setting_is_reported_as_saved_even_if_queue_is_temporarily_unavailable():
    service = Mock()
    service.admin_settings.return_value = {"target": {"approved": True, "enabled": True}}
    card = admin_action("admin", "user", action(action="set", target="target", mode="on"),
        is_admin=lambda _: True, users=lambda: [("target", "name")], service=service,
        queue_check=Mock(side_effect=TimeoutError()))
    assert "設定已儲存" in json.dumps(card, ensure_ascii=False)
