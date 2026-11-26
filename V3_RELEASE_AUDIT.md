# V3 pre-release audit — NO-GO for production

All changes are on test/v3-readonly-market-api only. No production deployment,
main change, migration, credentials, real orders or schedule changes.

## Accounting
Optional state accounting_mode=t2 uses trade-date economic position recognition
and explicit settlement obligations. The calendar must be verified; T+2 counts
trading days, not calendar days. Settled cash is separate from available cash;
sell proceeds cannot fund buys before settlement. Pending purchased shares
cannot be sold; due but unsettled obligations block new trades. Settlement
requires explicit evidence and is atomic. No automatic migration of history.
Old instant-cash mode remains for historical/synthetic regression fixtures.
The mixed oddlot/regular same-day reverse-direction guard applies to both modes.

Corporate batches use pre-effective-day reconciled holdings, issuer event IDs,
and explicit evidence. Support cash dividend, exact split, deferred stock
dividend (allocated basis and marked share receivable), cash reduction
(proportional basis reduction and refund receivable), and separate payment.
Batch rollback on unsupported/conflicting actions. These are simplified paper
accounting policies, not statutory tax accounting. Rights subscriptions,
fractions/cash-in-lieu, corrections/reversals, actual issuer entitlement feed,
suspension/valuation after capital reduction, settlement exceptions, and
multiple share events for one issuer remain unsupported. Pending settlements
block share-changing actions; calendar and entitlement inputs need host review.

## Main host integration contract
Use v3_release_host.attach_staging(app, mode=..., market=..., ledger=..., token=...)
only after constructing isolated dependencies. Omitted/off mode is a no-op.
Only staging-readonly mode accepted; production mode is rejected. Existing
schema required, route collision checked, no new tables or network startup.
Register before serving requests, after app creation; no direct main.py
modification. The module is an integration hook, not a deployed main integration.
The saved BOT bootstrap has start_server_service(..., now=taiwan_now);
optional shared_collector injection still requires a trusted host and an audit
of every direct Fugle caller. A full current main.py import was not possible.
No credential persistence in browser; use a randomly generated, scoped secret,
TLS, restricted access, rate limiting, and rotation. Token shape checking does
not prove entropy. No HTTP mutation routes. Legacy synthetic fills are not
safe for externally provided input. Authentic provider origin, calendar,
catalogue, research and all settlement/company event evidence remain required.

## History
Original user-authorized 10m fund, 11/13 retrospective hypothetical checkpoint
retained; 11/13 onward full transaction/corporate-action records not recovered.
The initial 1m spreadsheet is a separate earlier plan. Do not infer no
intervening trades from reports with zero confirmed fills. Keep ready=False.
Do not activate t2 on legacy records without explicit reviewed reconciliation
of settled vs trade-date cash, unsettled trades, costs, entitlements and marks.

## Verification
Local discovery: 142 tests, 125 pass, 17 skipped (Flask unavailable; installation
attempt returned no matching distribution). Additional 3 CI cases
(Postgres x2 and provider parser x1) not run locally. Python compileall passed.
Full synthetic research -> shared quota -> five-level book -> paper ledger ->
T+2 settlement test passed. No actual provider, production app import, actual
HTTP authentication, Postgres, browser, or scheduler end-to-end verification.
GitHub connector reports no combined statuses; workflow-runs action is limited
to PR-triggered runs. Check actual push-run status in Actions UI. No CI success
claimed. Separate staging validation and human review required before release.
