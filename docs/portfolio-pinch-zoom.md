# Portfolio timeline pinch zoom

Scope: the existing SVG portfolio performance chart only. Two fingers spread/contract the horizontal timeline around the finger midpoint, and can move together to pan that midpoint. Return values, SVG data and the vertical scale stay unchanged. The plot scroll container owns the resulting overflow; page width and browser zoom stay unchanged. Single-finger native horizontal/vertical panning and date tracking remain available.

A non-passive two-touch handler cancels only the plot pinch. The plot permits pan-x/pan-y while excluding native page pinch. Width updates are coalesced with requestAnimationFrame. Width is clamped between the visible plot width and a bounded maximum. No network request or recalculation is needed.

A 44px reset button appears only after zoom changes the original width. It restores the original responsive CSS and pre-zoom horizontal offset. Plus/minus keys zoom around the plot center; 0 resets. Each period has independent state. Resize restores responsive defaults; touch cancellation and page cleanup release gesture state and listeners. The existing chart fade completes before interaction.

Validation: Chromium uses native multi-touch input for spread/contraction and native single-touch vertical scrolling. WebKit uses synthetic touch lists to exercise the same handlers (physical iPhone gesture arbitration is not available in the automated runner). Both engines verify midpoint anchoring, stable page scale, unchanged path/viewBox/height, reset, keyboard access, cancellation, period independence and cleanup. The existing UI suite verifies native one-finger horizontal panning, tracking, period changes and unrelated pages/trades.

Deployment uses shared JS/CSS version 20261116-6. No schema, API or data changes. Revert the commit and refresh shared asset links to roll back.
