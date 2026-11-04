from datetime import datetime
from contextlib import contextmanager
import pytest
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import requests

from fugle_background import Collector, BackgroundService, quote_row

NOW = datetime(2026, 11, 4, 10, 30, tzinfo=ZoneInfo('Asia/Taipei'))


def payload(code='2308', **extra):
    return dict(symbol=code, date='2026-11-04', referencePrice=100,
                lastTrade=dict(price=102, time=int(NOW.timestamp()*1_000_000)), **extra)


def response(data=None, status=200, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {},
                           json=lambda: data, raise_for_status=lambda: None)


def test_quote_has_actual_timestamp_and_reference_price():
    row = quote_row('2308', payload(), NOW)
    assert row['price'] == 102 and row['reference_price'] == 100
    assert row['time'] == NOW.timestamp()*1_000_000


def test_wrong_day_symbol_trial_and_non_final_close_cannot_become_current_quotes():
    p = payload(); p['date'] = '2026-11-03'
    assert quote_row('2308', p, NOW) is None
    assert quote_row('2330', payload(), NOW) is None
    assert quote_row('2308', payload(type='ODDLOT'), NOW) is None
    assert quote_row('2308', payload(), NOW.replace(hour=14)) is None
    assert quote_row('2308', dict(symbol='2308', date='2026-11-04',
                                 isTrial=True, lastTrial=dict(price=103)), NOW) is None


def test_collector_runs_without_any_page_request_and_drops_sold_positions():
    session = Mock(); session.get.return_value = response(payload())
    collector = Collector('test-key', session=session)
    result = collector.collect(['2308'], NOW, dict(trading_date='2026-11-04', quotes={'2330': {}}))
    assert set(result['quotes']) == {'2308'} and result['updated'] == 1
    assert session.get.call_args.kwargs['allow_redirects'] is False


def test_timeouts_preserve_last_good_quote_without_changing_observation_time():
    session = Mock(); session.get.side_effect = requests.Timeout('test')
    old = {'trading_date': '2026-11-04', 'quotes': {'2308': {'observed_at': 123}}}
    result = Collector('test-key', session=session).collect(['2308'], NOW, old)
    assert result['quotes']['2308']['observed_at'] == 123
    assert result['status'] == 'partial' and result['updated'] == 0


def test_rate_limit_cools_down_and_does_not_request_again():
    session = Mock(); session.get.return_value = response(status=429, headers={'Retry-After': '120'})
    tick = [100.0]
    collector = Collector('test-key', session=session, clock=lambda: tick[0])
    assert collector.collect(['2308'], NOW)['status'] == 'rate_limited'
    tick[0] += 61
    assert collector.collect(['2308'], NOW)['requested'] == 0
    assert session.get.call_count == 1


def test_rolling_quota_and_no_parallel_cycle():
    session = Mock(); session.get.return_value = response(payload())
    collector = Collector('test-key', session=session, clock=lambda: 100)
    for _ in range(35):
        collector.collect(['2308'], NOW)
    assert session.get.call_count == 30
    with collector._lock:
        assert collector.collect(['2308'], NOW) is None


def test_weekend_and_night_do_not_download_quotes():
    session = Mock()
    collector = Collector('test-key', session=session)
    assert collector.collect(['2308'], NOW.replace(hour=22))['status'] == 'outside_session'
    assert collector.collect(['2308'], NOW.replace(day=7))['status'] == 'outside_session'
    session.get.assert_not_called()


def test_budget_advances_cursor_and_previous_day_is_not_reused():
    tick = [100.0]
    session = Mock()
    def get(url, **kwargs):
        tick[0] += 26
        return response(payload(url.rsplit('/', 1)[-1]))
    session.get.side_effect = get
    collector = Collector('test-key', session=session, clock=lambda: tick[0])
    first = collector.collect(['2308', '2330'], NOW, dict(trading_date='2026-11-03', quotes={'2330': {}}))
    assert set(first['quotes']) == {'2308'} and first['status'] == 'partial'
    second = collector.collect(['2308', '2330'], NOW)
    assert set(second['quotes']) == {'2330'}


def test_snapshot_is_private_and_reading_it_never_starts_a_download():
    @contextmanager
    def lease():
        yield True
    session = Mock(); session.get.return_value = response(payload())
    save = Mock()
    service = BackgroundService('owner', Collector('private-test-key', session=session),
        holdings=lambda uid: ['2308'], load=lambda uid: {}, save=save, lease=lease)
    assert service.read('owner') == {}
    session.get.assert_not_called()
    assert service.tick(NOW) == 'ready'
    save.assert_called_once()
    with pytest.raises(PermissionError):
        service.read('other')
    copy = service.read('owner'); copy['quotes'].clear()
    assert '2308' in service.read('owner')['quotes']
    assert 'private-test-key' not in str(save.call_args)


def test_losing_shared_lease_never_starts_duplicate_work():
    @contextmanager
    def lease():
        yield False
    collector, holdings, load, save = Mock(), Mock(), Mock(), Mock()
    service = BackgroundService('owner', collector, holdings=holdings, load=load,
                                save=save, lease=lease)
    assert service.tick(NOW) == 'already_running'
    for action in [collector.collect, holdings, load, save]:
        action.assert_not_called()


def test_other_instance_recent_snapshot_prevents_duplicate_polling():
    @contextmanager
    def lease():
        yield True
    collector, holdings, save = Mock(), Mock(), Mock()
    snapshot = {'checked_at': NOW.isoformat(), 'quotes': {}}
    service = BackgroundService('owner', collector, holdings=holdings,
                                load=lambda uid: snapshot, save=save, lease=lease)
    assert service.tick(NOW) == 'already_updated'
    collector.collect.assert_not_called(); holdings.assert_not_called(); save.assert_not_called()


def test_confirmed_closes_stop_downloads_without_falsifying_fetch_time():
    @contextmanager
    def lease():
        yield True
    collector, save = Mock(), Mock()
    previous = {'checked_at': NOW.isoformat(), 'trading_date': '2026-11-04',
                'quotes': {'2308': {'closed': True, 'observed_at': 123}}}
    service = BackgroundService('owner', collector, holdings=lambda uid: ['2308'],
                                load=lambda uid: previous, save=save, lease=lease)
    assert service.tick(NOW.replace(hour=14, minute=0)) == 'final_close_saved'
    assert service.read('owner')['quotes']['2308']['observed_at'] == 123
    collector.collect.assert_not_called(); save.assert_not_called()


def test_only_trusted_server_normalization_can_reuse_saved_final_close():
    from fugle_live import normalize_quotes
    close = NOW.replace(hour=13, minute=30)
    late = NOW.replace(hour=22)
    row = dict(code='2308', price=102, previous_close=100,
               time=close.timestamp()*1_000_000, observed_at=close.timestamp()*1000,
               closed=True)
    assert normalize_quotes([row], {'2308'}, late) == {}
    saved = normalize_quotes([row], {'2308'}, late, max_observation_age=86400)['2308']
    assert saved['fetched_at'].endswith('13:30:00') and saved['close_is_final']
    assert normalize_quotes([dict(row, closed=False)], {'2308'}, late,
                            max_observation_age=86400) == {}
