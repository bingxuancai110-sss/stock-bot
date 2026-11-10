"""Short-lived task-owned connections; never borrow a shared long-lived SSL stream."""
import json


class SnapshotStore:
    def __init__(self, connect, release):
        self.connect, self.release = connect, release

    def load(self, key, max_age_seconds=86400):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT data_date,computed_at,payload,source_meta FROM shared_data_snapshots
                    WHERE snapshot_key=%s AND computed_at >= NOW() - (%s * INTERVAL '1 second')""", (key, max_age_seconds))
                row = cur.fetchone()
            conn.commit()
            return dict(zip(("data_date", "computed_at", "payload", "source_meta"), row)) if row else None
        finally:
            self.release(conn)

    def save(self, key, payload, data_date=None, source_meta=None):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO shared_data_snapshots(snapshot_key,data_date,computed_at,payload,source_meta)
                    VALUES(%s,%s,NOW(),%s::jsonb,%s::jsonb) ON CONFLICT(snapshot_key) DO UPDATE SET
                    data_date=EXCLUDED.data_date,computed_at=EXCLUDED.computed_at,
                    payload=EXCLUDED.payload,source_meta=EXCLUDED.source_meta""",
                    (key, data_date, json.dumps(payload, ensure_ascii=False), json.dumps(source_meta or {})))
                if key.startswith("taiex_reminder_run:"):
                    cur.execute("""DELETE FROM shared_data_snapshots WHERE snapshot_key LIKE 'taiex_reminder_run:%%'
                        AND computed_at < NOW()-INTERVAL '30 days'""")
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            self.release(conn)

    def feature_enabled(self):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT enabled FROM admin_feature_flags WHERE feature_key='line_daily'")
                row = cur.fetchone()
            conn.commit()
            return bool(row[0]) if row else True
        finally:
            self.release(conn)

    def quiet(self, user_id, now):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT COALESCE(quiet_hours_enabled,FALSE),COALESCE(quiet_start,23),
                    COALESCE(quiet_end,7) FROM users WHERE user_id=%s""", (user_id,))
                row = cur.fetchone()
            conn.commit()
            if not row or not row[0]:
                return False
            start, end, hour = int(row[1]), int(row[2]), now.hour
            return True if start == end else (start <= hour < end if start < end else hour >= start or hour < end)
        finally:
            self.release(conn)
