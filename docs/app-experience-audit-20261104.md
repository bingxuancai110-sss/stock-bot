# App experience audit — 2026-11-04

This is an ongoing audit, not a completion claim. Production pages are checked in the user's authenticated Chrome. Isolated renderer fixtures check regressions; they are not evidence of production layout.

## Acceptance scope

- Seven primary destinations remain reachable: Today, Market, Positions, Screener, Leaderboard, US Rotation, More.
- Mobile navigation, header, spacing, touch targets, and theme handling form one consistent shell.
- Page changes show immediate feedback, preserve the previous page on failure, and do not block navigation with a second full-screen overlay.
- Initial entry has a restrained full-screen launch animation with honest loading/error states and reduced-motion support.
- Portfolio and ranking charts work directly with pointer/touch/keyboard, without an extra tracking mode; scrolling and existing point dialogs remain available.
- Holdings retain privacy controls, allocation ring, benchmark comparison, trade entry, history, sorting, and stock details. Financial calculations and account boundaries remain unchanged.
- No trade/account writes are made during production UI inspection.
- All seven production pages need final desktop/mobile verification after the final visual changes before this work can be called complete.

## Current production observations

- Authenticated real holdings, market quotes, and screener snapshots render.
- At a measured 390 CSS-pixel viewport, Positions, Screener, Market and More did not horizontally overflow. A prior raw device-emulation overflow report was not reproducible with measured layout dimensions.
- The shared header was being overridden into a rounded shadowed panel by a later rule in ui-feedback.css.
- Portfolio charts still required a touch-tracking button despite the user's request for direct interaction.
- A negative excess return was labelled “outperformed market”.
- US Rotation has a large explanatory opening before the first visualization; this still needs density and hierarchy review.
- Primary navigation keeps the old content visible while a new page is being fetched, but several routes still take longer than the requested three-second target.

## Current patch

- Direct chart tracking; horizontal drag preserves history scrolling, vertical motion remains page scrolling, and dragging does not accidentally open a point dialog.
- Neutral “relative to market” label.
- Compact flat shared header.
- Minimal market-bar launch animation, without decorative rings or fabricated completed processing stages.
- Remove the transformed containing block while the initial loader is present, so its fixed viewport coverage is real.
- Respect `hidden` on transaction timeline rows; the grid layout previously overrode filtering.
- Mount confirmation and submit feedback inside the open native trade dialog; outside elements are inert while a native modal is open.
- Keep the previous page at full opacity while route requests are pending.

## Visual reference

The original `seven-page-targets.html`, `seven-page-preview.png`, and `page-1.png` through `page-7.png` have been recovered from the conversation's local visualization artifacts. They define the flat header, continuous data rows, compact navigation and restrained colors. Production still needs comparison against those references; the mockups are not runtime evidence.

## Checks in this working branch

- Existing Python suite: 103 passed.
- JavaScript syntax and whitespace checks passed.
- Browser harness is being brought into alignment with the actual renderer. Repairs include Python/JavaScript API syntax, real expanded actions, sorted ticker expectations, chart coordinates within the visible scrolled viewport, and modal-sheet lifecycle.
- Full Chromium run: 124 checks passed, zero page errors. Production verification and the remaining all-page visual work are still pending. No overall completion claim is justified yet.

## Production verification of interaction patch

