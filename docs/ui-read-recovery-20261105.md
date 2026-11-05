# UI read recovery and production audit — 2026-11-05

The production walkthrough found that a transient HTTP 503 on the rotation
snapshot endpoint was rendered as a genuinely empty database. The page now
performs at most three GET attempts, with bounded waits and per-request timeout.
Authentication and other non-transient failures do not retry automatically.
Terminal failure offers a manual read retry and explicitly distinguishes missing
data from unavailable data. Failed refresh retains the previously displayed
snapshot; navigating away aborts the read. No polling or new data download is
introduced by the retry controls.

The workbench detail header now has a light background and readable dark name,
price and metadata. Selecting an unloaded source clears the previous source's
count and explanation while reading its saved snapshot. Financial colors remain
unchanged. The stylesheet cache version is 20261105-8.

## Validation

- Chromium and WebKit: 204 isolated interaction checks each, no page errors.
  Added cases cover transient 503 recovery, exhausted retries, manual recovery,
  retained data after refresh failure, and detail-header contrast.
- Background admission: 9 tests, including full-symbol batch coverage and no
  partial result on memory-pressure interruption.
- These tests use the actual renderers with isolated data and intercepted writes;
  they are not evidence of successful real account transactions.

## Production stability finding

Revisiting the real workbench review page and then opening 2308 exposed continued
container memory growth after the previous admission-only protection. Metrics
rose from 256 MB to 520 MB between 13:10 and 13:14 UTC; the real chart navigation
timed out while leaving the prior page available. The precise allocation source
has not been established; this observation does not prove review is the sole cause.

The guard now reads the container cgroup usage in addition to worker RSS, since
Render enforces a container limit. Optional review downloads recheck memory
between batches of four with one request worker. Low headroom raises before
publishing results, preserving the last completed statistics instead of creating
partial statistics. All symbols and the existing return formula are retained
when the operation completes. No instance size, billing, database schema,
holdings math, or transaction mechanism changes.

Production memory and the timed-out chart path must be retested after deployment;
the bounded-work tests alone do not establish that the memory incident is solved.
