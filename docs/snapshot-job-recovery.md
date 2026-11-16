# Snapshot reliability repair (2026-11-16)

## Production evidence

Radar reached 60/240 candidates on a web instance that was replaced during deployments. Deep history batches also repeated a full MIS recovery tree every four symbols, making a scan vulnerable to interruption. Preserve the 240-candidate coverage, four-history batch size, two history workers and memory admission checks.

The daily portfolio/leaderboard recovery waited for same-day final TAIEX, then spent 620.7 seconds obtaining missing security quotes before completing on 2026-11-16 at 14:37 Taiwan time. Production logs also contain SSL EOF/bad-record-MAC failures on pooled database sockets. A read-only request to TWSE MI_INDEX confirmed that today's official closes were already available.

## Changes

Daily snapshots prefer same-day official TWSE/TPEx closing reports first. Only missing symbols use the bounded MIS quick path. Dates, positive prices, official provenance and close_is_final remain mandatory; old quotes never become today's closes. Save valid users with the existing UPSERT and retain previous saved rows if another user lacks quotes, instead of preemptively deleting every user's current-day row. Incomplete portfolios still block fresh ranking publication.

Radar prefetches official live quotes once for the candidate pool with bounded quick requests, then passes each four-symbol subset into the existing historical calculation. Missing official quotes retain their real Yahoo provenance; scoring weights and thresholds are unchanged. No partial ranking replaces the last complete snapshot.

Pooled database connections idle for 30 seconds receive SELECT 1 and rollback before checkout. Failed idle SSL sockets are discarded and checkout retries remain bounded. Hot connections do not pay an extra query round trip. Track idle time only for returned connections.

## Validation and operational limits

Tests exercise idle/dead/hot sockets, official-first snapshots, missing/stale quote rejection, preservation of saved data, ranking publication, bounded MIS fallback and full 240-candidate radar coverage. Market-source tests remain isolated and use the real function bodies without app startup. Function extraction avoids repeatedly parsing unrelated large UI templates.

No schema or user-setting migration is needed. No paid service or API is added. Deploy the merged commit on the existing Render service. Verify same-day leaderboard data and fresh runtime logs. A terminated intraday scan cannot recreate the past market moment; retain the last genuine snapshot and validate the faster path on the next real intraday run. Revert the commit to restore prior behavior; preserved snapshot data requires no rollback.
