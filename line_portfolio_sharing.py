"""Resolve the explicitly shared owner, independently of the message sender."""


def shared_owner(environ):
    admins = {uid.strip() for uid in environ.get("ADMIN_USER_ID", "").split(",") if uid.strip()}
    configured = environ.get("LINE_SHARED_PORTFOLIO_USER_ID", "").strip()
    if configured:
        # A dedicated setting must still name an administrator, never another user.
        return configured if configured in admins else None
    # This deployment's sole administrator is the owner who authorized sharing.
    # Never select the first of multiple admins or fall back to the requester.
    return next(iter(admins)) if len(admins) == 1 else None
