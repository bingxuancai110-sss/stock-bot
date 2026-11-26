# V3 book execution and reconciliation (test branch only)

Official contracts:
- https://developer.fugle.tw/docs/data/http-api/intraday/quote/
- https://developer.fugle.tw/docs/data/http-api/intraday/ticker/
- https://developer.fugle.tw/docs/data/websocket-api/getting-started/
- https://www.twse.com.tw/zh/products/system/trading.html

fetch_book uses the existing shared Engine session, credential, namespace and
rolling quota. It reserves one V3 request for each ticker and quote request;
both attempts count against the same quota as regular market quotes. Oddlot
quotes use type=oddlot, not a regular quote relabeled as shares.
No new HTTP route, scheduler, key, environment, database migration or deployment.

For the first prototype only normal TWD common shares with verified 1000-share
board lot and active market status are allowed. Five-level prices and sizes
are validated against ticker limits, identity, dates, trading phase and
five-second freshness. Regular execution is only allowed before 13:25 and
requires continuous trading. Oddlot estimates begin at 09:10 and also stop
at 13:25 as a conservative local restriction, not an exchange closing rule.
No trial, halt, crossed book, unpriced market order or limit-price level.
Estimate consumes at most 10% of displayed depth per level, requires the
entire order within its explicit price limit, calculates VWAP, and is a
hypothetical estimate, not exchange matching or assured executable quantity.
No partial fills. Oddlot auction estimates are indicative only and cannot
be booked by Ledger.fill. Existing oddlot holdings are preserved.

Ledger.fill optionally accepts raw quote, ticker and observation time and
revalidates them in the transaction. It consumes a book snapshot ID once;
duplicate event IDs remain idempotent. Existing synthetic quote path remains
for backward regression compatibility and must not be enabled in a real
host. Host must authenticate data origin and use the verified book path;
freshness and provider fields alone are not source authentication. Depth
replenishment, queue priority, slippage beyond the depth estimate, execution
delay, tick validation, oddlot auction matching, settlement, corporate
actions and actual fees remain release gates.

reconcile_preview replays post-checkpoint documented simulated trades against
reported 11/13 cash and holdings and checks per-event cash, duplicate IDs,
chronology and terminal balances. It never writes or unlocks the ledger.
No subsequent full verified history has been located; a zero-event arithmetic
match is not proof of no intervening trades. Original checkpoint remains
ready=False. Do not merge personal holdings or old NT$1m version.

Local tests: 16 new cases; combined 104 cases, 89 passed and 15 Flask skipped.
Three additional existing CI-only provider/PostgreSQL cases are not included
in that local run. Full-chain fixture uses synthetic ready state and mocked
provider responses, not real capital or a reconciled history. Host activation,
actual provider permissions/terms (including data transfer), authenticated
schedule, actual snapshots and full CI results are still unverified.

Main comparison at development time: one new commit 6a27bde222c3e59c0411d0baae62616e9ad71fea
changed main.py, short_term.py, static/short-term.css and static/short-term.js.
No overlapping changed paths with this test branch; short_term SELECTION_VERSION
still 15. No full main.py import or main merge was attempted; no claim of
complete compatibility. Recheck current main and deployment configuration before release.
