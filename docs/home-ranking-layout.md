# Home and ranking layout pass

Home groups the existing source, synchronization, and quote-quality nodes inside a native disclosure. Its summary retains a pending-data warning and available provider timestamp. Original nodes and data attributes remain intact for live updates; no fetching or valuation logic is added. The observer disconnects on departure and reconnects without nesting disclosures on return. Today's contribution highlights use two parallel cards.

Ranking cards align name and return, place movement below the return, and use two columns for supporting metrics. Excess return occupies a full row to accommodate its benchmark comparison. Long labels wrap; selected period text retains contrast including WebKit text fill.

Production files: `main.py` (asset versions only), `static/app-experience.css`, `static/app-experience.js`. No API, schema, financial calculation, trade, permission, or snapshot changes.

Validation: Chromium and WebKit each passed 214 shared UI checks, 15 page lifecycle checks, and 16 homepage disclosure / responsive ranking checks. Isolated tests intercept all fixture traffic and perform no production writes. Widths 320, 390, 768 and 1280 were covered for home; long ranking content covered 320, 390 and 1280. Physical iPhone behavior still requires device testing.

Deployment: merge the reviewed branch into main; the existing Render service automatically deploys it. Wait for the corresponding commit to become live, then verify home disclosure, ranking layout, and page navigation on the authenticated site. Rollback by reverting this commit and deploying the revert; no database rollback is needed.
