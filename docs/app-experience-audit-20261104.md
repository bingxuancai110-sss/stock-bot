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

## Remaining verification/work

- Complete chart touch, keyboard, existing dialogs and legend regression checks.
- Deploy this patch and repeat production checks; record actual results rather than extrapolating from fixtures.
- Finish all-seven-page action audit, including market fullscreen/drawing and the mobile holdings transaction sheet without submitting real transactions.
- Refine US Rotation density and repeated explanatory copy while retaining all research information.
- Measure slow route transitions and address their actual bottlenecks without adding foreground market scans.
