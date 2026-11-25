"""Short-lived task-owned connections; never borrow a shared long-lived SSL stream."""
from base64 import b64decode, b64encode
import json
import zlib


class SnapshotStore:
    def __init__(self, connect, release, *, load_timeout_ms=None):
        self.connect, self.release = connect, release
        self.load_timeout_ms = load_timeout_ms

    def load(self, key, max_age_seconds=86400):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                if self.load_timeout_ms:
                    cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(self.load_timeout_ms),))
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

    def load_short_cycle(self, key, session_day, selection_version):
        return self._load_short_cycle([key], session_day, selection_version)

    def _load_short_cycle(self, keys, session_day, selection_version):
        """Read the saved candidate queue without loading all strategy charts."""
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout=5000")
                cur.execute("""SELECT payload->'cycle'->'queue', payload->'cycle'->'stocks', payload->'scope'
                    FROM shared_data_snapshots WHERE snapshot_key=ANY(%s)
                      AND payload->'cycle'->>'day'=%s AND payload->>'selection_version'=%s
                    ORDER BY computed_at DESC LIMIT 1""",
                    (keys, session_day, str(selection_version)))
                row = cur.fetchone()
            conn.commit()
            return dict(zip(('queue', 'stocks', 'scope'), row)) if row else None
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


class ShortTermSnapshotStore(SnapshotStore):
    """Write small DB checkpoints and keep the legacy key readable for rollback."""
    suffix = ':compressed-v1'

    def claim_close_batch(self, key):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout=4000")
                cur.execute("""INSERT INTO shared_data_snapshots(snapshot_key,computed_at,payload,source_meta)
                    VALUES(%s,NOW(),'{}','{}') ON CONFLICT(snapshot_key) DO UPDATE SET computed_at=NOW()
                    WHERE shared_data_snapshots.computed_at < NOW()-INTERVAL '10 minutes'
                    RETURNING computed_at""", (key,))
                row = cur.fetchone()
            conn.commit()
            return row[0] if row else None
        finally:
            self.release(conn)

    def finish_close_batch(self, key, claimed_at):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout=4000")
                cur.execute("DELETE FROM shared_data_snapshots WHERE snapshot_key=%s AND computed_at=%s",
                            (key, claimed_at))
            conn.commit()
        finally:
            self.release(conn)

    @staticmethod
    def pack(payload):
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'))
        compressor = zlib.compressobj(level=1)
        packed = bytearray(compressor.compress(b'{'))
        for index, (name, value) in enumerate(payload.items()):
            prefix = (',' if index else '') + encoder.encode(name) + ':'
            packed.extend(compressor.compress(prefix.encode()))
            if name in ('rows', 'all_rows', 'short_rows'):
                packed.extend(compressor.compress(b'['))
                for i, row in enumerate(value):
                    if i:
                        packed.extend(compressor.compress(b','))
                    packed.extend(compressor.compress(encoder.encode(row).encode()))
                packed.extend(compressor.compress(b']'))
            else:
                packed.extend(compressor.compress(encoder.encode(value).encode()))
        packed.extend(compressor.compress(b'}'))
        packed.extend(compressor.flush())
        return b64encode(packed).decode('ascii')

    def load(self, key, max_age_seconds=86400):
        snapshot = super().load(key + self.suffix, max_age_seconds)
        if not snapshot:
            return super().load(key, max_age_seconds)
        snapshot['payload'] = json.loads(zlib.decompress(b64decode(snapshot['payload']['packed'])))
        return snapshot

    def save(self, key, payload, data_date=None, source_meta=None):
        # Closing recovery can seed from the intraday queue without decoding
        # all of the scan's rows.
        cycle = payload.get('cycle', {})
        envelope = {
            'packed': self.pack(payload),
            'cycle': {field: cycle.get(field) for field in ('day', 'queue', 'stocks')},
            'scope': payload.get('scope'),
            'selection_version': payload.get('selection_version'),
        }
        return super().save(key + self.suffix, envelope, data_date, source_meta)

    def load_short_cycle(self, key, session_day, selection_version):
        return self._load_short_cycle([key + self.suffix, key], session_day, selection_version)
