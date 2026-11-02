from datetime import datetime
from zoneinfo import ZoneInfo
from market_quotes import prefer_quote, quote_freshness
from us_rotation.industry_guides import public_industry_guides
from us_rotation.config import INDUSTRIES

NOW = datetime(2026, 11, 2, 10, 45, tzinfo=ZoneInfo('Asia/Taipei'))

def q(source, stamp, fetched='2026-11-02 10:45:00'):
    return dict(close=100, source=source, updated_at=stamp, fetched_at=fetched)


def test_delayed_download_does_not_replace_a_newer_official_trade():
    official=q('TWSE MIS 盤中最後成交', '2026-11-02 10:44:59', '2026-11-02 10:44:59')
    delayed=q('Yahoo Finance 1 分 K 備援', '2026-11-02 10:30:00')
    assert not prefer_quote(official, delayed)
    assert prefer_quote(delayed, official)


def test_same_trade_can_be_reconfirmed_by_official_feed():
    old=q('TWSE MIS', '20261102 09:10:00', '2026-11-02 10:40:00')
    new=q('TWSE MIS', '2026-11-02 09:10:00')
    assert prefer_quote(old, new)
    assert not prefer_quote(new, old)


def test_equal_time_prefers_official_over_backup():
    official=q('TWSE MIS', '2026-11-02 10:30:00')
    backup=q('Yahoo Finance', '2026-11-02 10:30:00')
    assert prefer_quote(backup, official)
    assert not prefer_quote(official, backup)


def test_visible_backup_age_uses_market_time_not_download_time():
    quality=quote_freshness({'1':q('Yahoo Finance', '2026-11-02 10:30:00'),
                             '2':q('TWSE MIS', '2026-11-02 10:44:59')}, now=NOW)
    assert quality['backup_age_seconds']==900
    assert quality['official_count']==quality['backup_count']==1
    assert '15 分鐘前' in quality['text']
    assert '重新抓取不代表成交時間已更新' in quality['text']


def test_quiet_stock_trade_age_is_not_labelled_as_feed_delay():
    quality=quote_freshness({'1':q('TWSE MIS', '2026-11-02 09:10:00')}, now=NOW)
    assert quality['backup_age_seconds'] is None
    assert '未重新確認' not in quality['text']
    quality=quote_freshness({'1':q('TWSE MIS', '2026-11-02 09:10:00','2026-11-02 10:40:00')}, now=NOW)
    assert '300 秒未重新確認' in quality['text']


def test_no_quote_does_not_invent_a_freshness_time():
    quality=quote_freshness({'1':None}, now=NOW)
    assert quality['official_count']==quality['backup_count']==0
    assert quality['oldest_backup_at'] is None
    assert '尚待確認' in quality['text']


def test_all_sector_guides_explain_scope_and_mismatches():
    guides=public_industry_guides()
    assert set(guides)=={x['id'] for x in INDUSTRIES}
    assert all(all(g[k] for k in ['us','tw','limit','relation']) for g in guides.values())
    assert '網通硬體' in guides['communication_services']['limit']
    assert '不是綠能' in guides['energy']['limit']
    assert '電力公司不是台灣重電' in guides['utilities']['limit']


def test_materials_example_is_a_steel_comparison_not_a_customer_claim():
    from us_rotation.config import SEED_SUPPLY_CHAIN
    steel=next(x for x in SEED_SUPPLY_CHAIN if x['code']=='2002')
    assert steel['us_industry']=='materials'
    assert steel['evidence_url']=='https://www.csc.com.tw/index.html'
    assert '未確認' in steel['confidence']
