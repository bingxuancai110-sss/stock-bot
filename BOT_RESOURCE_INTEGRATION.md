# BOT resource integration — test branch, default off

## Current source and integration
Retrieved complete main.py via Git blob (test branch
96b57e5005a665f3e01303f5e002dbe91257df08; production
3d8aade99d6c0d9eb9665846839f28ff6f05039c at main
a298f201137c0a5189a41bcf4a9bf141470e60e1). All six exact patch anchors
matched the test-branch blob. Large direct replacement failed in connector
transport; refetch confirmed original main.py blob unchanged. Instead
prepare_bot_resource_main.py generates a separate, syntax-checked copy.
CI reads the actual checkout's main.py, applies the patch in memory,
compiles it, and extracts only selected function ASTs for mocked execution.
It does not import main, start Flask, connect production DB, or deploy.
No generated main committed or production ref changed.

The generated copy defaults off. BOT_RESOURCE_TEST_MODE=1 enables public
snapshot save/load and eligible all-public bulk reads, scoped requests
module metering, and memory admission for two existing optional background
jobs. BOT_RESOURCE_HISTORY_TEST=1 additionally enables completed-day review
history only after close on a verified trading day, with known exchange
suffix and confirmed final-close series/coverage. Some existing Yahoo
skip_mis rows lack final-close flag: the new path must defer rather than
invent it. No automatic use in intraday or on holiday. The old consumer
return formulas and private snapshot path remain unchanged when disabled.
The existing serialized executor retains job lifecycle/order; only warmup
and short-term check are gated in the copy, not every direct executor,
timer, or foreground task. Do not run the generated app against production
credentials. Actual startup and all consumers still need isolated validation.

## Writer
Public keys only; canonical serialized content + date + source meta digest
and committed row version remembered per process. SELECT FOR UPDATE and
version check before optimization. Identical confirmed refresh updates
computed_at only, retaining legacy freshness semantics; explicit known
replay skips write; unknown replay rejects. No DB schema changes. Does
not optimize first write after restart or unrelated/private/short-term
compressed writer. Existing writers may change row version; then a normal
fresh write uses full original upsert. New read path checks version and
age each time and may issue more metadata queries than a cold bulk load.
Source shape validation is limited; actual source contracts need review.
Measure decoded RAM, latency, row locking, TOAST/WAL, and real billed bytes.

## Meter and history
Main module requests.get/post/request logical calls only; preserves original
response, exception and call args; counts already-buffered response and
known request body bytes by fixed provider categories. Does not capture
sessions, urllib, internal redirects/retries or actual TLS/wire bytes;
streamed bodies remain unmeasured. Supabase versioned reader/writer counts
application bytes only. No URL, key, user ID or payload log. No provider
requests were made in this audit. Additional provider-specific callers
need explicit instrumentation, and aggregate metrics are process-local.
History cache is bounded and opt-in; rejects unknown market/coverage/final
close. Existing research may defer more often; never turn incomplete
history into completed statistics.

## Validation
New suite: 20 tests, 19 passed locally, 1 PostgreSQL skipped. Main source
for local compile and AST parity was uploaded 11/21 main (3)(1).py, not
the exact current GitHub checkout. Actual current blob anchors verified
by connector. Local combined resource regressions: 57 run, 55 pass,
2 PostgreSQL skipped, no failures. Mock 100 identical confirmed writes:
one full JSON body, 99 metadata-only updates. This measures logical body
transmissions, not live traffic or DB write elimination. Strict CI raised
to >=207, no skips/errors/failures. New CI status unconfirmed. Need actual
latest checkout results, real PostgreSQL tests, independent staging app
startup and matched workload measurement before enabling anything.
No new service, production DB operations, live data calls or main change.