Commit `23f87d5` became live on Render at 2026-11-04 14:32:58 UTC (PR #74).

- User's authenticated Chrome loaded resource version `20261104-7`; shared header computed radius is zero and no tracking switch is rendered.
- Real touch drag on the portfolio chart moved history scroll position from 848 to 748 pixels and displayed actual cached date/portfolio/benchmark values. A vertical touch gesture scrolled the page about 242 pixels and cleared the chart tooltip. No point dialog opened accidentally.
- Opened the real transaction sheet, filled unsaved UI-validation values, opened confirmation, and clicked Return successfully. Cleared the temporary inputs and closed the sheet. No record was submitted.
- In the real transaction timeline, choosing Buy hid all 60 Sell entries. Restored All and Current Holdings afterward.
- Saved actual production launch, chart, preview and timeline JPEG evidence in the workspace proof directory. Temporary browser viewport override was reset.
- This verifies the interaction patch only. Leaderboard, US Rotation and the other pages still require alignment with the recovered seven-page visual references and final end-to-end inspection.

## 2026-11-05 worktree progress

- Mobile Positions now overrides the legacy two-column rule with a full-width, flat holding list; desktop grid is retained. Removed the later rule that reintroduced red/green dots beside holding names.
- Mobile Positions summary now has a full-width asset headline, two readable P/L rows and a compact three-column supporting-metrics row. Benchmark comparison and allocation donut remain before the stock list.
- Screener mobile controls now form a compact status strip, asset selector, search/actions group, and scan list. Selection and filter controls remain the same DOM controls.
- The renderer-based browser checks now also assert the screener's three-way mobile selector, search/action hierarchy, wrapping filter controls, and minimum touch heights. Fixture file reads/writes explicitly use UTF-8, so they run under Windows' legacy default code page as well.
- The authenticated-shell fixture now also renders the US Rotation page from its actual blueprint renderer, and the browser suite checks its explicit no-snapshot state, in-place period/advanced controls, and mobile overflow at 360/390/430px. This expands automated page coverage but still does not replace a production session check.
- The fixture now renders the actual market K-line route with isolated OHLC bars, and the browser suite is set up to check zoom, fullscreen horizontal-line drawing, dragged trendlines, and closing fullscreen. Drawing API writes are intercepted and never touch an account.
- Leaderboard places one selected period chart before its corresponding ranking list. Short, long, and season chart panels follow the existing no-fetch period switch; member-series legend toggles stay in the same chart.
- US Rotation now uses a flatter research layout with grouped metrics and continuous sector rows; red/green fills remain on actual warning states. Research formulas, sample scope and data requirements are unchanged.
- Bumped shared CSS asset versions to `20261105-1` and the latest portfolio sheet to `20261105-2` so clients do not keep previous cached rules.
- Current checks: Python compilation, renderer fixture generation, extracted JavaScript syntax, and `git -c core.whitespace=cr-at-eol diff --check` passed. The Playwright browser harness cannot start because Playwright is not installed and network access cannot reach PyPI. Computer Use reports Local browser use is disabled in Work settings; I cannot change that permission. These latest UI changes have not been verified on the live website.
- The seven-page goal remains incomplete; this is not a deployment or a completion claim.

## Latest executed checks — 2026-11-05

- Installed development-only test dependencies and browser binaries under local
  `.validation-*` directories; production requirements were not modified.
- Chromium and WebKit: 145 actual-renderer interaction checks each passed, zero page errors,
  including lightweight trade success responses and fullscreen drawing.
- Python: 129 passed in the full run; four cache/LINE probe cases initially failed
  because their loading-animation stub did not accept the current `max_attempts`
  argument and selected the wrong Reply-vs-Push path. Updated that probe to model
  successful loading, then those four cases passed. This is a split-run result.
- Visual inspection of the rendered phone list found the price and third metric
  still wrapped by legacy rules. Added scoped mobile grid overrides to align
  name/price and the three metrics without changing the desktop layout.
- WebKit exposed a selected-history-button style conflict; a targeted selected
  control rule passed revalidation. A 360px homepage overflow came from nested
  negative margins extending beyond the page gutter; removed those margins and
  reran both browser suites successfully. No Safari device or live-site claim
  follows from these isolated browser runs.
- The urgent trade fix was merged as PR #79 and became live at 10:31 Taiwan
  on 2026-11-05. The visual refinement is isolated in draft PR #80. No historical
  portfolio records were changed; see production-checkpoint-20261105.md.

## Remaining verification/work

- Complete chart touch, keyboard, existing dialogs and legend regression checks.
- Deploy this patch and repeat production checks; record actual results rather than extrapolating from fixtures.
- Finish all-seven-page action audit, including market fullscreen/drawing and the mobile holdings transaction sheet without submitting real transactions.
- Refine US Rotation density and repeated explanatory copy while retaining all research information.
- Measure slow route transitions and address their actual bottlenecks without adding foreground market scans.

## Clean-run findings

- A fresh artifact directory exposed script extraction running before all HTML
  renderers. Moved extraction to the end, then generated all pages in a new folder.
- Seven-page capture found the closed screener drawer expanded the layout
  viewport from 390 to 778px. The former scrollWidth-vs-innerWidth assertion
  missed this because both expanded. Keep the drawer box inside the viewport
  and reveal it with clipping; explicitly test against the configured phone
  width and visible bottom navigation. Chromium and WebKit: 147 checks each passed.
- Both engines subsequently passed 150 checks with a populated fixture API: a
  real screener row click opened the actual stock-detail renderer, and the real
  close button restored the list. This verifies the interaction path using
  isolated input data; production account data remains unverified.

## Mobile holdings refinement — 2026-11-05

- Closed phone holdings now use compact rows with aligned price and holding-return
  columns. The entire row opens the original detail disclosure; complete figures,
  daily change, research, buy and sell controls remain available when expanded.
  Desktop tiles keep their existing layout. Confirmed limit-board badges remain
  visible even on closed rows.
- Reset inherited named grid areas in the mobile asset summary. The legacy areas
  placed total value and unrealized P/L on the same row, and overlapped position
  count with today's P/L. Added geometric overlap verification rather than relying
  only on column widths.
- The privacy restoration attribute used `fmt_pct`, which returns HTML. Replaced
  that attribute value with plain formatted text, preserving the financial value.
- Quote updates now synchronize every repeated value, return and weight field;
  the expanded market value also has its update binding. Missing values have no
  positive/negative color. Calculations and transaction handling are unchanged.
- The fixture now includes the actual outer performance-chart wrapper. That
  exposed duplicate `portfolio-trend` IDs; the outer section now has a separate ID
  and the chart keeps its existing interaction root.
- Chromium and WebKit each passed 157 isolated interaction checks with no page
  errors. The nine position-submission regression tests passed. These are local
  renderer/fixture results, not signed-in production or iPhone device validation.
- Portfolio stylesheet version is `20261105-4`. Changes remain in PR #80; the
  seven-page production visual comparison remains outstanding.

## Rotation reading order and disclosure refinement — 2026-11-05

- Moved the existing risk/stop observation section ahead of industry summaries.
  Removed its duplicate page H1 and kept methodology/update explanations in
  disclosures. The source, trading date and existing import entry stay available.
- Phone industry heat cells now form compact continuous rows. Risk values,
  thresholds, signal badges and data dates remain directly visible; indicator
  explanations and historical statistics remain in an explicit disclosure.
- Risk trend charts now render when their disclosure opens. Closed disclosures
  are checked explicitly, and hidden-market charts are skipped. Market changes,
  period selection, keyboard inspection and sector/company navigation stay local.
- Shared disclosures now use one CSS chevron and suppress native markers in both
  engines, eliminating the duplicated triangle/arrow. The compact holding row
  keeps its whole-row disclosure target without an arrow over its values.
- Added a populated rotation test payload, explicitly synthetic and isolated from
  production, using the actual risk-description/profile functions. Normal fixture
  shells now exercise the authenticated account-entry markup using a test user ID.
  This does not authenticate a real user or prove production account behavior.
- Chromium and WebKit each passed 178 interaction checks without page errors.
  The four US-rotation Python test files passed 42 tests. Generated seven phone
  captures for internal comparison; actual signed-in production screenshots remain
  unavailable and the seven-page goal is not complete.
- The phone capture audit still shows homepage highlight cards and a gradient
  diverging from the approved flat dashboard, plus verbose workbench rows. These
  are the next visual corrections; existing detail/data paths must remain reachable.
- `app-shell.css` and `ui-feedback.css` versions are `20261105-3`; portfolio CSS
  remains `20261105-4`. This branch has not been deployed.

## Homepage and screener density — 2026-11-05

- Replaced the homepage asset gradient/shadow with a flat deep-teal surface.
  Phone quick actions occupy one four-item row with original destinations and
  touch targets; their redundant subtitles and arrows are visually suppressed.
- Phone contribution highlights are now continuous name/value rows rather than
  two bordered cards. Existing contribution/stock-percentage toggles, expand,
  sort, privacy and source/status information remain available.
- Standard phone screener rows now prioritize name, industry, price/change and
  score. Detailed fundamental/institutional lines stay available through the
  existing detail drawer; desktop rows and specialized strategy/ETF views keep
  their layouts. The meaningful signal note remains below the compact quote row.
- Both Chromium and WebKit passed 183 isolated interaction checks with no page
  errors, including the real screener detail open/close path. Refreshed and
  inspected the seven phone captures. The final signal-note alignment is a CSS
  presentation adjustment; production browser/device validation remains pending.
- Workbench stylesheet version is `20261105-4`. No financial calculation,
  transaction, LINE, data-source or database change accompanies these CSS edits.
