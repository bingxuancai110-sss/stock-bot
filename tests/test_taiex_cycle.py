import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

from taiex_cycle import advance, initial_state
from taiex_line_cards import alert_card
from taiex_reduction import ReductionAlerts, evaluate


def history(tail):
    day = date(2026, 11, 6)
    prior = []
    while len(prior) < 27:
        if day.weekday() < 5:
            prior.append(day)
        day -= timedelta(days=1)
    rows = [(day, 120.0) for day in reversed(prior)]
    day = rows[-1][0]
    for close in tail:
        day = next_day(day)
        rows.append((day, close))
    return rows


def next_day(day):
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def observations(tail):
    rows = history(tail)
    return evaluate(rows, rows[-1][0], lambda d: d.weekday() < 5)["observations"]


def walk(tail):
    rows = observations(tail)
    state = initial_state(rows[7])  # The last 120 close before the new crossing.
    states = []
    for row in rows[8:]:
        state = advance(state, row, next_day(date.fromisoformat(row["date"])).isoformat())
        states.append(state)
    return states


def events(states):
    return [event for state in states for event in state["events"]]


def test_forward_simulation_warns_confirms_and_refills_at_exact_thresholds():
    states = walk([115, 114, 113, 100, 101, 103, 104.5, 105])
    result = events(states)
    assert [item["level"] for item in result] == [2, 3, 30, 45]
    assert [item["date"] for item in result] == ["2026-11-10", "2026-11-11", "2026-11-16", "2026-11-17"]
    assert result[1]["next_day"] == "2026-11-12"
    assert result[2]["target_pct"] == 75 and result[3]["target_pct"] == 100
    assert result[2]["low"] == 100 and result[2]["low_date"] == "2026-11-12"
    assert states[-1]["cycle"] is None
    for event in result:
        card = alert_card(event, date.fromisoformat(event["next_day"]))
        assert card["type"] == "flex"
    assert "並非你的實際成交" in json.dumps(alert_card(result[-1]), ensure_ascii=False)


def test_enabling_during_old_below_ma_trend_never_backfills_warning_or_reduction():
    rows = observations([115, 114, 113, 100, 101, 103, 104.5])
    state = initial_state(rows[8])  # Enable after the index already closed below MA20.
    for row in rows[9:]:
        state = advance(state, row, next_day(date.fromisoformat(row["date"])).isoformat())
        assert state["events"] == []
        assert state["cycle"] is None


def test_old_trend_must_recover_above_ma_before_new_cross_is_counted():
    rows = observations([115, 114, 113, 100, 140, 100, 99, 98])
    state = initial_state(rows[8])
    result = []
    for row in rows[9:]:
        state = advance(state, row, next_day(date.fromisoformat(row["date"])).isoformat())
        result.extend(state["events"])
    assert [item["level"] for item in result] == [2, 3]


def test_reduction_low_starts_at_scheduled_execution_not_signal_close():
    states = walk([115, 114, 80, 100, 102.99, 103])
    result = events(states)
    assert [e["level"] for e in result] == [2, 3, 30]
    assert result[-1]["low"] == 100  # The earlier 80 signal close is not the cycle low.


def test_recrossing_three_percent_does_not_repeat_first_refill():
    result = events(walk([115, 114, 113, 100, 103, 101, 103.1, 104.5, 105]))
    assert [e["level"] for e in result] == [2, 3, 30, 45]


def test_lower_low_after_partial_refill_adjusts_second_threshold_without_repeating_first():
    result = events(walk([115, 114, 113, 100, 103, 99, 102, 103.455]))
    assert [e["level"] for e in result] == [2, 3, 30, 45]
    assert result[-1]["low"] == 99


def test_jump_across_both_thresholds_is_one_combined_full_refill():
    result = events(walk([115, 114, 113, 100, 105]))
    assert [e["level"] for e in result] == [2, 3, 45]
    assert result[-1]["crossed_both"] is True
    assert "一次跨過" in json.dumps(alert_card(result[-1]), ensure_ascii=False)


