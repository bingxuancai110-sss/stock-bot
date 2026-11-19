# Selection logic audit, 2026-11-19

This release improves causal decision consistency and research coverage. It does not establish improved investment returns or a win rate. No score fitting, institution buying requirement, nominal price ceiling, actual account change, paid API, or database migration is introduced.

## Findings and corrections

Radar's whole-market quote pass was followed by a limited detailed-analysis pool with three buckets: percentage strength, turnover, and institution net-buy lots. The last bucket still let institution lots determine research priority despite the technical-first requirement. Replace it with deterministic category-balanced price-strength coverage. Institution values remain annotations and cannot change the new pool. The pool keeps its bounded capacity and two-worker/four-symbol memory policy. Negative/flat candidates remain eligible for research; there is no fabricated technical confirmation from this coverage step. The Radar policy moves from 2 to 3 so prior-policy snapshots require refreshing.

Short-term pattern selection previously took the first triggered family. A tentative descending-line reversal could therefore veto an independently confirmed breakout, continuation or pullback. Prefer confirmed triggered families, then retain a tentative reversal as observation if no other family qualifies. The two-close/MA20 reversal confirmation, hard stop, maximum initial risk and minimum reward/risk rules remain mandatory.

When no confirmed overhead pivot existed, the resistance fallback chose the farthest historical high rather than the nearest overhead high. That could overstate reward/risk. Use the nearest fallback high. Confirmed structural pivots retain their existing precedence.

Rank current exits/reductions, entries/additions and held models ahead of generic observations. Keep stale/unconfirmed rows behind current rows, distinguish provisional signals, and put risk-blocked watches behind otherwise similar candidates. Existing dimensionless technical tie-breaks remain; neither nominal share price nor institution lots enters short-term ordering. This is ordering, not a new predictive score.

## Migration and verification

Short-term uses a separate `short_term:short-wave-v6` namespace (including a separate intraday key), selection version 11. A fresh background replay is required because historical entry/risk decisions may change. Prior v5 snapshots remain untouched for rollback; v5 model rows are not relabelled as v6 signals. Actual holdings, transactions and LINE behavior are unaffected. During the first replay only genuinely processed v6 candidates are available; do not interpret partial coverage as a completed market pass.

Tests cover institution/price/input-order invariance of Radar research slots, category coverage, duplicate/negative/empty inputs, nearest resistance, overlapping family confirmation, stale-versus-current/risk ordering, plus existing causal replay, real 7791 downtrend, wave holding, source timestamps, model sizing, scheduler and memory checks. Synthetic fixtures are isolated and are not production performance evidence.

Deploy through existing main auto-deploy, then run the authenticated independent short-term workflow and inspect saved complete scan status and dates. Roll back by reverting this release; v5 snapshots remain available. No schema rollback is needed. A later historical/out-of-sample comparison with fees, tax and gaps is required before making any performance claim; it is not replaced by these software tests.
