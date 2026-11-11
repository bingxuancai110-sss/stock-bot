from datetime import date, datetime, timezone
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest

from taiex_cron_auth import authorized, AUDIENCE, ISSUER, REPOSITORY, WORKFLOW
from taiex_market_data import parse_month, official_history
from taiex_jobs import ReminderJobs
from taiex_reduction import evaluate


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed(private, **overrides):
    now = int(time.time())
    claims = dict(iss=ISSUER, aud=AUDIENCE, iat=now, nbf=now-1, exp=now+300,
                  repository=REPOSITORY, repository_id="1327818651", ref="refs/heads/main",
                  workflow_ref=WORKFLOW, event_name="schedule")
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": "test"})


def test_real_crypto_accepts_only_scoped_github_workflow(private_key):
    keys = Mock()
    keys.get_signing_key_from_jwt.return_value = SimpleNamespace(key=private_key.public_key())
    assert authorized({"Authorization": "Bearer " + signed(private_key)}, None, None, keys)
    for fields in (dict(repository="attacker/repo"), dict(repository_id="1"),
                   dict(ref="refs/pull/1/merge"), dict(workflow_ref="wrong"), dict(event_name="pull_request"),
                   dict(aud="wrong"), dict(iss="wrong"), dict(exp=int(time.time())-100),
                   dict(exp=int(time.time())+3600)):
        assert not authorized({"Authorization": "Bearer " + signed(private_key, **fields)}, None, None, keys)


def test_forged_signature_and_algorithm_are_rejected(private_key):
    keys = Mock()
    keys.get_signing_key_from_jwt.return_value = SimpleNamespace(key=private_key.public_key())
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert not authorized({"Authorization": "Bearer " + signed(other)}, None, None, keys)
    bad = jwt.encode({"repository": REPOSITORY}, "x" * 32, algorithm="HS256")
    assert not authorized({"Authorization": "Bearer " + bad}, None, None, keys)
    assert not authorized({"Authorization": "Bearer garbage"}, None, None, keys)


def test_legacy_secret_is_retained_and_missing_credentials_are_not_allowed():
    assert authorized({}, "known", "known")
    assert not authorized({}, "wrong", "known")
    assert not authorized({}, None, None)


def test_us_ingest_and_monthly_workflow_credentials_cannot_cross_scopes(private_key):
    from us_rotation.daily_sync import AUDIENCE as US_AUDIENCE, WORKFLOW as US_WORKFLOW
    keys = Mock()
    keys.get_signing_key_from_jwt.return_value = SimpleNamespace(key=private_key.public_key())
    us = {"Authorization": "Bearer " + signed(private_key, aud=US_AUDIENCE, workflow_ref=US_WORKFLOW)}
    monthly = {"Authorization": "Bearer " + signed(private_key)}
    assert authorized(us, None, None, keys, audience=US_AUDIENCE, workflow=US_WORKFLOW)
    assert not authorized(us, None, None, keys)
    assert not authorized(monthly, None, None, keys, audience=US_AUDIENCE, workflow=US_WORKFLOW)


def month_payload(month, days):
    return {"stat": "OK", "fields": ["日期", "收盤指數"],
            "data": [[f"115/{month:02d}/{d:02d}", "42,000.00"] for d in days]}


def test_official_month_handles_exchange_sessions_without_assuming_weekdays():
    parsed = parse_month(month_payload(10, [22, 27, 28]), date(2026,10,1), date(2026,11,10))
    assert [d.day for d, _ in parsed] == [22, 27, 28]
    assert all(value == 42000 for _, value in parsed)


@pytest.mark.parametrize("payload", [
    {"stat": "error"}, month_payload(10, [22, 22]), month_payload(11, [10]),
    {"stat": "OK", "fields": ["日期", "收盤指數"], "data": [["115/10/22", "nan"]]},
])
def test_incomplete_wrong_month_or_invalid_official_data_rejected(payload):
    with pytest.raises(ValueError):
        parse_month(payload, date(2026,10,1), date(2026,11,10))


