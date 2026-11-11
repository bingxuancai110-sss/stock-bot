import ast
import csv
from io import StringIO
from pathlib import Path
import re
from unittest.mock import Mock, MagicMock

from flask import Flask
import pytest

from revenue_parser import parse_html, parse_csv, parse_rows, VERSION
from revenue_repair import RepairStore, checked_rows, period_key
import revenue_repair
import revenue_repair_routes

# Captured public 2026-10 MOPS header and rows; these are parser regression fixtures.
HEADER = ['公司代號','公司名稱','當月營收','上月營收','去年當月營收','上月比較增減(%)',
          '去年同月增減(%)','當月累計營收','去年累計營收','前期比較增減(%)']
DELTA = ['2308','台達電','74,666,163','65,399,563','57,383,550','14.16','30.11','614,333,699','450,655,619','36.31']
EMC = ['2383','台光電','24,472,758','21,532,230','8,074,815','13.65','203.07','165,699,719','77,408,197','114.05']


def test_actual_official_header_does_not_confuse_three_growth_periods():
    data = parse_rows([HEADER, DELTA, EMC])
    assert data['2383']['cum_yoy_pct'] == 114.05
    assert data['2383']['mom_pct'] == 13.65 and data['2383']['yoy_pct'] == 203.07
    assert data['2308']['cum_yoy_pct'] == 36.31 and data['2308']['mom_pct'] == 14.16


def test_column_reordering_and_repeated_html_industry_headers():
    def table(rows):
        return '<table>'+''.join('<tr>'+''.join('<td>'+v+'</td>' for v in r)+'</tr>' for r in rows)+'</table>'
    order = [4,8,2,0,6,1,3,7,9,5]
    shuffled = [[r[i] for i in order] for r in [HEADER,EMC]]
    data = parse_html(table([HEADER,DELTA])+table(shuffled))
    assert data['2383']['cum_yoy_pct'] == 114.05 and data['2308']['mom_pct'] == 14.16


def test_csv_and_big5_html_produce_identical_financial_values():
    out=StringIO();writer=csv.writer(out);writer.writerows([HEADER,DELTA,EMC])
    assert parse_csv(out.getvalue().encode('cp950')) == parse_rows([HEADER,DELTA,EMC])
    assert set(parse_csv(out.getvalue().encode(), '2383')) == {'2383'}


def test_missing_cumulative_label_uses_amounts_not_a_positional_guess():
    rows = [[v for i,v in enumerate(r) if i != 9] for r in [HEADER,DELTA]]
    assert parse_rows(rows)['2308']['cum_yoy_pct'] == round((614333699/450655619-1)*100, 2)
    rows = [[v for i,v in enumerate(r) if i not in {7,8,9}] for r in [HEADER,DELTA]]
    assert parse_rows(rows)['2308']['cum_yoy_pct'] is None


def test_inconsistent_percentage_rejected_and_negative_official_month_preserved():
    row = EMC.copy();row[9]='13.65'
    with pytest.raises(ValueError, match='不一致'):
        parse_rows([HEADER,row])
    row = EMC.copy();row[2]='(100)'
    assert parse_rows([HEADER,row])['2383']['month_revenue'] == -100
    with pytest.raises(ValueError, match='欄名重複'):
        parse_rows([HEADER+['前期比較增減(%)'], EMC+['13.65']])


def score_functions():
    source=Path('main.py').read_text(encoding='utf-8-sig');ns={}
    for name in ('score_from_cum_revenue_growth','score_from_valuation'):
        match=re.search(r'(?ms)^def '+name+r'\b.*?(?=^(?:def |class |@)|\Z)',source)
        exec(compile(ast.parse(match[0]),name,'exec'),ns)
    return ns


