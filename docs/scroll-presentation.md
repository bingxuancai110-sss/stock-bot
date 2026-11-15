# Stable scrolling and restrained loading presentation

## Production inspection

The seven main pages were opened in the user's signed-in local Chrome using the production website, including home scrolling, holdings scrolling/expansion and screener source switches. Chrome remained at the user's original 80% zoom. No trades, account settings or financial records were changed. Browser zoom reset was rejected by automatic approval review and was not retried or bypassed.

Observed issues: small desktop metadata, an allocation legend stretching a small ring over many rows, the default short-range chart wider than its frame, source controls that are hard to read, and hover padding that shifts stock-row content as the pointer passes over it. Legacy entrance translations and estimated content heights were also still active in the shared styles.

## Changes

- Remove staggered translated row entrances and selected-navigation bounce, retaining press/disclosure feedback and reduced-motion support.
- Use actual layout height for history/chart sections rather than content-visibility placeholders that may change document height on approach.
- Use solid navigation surfaces without backdrop blur. Stock-row hover changes background only, without changing horizontal padding.
- Keep the loading rail single-color, defer the small wait notice until 1.5 seconds, and place it above navigation rather than over the page heading. A prolonged wait uses one actionable status without shifting the document. Hide the changing elapsed timer from screen-reader announcements. Keep the launch illustration but stop perpetual bar jumping.
- At desktop widths, improve holdings and screener metadata sizes, enlarge the allocation ring and use a two-column legend. Fit 1-week/1-month/3-month holdings charts within the desktop frame at a stable height. Retain phone and long-range horizontal panning.
- Update CSS asset versions to 20261115-2. No data/API/DB/financial calculation changes.

## Verification

Actual renderer fixture tests with synthetic records and intercepted requests are separate from the real Chrome inspection. Chromium and WebKit: 214 existing UI checks, 15 navigation checks, and 19 presentation checks per engine. The presentation checks cover stationary rows, stable document height, opaque navigation, delayed loading feedback, accessible timers, desktop allocation density, minimum metadata size, fitted short-range charts, long-range panning and hover stability.

Renderer AST generation, shared/extracted inline JS syntax, and Git whitespace checks pass. No iPhone hardware FPS or production latency improvement is claimed. Backend request latency remains a separate issue. The desktop expanded-card grid retains its existing layout.

Rollback: revert this change. No database migration or data rollback is needed.
