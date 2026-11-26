# V3 management extension (isolated branch, not activated)

- The oddlot path records a paper order before a later matching market print.
  It requires normal security and independent oddlot book evidence at submission,
  strict price improvement, a fresh actual market print, and at most 10% of
  that print's share quantity. It consumes each print ID only once in an atomic
  ledger transaction. This is a hypothetical fill, never confirmation of the
  fund's queue position or actual execution. No provider call or broker order is
  made by this module. Host must authenticate provider origin, implement
  /intraday/trades?type=oddlot through shared quota, confirm trading halt state at
  fill, use synchronized timestamps, and prohibit all same-day oddlot round trips.
  The existing check blocks oddlot sells following same-day buys; other mixed
  session order sequences and settlement still need review.
- Fees: configurable discount and minimum fee, round to whole NTD for this
  optional model. Default legacy regular-lot path unchanged. Common share sell
  tax 0.3%; equity ETF 0.1% supported by calculator only. No special exemptions,
  day-trade tax, securities lending, income tax, brokerage-specific rounding,
  T+2 payable/receivable settlement, or other instrument ledger support yet.
- Cash dividend: recognize a receivable on effective date using verified
  entitlement, include it in NAV, release to cash on/after pay date. Exact
  share changes preserve aggregate basis. Fractional entitlement, multiple
  corporate events on the same effective date, deferred stock dividend delivery,
  capital reduction/refund, rights issues, reversals, and official event feed
  remain unsupported. Current action path requires pre-event reconciled holdings
  and explicit event/evidence; no inference from adjusted prices.
- register_host(app, market=..., ledger=..., token=...) explicitly registers the
  market and fund read-only endpoints; /v3/fund is a static shell without data,
  /api/v3/fund/performance requires bearer auth and no-store. The shell does
  not persist the token or render untrusted HTML. This is not yet wired into main.
- research_tick is a proposal-only callable for 09:15, 10:15, 11:15, 12:15,
  13:15 on a verified trading day, with injected research and quote readers.
  No live scheduling, auto research, automatic orders or notifications. The
  08:15 and 17:00 workflow, secure scheduler credential, hosted storage and
  deployment remain separate release gates.
- The original 10m historical checkpoint remains ready=False. No production
  database or ledger is initialized or changed.
- Local regression: 123 discovered, 107 pass, 16 skipped (Flask missing).
  Three additional prior CI-only PostgreSQL/parser tests remain unrun locally.
  Dashboard inline JS passed node --check; no actual browser/HTTP test.
  Test fixtures use synthetic ledger and mocked provider data.
- TWSE announced 2026-12-07 session expansion, 2027-07-05 one-second matching:
  https://twse-regulation.twse.com.tw/TW/int/DAT01.aspx?FLCODE=FE401877
  Conservative prototype paper time window remains unchanged. Verify official
  trading rules and Fugle actual responses before any activation.
