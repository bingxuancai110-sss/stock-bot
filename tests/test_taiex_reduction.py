from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock

import pytest

from taiex_reduction import ReductionAlerts, SNAPSHOT_KEY, evaluate, format_alert, push_text


def sessions(count=30):
    day = date(2026, 9, 1)
    result = []
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day)
        day += timedelta(days=1)
    return result


def prices(tail):
    values = [100] * 27 + tail
    return list(zip(sessions(len(values)), values))


def calculate(tail):
    rows = prices(tail)
    return evaluate(rows, rows[-1][0], lambda day: day.weekday() < 5)


def test_second_day_warns_third_confirms_fourth_does_not_repeat():
    assert calculate([90])["streak"] == 1
    warning = calculate([90, 89])
    confirm = calculate([90, 89, 88])
    assert warning["streak"] == 2
    assert confirm["streak"] == 3
    assert calculate([90, 89, 88, 87])["streak"] == 4
    assert "尚未觸發減碼" in format_alert(warning, date(2026, 10, 14))
    message = format_alert(confirm, date(2026, 10, 14))
    assert "2026-10-14" in message and "50%" in message
    with pytest.raises(ValueError):
        format_alert(calculate([90, 89, 88, 87]), date(2026, 10, 14))


def test_equal_ma_breaks_streak_and_includes_current_session():
    assert calculate([100, 100])["streak"] == 0
    result = calculate([90, 89])
    assert result["rows"][-1]["ma20"] == pytest.approx((18 * 100 + 90 + 89) / 20)
    assert calculate([90, 89, 110, 90])["streak"] == 1


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_invalid_prices_do_not_trigger(bad):
    with pytest.raises(ValueError):
        calculate([90, bad])


def test_missing_duplicate_stale_and_short_history_rejected():
    history = prices([90, 89, 88])
    calendar = lambda day: day.weekday() < 5
    for rows in (history[:10], history[:-1], history[:10] + history[11:], history + history[-1:]):
        with pytest.raises(ValueError):
            evaluate(rows, history[-1][0], calendar)


def test_holiday_does_not_count_but_missing_real_session_does():
    history = prices([90, 89])
    holiday = history[-2][0]
    result = evaluate([r for r in history if r[0] != holiday], history[-1][0],
                      lambda day: day.weekday() < 5 and day != holiday)
    assert result["streak"] == 1


def service(streak=2, subscribers=("only-opted-in",), cached=True):
    history = prices([90 - i for i in range(streak)])
    today = history[-1][0]
    result = evaluate(history, today, lambda day: day.weekday() < 5)
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = [(True,), (True,), (True,), ("stable-key", "original-body", None, True)]
    cursor.fetchall.return_value = [(uid,) for uid in subscribers]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    deps = dict(connect=Mock(return_value=conn), release=Mock(),
                load=Mock(return_value={"payload": result} if cached else None),
                save=Mock(return_value=True), history=Mock(return_value=history),
                official=Mock(return_value={"date": today.strftime("%Y%m%d"),
                              "close": history[-1][1], "close_is_final": True}),
                calendar=lambda d: d.weekday() < 5,
                now=lambda: datetime.combine(today, datetime.min.time(), timezone(timedelta(hours=8))).replace(hour=14),
                send=Mock(), quiet=Mock(return_value=False), enabled=lambda: True)
    return ReductionAlerts(**deps), deps, cursor, conn


def test_job_reads_same_day_snapshot_and_only_sends_opt_in_with_persisted_key():
    worker, deps, cur, conn = service()
    assert "LINE接受1則" in worker.run()
    deps["history"].assert_not_called()
    deps["official"].assert_not_called()
    deps["send"].assert_called_once_with("only-opted-in", "original-body", "stable-key")
    assert any("INSERT INTO taiex_reduction_deliveries" in call.args[0]
               for call in cur.execute.call_args_list)
    assert conn.commit.call_count >= 3
    deps["release"].assert_called_once_with(conn)


def test_already_sent_does_not_send_again_even_after_restart():
    worker, deps, cur, _ = service()
    cur.fetchone.side_effect = [(True,), (True,), (True,), ("stable-key", "original", datetime.now(), True)]
    worker.run()
    deps["send"].assert_not_called()


def test_provider_retry_window_expired_never_resends_unknown_result():
    worker, deps, cur, _ = service()
    cur.fetchone.side_effect = [(True,), (True,), (True,), ("stable-key", "original", None, False)]
    worker.run()
    deps["send"].assert_not_called()


def test_first_day_and_fourth_day_do_not_send():
    for streak in (1, 4):
        worker, deps, _, _ = service(streak)
        worker.run()
        deps["send"].assert_not_called()


def test_missing_official_close_or_failed_snapshot_never_sends():
    for invalid in ({}, {"date": "20200101", "close_is_final": True}):
        worker, deps, _, _ = service(cached=False)
        deps["official"].return_value = invalid
        with pytest.raises(ValueError):
            worker.run()
        deps["send"].assert_not_called()
    worker, deps, _, _ = service(cached=False)
    deps["save"].return_value = False
    with pytest.raises(RuntimeError):
        worker.run()
    deps["send"].assert_not_called()


def test_successful_fetch_saves_verified_close_and_then_notifies():
    worker, deps, _, _ = service(cached=False)
    worker.run()
    assert deps["save"].call_args.args[0] == SNAPSHOT_KEY
    assert deps["save"].call_args.args[1]["streak"] == 2
    deps["send"].assert_called_once()


def test_no_subscribers_or_other_instance_does_not_fetch_or_send():
    for locked, users in ((False, ("x",)), (True, ())):
        worker, deps, cur, _ = service(subscribers=users)
        cur.fetchone.side_effect = [(locked,) if locked else None]
        worker.run()
        deps["history"].assert_not_called()
        deps["send"].assert_not_called()


