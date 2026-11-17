# Short-term workbench — daily-bar trial v1

Entry: 選股 → 短線, or `/web/workbench?tab=短線` after authentication.
This independent source does not use the five-factor score, fundamentals,
portfolio holdings, order placement, or LINE. Existing sources remain intact.

## Data and selection

- Read the latest five institutional sessions from existing `inst_history`.
  Consider the top 80 non-ETF four-digit equity codes with foreign or investment
  trust net buying and at least three buying sessions. This is a bounded research
  universe, not an exhaustive market ranking. Retain prior displayed active models
  in the next candidate pool. At most six rows are displayed.
- Use the existing Yahoo daily OHLCV adapter, six-month range, one symbol at a
  time in the existing single-worker background queue. Do not call MIS for this
  close-based strategy. Reuse existing quote caches; no paid APIs or framework.
- Require 65 valid completed bars. Before 14:00 Taiwan time, exclude today's bar.
  Reject malformed OHLC, duplicate dates and large price-scale discontinuities;
  detected large adjustment-factor changes require a new warm-up segment.
- Entry filter: 20-day mean turnover >= NT$100m, mean volume >= 1m shares;
  absolute daily move <=5%, five-day gain <=15%, 20-day gain <=30%, premium to
  MA20 <=8%, ATR14/close <=4.5%, volume ratio <=3. Existing modeled positions can
  remain for risk/exit tracking even if they fail new-entry filters.
- No official attention/disposition list is integrated in v1. These filters cannot
  certify that a stock is free of speculation; the UI states this limitation.

## Rules (not validated profitability claims)

- Trend: close > MA20 > MA60, MA20 above its value five sessions earlier.
- Chips: exactly five preceding price-session reports, excluding signal-day
  reports. Foreign or trust five-day sum positive with at least three positive
  days and nonnegative last day; combined foreign/trust net >=1% of aligned
  five-day share volume. Missing/null data is not zero or neutral evidence.
- Entry: close above preceding 20-bar high, volume >=1.25x preceding 20-day mean,
  close in top 40% of candle; alternatively a MA20 test followed by close above
  previous high and volume 0.8–2x. Both require trend, chips and risk filters.
- Initial stop: five-bar low minus 0.25 ATR14. Risk <=6% of entry. First target
  is a prior confirmed swing-high pressure level (known two sessions later),
  otherwise prior 60-bar high above close; without overhead resistance label a
  2R reference explicitly. Require target distance >=1.5R.
- Replay position starts at one model unit, not a share order. At most one add
  of 0.5 original units after >=1R gain, a new ten-bar high, 1.25x volume and
  supportive chips, only when combined loss at the existing stop does not exceed
  original risk. No averaging down. At most one reduction of half remaining units
  for target rejection or close below MA10 with two-day foreign/trust net selling.
- Stop touches take precedence over target touches on the same daily bar; gaps
  use the lower open as trigger reference. Neither reference is a guaranteed fill.
  No same-bar re-entry. Structural trailing stop ratchets upward after close and
  is effective next session. A confirmed close above target advances the target
  to next pressure or a new 2R reference. Old signal reasons/stops stay visible.
- History arrows are explicitly labeled rule replay, not actual transactions,
  forward-tested outcomes or measured strategy returns. No win rate is claimed.
  Window-based replay is not a permanent portfolio ledger; execution timing,
  costs, slippage and locked-limit fills require a separate backtest framework.

## Storage, API, performance

No schema migration. Reuse `shared_data_snapshots` key `short_term:short-daily-v1`
with versioned public-market payload. No user portfolio records are read/written.
SnapshotStore uses task-owned connections and transactional save; failed writes
retain previous results. Previously stored event IDs retain their recorded prices
and reasons. Exposed API GET `/web/api/workbench/short-term` is login-protected,
no-store; POST is unsupported. `refresh=1` requests a coalesced background build
subject to 60-second admission cooldown. Stale age is 30 minutes or a new day;
opening other workbench tabs never starts short-term work. Poll every five seconds
while visible/building, bounded to 90 attempts. Server per-symbol memory checks
retain a 96 MiB reserve and abort without publishing partial results.

Empty scans retain the newest actual completed-bar date from checked quotes and
expose counts by each rejected stock's first exclusion reason. Missing quotes are
counted separately; a completely missing quote batch reports source failure and
cannot publish a misleading empty success. Follow-up validation adds two backend
tests and one browser check per engine for this case.

Frontend renders only an expanded stock's SVG, 30/60/120 sessions with horizontal
pointer inspection, earlier/later navigation, clickable/keyboard-accessible arrows,
original-event inspector, structural stop steps and dated institutional evidence.
One chart expands at once. Errors preserve prior rows; light/dark preferences and
reduced motion are supported. No full page reload on card interactions.

