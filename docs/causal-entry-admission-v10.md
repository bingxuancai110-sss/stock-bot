# Causal entry admission v10 — 2026-11-20

## Defects corrected
- Raw low ticks were incorrectly used as short profit checkpoints; use causally confirmed prominent supports, with a prior 60-session low fallback and an explicitly labelled 2R model reference when no structural support exists.
- An ATR above 6% rejected every setup, even with a valid nearby structural stop. Retain the 6–12% structural stop-distance cap and extreme daily-move restriction, while reporting ATR separately.
- Evaluate each triggered pattern independently. A distant reversal stop must not veto an independently valid breakout or retest.
- Add completed-bar decisive breakdown and bearish continuation, plus MA10 failed rebounds. Two-close confirmation remains supported.
- In an established trend, the first structural checkpoint controls reduction, not admission of the entire trend trade. Initial reversals still require 1.5R; report the actual checkpoint R without fabricating extra price space.
- Exclude zero-volume non-trading bars and preserve original-date alignment of adjusted prices.
- Distinguish today's entries from entries/exits over the saved candidates' last 30 trading bars. This count is not whole-market performance.

## Safety and interpretation
Only completed historical bars produce confirmed history events. Intraday indications remain provisional. Existing stop priority, adverse opening gap handling, bounded additions, partial reductions and direction isolation remain in force. No orders are sent. Short borrow eligibility is not verified. Historical replay is a model reference, not an actual fill or profitability guarantee.

## Validation and rollout
159 targeted backend checks; Chromium and WebKit mobile regression suites; public-source daily OHLC prefix replay. Genuine replays include both stopped-out trades and completed trends. v10 uses a separate snapshot namespace and selection version 15; old snapshots must not be advertised as new confirmed entries. Static assets use 20261120-5. Deploy through main, then request the existing authenticated serial rebuild workflow and verify real saved results.

## Rollback
Revert this change and deploy main. Restore the v9 snapshot namespace by reverting VERSION and selection constants together; do not relabel v10 snapshots as v9. No database schema, account, transaction, fee or LINE notification data is changed.