def test_quiet_hours_are_respected():
    worker, deps, _, _ = service()
    deps["quiet"].return_value = True
    worker.run()
    deps["send"].assert_not_called()


def test_pre_close_and_weekend_have_no_db_or_network_work():
    worker, deps, _, _ = service()
    for now in (datetime(2026, 11, 9, 13, 39), datetime(2026, 11, 8, 15)):
        worker.now = lambda: now
        worker.run()
    deps["connect"].assert_not_called()
    deps["send"].assert_not_called()


def test_delivery_failure_is_not_marked_sent_and_lock_is_released():
    worker, deps, cur, conn = service()
    deps["send"].side_effect = TimeoutError()
    assert "未確認1則" in worker.run()
    assert not any("SET sent_at" in call.args[0] for call in cur.execute.call_args_list)
    assert any("DELETE FROM taiex_reduction_job_lock" in call.args[0] for call in cur.execute.call_args_list)
    conn.rollback.assert_called_once()


def test_retry_keeps_exact_body_and_key_and_accepts_confirmed_409():
    accepted = Mock(status_code=409, headers={"x-line-accepted-request-id": "accepted"})
    post = Mock(side_effect=[TimeoutError(), accepted])
    push_text("secret", "self", "unchanged", "same-key", post, sleep=lambda _: None)
    assert post.call_count == 2
    assert post.call_args_list[0] == post.call_args_list[1]
    assert post.call_args.kwargs["headers"]["X-Line-Retry-Key"] == "same-key"


def test_permanent_failure_or_unconfirmed_409_is_not_accepted():
    for status in (400, 401, 403, 409):
        post = Mock(return_value=Mock(status_code=status, headers={}))
        with pytest.raises(ValueError):
            push_text("secret", "self", "text", "key", post)
        assert post.call_count == 1


@pytest.mark.parametrize("command,enabled", [("減碼提醒開啟", True), ("減碼提醒關閉", False)])
def test_subscription_writes_only_the_sender_and_status_is_not_a_broadcast(command, enabled):
    worker, deps, cur, conn = service()
    cur.fetchone.side_effect = [(enabled, True)]
    response = worker.command("sender-only", command)
    write = next(call for call in cur.execute.call_args_list
                 if "UPDATE taiex_reduction_subscriptions" in call.args[0])
    assert write.args[1] == (enabled, "sender-only")
    assert "approved=TRUE" in write.args[0]
    assert ("已開啟" if enabled else "未開啟") in response
    deps["send"].assert_not_called()
    deps["history"].assert_not_called()
    conn.commit.assert_called_once()


def test_disabled_global_switch_does_not_contact_db_or_line():
    worker, deps, _, _ = service()
    worker.enabled = lambda: False
    worker.run()
    deps["connect"].assert_not_called()
    deps["send"].assert_not_called()


def test_lost_job_lease_stops_sending():
    worker, deps, cur, _ = service()
    cur.fetchone.side_effect = [(True,), None]
    with pytest.raises(RuntimeError):
        worker.run()
    deps["send"].assert_not_called()


def test_unapproved_user_cannot_enable_their_own_permission():
    worker, deps, cur, _ = service()
    cur.fetchone.side_effect = [None]
    assert "尚未獲管理員核准" in worker.command("not-approved", "減碼提醒開啟")
    writes = [call.args[0] for call in cur.execute.call_args_list if "UPDATE " in call.args[0]]
    assert all("approved=TRUE" in sql for sql in writes)
    assert not any("approved=" in sql.replace("approved=TRUE", "") for sql in writes)
    deps["send"].assert_not_called()


@pytest.mark.parametrize("mode,approved,enabled", [("on", True, True), ("allowed", True, False), ("off", False, False)])
def test_admin_can_grant_pause_or_revoke(mode, approved, enabled):
    worker, _, cur, _ = service()
    cur.fetchall.return_value = [("target", enabled, approved)]
    result = worker.admin_settings("admin", "target", mode)
    write = next(call for call in cur.execute.call_args_list if "INSERT INTO taiex_reduction_subscriptions" in call.args[0])
    assert write.args[1] == ("target", enabled, approved, "admin")
    assert result["target"] == {"enabled": enabled, "approved": approved}


def test_admin_test_is_labeled_and_only_goes_to_caller_with_separate_level():
    worker, deps, cur, _ = service()
    cur.fetchone.side_effect = [("test-key", "test-message", None, True)]
    assert "已獲LINE接受" in worker.send_test("admin-self")
    deps["send"].assert_called_once_with("admin-self", "test-message", "test-key")
    write = next(call for call in cur.execute.call_args_list if "INSERT INTO taiex_reduction_deliveries" in call.args[0])
    assert write.args[1][1] == "admin-self"
    assert "測試通知" in write.args[1][-1]
    assert "並非真實減碼訊號" in write.args[1][-1]
    assert "10" in write.args[0]
    deps["history"].assert_not_called()


def test_admin_test_repeated_click_is_not_sent_twice():
    worker, deps, cur, _ = service()
    cur.fetchone.side_effect = [("test-key", "test-message", datetime.now(), True)]
    assert "未再次發送" in worker.send_test("admin-self")
    deps["send"].assert_not_called()


def test_admin_ui_escapes_user_names_and_includes_csrf_on_both_forms():
    from taiex_reduction import render_admin
    result = render_admin([("user", "<script>bad</script>")], {}, '<input name="csrf_token">', "<b>status</b>")
    assert '<script>bad</script>' not in result
    assert '&lt;script&gt;' in result
    assert result.count('name="csrf_token"') == 2
    assert 'name="target"' in result
    assert 'value="test"' in result
