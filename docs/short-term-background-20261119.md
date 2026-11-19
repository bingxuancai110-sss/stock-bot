# Short-term producer independent of viewers

Opening, polling, filtering or returning to the short-term page reads saved results without starting a market pass. The explicit Update action remains available. All users share one producer and persisted cursor; a process restart resumes the saved current-session cycle.

The existing scoped-OIDC GitHub workflow checks every ten minutes during Taiwan trading hours (09:05–13:25) and after close (14:05–20:55), weekdays. Calendar/session admission remains server-side. GitHub schedules are best effort, not exact timing guarantees. No new Render paid service or secret is required. A completed current closing scan is reused by later schedule checks, while stale intraday snapshots start a new cycle. Initial scans or new sessions may still take minutes; this change does not claim incremental per-bar strategy calculation.

Each batch saves its checkpoint then queues the next batch. A time-budget yield continues without needing a page request. Memory admission still stops work safely; a later scheduled tick resumes from its cursor. Closing batches do not repeatedly revisit unchanged held-model bars. Intraday position priority remains.

The authenticated GET cron endpoint is status-only; POST checks whether work is due. Status reports saved date/time, cursor, total, confirmed/pending candidate counts and source failures. Enqueue is distinct from complete. Manual workflow runs verify a saved complete result for up to thirty minutes, with status polling and safe deferred-start retries. Scheduled runs only trigger/check and exit.

Verification covers read-only stale page revisits, background triggering without a viewer, checkpoint resume across restart, automatic continuation after time yield, reuse of completed closing scans, authenticated post-close/calendar admission and status-only GET, plus existing strategy/source-date/memory regressions. No database schema or user transaction changes. Roll back by reverting the release together with its workflow/script; restoring the earlier endpoint alone is incompatible with the new runner status contract.
