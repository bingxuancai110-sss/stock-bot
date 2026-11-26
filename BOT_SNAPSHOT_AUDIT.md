# Snapshot traffic and cooperative resource audit — test branch only

## Evidence and scope
Current main a298f201137c0a5189a41bcf4a9bf141470e60e1. Current
taiex_job_store.py blob 42ada92032888957df399a4267f189b98b030fce
matches the test branch. SnapshotStore.load selects the complete JSONB
payload. ShortTermSnapshotStore already packs compressed checkpoints,
stores cycle/queue/stocks separately, and reads the short cycle with the
top-level selection_version filter. Do not replace these protections.
Current short_term.py VERSION short-wave-v10 uses startup grace, an
injected background slot and close-batch lease; the first full payload
load can restore and compact all rows. Current main.py cannot be retrieved
in full through connector. The 11/21 uploaded source shows a 300s default
shared snapshot TTL, count-based cache eviction, bulk SELECT payload,
and potential additional warmup writes of externally sourced snapshots.
That is older code evidence, not proof of current duplicate writes.

Prior Render evidence: 11/26 23:00 Taiwan hourly bandwidth bucket 999.17 MB
overlaps rebuild, Gunicorn restart, 772-symbol short-term snapshot loads,
and repeated JSONB reads/writes. Not causal attribution. No per-provider
bytes or Supabase query statistics available; no direct production DB
connection or new provider requests performed in this stage.

## New optional module bot_snapshot_budget.py
- SQLReader uses explicit read-only transaction, 5s statement timeout,
  parameterized fixed SQL projections, and row-version condition
  (data_date, computed_at, xmin::text). Metadata requests avoid payload.
  Full/cycle body fetched only for the observed version, with server-side
  UTF-8 text size cap (assumes UTF-8 database encoding); server may still
  detoast/serialize JSONB. Version is not a permanent content identity.
- VersionedSnapshots allowlists four public snapshot names and configured
  short-term key variants. No account or holdings keys, no live quotes.
  Each load rechecks metadata; unchanged version reuses serialized body.
  Freshness and a host-supplied schema/session/source validator required.
  No stale fallback on version race. Bounded serialized cache (2 MiB,
  8 entries), bounded decoded zlib bytes (8 MiB), nonblocking single-reader
  guard, existing 160 MiB admission checks. These do not bound total RSS,
  decoded object overhead or all caller-owned results. Compressed data is
  decoded again on cache hit; measure CPU/latency and memory before rollout.
- Meter uses fixed provider and operation categories, aggregate calls,
  errors and payload bytes, no URLs/credentials/account identifiers.
  SQLReader automatically measures returned field representation sizes;
  not protocol/billed traffic. External provider adapters must explicitly
  record known byte sizes and failed attempts, including connection failures;
  SQLReader connection acquisition failure is not currently counted.
- BackgroundGate: process-local cooperative queue, fixed callbacks, at most
  16 named jobs, no duplicate queued/active job, one active job, priority
  aging, reserve check, explicit tick; requeue on memory/Deferred.
  Existing main jobs do not yet use it. No cross-process lock, persistent
  queue, automatic scheduling, hard timeout or preemption. Handlers must
  checkpoint, recheck memory at bounded stages, and be idempotent.
  A deferred highest-priority job can block other pending jobs on that tick.
- No changes to actual snapshot writers: do not skip same-value confirmed
  refreshes without defining freshness semantics; SQL ON CONFLICT WHERE
  alone would still send full payload. First measure write attempts,
  canonical payload size, source date and producer revision; only then
  consider digest/metadata guarded updates with concurrency and parity tests.

## Release gates
Feature off by default; no main/host wiring yet. Validate exact public schema,
including source_meta, real snapshot size, close-session policy, permissions,
and source-data date. Wire only selected research readers, compare output
against existing full payload, and preserve fallback only when feature off.
Never use this cache as real-time or execution data. Integrate a single
BackgroundGate with the existing executor/slots without nested lock deadlock;
map essential foreground/alerts to separate capacity and retain existing
cross-process leases. Observe cold startup with metadata-only/lazy loads,
then compare actual RSS, latency, errors, completed work and billed bandwidth
against matching production load. Actual DB write dedup and provider-wide
instrumentation are pending. Do not deploy or buy resources.

## Validation
17 new local unit cases passed; previous 19 local resource cases rerun
successfully, total 36 passed. Additional PostgreSQL test uses a temp table
on the CI loopback v3test database only. Local environment lacks PostgreSQL,
so one skipped; CI strict gate >=187, zero skips/errors/failures, not yet
verified. New source/test blob hashes checked against local files.
No production changes, provider calls, new service or paid resources.
