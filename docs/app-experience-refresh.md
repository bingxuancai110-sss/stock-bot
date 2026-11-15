# App-wide visual and navigation experience

The original opaque viewport chrome below is superseded by [frosted-scroll-surfaces.md](frosted-scroll-surfaces.md), following the user's request for an artistic frosted/blurred scrolling treatment.

The preceding interaction release reduced DOM work, but left the overall hierarchy and navigation presentation almost unchanged. Production phone-size inspection found 9 px navigation labels, 14 px stock quotes and 10 px holding metadata. This release addresses the visible experience as a shared layer, retaining the existing application and all financial logic.

## Visible behavior

- Mobile navigation uses larger labels/icons and a moving selection surface. A requested destination responds immediately, shows a busy state and a readable status after 120 ms. It retains the current page's accessibility semantics until navigation succeeds, and restores the current destination on error/cancel. Status is fixed and does not shift content; the existing long-wait recovery controls take over after seven seconds.
- The mobile header stays visible. After 28 px of scrolling, its date fades and title settles into a compact presentation. Header height stays constant; an opaque surface prevents charts or rows showing through it. Scroll work is coalesced into one frame and the body class changes only at the threshold.
- Holdings use 16 px names and 17 px quotes, clearer row spacing, readable section controls and a two-column allocation legend with 44 px targets. The ring remains. Expanded details retain transparent hit targets and the same buy/sell/analysis actions.
- Screener tools, source tabs, names/quotes, chart controls, More links and US rotation summaries use a shared mobile type/spacing hierarchy. Inputs are at least 16 px to avoid Safari focus zoom. Anchor margins account for the header and bottom navigation.
- Page arrivals use a short opacity treatment on a safe summary surface. The root and chart containers are never translated. No custom scroll physics or scroll interception is introduced. Reduced-motion settings disable animation.

## Architecture and verification

`static/app-experience.css` and `.js` own the shared visible layer. Existing `ui-feedback.js` emits two presentation lifecycle events for route pending/completion. The layer performs no fetches, stores no account state, changes no trade submission or data source, and does not change any scores, prices, portfolio arithmetic or database schema.

The browser suite reuses unchanged actual-renderer bodies with the current shared asset references; isolated inputs and requests never reach production. Additional checks cover header geometry/opacity, 320/360/390/430 px overflow, larger type, allocation layout, immediate pre-response feedback, cursor movement, accessibility semantics, successful/failed navigation, reduced motion and no transaction writes. Existing chart, privacy, disclosure, transaction safety and navigation tests remain required. Preview screenshots contain explicitly synthetic test companies, not production portfolio performance.

After Render is live, verify the real website at phone size, current asset versions, holding expansion, navigation, source switching and saved radar results. This does not claim a measured improvement in physical iPhone frame rate or backend latency. Revert this release to roll back; no migration is required.
