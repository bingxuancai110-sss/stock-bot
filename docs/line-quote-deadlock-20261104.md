# LINE quote/holdings report deadlock

## Incident evidence

On 2026-11-04 at 14:22:59 UTC, a LINE query for 2308 started. The quote stage
completed in 1.3 seconds, then the five-factor stage never completed. A holdings
query at 14:28:28 UTC was rejected because the first report still held the global
heavy-query slot. This was not a six-minute quote download.

The scoring path calls `get_realtime_stocks_bulk`, which supplies an official
quote to `get_realtime_stock`. On a history-cache hit, that function already
holds `_realtime_cache_lock` and calls `_store_realtime_cache`, which acquires
the same lock. The primitive `threading.Lock` waits on itself indefinitely.
Other quote readers and the LINE global admission slot are then blocked too.

## Fix and scope

Use `threading.RLock` for the quote cache's intentionally nested acquisition.
Keep the LRU capacity, quote metadata, history fields, five-factor formulas,
portfolio accounting, LINE Reply-first/Push-fallback behavior and serialized
heavy-query admission unchanged. Do not forcibly unlock a still-running report
or create overlapping replacement workers.

## Validation

`tests/test_realtime_cache_reentrancy.py` extracts the actual production cache
and LINE worker functions without importing application startup. All market
downloads and LINE delivery are stubbed; no real account messages are sent.

- Replacing the fixed lock with the old primitive lock in an isolated child
  reproduces the indefinite wait; the test harness terminates that child.
- The fixed official-price refresh returns and retains source/trading date,
  final-close and limit-price metadata, and technical fields.
- The original cached history is not mutated.
- Concurrent writes respect the existing LRU capacity and release the lock.
- Consecutive quote and holdings worker invocations reply and release both
  the per-user claim and the global slot, without using Push fallback.

Deployment must restart the old process: a thread already stuck in the old
primitive lock cannot recover simply by changing the source on disk. Verify
the deployed commit is live and inspect real subsequent LINE stage/completion
logs. Stubbed tests do not establish an end-to-end LINE delivery time.

Rollback: revert this isolated commit and let the existing main auto-deploy
run. That restores the known deadlock, so prefer a follow-up fix if a separate
issue appears. No database or environment changes are required.