## Validation and deployment

17 engine/service/API tests cover aligned chip dates, missing data, filtering,
prefix invariance, stop precedence/gaps, add risk/limit, reduction, incomplete bars,
scale changes, job coalescing, snapshot retention and authentication/read-only API.
Chromium/WebKit each: 24 short-term UI checks and 214 existing UI regression checks.
Fixtures use explicitly synthetic isolated data, never production recommendations.
Run `python -m pytest tests/test_short_term.py -q`; browser runners require the
actual-renderer artifacts produced by `tests/browser/fixtures.py`.

Merge to main for the existing Render auto-deploy. Verify live deployment SHA,
open the authenticated short-term tab and wait for the first real scan, then check
actual data dates, candidates, chart and logs. No new env vars, paid resources or
database migration. Roll back by reverting this commit and redeploying main;
the versioned market snapshot may remain unused. Never delete portfolio data.

Next validation before treating as an actionable strategy: walk-forward/out-of-
sample backtests with fees, taxes, next-session execution, corporate actions,
survivorship-safe selection and unfillable limit sessions. Official attention/
disposition exclusions and permanent forward signal storage are later work.

## Selection policy 2 — 2026-11-17

The initial live scan had no candidates: 12 failed full trend confirmation, 10
were overheated/volatile, one failed chip support, one failed risk geometry.
The bounded pool expands from 24 to 80; the display remains at six. Genuine
entry/add/reduce/exit rules are unchanged. A flat stock may now appear as an
observation only if liquid, not overheated, with complete chip history and a
price above MA20 or MA60, near MA20 (within 6%) or preceding resistance (5%).
Every missing confirmation is exposed; no observation receives an invented buy
arrow. Candidate planning risk uses its observation entry price, not today's
lower close. Today's entry count is distinct from older replay signals, which
carry their original dates. Old policy snapshots trigger a coalesced rebuild.
Missing institution values render as unknown rather than NaN. Desktop workbench
status follows the independent panel instead of waiting for an unused source.

Validation adds watch-before-breakout, recovery without trend confirmation,
policy cache invalidation and an observation UI case. 23 backend strategy tests
and 26 short-term UI checks on each browser engine.

Each build checks a 120-second execution deadline between symbols, preserving the previous snapshot on timeout so a slow upstream cannot monopolize the shared job queue.

## Current-session notices — selection policy 3

The panel now leads with the latest completed-session entry notices, or an explicit
dated statement that no new entry was confirmed. New exit/reduce/add notices appear
there as well, across all checked candidates before the six-card display limit.
Older entries cannot become new notices simply because the snapshot was refreshed.
Session validity uses the existing TWSE trading calendar and the 14:00 completed-
bar cutoff; stale or failed data is unconfirmed, rather than a no-entry claim.
An individually stale stock cannot keep a new-entry badge. Original event history
is preserved for explicit inspection.

Each card states its current action: new entry, add/reduce/exit this session, an
existing modeled position (not a new entry), already exited and waiting, or no
entry triggered. In the reported example, an October 13 entry and November 6 exit
show `已出場，等待新訊號` on November 17, with neither event in the new-entry banner.
Historical markers and flat-position trailing lines are hidden by default. The
user explicitly switches to historical replay or selects a timeline event to see
them. Returning to current mode clears old markers. Protection levels are labeled
support-stop (`支撐停損`) without equating them to raw structural support.

Current signals are completed-bar observations, not guaranteed fills: the next
session's execution price and downside distance need confirmation. No real order,
personal portfolio, LINE notification or push-notification permission is used.

Validation: 32 strategy/service/API tests, 42 Radar/snapshot regressions; Chromium
and WebKit each pass 31 short-term and 214 existing interaction checks (564 total).
Specific cases cover October entry/November exit, old active holdings, true fresh
entries above the card list, stale source dates, exchange holidays, explicit history
mode and returning from history. Revert this release for rollback; no schema change.

## Simple per-share price spread

At the user's request, show final exit price minus the cycle's initial entry price:
24 to 36 is +12 per share; 100 to 1000 is +900. Gains are red, losses green, zero
neutral. This deliberately ignores quantities, fees/taxes and add/reduce average
cost. It is a signal-price comparison, not realized user-account profit. Multiple
completed cycles of the same stock show a clearly labeled sum of their per-share
differences; incomplete cycles use current close minus entry and are separately
labeled unrealized. Unmatched exits do not invent a cost basis.

Both the card and the selected historical cycle show the dated price pair. A new
exit notice can include that cycle's spread. Calculation decorates existing stored
history at read time, requiring no new market requests, schema or paid resource.
Validation: 38 strategy/service/API tests and 35 short-term browser checks per
engine, including the user's numerical examples, losses, zero, multiple cycles,
unrealized separation and red/green computed styles.
