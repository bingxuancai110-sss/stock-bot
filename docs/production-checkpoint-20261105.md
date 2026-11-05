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
