"""Audited corrections to public financial data; never touches trades or scores."""
from datetime import datetime, timezone
import json
import math
import re

from psycopg2.extras import execute_values
from revenue_parser import VERSION

AUDIENCE = "https://stock-bot-6xct.onrender.com/cron/revenue-repair"
WORKFLOW = "bingxuancai110-sss/stock-bot/.github/workflows/revenue-repair.yml@refs/heads/main"
STATUS_KEY = "revenue_repair_v2_status"
FIELDS = ("yoy_pct", "cum_yoy_pct", "mom_pct", "month_revenue")


def period_key(value):
    text = re.sub(r"[/\-]", "", str(value or ""))
    if len(text) == 5 and text.isdigit():
        text = str(int(text[:3]) + 1911) + text[3:]
    if not re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", text):
        raise ValueError("資料月份無效")
    return int(text)


def checked_rows(data):
    if not isinstance(data, dict) or not data or len(data) > 6000:
        raise ValueError("營收資料筆數無效")
    for code, item in data.items():
        if not re.fullmatch(r"\d{4,6}[A-Z]?", code) or not isinstance(item, dict) or item.get("parser_version") != VERSION:
            raise ValueError("營收代號或來源版本無效")
        for field in FIELDS:
            value = item.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError("營收數值無效")
        if item.get("month_revenue") is None:
            raise ValueError("月營收無效")
    return data


class RepairStore:
    def __init__(self, connect, release):
        self.connect, self.release = connect, release

    def plan(self, latest):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT period FROM revenue_history")
                periods = {period_key(r[0]) for r in cur.fetchall()}
            conn.commit()
            return sorted({latest} | {p for p in periods if p <= latest}, reverse=True)[:24]
        finally:
            self.release(conn)

    def apply(self, period, latest, data):
        period, latest, data = period_key(period), period_key(latest), checked_rows(data)
        if period > latest:
            raise ValueError("不更正尚未完整公告的月份")
        roc = f"{period // 100 - 1911:03d}{period % 100:02d}"
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(82831842)")
                cur.execute("""SELECT code,period,yoy_pct,cum_yoy_pct,mom_pct,month_revenue
                    FROM revenue_history WHERE period=ANY(%s) FOR UPDATE""", ([roc, str(period)],))
                before = cur.fetchall()
                changed, writes, existing = [], [], set()
                for row in before:
                    code, old_period = str(row[0]), str(row[1])
                    existing.add(code)
                    if code not in data:
                        continue
                    item = data[code]
                    new = tuple(item.get(f) for f in FIELDS)
                    old = tuple(float(x) if x is not None else None for x in row[2:])
                    if old != new:
                        changed.append(list(row))
                        writes.append((code, old_period, *new))
                if period == latest:
                    writes.extend((c, roc, *(item.get(f) for f in FIELDS)) for c, item in data.items() if c not in existing)
                # Capture the original state once. Retries must not overwrite rollback evidence.
                prior_snapshot = None
                if period == latest:
                    cur.execute("SELECT payload,source_meta,data_date FROM shared_data_snapshots WHERE snapshot_key='monthly_revenue' FOR UPDATE")
                    prior_snapshot = cur.fetchone()
                audit = {"period": roc, "before": changed, "inserted_codes": [c for c in data if c not in existing] if period == latest else [],
                         "monthly_snapshot": list(prior_snapshot) if prior_snapshot else None,
                         "parser_version": VERSION}
                cur.execute("""INSERT INTO shared_data_snapshots(snapshot_key,data_date,computed_at,payload,source_meta)
                    VALUES(%s,CURRENT_DATE,NOW(),%s::jsonb,'{}'::jsonb) ON CONFLICT(snapshot_key) DO NOTHING""",
                    ("revenue_repair_v2_backup:" + roc, json.dumps(audit, ensure_ascii=False, default=str)))
                if writes:
                    execute_values(cur, """INSERT INTO revenue_history(code,period,yoy_pct,cum_yoy_pct,mom_pct,month_revenue)
                        VALUES %s ON CONFLICT(code,period) DO UPDATE SET yoy_pct=EXCLUDED.yoy_pct,
                        cum_yoy_pct=EXCLUDED.cum_yoy_pct,mom_pct=EXCLUDED.mom_pct,month_revenue=EXCLUDED.month_revenue""", writes, page_size=500)
                if period == latest:
                    cur.execute("""INSERT INTO shared_data_snapshots(snapshot_key,data_date,computed_at,payload,source_meta)
                        VALUES('monthly_revenue',CURRENT_DATE,NOW(),%s::jsonb,%s::jsonb)
                        ON CONFLICT(snapshot_key) DO UPDATE SET data_date=EXCLUDED.data_date,computed_at=NOW(),
                        payload=EXCLUDED.payload,source_meta=EXCLUDED.source_meta""",
                        (json.dumps(data, ensure_ascii=False), json.dumps({"period":roc, "source":"MOPS named columns", "parser_version":VERSION})))
                    if changed:
                        notice = {"period":roc, "corrected_codes":sorted({r[0] for r in changed}),
                                  "repaired_at":datetime.now(timezone.utc).isoformat()}
                        cur.execute("""INSERT INTO shared_data_snapshots(snapshot_key,data_date,computed_at,payload,source_meta)
                            VALUES(%s,CURRENT_DATE,NOW(),%s::jsonb,'{}'::jsonb)
                            ON CONFLICT(snapshot_key) DO UPDATE SET computed_at=NOW(),payload=EXCLUDED.payload""",
                            (STATUS_KEY, json.dumps(notice)))
            conn.commit()
            return {"period":roc, "corrected_rows":len(changed), "written_rows":len(writes), "source_rows":len(data)}
        except Exception:
            conn.rollback()
            raise
        finally:
            self.release(conn)
