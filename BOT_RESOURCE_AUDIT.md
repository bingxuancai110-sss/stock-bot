# BOT resource audit — 2026-11-27 (test branch only)

## Observations (read-only Render metrics)
- 512 MiB limit. 11/27 00:00–07:20 Taiwan, five-minute samples peak
  352526340 bytes (~336.2 MiB). App logs during review #8 reported 352 MiB
  RSS and skipped background work; container metric and app RSS are not the
  same measurement. Do not lower the existing 160 MiB review reserve.
- 11/23 08:00–11/27 00:00 Taiwan: 89 hourly bandwidth samples, ~29.45 MB
  mean, max 999.17 MB at 11/26 23:00. Other large buckets: 545.67 MB at
  11/25 22:00, 286.67 MB at 11/25 12:00, 231.00 MB at 11/25 09:00.
  Hourly memory sample max ~504.8 MiB (529293300 bytes), mean ~370.5 MiB.
- The 11/26 23:00 bucket overlaps a build (23:21), gunicorn startup and
  worker exit (23:23), repeated short-term snapshot loads (772 symbols),
  shared JSONB snapshot reads/writes. Correlation, NOT attribution; external
  provider/DB bytes and build-time billing cannot be separated from these
  aggregate metrics. Don't assume all outbound traffic is Fugle or Yahoo.
- Prior user billing screenshot: 4.33/5 GB used; service-initiated 3.97 GB,
  HTTP responses 364 MB; historical snapshot, not current quota.
- Latest main a298f201137c0a5189a41bcf4a9bf141470e60e1. The full main.py
  could not be retrieved via GitHub connector; inspect 11/21 uploaded
  main (3)(1).py and current main regression tests instead. Existing bounded
  realtime LRU, homepage persistent snapshots, 4-symbol review batches,
  shared three-mode review history, memory admission, and skip_mis are
  already present. Don't remove or duplicate these protections.

## Implemented optional component
bot_history_budget.HistoryBudget: one instance per process for public
completed-close research, with injected existing get_realtime_stocks_bulk,
_background_work_memory_safe and trusted history validator. Explicit
completed_day, market suffix and source_revision. Validated close_dates,
closes, adj_closes and final-close flag; date objects restored for legacy
return computations. No live quotes, holdings, credentials, or OHLCV
consumer use. Requires host verification of provider origin, exact range
coverage, adjustment semantics, final-close metadata and eligible session.
No automatic fallback that bypasses the gate.
- Default 2 MiB serialized cache and result ceiling, 200 items, 15-minute
  TTL; limits are prototype parameters, not measured RSS. Decoded rows and
  Python overhead require separate measurement.
- One active history download per instance; other requests defer rather
  than occupy a worker. Admission checked before and after each fetch and
  before returning, four-symbol batches, one worker, skip MIS.
- Exact key per symbol/market/range/confirmed date/source revision, LRU;
  successful cached rows can be reused after an interrupted batch; no
  partial result is returned. 60-second cooldown on failed batch.
- stats exposes local hit/miss/eviction/bulk invocation/requested-symbol
  counters and cache payload bytes. These are not HTTP request counts or
  Render billed bandwidth. clear is nonblocking.
- This component is NOT wired into main or the existing scheduler. It only
  handles closing-price research, not all expensive jobs. Current Yahoo
  output may not carry an explicit final-close flag; must verify actual
  source contract before use. If not confirmed, defer, never invent flag.
  No cross-process coordination, disk cache or persistent resume.

## Integration plan / remaining gates
1. Inspect full current main; enumerate outbound callsites by provider and
   purpose; measure request attempts, response size and elapsed time without
   recording URLs, tokens, user IDs or holdings. Compare with Render billing.
2. Instrument cold startup and each snapshot read/write size; measure
   duplication and cache reload frequency. Use version checks and lazy
   load of large immutable snapshot bodies only after actual consumer
   requirements and timestamp semantics verified. Don't change JSONB schema.
3. Use one HistoryBudget instance for verified completed-close review
   consumers only, after parity tests against full original statistics.
   Keep original fallback path when feature off. Audit cache reclamation,
   total resident set, and concurrent jobs. A global scheduler/resource
   admission policy across warmup/ranking/review/short-term/V3 remains needed.
4. Retain scoped live quote cache, existing provider quota and OIDC controls;
   direct provider calls outside V3 gate still require audit. Never claim
   lower billed traffic until before/after measurement under matching load.
5. Prefer existing CI and no new Render services until quota confirmed.
   Staging synthetic-only and ledger lock remain. No main change, deployment,
   new paid resources, provider calls or automation modifications.

## Tests
11 new unit tests passed locally; reran previous 8 retry/provenance tests
with them: 19 passed, zero failures. Test code and module hashes checked
against GitHub. Overlap fixture: two 8-symbol jobs with four shared names,
four bulk invocations without shared cache vs three with cache; 16 vs 12
symbol requests, 25% reduction in this fixture only. No live HTTP traffic,
production memory or end-to-end BOT test. Strict CI minimum raised to 169;
new CI result unverified. Existing screenshot verified prior 150+ gate only.
