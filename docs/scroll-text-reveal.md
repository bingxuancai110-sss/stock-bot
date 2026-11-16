# Scroll-linked text fade

The initial 200 ms entry animation was too brief to give the requested gradual reveal. Editorial headings and screener descriptions below the initial viewport now fade according to scroll position: 8% opacity at 110 px above the viewport bottom, increasing to 100% over the next 180 px. A 180 ms opacity transition smooths updates. Text remains stationary, with no blur/filter. Once fully revealed it stays clear on back-scroll and content refresh.

Quotes, P&L, buttons, trading forms, charts, assertive alerts and status messages stay immediate. Initially visible text is unchanged. Reduced motion clears pending fades; disabled JavaScript leaves normal visible content. Near-viewport observation bounds active scroll work. Returning from a hidden tab schedules a fresh scan. Bottom-of-document content is restored fully even when it cannot reach the usual completion position.

No schema, API, financial calculation or persistent setting changes. Shared UI JS and experience CSS use version 20261116-3. Revert the commit to restore the prior entry animation.

Validation: isolated Chromium and WebKit tests measure faint, intermediate and fully visible states at separate scroll positions, replay prevention, financial/action stability and reduced-motion fallback. Production validation follows deployment.
