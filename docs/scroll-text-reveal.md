# Scroll-linked text fade

The initial 200 ms entry animation was too brief to give the requested gradual reveal. Editorial headings and screener descriptions below the initial viewport now fade according to scroll position: 8% opacity at 110 px above the viewport bottom, increasing to 100% over the next 180 px. A 180 ms opacity transition smooths updates. Text remains stationary, with no blur/filter. Once fully revealed it stays clear on back-scroll and content refresh.

Quotes, P&L, buttons, trading forms, charts, assertive alerts and status messages stay immediate. Initially visible text is unchanged. Reduced motion clears pending fades; disabled JavaScript leaves normal visible content. Near-viewport observation bounds active scroll work. Returning from a hidden tab schedules a fresh scan. Bottom-of-document content is restored fully even when it cannot reach the usual completion position.

No schema, API, financial calculation or persistent setting changes. Shared UI JS and experience CSS use version 20261116-3. Revert the commit to restore the prior entry animation.

Validation: isolated Chromium and WebKit tests measure faint, intermediate and fully visible states at separate scroll positions, replay prevention, financial/action stability and reduced-motion fallback. Production validation follows deployment.


## Chart and page-wide coverage

Plot SVGs now use a 40% opacity floor and the same 180 px scroll band: portfolio performance and allocation, market K-line, leaderboard comparisons, US risk and rotation plots, historical analysis charts, and explicitly marked chart hosts. Decorative SVG icons are excluded. Only the plot itself fades; controls, readouts and summaries remain immediate. No plot transforms, blur, path changes, dimensions, pointer interception or replay are introduced. Captured pointerdown, focusin and keydown restore full clarity before existing chart handlers run.

Home feature labels, More tool names, leaderboard member names and US research labels join existing headings and screener descriptions. Sensitive values and important status feedback remain excluded. Shared UI JS version: 20261116-4. Existing experience CSS stays 20261116-3.

Added browser checks cover intermediate plot opacity, scroll-pause behavior, actual portfolio SVG geometry/data preservation, immediate touch/keyboard clarity, plot-family coverage, icon exclusion, document height and reduced-motion restoration. Run the original interaction suite as well to verify K-line, portfolio, leaderboard and US chart tracking.
