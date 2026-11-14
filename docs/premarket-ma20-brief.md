# Premarket MA20 reminder recap

The existing scheduled and manual LINE premarket message and its full web page now include a read-only recap of the user's saved TAIEX MA20 strategy state.

- Only enabled, administrator-approved subscriptions are included.
- Show day-two warning, day-three confirmation, and 3% / 4.5% recovery events with their close date and next-session execution target.
- Continue tracking the strategy's lowest close; do not calculate new signals or modify strategy state from a page request.
- Existing below-MA baselines do not generate retrospective warnings. Stale or incomplete snapshots show an explicit unavailable status, never a fabricated all-clear.
- Existing premarket events, market overview, news, and full-analysis link remain available.
- No additional push, schedule, database migration, paid API, trade, or holdings modification is introduced.

Implementation: taiex_morning.py reads one user-scoped subscription/state query with a five-second statement timeout and closes its own connection. main.py integrates the recap into the shared morning builder and fragment-aware premarket page.

Validation: 79 focused tests passed across test_taiex_morning.py, test_taiex_cycle.py, test_taiex_reduction.py, and test_taiex_line_cards.py. The tests exercise saved strategy transitions, weekend freshness, invalid data, subscription scope, Flex/HTML escaping, and the actual extracted morning builder. No live user data or LINE deliveries are created by these tests.

Roll back by reverting this change. No schema or saved strategy data needs reverting. Production message receipt still requires checking an approved user's LINE conversation; fixture tests do not prove delivery.

## Compact LINE alert layout

The LINE-only warning area uses a title/status header, closing date, two-column close/MA20 values, and a separate interpretation/action line. The original detailed web description and text fallback remain intact. Numeric metadata is copied from the same validated observation; strategy rules and subscriptions are unchanged. Existing lower market/news sections are untouched.

Validation: 81 focused tests passed, including separate metric columns, unique numeric values, preserved action/date, and unavailable state without fabricated zero prices. Native LINE rendering and receipt are not reproduced by structural tests. Previously sent LINE messages retain their original layout; request a new premarket message to see the update.

## Unframed LINE warning section

Replace the nested panel and pill with an unframed strategy section: small date/label header, primary status headline, smaller paired close/MA20 values, short interpretation, and a thin divider. No lower premarket content changes. Keep action dates, targets, source, and unavailable-data messages. The same 81 focused tests pass with additional assertions that the warning section has no background, border, or rounded panel.
