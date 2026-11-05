# Production checkpoint — 2026-11-05

Trade acknowledgement and buy replay protection from PR #79 are deployed.

- Source commit: `2f135ee934bcdb7ce6e97e88d1712aeafa82c466`.
- Render deploy: `dep-dblujuu7bikc73ebkf90`.
- Service: `srv-d9rjprfavr4c739fkpbg` in user-confirmed My Workspace.
- Render status: `live`, finished `2026-11-05T02:31:33.121745Z` (10:31 Taiwan).
- Read-only HTTP checks after live: `/health` 200;
  unauthenticated `/web/api/positions/quotes` 401 and
  `/web/api/positions/history` 401.
- The new instance started Gunicorn on port 10000; no initialization error was
  shown in the inspected startup log range. This is not direct inspection of the
  dedup table and does not prove a live user transaction was executed correctly.

No account transaction was submitted, edited or removed for these checks.
Historical duplicate lots were not automatically removed.

The separate UI branch is based on this deployed commit. Both fixture browser
engines passed 145 checks; final authenticated seven-page screenshot comparison
is still outstanding. The browser inventory continues to report Local browser
use disabled in Work settings. Health and permission checks do not substitute
for that visual/account-level verification.
# Portfolio swipe hotfix — 2026-11-05 13:58 Taiwan

- User subsequently confirmed the production chart can move again (「可以動了」).
  This confirms the reported swipe symptom resolved for that session; it does
  not constitute all-seven-page or all-device acceptance.
- The complete Python suite on UI branch commit `86f490e` passed in one run:
  **133 passed in 371.48s**, with no split-run extrapolation.

- PR #81 merged as `b3e3888436fe05ba371315f7cdfbd88d234e751c`.
- Render deploy `dep-dbm1l53m8hqs73cmc0ag` became live at
  `2026-11-05T05:58:40.66618Z`. Only shared chart touch handling and cache-version
  references were deployed; PR #80's seven-page redesign remains a draft.
- After live: `/health` and both `ui-feedback` assets returned 200. The CSS contains
  the native portfolio touch override; JS contains mouse-only manual panning.
  Unauthenticated positions quotes still returns 401.
- Local Chromium native touch moved chart scrollLeft 848 → 681; WebKit mouse
  dragging also passed. Actual signed-in production/iPhone verification remains
  unavailable. Existing open pages must reload to load the revised script.
- The ongoing UI branch merged this production commit and retained its own
  updated asset versions. No transaction, database or financial calculation change.

