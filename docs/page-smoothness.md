# Page navigation and viewport interaction

## Behavior

- SPA navigation captures the outgoing page's scroll position and restores the destination only after its actual fragment/scripts are ready. Remove the old 80 ms back-navigation timer.
- View identity excludes volatile `_nav`, `fragment`, and `fast` parameters. The same open document remembers up to 16 views in memory, including chart period, benchmark visibility and leaderboard comparison. It does not retain trade inputs or persist account data to browser storage. Full-document workbench navigation keeps its existing lifecycle; state is not promised across a reload.
- Offscreen SVG chart interaction is initialized within 240 px of the viewport. Existing chart data remains visible; this does not fetch prices, recompute returns, or change server-side rendering. Hidden chart periods initialize when made visible. Unsupported browsers initialize directly.
- Lower home sections, history, comparison charts, rotation sections and More groups receive a one-time 180 ms opacity arrival. Above-the-fold content stays static. Nothing is hidden pending JavaScript; no translating containers, extra placeholder space, or gesture interception. Respect reduced motion.
- Disconnect viewport observers and queued work on departure. Chart cleanup removes generated readouts, guides and event listeners and restores its original accessibility attributes, allowing a clean BFCache reinitialization. Reuse the same point list for nearest-point lookup and rendering within a touch frame.
- Bump shared CSS and JS to `20261115-1`.

## Validation

Actual renderer fixtures with isolated data; all fixture.test requests are intercepted, with no real-user writes. Chromium and WebKit each pass 214 existing UI checks and 15 focused smoothness checks, including a delayed back response, home URL timestamp changes, comparison restoration, no extra requests for viewport initialization, one-time arrival, cancelled old-page jobs, clean chart reinitialization, reduced motion and missing IntersectionObserver.

Python renderer AST generation, extracted inline JavaScript syntax, shared JS syntax and Git whitespace checks pass. This is not iPhone Safari/PWA hardware FPS measurement or logged-in production account verification. This change does not claim to fix backend data-download latency or defer server-side HTML generation.

Run fixtures.py, then tests/browser/run_ui_feedback.py and tests/browser/run_page_smoothness.py with chromium or webkit using the existing verification environment. Roll back by reverting this change; no database migration or data rollback is required.
