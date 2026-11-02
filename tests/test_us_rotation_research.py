"""Date alignment and missing-data guards for the US/Taiwan observation screen."""
import ast
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from us_rotation.research import UNIVERSE, VERSION, aligned_returns, build_research, codes
from us_rotation import routes

TODAY = date(2026, 11, 2)
END = '2026-10-30'


def row(**changes):
    return dict(dict(code='2330', price_date=END, benchmark_date=END,
                     close=110, ma20=100, price_return_20d_pct=10,
                     relative_market_20d_pp=5, revenue_period='11509',
                     revenue_yoy_pct=0, total_net_lots=0,
                     institutional_source_date=END), **changes)


def screen(data, us_return=8, us_relative=3, us_date=END):
    snap = {'as_of': us_date, 'industries': [dict(id='semiconductors',
            return_20d_pct=us_return, relative_20d_pct=us_relative)]}
    return next(r for r in build_research(snap, [data], TODAY)['sectors']['semiconductors']['rows'] if r['code']=='2330')


def test_fixed_sample_is_unique_and_has_official_issuer_provenance():
    assert len(codes()) == 58
    assert len(UNIVERSE) == 12
    assert UNIVERSE['real_estate']['companies'] == []
    for config in UNIVERSE.values():
        assert len({r['code'] for r in config['companies']}) == len(config['companies'])
        assert all(r['source_url'].startswith('https://') and r['name'] and r['official_industry_code'] for r in config['companies'])


def test_price_return_uses_exact_market_sessions_and_not_adjusted_dividends():
    days = [(TODAY-timedelta(days=40)+timedelta(days=i)).isoformat() for i in range(30)]
    market = [dict(trade_date=d, close=200+i) for i,d in enumerate(days)]
    result = aligned_returns(days, [100+i for i in range(30)], market, days[-1])
    assert result['comparison_start_20d'] == days[9]
    assert result['price_return_20d_pct'] == pytest.approx((129/109-1)*100, abs=0.0001)
    assert result['relative_market_20d_pp'] == pytest.approx((129/109-229/209)*100, abs=0.0001)
    # A suspended stock missing the market's starting session cannot use its own 20th session.
    result = aligned_returns(days[:9]+days[10:], [100+i for i in range(9)]+[100+i for i in range(10,30)], market, days[-1])
    assert result['price_return_20d_pct'] is not None
    assert result['relative_market_20d_pp'] is None
    assert aligned_returns(days, list(range(100,130)), market, days[-2])['relative_market_20d_pp'] is None
    assert aligned_returns(days, list(range(100,130)), [], days[-1])['price_return_20d_pct'] is not None


@pytest.mark.parametrize('changes', [
    {'price_date':'2026-10-01'}, {'price_date':'2026-11-03'},
    {'benchmark_date':'2026-10-29'}, {'relative_market_20d_pp':None},
    {'price_return_20d_pct':None}, {'ma20':None}, {'ma20':0}, {'close':0},
])
def test_missing_stale_or_misaligned_price_never_becomes_aligned(changes):
    assert screen(row(**changes))['state'] == 'check'


def test_states_require_positive_absolute_and_relative_returns_and_ma():
    assert screen(row())['state'] == 'aligned'
    for changes in [{'relative_market_20d_pp':0},{'price_return_20d_pct':-1},{'close':99}]:
        assert screen(row(**changes))['state'] == 'waiting'
    assert screen(row(), us_return=-1)['state'] == 'local'
    assert screen(row(), us_return=None)['state'] == 'check'
    assert screen(row(), us_date='2026-10-01')['state'] == 'check'


def test_zero_revenue_is_preserved_and_separate_institutional_dates_are_visible():
    data = screen(row())
    assert data['revenue_period_label'] == '2026/09'
    assert data['revenue_state'] == '持平'
    assert data['total_net_lots'] == 0 and data['institutional_aligned']
    assert not screen(row(institutional_source_date='2026-10-29'))['institutional_aligned']
    assert screen(row(revenue_period='11506'))['revenue_state'] == '較早營收'
    assert screen(row(revenue_period=None))['revenue_state'] == '月份待核對'
    financial = build_research({}, [row(code='2881')], TODAY)['sectors']['financials']['rows']
    assert next(r for r in financial if r['code']=='2881')['revenue_state'] == '金融另查'


def test_background_failure_keeps_old_price_and_its_original_date(monkeypatch):
    monkeypatch.setattr(routes, 'read_latest_tw_company_snapshots', Mock(return_value=[row(research_version='old')]))
    monkeypatch.setattr(routes, 'fetch_symbol_history', Mock(return_value=[]))
    save = Mock(); monkeypatch.setattr(routes, 'save_tw_company_snapshots', save)
    collect = Mock(return_value={'as_of_date':'2026-11-02','rows':[row(close=None, price_date=None)]})
    bp=routes.create_blueprint(web_login_required=lambda f:f, render_page=Mock(),
         get_db_connection=Mock(),release_db_connection=Mock(),run_in_background=Mock(),fetch_tw_company_snapshot=collect)
    bp.us_rotation_research_job(force=True)
    assert collect.call_args.args[0] == codes()
    assert save.call_args.args[2] == END
    saved=save.call_args.args[3][0]
    assert saved['price_date']==END and saved['close']==110 and saved['research_version']==VERSION
    assert '保留既有' in saved['note']


def test_collector_excludes_intraday_prices_and_uses_raw_completed_close_ma():
    # Import only the collector: importing main would launch production workers.
    tree=ast.parse(Path(__file__).parents[1].joinpath('main.py').read_text())
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_build_us_rotation_tw_snapshot')
    days=[date(2026,9,1)+timedelta(days=i) for i in range(63)]
    days=[d for d in days if d.weekday()<5]
    prices={str(d):100+i for i,d in enumerate(days)}
    ns=dict(taiwan_now=lambda:datetime(2026,11,2,11),timedelta=timedelta,
      is_twse_trading_day=lambda d:d.weekday()<5,
      get_realtime_stocks_bulk=Mock(return_value={'2330':{'close_dates':list(prices),'closes':list(prices.values()),'adj_closes':[v/2 for v in prices.values()]}}),
      fetch_monthly_revenue=lambda **kw:{},fetch_institutional_data=lambda c:{'2330':{'source_date':'20261030','total_net_lots':0}},fetch_valuation=lambda:{},
      stock_display_name=lambda *a:'台積電',_revenue_cache={},_t86_cache={})
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'<collector>','exec'),ns)
    result=ns[fn.name](['2330'],benchmark_history=[dict(trade_date=d,close=v*2) for d,v in prices.items()])['rows'][0]
    completed=[v for d,v in prices.items() if d<=END]
    assert result['price_date']==END and result['close']==completed[-1]
    assert result['ma20']==sum(completed[-20:])/20
    assert result['relative_market_20d_pp']==0
    assert result['revenue_yoy_pct'] is None and result['total_net_lots'] == 0
    assert result['institutional_source_date']==END
