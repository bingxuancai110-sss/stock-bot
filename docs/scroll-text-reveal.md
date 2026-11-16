# First-appearance text reveal

The user approved a trial of short, first-appearance text reveals alongside the frosted viewport edges. Below the initial usable viewport, editorial headings, section labels and screener name/description groups fade from 20% opacity and translate upward by 5 px over 200 ms. Intersection bounds leave room for the header and bottom fog strips, so the effect starts in the readable area. It plays once per mounted element and does not replay when scrolling back or refreshing its text.

There is no hidden waiting state: before JavaScript/observation, content is visible. Initially visible headings stay unchanged. Financial quote/P&L nodes, buttons/forms and SVG/chart containers are excluded from the animated targets. A screener row's name/description span may animate inside its button while its button hit area, quote and score remain stable. Nested reveal candidates are deduplicated. The existing near-viewport chart initialization remains separate.

Animation classes are removed on completion/cancellation, preference changes and page departure. Reduced-motion users receive immediate content. No request, stored user setting, price calculation or transaction logic is added.

Tests use isolated actual-renderer fixtures and cover the visible fallback, 200 ms/5 px treatment, safe screener text, quote/button/chart stability, unchanged document height, replay prevention, reduced motion, JavaScript-unavailable content and no transaction submission. Existing UI/chart/privacy/transaction and viewport lifecycle checks remain required. Production validation follows the live Render deploy.

Shared UI JS version: 20261116-2; experience CSS version: 20261116-1. Polite live-region containers (including the production screener list) allow editorial reveals; assertive alerts and status messages remain immediate. Revert this commit to restore the prior opacity-only section treatment. No schema migration is needed.