def test_four_month_history_is_cached_and_requires_same_day_close():
    cache = {}
    def save(k, v, **_):
        cache[k] = {"payload": v}
        return True
    def get(_, params, **kwargs):
        month = int(params["date"][4:6])
        return SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: month_payload(month, list(range(1,11)) if month == 11 else [1,2,3]))
    network = Mock(side_effect=get)
    rows = official_history(date(2026,11,10), lambda k, **_: cache.get(k), save, network)
    assert network.call_count == 4 and rows[-1][0] == date(2026,11,10)
    official_history(date(2026,11,10), lambda k, **_: cache.get(k), save, network)
    assert network.call_count == 4
    with pytest.raises(ValueError):
        official_history(date(2026,11,11), lambda k, **_: cache.get(k), save, network)


def test_evaluate_accepts_complete_official_sessions_but_fixed_weekday_calendar_detects_gap():
    rows = []
    day = date(2026,9,1)
    from datetime import timedelta
    while day <= date(2026,11,10):
        if day.weekday() < 5 and day != date(2026,10,23):
            rows.append((day, 100))
        day += timedelta(days=1)
    with pytest.raises(ValueError):
        evaluate(rows, rows[-1][0], lambda d: d.weekday() < 5)
    sessions = {d for d, _ in rows}
    assert evaluate(rows, rows[-1][0], lambda d: d in sessions)["streak"] == 0


class MemoryStore:
    def __init__(self):
        self.data = {}
    def save(self, key, payload, **_):
        self.data[key] = {"payload": payload}
    def load(self, key, **_):
        return self.data.get(key)


def test_background_job_records_actual_completion_not_just_queue_acceptance():
    store = MemoryStore()
    jobs = ReminderJobs(store, lambda: "LINE接受0則、未確認0則")
    run_id = "00000000-0000-0000-0000-000000000001"
    jobs.execute(run_id)
    assert jobs.status(run_id)["state"] == "ok"
    jobs.run = Mock(side_effect=ValueError("官方收盤尚未完整"))
    jobs.execute(run_id)
    assert jobs.status(run_id)["state"] == "error"
    assert "官方收盤" in jobs.status(run_id)["message"]
    jobs.run = lambda: "LINE接受0則、未確認1則"
    jobs.execute(run_id)
    assert jobs.status(run_id)["state"] == "error"


def test_bad_run_id_cannot_read_other_snapshot_keys():
    jobs = ReminderJobs(MemoryStore(), lambda: "ok")
    with pytest.raises(ValueError):
        jobs.status("users")


def workflow_client():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("test_cron_client", Path(__file__).parents[1] / "scripts/check_taiex_reminder.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workflow_waits_for_actual_completion_and_allows_deploy_transition():
    client = workflow_client()
    client.call = Mock(side_effect=[(403, {}), (202, {"id": "one", "state": "queued"}),
                                   (200, {"state": "running"}), (200, {"state": "ok"})])
    elapsed = [0]
    result = client.run(timeout=60, clock=lambda: elapsed[0], sleep=lambda n: elapsed.__setitem__(0, elapsed[0]+n))
    assert result["state"] == "ok"
    assert client.call.call_args.args == ("GET", "/cron/taiex-reduction/status?id=one")


def test_workflow_does_not_treat_202_as_success_or_poll_forever():
    client = workflow_client()
    client.call = Mock(return_value=(202, {"id": "one", "state": "queued"}))
    elapsed = [0]
    with pytest.raises(TimeoutError):
        client.run(timeout=20, clock=lambda: elapsed[0], sleep=lambda n: elapsed.__setitem__(0, elapsed[0]+n))
    assert client.call.call_count == 2
    client.call = Mock(side_effect=[(202, {"id": "one", "state": "queued"}),
                                   (200, {"state": "error", "message": "缺資料"})])
    with pytest.raises(RuntimeError):
        client.run(timeout=20, sleep=lambda _: None)
