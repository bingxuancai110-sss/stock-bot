# Brokerage visual polish

The user requested continued layout/art direction. Keep existing financial behavior, frosted viewport edges, scroll reveal, chart pinch zoom and all seven navigation destinations.

The mobile holdings summary now has a dark asset headline, two side-by-side P/L cells and a compact three-column supporting row. Use existing values, privacy masking and Taiwanese up/down colors; quote-quality metadata stays visible. Remove only repeated explanatory captions. The privacy button stays at least 44px.

Put portfolio charts, allocation, tool groups, ranking summaries and US sections on quiet bordered surfaces. Avoid double borders inside the portfolio chart and use a 320px mobile plot height while leaving horizontal width under the pinch controller. Keep the allocation ring and single-column holdings list.

Home has a dark summary, light feature tiles and high-contrast financial accents. Selected mobile navigation uses a dark moving pill and light text, with pending-route and no-JS states preserved. Screener row wrappers use a shared sheet boundary; detail controls stay separate and accessible. Theme surface/line tokens are retained, with a neutral dark headline accent.

Only shared CSS and its asset URL change in production. No API, schema, scoring, quote or background-job changes. Update the old full-width P/L layout check for the intentional parallel layout, retain overlap/privacy checks and run mobile layout, existing UI and pinch suites. Review real site screenshots after one post-close deployment. Revert the commit to roll back; no data migration.

Shared experience CSS version: 20261116-8.
