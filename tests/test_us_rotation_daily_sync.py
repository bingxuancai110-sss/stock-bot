from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

from flask import Flask
import pytest

from us_rotation import routes
from us_rotation.daily_sync import (encode_closed_history, validate_risk_dates,
                                    freshness_warning, RISK_SYMBOLS, MAX_BYTES)
from us_rotation.importer import REQUIRED_SYMBOLS, parse_history_csv


def data():
    days, day = [], date(2026, 6, 1)
    while len(days) < 80:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return {s: [dict(trade_date=d, open=20, high=22, low=18, close=21,
                    adjusted_close=21, volume=1000) for d in days]
            for s in REQUIRED_SYMBOLS | RISK_SYMBOLS}


def test_final_session_only_and_market_holiday_alignment():
    prices = data()
    # An in-progress US session must not enter the saved daily-close series.
    for rows in prices.values():
        rows.append(dict(rows[-1], trade_date="2026-11-11"))
    text = encode_closed_history(prices, datetime(2026, 11, 11, 18, tzinfo=timezone.utc))
    parsed, _ = parse_history_csv(text)
    assert all(rows[-1]["trade_date"] != "2026-11-11" for rows in parsed.values())
    final = encode_closed_history(prices, datetime(2026, 11, 11, 23, tzinfo=timezone.utc))
    assert parse_history_csv(final)[0]["^VIX"][-1]["trade_date"] == "2026-11-11"


@pytest.mark.parametrize("symbol", sorted(RISK_SYMBOLS))
def test_missing_or_lagged_risk_is_not_reported_as_success(symbol):
    prices = data()
    prices[symbol] = prices[symbol][:-1]
    with pytest.raises(ValueError, match="日期未對齊"):
        validate_risk_dates(prices)
    del prices[symbol]
    with pytest.raises(ValueError):
        encode_closed_history(prices)


def test_stale_or_unknown_snapshot_time_is_visible():
    now = datetime(2026,11,11,6,tzinfo=timezone.utc)
    assert "歷史快照" in freshness_warning({"fetched_at":"2026-11-06T07:00:00Z"}, now)
    assert freshness_warning({"fetched_at":"2026-11-11T06:00:00Z"}, now) == ""
    assert "時間未確認" in freshness_warning({"fetched_at":None}, now)


def client(monkeypatch, auth=True):
    monkeypatch.setattr("taiex_cron_auth.authorized", Mock(return_value=auth))
    save = Mock(return_value=123)
    monkeypatch.setattr(routes, "save_snapshot", save)
    monkeypatch.setattr(routes, "read_latest", Mock(return_value=None))
    app = Flask(__name__)
    connect, release = Mock(), Mock()
    app.register_blueprint(routes.create_blueprint(web_login_required=lambda f:f, render_page=Mock(),
        get_db_connection=Mock(), release_db_connection=Mock(), run_in_background=Mock(),
        fetch_tw_company_snapshot=Mock(), job_connect=connect, job_release=release))
    return app.test_client(), save, connect, release


def test_ingest_acknowledges_only_committed_snapshot_and_never_calls_taiwan_scan(monkeypatch):
    http, save, connect, release = client(monkeypatch)
    monkeypatch.setattr(routes, "fetch_market_histories", Mock(side_effect=AssertionError("no web downloads")))
    response = http.post("/cron/us-rotation-ingest", data=encode_closed_history(data()))
    assert response.status_code == 200
    result = response.get_json()
    assert result["ok"] and result["snapshot_id"] == 123 and result["as_of"] == result["vix_date"]
    assert save.call_args.args[:2] == (connect, release)
    assert save.call_args.args[2]["current_risk_warning"]["us"]["value"] == 21


def test_unauthorized_ingest_rejected_before_write(monkeypatch):
    http, save, _, _ = client(monkeypatch, auth=False)
    assert http.post("/cron/us-rotation-ingest", data="bad").status_code == 403
    save.assert_not_called()


def test_oversize_invalid_and_failed_database_never_acknowledged(monkeypatch):
    http, save, _, _ = client(monkeypatch)
    assert http.post("/cron/us-rotation-ingest", data=b"x"*(MAX_BYTES+1)).status_code == 413
    assert http.post("/cron/us-rotation-ingest", data="bad").status_code == 422
    save.assert_not_called()
    save.side_effect = RuntimeError("db failed")
    response = http.post("/cron/us-rotation-ingest", data=encode_closed_history(data()))
    assert response.status_code == 503 and response.get_json()["ok"] is False


def test_older_snapshot_cannot_replace_latest(monkeypatch):
    http, save, _, _ = client(monkeypatch)
    monkeypatch.setattr(routes, "read_latest", Mock(return_value={"as_of":"2026-11-10"}))
    assert http.post("/cron/us-rotation-ingest", data=encode_closed_history(data())).status_code == 409
    save.assert_not_called()
