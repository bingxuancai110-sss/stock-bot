# Site interaction performance

Keep the current visual design, frosted edges, scroll reveals, chart tracking and portfolio pinch zoom. No API, database or financial behavior changes.

## Changes

Chart source data is parsed and indexed once per mounted chart. Pointer tracking uses a sorted x-coordinate index and binary search instead of re-parsing and scanning every point every frame. Same-date tooltip strings are prepared with the index. Source point changes and series visibility changes invalidate it; departure disconnects the observer and releases the cache.

Tracking reads chart/panel geometry before changing tooltip styles. Repeated selection of the same date avoids replacing tooltip text and repainting the crosshair. Only the previous/current highlighted point is touched, rather than querying the entire SVG for active marks.

Scroll reveals collect geometry first, then apply opacity updates together, skipping unchanged styles. Page readiness events show a mounted hero only once instead of stacking identical Web Animations.

## Measured evidence and limits

On an isolated 4,000-point series, 30 consecutive tracking frames parsed that series 30 times before the change and once afterward. Six repeated readiness events started six hero animations before and one afterward. Chromium and WebKit both verify data invalidation, hidden-series exclusion and correct tracking. These counts show reduced repeated work; they are not physical-device FPS claims. Network/server loading latency is unchanged.

Run run_interaction_performance.py, run_ui_feedback.py, run_page_smoothness.py, run_app_experience.py, run_portfolio_pinch.py, run_chart_reveal.py and run_scroll_text_reveal.py for both engines. All fixture traffic is intercepted and no production trades are submitted.

Shared UI JS and experience JS: 20261116-7. Experience CSS stays 20261116-6. Revert this commit and refresh asset links to roll back; no data migration is needed.
