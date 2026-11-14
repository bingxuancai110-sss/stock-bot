import ast
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from taiex_cycle import initial_state, advance
from taiex_morning import summarize, read_brief, flex, html, lines

CALENDAR=lambda day:day.weekday()<5
def observation(day,close,ma=100):
    return dict(date=day,close=close,ma20=ma,below=close<ma,source='TWSE official test fixture')

def warning():
    state=initial_state(observation('2026-11-09',101))
    state=advance(state,observation('2026-11-10',99),'2026-11-11')
    return advance(state,observation('2026-11-11',98),'2026-11-12')

def reduction():return advance(warning(),observation('2026-11-12',97),'2026-11-13')


def test_day_two_warning_is_not_a_reduction_order():
    brief=summarize(warning(),date(2026,11,12),CALENDAR)
    text='\n'.join(lines(brief))
    assert brief['kind']=='warning' and '第2日預警' in text
    assert '尚未確認減碼' in text and '降至 50%' not in text
    assert '2026-11-11' in text and 'TWSE' in text


def test_day_three_confirmation_names_today_close_and_strategy_target_only():
    state=reduction();before=deepcopy(state)
    brief=summarize(state,date(2026,11,13),CALENDAR)
    assert brief['kind']=='reduce' and '今日收盤策略目標：股票配置降至 50%' in '\n'.join(lines(brief))
    assert state==before


@pytest.mark.parametrize('close,target,kind',[(97.85,75,'3%'),(99.275,100,'4.5%')])
def test_refill_uses_existing_event_and_execution_date(close,target,kind):
    state=advance(reduction(),observation('2026-11-13',95),'2026-11-16')
    state=advance(state,observation('2026-11-16',close),'2026-11-17')
    brief=summarize(state,date(2026,11,17),CALENDAR)
    text='\n'.join(lines(brief))
    assert brief['kind']=='refill' and kind in text and f'補至 {target}%' in text
    if target==100:assert '合併摘要' in text


def test_lower_low_tracking_does_not_invent_a_refill():
    state=advance(reduction(),observation('2026-11-13',95),'2026-11-16')
    state=advance(state,observation('2026-11-16',90),'2026-11-17')
    brief=summarize(state,date(2026,11,17),CALENDAR)
    assert brief['kind']=='tracking' and '90.00' in '\n'.join(lines(brief))
    assert '今日收盤策略目標' not in '\n'.join(lines(brief))


def test_old_below_baseline_is_not_retroactive_warning():
    state=initial_state(observation('2026-11-12',90))
    brief=summarize(state,date(2026,11,13),CALENDAR)
    assert brief['kind']=='neutral' and '舊走勢不補發' in '\n'.join(lines(brief))


def test_stale_or_missing_data_never_claims_no_new_warning_or_today_execution():
    brief=summarize(reduction(),date(2026,11,17),CALENDAR)
    text='\n'.join(lines(brief))
    assert brief['kind']=='unavailable' and '待更新' in text
    assert '今日收盤' not in text and '未有新訊號' not in text
    assert summarize(None,date(2026,11,13),CALENDAR)['kind']=='unavailable'
    state=reduction();state['recent_rows'][-1]['close']=float('nan')
    assert summarize(state,date(2026,11,13),CALENDAR)['kind']=='unavailable'


def test_weekend_uses_last_trading_close_not_calendar_yesterday():
    state=advance(reduction(),observation('2026-11-13',95),'2026-11-16')
    assert summarize(state,date(2026,11,16),CALENDAR)['kind']=='tracking'
    assert summarize(state,date(2026,11,14),CALENDAR)['kind']=='tracking'


