# App-wide visual system

The seven primary destinations share neutral press feedback, visible keyboard focus and consistent segmented selection. Financial up/down and warning/confirmation colors retain their meaning. Shared form loading states and focus treatment also apply on secondary pages; field values and behavior are unchanged.

| Page | Visible changes |
| --- | --- |
| Today | Two-column phone shortcut cards with larger horizontal targets; framed holdings/press states |
| Market | Search and theme control share a row on the quote board; framed quotes; chart panel spacing, 44px tools and period rail; dark-mode palette retained |
| Holdings | Shared focus/loading treatment complements the existing selected holding and expanded cost surfaces |
| Screener | Dark selected asset/sort controls, underlined source tabs, search/action spacing and neutral result press |
| Leaderboard | Shared period/history selection, separated personal ranking metrics and 44px disclosures |
| US rotation | Full-width market switch, padded risk cards, consistent period controls and research disclosures; signal colors preserved |
| More | Remove redundant intro only on the normal tool index; consistent icon/row spacing and neutral press feedback |

One new stylesheet `static/app-polish.css` is loaded by the existing shell. No JavaScript runtime, APIs, database, calculations, credentials or deployment configuration are changed. Existing native scrolling, chart gestures, trade completion cards and data provenance remain intact. Motion is disabled under reduced-motion preferences.

Validation: Chromium and WebKit each passed 49 app-wide checks and 214 shared interaction checks. Nine renderer views (seven primary destinations, both market branches, plus settings) cover 320/390/1280px widths, dark market contrast, mobile tool targets, source selection, market-switch alignment and no unintended writes. Existing regression checks cover trading, privacy, drawing/fullscreen, chart inspection, screener detail/lab/filter flows and navigation. Fixtures now generate the actual market quote-board branch separately from the K-line branch. Physical device verification is separate.

Deployment: merge into main, wait for the matching Render deploy to become live, and inspect all seven primary pages on the authenticated site. Revert this commit to roll back; no database rollback is necessary.
