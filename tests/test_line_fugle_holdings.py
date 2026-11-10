import ast
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo
import time

import pytest


@pytest.fixture(scope='module')
def compiled():
    tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text(encoding='utf-8'))
    names = {'_line_fugle_holdings_quotes', '_line_hidden_holdings_flex',
             '_line_hidden_combined_portfolio_flex'}
    return {n.name: compile(ast.Module(body=[n], type_ignores=[]), '<actual-line>', 'exec')
            for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names}


def test_private_collector_is_owner_scoped_and_rejects_old_observations(compiled):
    now = datetime(2026, 11, 10, 10, 42, tzinfo=ZoneInfo('Asia/Taipei'))
    row = dict(code='2330', price=110, previous_close=100,
               time=now.timestamp()*1e6, observed_at=now.timestamp()*1000)
    service = Mock()
    service.enabled_for.side_effect = lambda uid: uid == 'owner'
    service.read.return_value = {'quotes': {'2330': row, 'IX0001': {'code': 'IX0001'}}}
    ns = dict(_fugle_background_service=service, taiwan_now=lambda: now,
              _taiwan_post_close=lambda _: False)
    exec(compiled['_line_fugle_holdings_quotes'], ns)
    get = ns['_line_fugle_holdings_quotes']
    assert get('other', ['2330']) == {}
    service.read.assert_not_called()
    assert get('owner', ['2330'])['2330']['pct'] == 10
    row['observed_at'] -= 181000
    assert get('owner', ['2330']) == {}


def test_fresh_fugle_wins_and_mis_only_fetches_missing_symbols(compiled):
    fresh = {'close': 110, 'pct': 10}
    fallback = {'close': 99, 'pct': -1}
    ns = dict(time=time, merge_positions=lambda x: x,
              get_positions=lambda _: [{'code': '2330'}, {'code': '2383'}],
              _load_line_holdings_quote_snapshot=lambda *a: {'2330': {'close': 100, 'pct': 0}},
              _line_fugle_holdings_quotes=lambda *a: {'2330': fresh},
              _is_taiwan_intraday_window=lambda: True,
              _home_market_suffix_map=lambda c: {},
              _fetch_twse_mis_quotes=Mock(return_value={'2383': fallback}),
              _line_hidden_holdings_flex=Mock(return_value=SimpleNamespace(contents={})),
              _line_hidden_operation_report=Mock(return_value=SimpleNamespace(contents={})),
              FlexSendMessage=lambda **kw: kw)
    exec(compiled['_line_hidden_combined_portfolio_flex'], ns)
    ns['_line_hidden_combined_portfolio_flex']('owner')
    assert ns['_fetch_twse_mis_quotes'].call_args.args[0] == ['2383']
    prices = ns['_line_hidden_holdings_flex'].call_args.kwargs['prices']
    assert prices == {'2330': fresh, '2383': fallback}
    assert ns['_line_hidden_operation_report'].call_args.kwargs['price_map'] == prices


def test_card_keeps_real_flat_price_but_marks_old_quote_pending_and_shows_times(compiled):
    ns = dict(FlexSendMessage=lambda **kw: kw, taiwan_today=lambda: datetime(2026,11,10).date(),
              stock_display_name=lambda c, *a: c, short_company_name=lambda n: n)
    exec(compiled['_line_hidden_holdings_flex'], ns)
    result = ns['_line_hidden_holdings_flex']('owner',
        positions=[{'code': '2330', 'shares': 1}, {'code': '2383', 'shares': 1}],
        prices={'2330': dict(close=100, pct=0, close_date='20261110', has_live_trade_price=True,
                             updated_at='2026-11-10 10:42:00', source='富果'),
                '2383': dict(close=100, pct=0, close_date='20261109', has_live_trade_price=True)})
    import json
    text = json.dumps(result, ensure_ascii=False)
    assert '+0.00%' in text  # A confirmed flat market quote is still valid.
    assert '待更新' in text
    assert '2026-11-10 10:42:00' in text
    assert '富果' in text

@pytest.mark.parametrize('price,expected', [(109.5, '漲停'), (89.6, '跌停'), (105, None)])
def test_official_limits_survive_collector_and_render_distinct_badges(compiled, price, expected):
    from fugle_background import quote_row
    from fugle_live import normalize_quotes
    now = datetime(2026,11,10,10,42,tzinfo=ZoneInfo('Asia/Taipei'))
    raw = quote_row('2330', dict(symbol='2330', date='2026-11-10', referencePrice=100,
        lastTrade={'price': price, 'time': int(now.timestamp()*1e6)},
        limitUpPrice=109.5, limitDownPrice=89.6), now)
    quote = normalize_quotes([raw], {'2330'}, now)['2330']
    ns = dict(FlexSendMessage=lambda **kw: kw, taiwan_today=lambda: now.date(),
              stock_display_name=lambda c,*a: c, short_company_name=lambda n:n)
    exec(compiled['_line_hidden_holdings_flex'], ns)
    card = ns['_line_hidden_holdings_flex']('owner', positions=[{'code':'2330','shares':1}], prices={'2330':quote})
    def boxes(value):
        if isinstance(value,dict):
            yield value
            for child in value.values():
                yield from boxes(child)
        elif isinstance(value,list):
            for child in value:
                yield from boxes(child)
    badges = [v for v in boxes(card) if v.get('text') in ('漲停','跌停')]
    assert [v['text'] for v in badges] == ([expected] if expected else [])
    if expected:
        assert badges[0]['color'] == '#FFFFFF'
        color = '#D93025' if expected == '漲停' else '#18845B'
        assert any(v.get('backgroundColor') == color for v in boxes(card))
