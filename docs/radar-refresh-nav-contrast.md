# Radar refresh and navigation contrast — 2026-11-17

Radar jobs were invoked every ten minutes but rejected by the 200 MiB reserve
on a 512 MiB service whose baseline was about 368–380 MiB. The existing engine
already processes four symbols per batch with two quote workers. Admission and
every-batch checks now use the same 96 MiB reserve. The complete 240-candidate
coverage, single background queue, memory cutoff, and refusal to publish partial
rankings remain in place. Monitor real scans after release; this is not a claim
that memory pressure cannot recur.

A saved but stale Radar snapshot now schedules one background rebuild. Its rows
and original source date remain visible. The browser polls while building even
when old rows exist, then replaces them with the completed snapshot. The page
labels the source date and whether the old snapshot is updating or deferred.
No synthetic dates or prices are written.

During bottom-navigation loading, only the destination under the moving dark
pill uses light text. The previous tab has dark text/icons and no pale border.
Failure still restores the original selection.

Validation: 42 backend tests; Chromium and WebKit navigation tests (22 each),
Radar stale-to-fresh polling tests (5 each), and full UI regression (214 each).
Tests intercept requests and do not write production portfolio transactions.

Deployment: merge to main, wait for Render's automatic deployment to become live,
then open Radar and verify its real source date and background completion logs.
Monitor memory during the first completed scan. No schema or secret changes.
Rollback: revert this release commit and let Render deploy main again. Saved
snapshots and portfolio data are unaffected by rollback.
