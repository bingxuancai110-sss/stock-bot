# V3 staging runbook — synthetic only, production NO-GO

## Preflight (must pass before creating a service)
- Confirm current branch SHA and successful strict CI summary: at least 150 tests,
  zero failures, errors and skips, including Gunicorn loopback and PostgreSQL.
  A green run from an older commit is not sufficient.
- Confirm current billing-cycle workspace quota and payment/spend settings in
  Render dashboard. 2026-11-26 screenshot showed 591.93/750 free hours,
  4.33/5 GB bandwidth, 150/500 pipeline minutes, one service and USD 0 accrued.
  This is historical evidence, not current availability.
- Do not create a service while free resources for existing stock-bot could
  be endangered. Recheck after December quota reset; budget for normal BOT usage
  first. Do not enable paid plan, database, disk, build tier or automatic wakeups.
- Verify original stock-bot is on main. Do not modify that service, branch,
  credentials, databases, environment group, webhook or production schedule.
- Ensure staging token is independently generated (e.g. secrets.token_urlsafe(48))
  in a secure local/admin context. Never paste it in chat, URL, repo or logs.
  Token shape checking is not proof of entropy.

## Proposed isolated service
- Name: stock-bot-v3-staging (subject to availability)
- Repo: bingxuancai110-sss/stock-bot
- Branch: test/v3-readonly-market-api
- Runtime: Python, free plan, autoDeploy=no; one instance, no persistent disk.
- Build: pip install -r requirements-v3-staging.txt
- Start: gunicorn 'v3_staging:create_app()' --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 30
- Env: V3_STAGING_TOKEN only; optional runtime version configuration if needed.
- Do not copy production environment or Fugle credentials.
- No provider requests, production DB, broker access or background jobs.
- Fixture state is disposable and explicitly synthetic; never import it into
  the actual historical ledger.

## HTTP acceptance
Use the actual new service URL, HTTPS and bearer header. Never follow redirects
with a token. Do not retain credentials in command history or screenshots.
- GET /healthz -> 200, synthetic_only and execution_authorized=false.
- GET /v3/fund -> 200, staging label, no data/token embedded, CSP/no-store.
- GET /api/v3/market/quotes?symbols=2330 without/wrong token -> 401;
  valid staging token -> 200, synthetic fixture, execution_authorized=false.
- Authorized unknown symbol -> 403; known missing symbol 2345 -> 503.
- GET /api/v3/fund/performance without/wrong token -> 401;
  valid token -> 200, data_mode=synthetic_only, reconciled=false, no real holdings.
- POST to read-only APIs -> 405; /api/v3/fill -> 404.
- Review staging logs and usage without exporting secrets; verify production
  Render service unchanged. Suspend staging after tests if available; otherwise
  allow normal idle spin-down, do not keep it awake, and monitor remaining quota.
  Confirm service suspension or deletion separately if required.

## Still blocked after synthetic deployment
Real Fugle API entitlement, data origin authentication and account quota;
complete audit of direct provider callers; actual BOT snapshots and current
main integration; authoritative trading calendar and listed universe;
complete independent historical ledger and corporate actions; authenticated
scheduler access. Keep all new paper fills blocked until each release gate
has been verified. Existing 08:15–13:15 and 17:00 tasks retain NT$10m baseline
and research-only temporary safety rules; do not change cadence or unlock.
Synthetic staging acceptance is not authorization for production deployment.