def test_reader_is_approved_enabled_user_scoped_and_read_only():
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value=(reduction(),);release=Mock()
    assert read_brief(lambda:conn,release,'owner',date(2026,11,13),CALENDAR)['kind']=='reduce'
    sql,params=cur.execute.call_args.args
    assert params==('owner',) and 'enabled=TRUE' in sql and 'approved=TRUE' in sql
    assert all(not re.search(r'\b(INSERT|UPDATE|DELETE|CREATE)\b',c.args[0]) for c in cur.execute.call_args_list)
    conn.commit.assert_called_once();release.assert_called_once_with(conn)
    cur.fetchone.return_value=None
    assert read_brief(lambda:conn,release,'visitor',date(2026,11,13),CALENDAR) is None


def test_card_and_html_are_safe_and_clearly_identify_summary():
    brief=summarize(reduction(),date(2026,11,13),CALENDAR)
    brief['source']='<script>bad</script>'
    card=flex(brief)
    assert card['type']=='box' and '僅提醒，不自動交易' in json.dumps(card,ensure_ascii=False)
    assert '<script>' not in html(brief) and '&lt;script&gt;' in html(brief)
    assert flex(None) is None and html(None)==''


def test_compact_card_separates_status_metrics_and_action_without_duplicate_numbers():
    brief=summarize(reduction(),date(2026,11,13),CALENDAR)
    card=flex(brief)
    header=card['contents'][0]
    assert header['layout']=='horizontal'
    assert header['contents'][0]['text']=='月線警示'
    assert header['contents'][1]['contents'][0]['text']=='第3日確認'
    metrics=card['contents'][2]
    assert metrics['layout']=='horizontal' and len(metrics['contents'])==2
    raw=json.dumps(card,ensure_ascii=False)
    assert '今日收盤策略目標：股票配置降至 50%' in raw
    assert raw.count('97.00')==1 and raw.count('100.00')==1
    assert '2026-11-12 收盤' in raw and 'TWSE 官方收盤' in raw


def test_compact_unavailable_card_never_renders_zero_prices_or_a_clear_status():
    brief=summarize(None,date(2026,11,13),CALENDAR)
    raw=json.dumps(flex(brief),ensure_ascii=False)
    assert '待更新' in raw and '等待第一筆' in raw
    assert '加權收盤' not in raw and '0.00' not in raw and '無新訊號' not in raw


def load_morning_builder(brief,url=True):
    source=Path('main.py').read_text(encoding='utf-8-sig')
    match=re.search(r'(?ms)^def build_morning_push_message\b.*?(?=^def |\Z)',source)
    fn=next(n for n in ast.parse(match[0]).body if isinstance(n,ast.FunctionDef))
    ns=dict(_morning_ma20_brief=Mock(return_value=brief),merge_change_events=Mock(return_value=[]),
        _morning_macro_news_lines=lambda:[],build_today_attention_push=lambda uid:'原有盤前事件',
        _morning_macro_lines=lambda:['原有市場摘要'],_morning_macro_news=lambda:[],
        _line_feature_web_url=lambda *args:'https://example.test/premarket' if url else None,
        _macro_metric_rows=lambda group:[],FlexSendMessage=lambda **kw:SimpleNamespace(**kw),
        TextSendMessage=lambda **kw:SimpleNamespace(**kw))
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'morning','exec'),ns)
    return ns


def test_same_brief_in_daily_push_and_manual_premarket_without_new_alert_send():
    ns=load_morning_builder(summarize(reduction(),date(2026,11,13),CALENDAR))
    result=ns['build_morning_push_message']('owner')
    raw=json.dumps(result.contents,ensure_ascii=False)
    assert '第3日確認' in raw and '查看完整盤前分析' in raw and len(result.alt_text)<=400
    ns['_morning_ma20_brief'].assert_called_once_with('owner')


def test_text_fallback_keeps_warning_and_non_subscriber_has_no_new_card():
    brief=summarize(warning(),date(2026,11,12),CALENDAR)
    result=load_morning_builder(brief,url=False)['build_morning_push_message']('owner')
    assert '第2日預警' in result.text and '原有盤前事件' in result.text
    result=load_morning_builder(None)['build_morning_push_message']('visitor')
    assert '月線警示' not in json.dumps(result.contents,ensure_ascii=False)
