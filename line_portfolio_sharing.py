"""Resolve the explicitly shared owner, independently of the message sender."""
import json

KEY = "line_shared_portfolio_owner"


def shared_owner(environ):
    admins = {uid.strip() for uid in environ.get("ADMIN_USER_ID", "").split(",") if uid.strip()}
    configured = environ.get("LINE_SHARED_PORTFOLIO_USER_ID", "").strip()
    if configured:
        # A dedicated setting must still name an administrator, never another user.
        return configured if configured in admins else None
    # This deployment's sole administrator is the owner who authorized sharing.
    # Never select the first of multiple admins or fall back to the requester.
    return next(iter(admins)) if len(admins) == 1 else None


class SharingSettings:
    def __init__(self, connect, release, environ):
        self.connect, self.release, self.environ = connect, release, environ

    def owner(self):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM shared_data_snapshots WHERE snapshot_key=%s", (KEY,))
                row = cur.fetchone()
            conn.commit()
        finally:
            self.release(conn)
        if not row:
            return shared_owner(self.environ)
        uid = str((row[0] or {}).get("user_id") or "")
        admins = {x.strip() for x in self.environ.get("ADMIN_USER_ID", "").split(",")}
        return uid if uid and uid in admins else None

    def enable(self, uid):
        admins = {x.strip() for x in self.environ.get("ADMIN_USER_ID", "").split(",") if x.strip()}
        configured = shared_owner(self.environ)
        if uid not in admins or (configured and configured != uid):
            return False
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                # Claim once, or idempotently confirm the same owner's claim.
                # A second administrator cannot replace the sharing account.
                cur.execute("""INSERT INTO shared_data_snapshots
                    (snapshot_key,data_date,computed_at,payload,source_meta)
                    VALUES(%s,CURRENT_DATE,NOW(),%s::jsonb,'{}'::jsonb)
                    ON CONFLICT(snapshot_key) DO UPDATE SET computed_at=NOW()
                    WHERE shared_data_snapshots.payload->>'user_id'=EXCLUDED.payload->>'user_id'
                    RETURNING snapshot_key""", (KEY, json.dumps({"user_id": uid})))
                ok = cur.fetchone() is not None
            conn.commit()
            return ok
        except Exception:
            conn.rollback()
            raise
        finally:
            self.release(conn)