def test_below_threshold_does_not_fire_and_repeated_date_does_not_advance_state():
    states = walk([115, 114, 113, 100, 102.99])
    assert [e["level"] for e in events(states)] == [2, 3]
    latest = observations([115, 114, 113, 100, 102.99])[-1]
    assert advance(states[-1], latest, "2026-11-16") == states[-1]


def call_persisted_helper(tail, saved_state, activation):
    rows = history(tail)
    result = evaluate(rows, rows[-1][0], lambda d: d.weekday() < 5)
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = (saved_state, activation)
    conn = MagicMock()
    conn.cursor.return_value = cursor
    service = ReductionAlerts(connect=None, release=None, load=None, save=None, history=None,
        official=None, calendar=lambda d: d.weekday() < 5,
        now=lambda: datetime(2026, 11, 16, 14, tzinfo=timezone(timedelta(hours=8))),
        send=None, quiet=None, enabled=None)
    emitted = service._user_events(conn, "user", result)
    write = next(c for c in cursor.execute.call_args_list if "INSERT INTO taiex_reduction_user_state" in c.args[0])
    return emitted, json.loads(write.args[1][1])


def test_weekend_activation_uses_friday_close_and_detects_genuine_monday_cross():
    activation = datetime(2026, 11, 8, 12, tzinfo=timezone(timedelta(hours=8)))
    emitted, state = call_persisted_helper([115], None, activation)
    assert emitted == [] and state["streak"] == 1
    emitted, _ = call_persisted_helper([115, 114], state, activation)
    assert [e["level"] for e in emitted] == [2]


def test_late_subscription_does_not_replay_old_events_or_assume_a_reduction():
    activation = datetime(2026, 11, 10, 15, tzinfo=timezone(timedelta(hours=8)))
    emitted, state = call_persisted_helper([115, 114, 113, 100], None, activation)
    assert emitted == [] and state["cycle"] is None


def test_restart_keeps_current_events_for_safe_delivery_retry():
    activation = datetime(2026, 11, 8, 12, tzinfo=timezone(timedelta(hours=8)))
    emitted, state = call_persisted_helper([115, 114], None, activation)
    retried, _ = call_persisted_helper([115, 114], json.dumps(state), activation)
    assert retried == emitted


def test_missed_day_replays_state_but_only_returns_current_days_event():
    activation = datetime(2026, 11, 8, 12, tzinfo=timezone(timedelta(hours=8)))
    _, state = call_persisted_helper([115], None, activation)
    emitted, state = call_persisted_helper([115, 114, 113, 100, 101, 103], state, activation)
    assert [e["level"] for e in emitted] == [30]
    assert state["cycle"]["low"] == 100


def test_completed_refill_does_not_start_new_reduction_while_still_below_ma():
    result = events(walk([115, 114, 113, 100, 103, 104.5, 100, 99, 98, 97]))
    assert [e["level"] for e in result] == [2, 3, 30, 45]


def test_users_initialized_above_and_below_do_not_share_signal_state():
    rows = observations([115, 114, 113])
    above = initial_state(rows[7])
    below = initial_state(rows[8])
    above = advance(above, rows[8], next_day(date.fromisoformat(rows[8]["date"])).isoformat())
    for row in rows[9:]:
        above = advance(above, row, next_day(date.fromisoformat(row["date"])).isoformat())
        below = advance(below, row, next_day(date.fromisoformat(row["date"])).isoformat())
    assert above["events"][0]["level"] == 3
    assert below["events"] == []


def test_long_history_gap_resets_to_current_observation_without_fabricating_refill():
    activation = datetime(2026, 8, 1, 12, tzinfo=timezone(timedelta(hours=8)))
    saved = {"as_of": "2026-01-01", "streak": 3, "cycle": {"fake-old": True}, "events": []}
    emitted, state = call_persisted_helper([115, 114, 113, 100, 105], saved, activation)
    assert emitted == [] and state["cycle"] is None
    assert state["reset_reason"]
