"""PostgreSQL persistence for daily rotation and point-in-time company mappings."""

from __future__ import annotations

import json

from psycopg2.extras import execute_values

from .config import SEED_SUPPLY_CHAIN


def seed_supply_chain(cursor):
    for row in SEED_SUPPLY_CHAIN:
        cursor.execute("""
            INSERT INTO us_supply_chain_map
                (map_key, us_industry, chain, tw_code, tw_name, product, role,
                 reason, evidence_url, evidence_date, confidence, evidence_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'product_verified_relationship_unverified')
            ON CONFLICT (map_key) DO NOTHING
        """, (row["map_key"], row["us_industry"], row["chain"], row["code"], row["name"],
              row["product"], row["role"], row["reason"], row["evidence_url"],
              row["evidence_date"], row["confidence"]))


def save_snapshot(get_db_connection, release_db_connection, snapshot, price_histories,
                  errors=None, source_meta=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        as_of = snapshot.get("as_of")
        cur.execute("""
            INSERT INTO us_rotation_snapshots
                (as_of_date, algorithm_version, source, fetched_at, payload, source_meta)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (as_of_date, algorithm_version)
            DO UPDATE SET source=EXCLUDED.source, fetched_at=EXCLUDED.fetched_at,
                          payload=EXCLUDED.payload, source_meta=EXCLUDED.source_meta
            RETURNING id
        """, (as_of, snapshot["algorithm_version"], snapshot["source"], snapshot["fetched_at"],
              json.dumps(snapshot, ensure_ascii=False),
              json.dumps({"errors": errors or {}, "unofficial_source": True,
                          **(source_meta or {})}, ensure_ascii=False)))
        snapshot_id = int(cur.fetchone()[0])
        values = [
            (symbol, row["trade_date"], row.get("close"), row.get("adjusted_close"),
             row.get("volume"), snapshot["source"], snapshot["fetched_at"])
            for symbol, rows in price_histories.items() for row in rows
        ]
        execute_values(cur, """
            INSERT INTO us_rotation_prices
                (symbol, trade_date, close, adjusted_close, volume, source, fetched_at)
            VALUES %s
            ON CONFLICT (symbol, trade_date) DO UPDATE
            SET close=EXCLUDED.close, adjusted_close=EXCLUDED.adjusted_close,
                volume=EXCLUDED.volume, source=EXCLUDED.source, fetched_at=EXCLUDED.fetched_at
        """, values, template="(%s,%s,%s,%s,%s,%s,%s)", page_size=500)
        cur.execute("""
            WITH starts AS (
                SELECT s.id AS snapshot_id, p.symbol, p.trade_date AS start_date,
                       p.adjusted_close AS start_close
                FROM us_rotation_snapshots s
                JOIN us_rotation_prices p ON p.trade_date=s.as_of_date
                WHERE s.as_of_date >= CURRENT_DATE - INTERVAL '150 days'
                  AND p.adjusted_close > 0
            ), future AS (
                SELECT starts.snapshot_id, starts.symbol, starts.start_date,
                       starts.start_close, p.trade_date AS end_date,
                       p.adjusted_close AS end_close,
                       ROW_NUMBER() OVER (
                           PARTITION BY starts.snapshot_id, starts.symbol
                           ORDER BY p.trade_date
                       ) AS session_no
                FROM starts
                JOIN us_rotation_prices p
                  ON p.symbol=starts.symbol AND p.trade_date>starts.start_date
                 AND p.adjusted_close>0
            )
            INSERT INTO us_rotation_forward_returns
                (snapshot_id, symbol, horizon_sessions, start_date, end_date, return_pct)
            SELECT snapshot_id, symbol, session_no, start_date, end_date,
                   ROUND(((end_close / start_close - 1) * 100)::numeric, 4)
            FROM future WHERE session_no IN (5,20,60)
            ON CONFLICT (snapshot_id, symbol, horizon_sessions) DO NOTHING
        """)
        cur.execute("""
            UPDATE us_rotation_snapshots s
            SET source_meta = source_meta || jsonb_build_object('snapshot_id', %s)
            WHERE id = %s
        """, (snapshot_id, snapshot_id))
        conn.commit()
        cur.close()
        return snapshot_id
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def read_latest(get_db_connection, release_db_connection):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, as_of_date::text, fetched_at::text, payload, source_meta
            FROM us_rotation_snapshots
            ORDER BY as_of_date DESC, id DESC LIMIT 1
        """)
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        payload = row[3] if isinstance(row[3], dict) else json.loads(row[3] or "{}")
        payload["snapshot_id"] = int(row[0])
        payload["as_of"] = row[1]
        payload["fetched_at"] = row[2]
        payload["source_meta"] = row[4] if isinstance(row[4], dict) else json.loads(row[4] or "{}")
        return payload
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def read_signal_prices(get_db_connection, release_db_connection, end_date):
    """Read bounded saved history: 60 displayed sessions plus 20 for credit returns."""
    if not end_date:
        return {}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            WITH recent AS (
                SELECT symbol, trade_date, close, adjusted_close,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS n
                FROM us_rotation_prices
                WHERE symbol=ANY(%s) AND trade_date<=%s
            )
            SELECT symbol, trade_date::text, close, adjusted_close
            FROM recent WHERE n<=81 ORDER BY symbol, trade_date
        """, (["^VIX", "HYG", "IEF", "^GSPC", "SPY", "^TWII"], end_date))
        histories = {}
        for symbol, day, close, adjusted in cur.fetchall():
            histories.setdefault(symbol, []).append({
                "trade_date": day, "close": float(close) if close is not None else None,
                "adjusted_close": float(adjusted) if adjusted is not None else None})
        cur.close()
        return histories
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def read_history(get_db_connection, release_db_connection, limit=25):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT as_of_date::text, payload
            FROM us_rotation_snapshots
            ORDER BY as_of_date DESC, id DESC LIMIT %s
        """, (max(1, min(int(limit), 90)),))
        rows = cur.fetchall()
        cur.close()
        output = []
        for day, payload in reversed(rows):
            if not isinstance(payload, dict):
                payload = json.loads(payload or "{}")
            output.append({"as_of": day, "industries": payload.get("industries") or []})
        return output
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def read_supply_chain(get_db_connection, release_db_connection):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT us_industry, chain, tw_code, tw_name, product, role, reason,
                   evidence_url, evidence_date, confidence, evidence_status
            FROM us_supply_chain_map
            WHERE active=TRUE
            ORDER BY us_industry, chain, tw_code
        """)
        rows = cur.fetchall()
        cur.close()
        fields = ("us_industry", "chain", "code", "name", "product", "role", "reason",
                  "evidence_url", "evidence_date", "confidence", "evidence_status")
        return [dict(zip(fields, row)) for row in rows]
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def save_tw_company_snapshots(get_db_connection, release_db_connection, as_of_date,
                              rows, source_meta=None):
    if not rows:
        return 0
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        values = [(as_of_date, row["code"], json.dumps(row, ensure_ascii=False),
                   json.dumps(source_meta or {}, ensure_ascii=False)) for row in rows]
        execute_values(cur, """
            INSERT INTO us_tw_company_snapshots
                (as_of_date, tw_code, payload, source_meta)
            VALUES %s
            ON CONFLICT (as_of_date, tw_code) DO UPDATE
            SET captured_at=NOW(), payload=EXCLUDED.payload, source_meta=EXCLUDED.source_meta
        """, values, template="(%s,%s,%s::jsonb,%s::jsonb)", page_size=100)
        conn.commit()
        cur.close()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def read_latest_tw_company_snapshots(get_db_connection, release_db_connection):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT as_of_date::text, tw_code, payload, source_meta, captured_at::text
            FROM us_tw_company_snapshots
            WHERE as_of_date=(SELECT MAX(as_of_date) FROM us_tw_company_snapshots)
            ORDER BY tw_code
        """)
        rows = cur.fetchall()
        cur.close()
        output = []
        for as_of_date, code, payload, source_meta, captured_at in rows:
            payload = payload if isinstance(payload, dict) else json.loads(payload or "{}")
            payload["as_of_date"] = as_of_date
            payload["captured_at"] = captured_at
            payload["code"] = code
            payload["source_meta"] = source_meta if isinstance(source_meta, dict) else json.loads(source_meta or "{}")
            output.append(payload)
        return output
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)

