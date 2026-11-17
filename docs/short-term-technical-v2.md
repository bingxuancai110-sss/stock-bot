# Short-term technical-first v2

This supersedes the original institution-first universe and liquidity gates.
Version `short-tech-v2` stores market snapshots at `short_term:short-tech-v2`;
the old v1 snapshot remains untouched. Price rules are versioned because removing
institution vetoes changes historical signals. The UI labels the method 技術 v2.

## Universe and bounded processing

Read all ordinary four-digit non-ETF symbols in `stock_info`, including listed and
OTC, electronic and non-electronic industries. No institution/net-buy condition,
share price, minimum lot count or turnover threshold chooses this universe.
Industry/market metadata reports coverage; it is not a fundamental score.

Stream at most 64 symbols at a time through the existing Yahoo Spark adapter with
three months of closes/timestamps, two HTTP workers and no shared Spark-cache
reads/writes. Discard those price arrays after calculating normalized technical
conditions. This protects Radar and other pages' cache contents and memory use.
All catalogue symbols are attempted; missing quotes are counted explicitly.

Use trend, proximity to a preceding closing high or MA20, and price extension
to create breakout/pullback/recovery buckets. Interleave buckets and retain prior
active modeled positions, then fetch full six-month OHLCV for at most 80 candidates,
one at a time. Display at most six. The UI distinguishes whole-market preliminary
coverage from full daily-K detail coverage, and reports listed/OTC/industry counts.
This is not a claim of complete OHLCV backtesting of every market symbol.

Task-owned DB connections are closed before HTTP calls. Auxiliary institution
history is fetched only after technical candidate selection; its failure leaves
chip evidence unknown and does not abort technical research. Per-chunk/per-symbol
memory checks retain 96 MiB; the shared background queue remains single-worker.
A 120-second deadline between requests aborts without publishing incomplete
rankings. Quote requests in flight retain their existing bounded HTTP timeouts.

## Entry and risk rules

Confirmed entry requires price > MA20 > MA60 with rising MA20, a volume-confirmed
breakout or MA20 pullback/recovery, and bounded initial downside/reward space.
Volume confirmation uses relative volume (breakout >=1.25x; pullback 0.8–2x),
never an absolute share/lot or turnover minimum. A bar needs genuine positive
traded volume; zero/missing trading data is not invented.

There is no mandatory foreign/trust net buy, streak, buy-ratio or complete-chip
requirement. Chip data is displayed as supportive, divergent or unknown, strictly
dated before the evaluated bar. Missing chip values remain unknown, not zero.

Initial stop is five-bar low minus 0.25 ATR14, downside <=6%, target distance
>=1.5R, with prior confirmed overhead pressure or explicitly labeled 2R reference.
Existing anti-chase guards remain: abs daily change <=5%, five-day gain <=15%,
20-day gain <=30%, MA20 premium <=8%, ATR14/close <=4.5%, relative volume <=3x.
These constraints lower exposure to overheated setups but cannot certify that
a security is free of speculation. Official attention/disposition exclusions
are not integrated; no false certification is shown.

One add of half initial model units is allowed after 1R, a fresh ten-bar high,
relative volume confirmation and original combined-risk limit; no chip veto or
averaging down. One reduction of half remaining model units occurs on target
rejection, two successive closes below MA10, or MA10 loss with auxiliary selling.
Stop touches take precedence over ambiguous same-bar targets, gap references use
the lower open, no same-bar re-entry, and trailing stop only ratchets upward for
the next session. Trigger/reference prices never guarantee an executable fill.

No absolute volume/turnover rule blocks entry. Average volume and traded value
are shown as reference facts only. An internal low-turnover advisory flag does
not enter the selection/entry predicates or final ranking.

## Current signals and price differences

Retain the latest-session banner, true new entry/exit/add/reduce dates, current
action, explicit old-entry/closed-position state, default-hidden historical
arrows, exchange-calendar validity, and red gains / green losses. The versioned
history is rule replay, not an investment-performance claim or personal account.
Per-share difference is final exit minus initial entry, ignoring quantities,
add/reduce average cost and fees/taxes, with open differences labeled unrealized.

No new framework, paid API, database schema, real orders, user holdings changes
or LINE communication. All research APIs retain existing authentication. Old
v1 and new v2 snapshots/event IDs are not mixed. Rollback is a code revert and
Render auto-deploy; the old version reads its old snapshot again.

## Validation

Cover high-price tiny-volume entry, selling/missing/null institution evidence,
same-day chip lookahead exclusion, both markets and electronic coverage, entire
catalogue enumeration without institutional prefilter, <=64-symbol streaming,
80-symbol detailed cap, Spark-cache isolation, auxiliary DB failure, risk and
stop/trailing behavior, current versus historical notices, per-share arithmetic,
and mobile/dark/light/reduced-motion UI. Preserve Radar/snapshot and all existing
UI regression checks. After deploy verify real scope counts, electronic names,
current signal dates, price-difference colors and background completion logs.
