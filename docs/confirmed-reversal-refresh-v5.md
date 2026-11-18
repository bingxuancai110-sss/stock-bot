# Confirmed reversal and independent intraday scheduling

## Reproduced causes (2026-11-18)

7791 closed at45.85 while the prior-pivot descending line projected to45.78947. The old rule accepted that roughly0.13% single-bar cross despite MA20=47.85250, MA60=52.49583 and a falling MA20. It was an unconfirmed downtrend bounce, not a confirmed reversal. The public six-month Yahoo OHLCV captured for this incident is retained in a dated test fixture; it is never published as fabricated live data.

Production app logs show cron-job.org reached `/cron/radar-live` every10minutes throughout09:00–13:30 Taiwan time. Every response was the87-byte memory-deferral message. Render container memory was approximately432–463MiB, above the416MiB admission threshold for a96MiB reserve. Short-term work depended on successful Radar production, so it also missed those scheduled updates. Two URLs alone do not isolate memory in a single Render worker.

## Changes

- Version `short-wave-v5`, selection10, separate snapshot namespace. Descending-line/bottom reversal must have a recent cross, two consecutive traded closes above the **same** known boundary plus max(0.5%price,0.25ATR), and both closes above their contemporaneous MA20. Intervening confirmed highs cannot invalidate the resistance line. Incomplete day-K stays provisional. Other three pattern families and existing risk limits remain.
- Pending confirmation is explicit in checks/reasons. Show descending boundary value and chart legend. 7791 is an observation, not a new entry.
- Keep the96MiB guard. Before refusing, evict expired/excess replaceable quote, rendered-page and in-memory persisted-snapshot copies under their existing locks, collect garbage, return freed glibc heap pages where supported, and remeasure actual container memory. A60second throttle prevents recycling on every symbol. Sessions, transactions, positions, persisted snapshots and user settings are untouched. Quote LRU default is240 rather than800, with128 surviving under pressure.
- New `/cron/short-term-live` authenticated via the existing scoped GitHub OIDC verifier (only this repository/main/workflow/audience) or existing cron secret. Queued acknowledgment202 is not completion; persistent memory deferral503 is visible as failure. No LINE messages.
- Radar retains its existingcron-job.org schedule at:00/:10/etc. Short-term gets a distinct GitHub Actions schedule at:05/:15/etc,09:05–13:25Taiwan weekdays. Remove Radar-triggered short-term work to prevent coupling/double runs. GitHub scheduling is best effort and may be delayed; execution logs and snapshot timestamps remain authoritative. Existing single-worker FIFO and checkpoints prevent overlap/fan-out; this is staggering, not separate RAM. No new paid service/API.

## Validation and release

Real7791 incident fixture, one-close rejection, two-close positive confirmation, old-break rejection, unreclaimed-MA20 rejection, wave holding, price/institution invariance, multi-batch1979 coverage, paging, independent endpoint auth/session/defer states, memory recovery and failure-closed admission, unchanged account/persisted data. Chromium/WebKit verify real7791 is observation, exact boundary/reason, polling, charts and paging.

Deploy through existing main auto-deploy. No schema migration. Revert this release (including the new workflow) to roll back; v4 snapshots remain. Live intraday freshness after this nighttime release still needs a next-session observation; a scheduled queue acceptance or nighttime skip does not prove a completed intraday scan.