def test_existing_formulas_reproduce_screenshots_and_corrected_inputs_without_changes():
    ns=score_functions();rev=ns['score_from_cum_revenue_growth'];val=ns['score_from_valuation']
    assert round(rev(13.65)*20/40) == 11 and val(46.76,13.65)[0] == 2
    assert round(rev(114.05)*20/40) == 20 and val(46.76,114.05)[0] == 12
    assert round(rev(36.31)*20/40) == 17 and val(38.29,36.31)[0] == 13


def test_audit_and_data_update_commit_together_without_touching_user_trades(monkeypatch):
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=[('2383','11510',203.07,13.65,203.07,24472758)]
    cur.fetchone.return_value=({'old':'snapshot'},{'period':'11510'},None)
    upsert=Mock();monkeypatch.setattr(revenue_repair,'execute_values',upsert)
    result=RepairStore(lambda:conn,lambda c:None).apply(202610,202610,parse_rows([HEADER,EMC]))
    assert result['corrected_rows'] == 1
    writes=upsert.call_args.args[2]
    assert writes[0][3] == 114.05 and writes[0][4] == 13.65
    statements=[c.args[0] for c in cur.execute.call_args_list]
    audit=next(s for s in statements if 'ON CONFLICT(snapshot_key) DO NOTHING' in s)
    assert audit and all('positions' not in s and 'watchlist_scores' not in s for s in statements)
    conn.commit.assert_called_once();conn.rollback.assert_not_called()


def test_failed_upsert_rolls_back_audit_and_corrections(monkeypatch):
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=[('2383','11510',203.07,13.65,203.07,24472758)]
    cur.fetchone.return_value=None
    monkeypatch.setattr(revenue_repair,'execute_values',Mock(side_effect=RuntimeError('db error')))
    with pytest.raises(RuntimeError):
        RepairStore(lambda:conn,lambda c:None).apply(202610,202610,parse_rows([HEADER,EMC]))
    conn.rollback.assert_called_once();conn.commit.assert_not_called()


def test_existing_correct_values_do_not_rewrite_or_overwrite_audit(monkeypatch):
    conn=MagicMock();cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value=[('2383','11510',203.07,114.05,13.65,24472758)]
    cur.fetchone.return_value=None
    upsert=Mock();monkeypatch.setattr(revenue_repair,'execute_values',upsert)
    result=RepairStore(lambda:conn,lambda c:None).apply(202610,202610,parse_rows([HEADER,EMC]))
    assert result['written_rows'] == 0
    upsert.assert_not_called()


def test_unknown_revision_nonfinite_and_future_month_rejected():
    with pytest.raises(ValueError):checked_rows({'2383':{'month_revenue':1}})
    row=parse_rows([HEADER,EMC]);row['2383']['cum_yoy_pct']=float('nan')
    with pytest.raises(ValueError):checked_rows(row)
    with pytest.raises(ValueError):RepairStore(Mock(),Mock()).apply(202611,202610,parse_rows([HEADER,EMC]))
    assert period_key('11510') == period_key('2026-10') == 202610


def test_unauthorized_repair_cannot_read_plan_or_write(monkeypatch):
    monkeypatch.setattr(revenue_repair_routes,'authorized',Mock(return_value=False))
    connect=Mock();app=Flask(__name__)
    app.register_blueprint(revenue_repair_routes.create_blueprint(connect,Mock(),Mock(),Mock()))
    assert app.test_client().get('/cron/revenue-repair').status_code == 403
    assert app.test_client().post('/cron/revenue-repair',json={}).status_code == 403
    connect.assert_not_called()


def test_incomplete_market_does_not_publish_partial_repair(monkeypatch):
    monkeypatch.setattr(revenue_repair_routes,'authorized',Mock(return_value=True))
    connect=Mock();app=Flask(__name__)
    app.register_blueprint(revenue_repair_routes.create_blueprint(connect,Mock(),Mock(),Mock()))
    response=app.test_client().post('/cron/revenue-repair',json={'markets':{'sii':1},'period':202610,'data':parse_rows([HEADER,EMC])})
    assert response.status_code == 422
    connect.assert_not_called()
