# Buy submission acknowledgement and duplicate protection

The lightweight POST result renders `.p2-trade-result.success`, whereas the
client accepted only the old `.msg` response. A committed buy therefore looked
unconfirmed, and submitting it again inserted another lot and add log.

Changes:

- Accept the explicit server result status; keep validation failures as errors.
- Give the buy form a submission key and retain it across failed responses.
- Reserve `(user_id, request_key)` in `position_submission_dedup` in the same
  transaction as the position lot and operation log. A committed replay adds
  nothing, including when the first commit reply was lost.
- Compare a stored payload hash, blocking changed input with a previously used key.
- Reject old buy forms without a key before inserting anything.
- Avoid saying no data was written when a commit outcome is unknown.

Schema is additive and created by the existing startup `init_db`. Deploy the
schema and code together; if creation fails, keyed buys fail rather than falling
back to an unprotected insert. Existing positions and operation logs are not
rewritten, and this does not automatically remove historical duplicates.

For a confirmed duplicate, the existing per-lot `delete_exact` action removes
one selected lot and a matching web add log without creating a sale. Confirm the
actual quantity, cost and date before removing a lot.

Verification on 2026-11-05:

- Nine isolated tests pass against the extracted actual helpers: replay,
  lost commit reply, rollback after log failure, changed payload, and user/key
  isolation, and four actual buy/sell/unknown-result renderer cases. The dedup table
  definition is also extracted from the real startup schema. SQLite is used;
  PostgreSQL contention is not exercised.
- In the companion UI worktree, the browser success fixture uses the actual result renderer instead of
  inventing a legacy `.msg` success. The browser harness asserts that response
  is accepted. Chromium and WebKit each passed 145 isolated renderer checks,
  including this lightweight success contract. These are not production account writes.
- Python compilation and diff whitespace check pass.
- Production verification and deployment have not been performed. Browser
  inventory still reports Local browser use disabled in Work settings.

Rollback: revert application changes only after checking outstanding submissions.
Keep the additive dedup table and its keys; do not drop it as part of rollback.
Reverting the submission guard also restores the old duplicate risk. Existing
buy/sell cost and profit calculations are unchanged by this patch.
