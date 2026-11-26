# V3 paper fund prototype — isolated test branch

Historical checkpoint: 2026-11-12 capital NT$10,000,000, initial NAV 100.
2026-11-13 reported hypothetical fills gross NT$5,986,021.50, fee NT$8,530,
cash NT$4,005,448.50, NAV 99.9147. These are reported historical figures,
not independently verified fills. All 7 positions are retained as legacy.
Subsequent history is incomplete; ready=False is intentional.
Do not manually flip ready in a real ledger. Only synthetic unit fixtures do so.
Reconciliation, independently verified trade import, fee allocation and
corporate actions must be built and reviewed before enabling continuation.

## Prototype policies (not backtested alpha)
Core target 5–20% based on external conviction; tactical target 2–5%.
Buy threshold 0.65; reduction threshold 0.35; externally supplied tactical stop.
Core 55%, tactical 20%, cash 25% remains a strategic target, not a rebalancer.
New buys enforce purchased-name maximum 20%, tactical aggregate 20%,
tactical transaction 8%, and cash floor 20/35/50% at 0/10/15% drawdown tiers.
The higher cash floors are conservative prototype buy gates, not automatic sales.
Price-driven concentration breaches and existing legacy sleeve allocation
require portfolio review. No leveraged trades or short sales. The short-side
research remains risk evidence. Regular 1000-share lots only; odd-lot
execution, tick sizes, settlement, dividends, splits, trading fees/minimums,
market depth timestamp validation and volume participation need further work.
Fees modeled at 0.1425% and ordinary stock sale tax 0.3% rounded to cents.
No fee discount or day-trade tax assumed. Historical basis excludes the
unallocated original fee; realized history remains incomplete.

## Ledger and execution
SQLite isolated database only. Atomic state/event/mark updates, event ID
idempotency and revision guard. All execution is paper only; no broker API.
Quotes must be supplied by a trusted host adapter; source strings alone
are not authentication. Current V3 quote endpoint does not provide full
required order-book/session fields or symbol in each row, so live integration
must not enable fills yet. Execution requires regular session, valid same-day
quote, verified bid/ask and size, limits, no halt, all holding valuations.
No automatic decision from research source count: external conviction and
stop must be supplied by independent research. Proposal output is not a trade.
No live scheduler, no actual account data, no production state created.

## Performance
NAV = marked equity / original capital * 100. Benchmark excess uses same-date
and nonfuture as-of total-return observations from 0050 and TAIEX; no benchmark
data is fabricated. Observed maximum drawdown is not complete historical MDD
until all historical marks are reconciled. Dividend/settlement accounting
and full return attribution are outstanding.

## Tests
Run python -m unittest -v test_v3_fund (26 cases). Regression tests use mocked
quotes and temporary SQLite databases. CI additionally includes Flask,
PostgreSQL and provider-parser tests; results must be confirmed before release.
No main merge, deployment, key changes or schedule changes authorized here.
