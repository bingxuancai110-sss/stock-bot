# Pattern-primary short-term v3

User intent: high and low nominal prices are eligible; patterns and price/volume technicals drive opportunity discovery. Institution data is an optional supportive annotation, never a veto or a reason to predict a stock cannot rise.

Version: `short-pattern-v3`, with separate close and `:intraday` snapshot keys. The previous `short-tech-v2` snapshots are retained. This is a material rule revision, so v2/v3 replay events and prices are not mixed. No SQL migration, account/portfolio mutation, LINE send, paid resource, or execution order is added.

Four independent families run on every detailed OHLCV prefix; their union forms the observation pool:
- Reversal: break a descending line through prior confirmed lower swing highs while not making a new ten-bar low, or a rising-bottom neckline break. Pivots require two following completed prior bars; no future pivot is used. Relative volume >=1.05 and a close in the upper 40% confirm the trigger. Does not require MA20 > MA60. Nearby lines/necklines are observation candidates.
- Breakout: cross the preceding 20-bar high after a bounded consolidation, with recovery above MA20, relative volume >=1.2 and a strong close. MA20 > MA60 is not a universal prerequisite.
- Pullback: retain the existing rising-MA20/MA60 recovery after touching MA20, reclaiming yesterday's high, relative volume 0.8–2.5.
- Continuation: a preceding rise >=8%, followed by five-bar consolidation <=8% above MA20 support, then a fresh five-bar high with relative volume >=1.1 and a strong close.

A family trigger is not automatically an entry: require genuine positive traded volume, initial downside <=6%, reward/risk >=1.5, and reject abs daily change >9.5% or ATR14/price >6%. Remove the old common 5%-daily / 15%-five-day / 30%-twenty-day / 8%-MA20-extension / 3x-volume vetoes which indiscriminately removed legitimate families. Historical pressure and support still constrain the risk plan. There is no price cap, absolute lot/turnover gate or fundamental score. Lightweight close-only preselection locates potential shapes; full OHLCV/volume/risk is required for a real research trigger. Coverage remains bounded to 80 details and six displayed per view; setup, sector and nominal-price bands interleave research coverage without forcing entry.

Add only after >=1R profit, new ten-bar high, volume confirmation and bounded combined risk. Reduce only on target rejection or two closes below MA10; remove the old institution-selling acceleration. Exit on the previously known trailing stop; gap/stop precedence and next-session trailing rules remain. Missing/selling/buying institution histories produce identical full event and trailing paths for identical OHLCV.

`institutional_buying` is true only when the five immediately preceding completed price sessions contain complete external/trust evidence and their combined net buying is positive. Show `法人同買` only then; no negative tag/veto when false/missing. The UI explains the five-day external+trust basis. It is not a claim of current-session three-institution synchronized buying.

Cards retain concise state/key levels, adding a short family label and optional positive institution tag. Optional `shape=reversal|breakout|pullback|continuation` filters the stored qualified union without rescanning. Active positions retain their entry-family membership. Event records preserve their original family and contemporaneous descending-line endpoints. K-lines draw the line when endpoints are available in the displayed history; historical events never borrow today's line. Dates, reasons and current/provisional distinctions remain.

Validation covers all four family triggers, reversal without bullish MA alignment, strong continuation formerly rejected for extension, price scaling up to >NT$10,000, complete institution invariance, pivot causality/prefix invariance, risk/stop sequencing, intraday retraction/freshness, bounded whole-market coverage, family selector and full Chromium/WebKit regressions. Synthetic scenarios are isolated from production. No claim of a profitable backtest or guaranteed fills is made.

Deploy through existing main auto-deploy. Verify live method, research completion, available pattern counts and optional annotation. Rollback by reverting the PR; old v2 storage remains available and no destructive migration is needed.
