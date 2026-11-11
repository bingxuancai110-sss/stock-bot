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
            configured = shared_owner(self.environ)
            if configured:
                return configured
            # The owner has explicitly authorized this named public report.
            # Reuse their pre-existing exact UID / verified Google-email mapping,
            # never a display-name search or the first account in an admin list.
            if self.environ.get("LINE_SHARED_PORTFOLIO_USER_ID", "").strip():
                return None  # An invalid explicit setting must not silently change owner.
            owner = self.environ.get("FUGLE_TEST_USER_ID", "").strip()
            if not owner and self.environ.get("FUGLE_TEST_USER_EMAIL", "").strip():
                owner = self._email_owner(self.environ["FUGLE_TEST_USER_EMAIL"].strip().lower())
            admins = {x.strip() for x in self.environ.get("ADMIN_USER_ID", "").split(",") if x.strip()}
            return owner if owner and owner in admins else None
        uid = str((row[0] or {}).get("user_id") or "")
        admins = {x.strip() for x in self.environ.get("ADMIN_USER_ID", "").split(",")}
        return uid if uid and uid in admins else None

    def _email_owner(self, email):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT DISTINCT user_id FROM user_auth_identities
                    WHERE provider='google' AND LOWER(email)=%s LIMIT 2""", (email,))
                rows = cur.fetchall()
            conn.commit()
            return str(rows[0][0]) if len(rows) == 1 and rows[0][0] else None
        finally:
            self.release(conn)

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
