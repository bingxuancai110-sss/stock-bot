# Private Fugle background collection

Status: implementation deployed behind configuration; activation requires a protected server key and a verified live cycle.

## Deployment configuration

Set `FUGLE_BACKGROUND_API_KEY` in the existing Render service's protected
environment-variable settings. The existing owner allowlist is reused: an exact
`FUGLE_TEST_USER_ID`, or a uniquely linked Google identity for
`FUGLE_TEST_USER_EMAIL`. No additional account ID is needed when the existing
email allowlist resolves uniquely. Do not put the key in chat, source control, a URL,
logs or shared quote snapshots. Do not copy a key out of browser storage.
A missing key or unresolved owner leaves browser-direct mode intact. The More
panel states which mode is enabled.

The new module does not create paid Render resources or change the compute
plan. It runs one lightweight timer in the existing web process, every 60
seconds, independently of page requests. A suspended/free Render service cannot
run Python timers. The `fugle-market-hours.yml` GitHub Actions workflow requests `/health` every
five minutes on weekdays 08:55–14:10 Taiwan time to wake/keep the existing
service available, without storing the provider key in GitHub. The collector
starts immediately on process startup and then waits 60 seconds between cycles.
GitHub schedules may be delayed or dropped and are disabled after 60 days of
repository inactivity. This is not a guarantee
of continuous availability on a free instance.

## Behavior

- Current owner holdings plus the actual `IX0001` TAIEX index, through the existing positions/cost logic. The index is excluded from holding counts and valuations.
- At most 30 REST calls per rolling minute, including failures. A cycle stops
  initiating requests after 25 seconds; individual requests have 2/3-second
  connection/read timeouts. No parallel cycles, no unbounded task queue.
- Weekdays 09:00–14:10 Taiwan time. Actual provider trade dates are validated,
  so holidays cannot manufacture today's prices. Once all positions have valid
  final closes, stop downloading for that trading date.
- A PostgreSQL advisory lock serializes collectors across processes. An
  authoritative persisted cadence check prevents consecutive instances from
  starting the same minute's update again.
- 429 backs off at least 60 seconds and respects numeric Retry-After; 401/403
  pause for five minutes. Provider bodies/headers and credentials are not logged.
- Failed/missing quotes preserve their actual previous observation time. Trial,
  odd-lot, future and wrong-day data cannot become official current trades.
- Private snapshots reuse `shared_data_snapshots`, under an owner-specific
  hashed key. They contain quote data only, never provider credentials.
- The authenticated owner-only `/web/quote-lab/background` reads completed
  snapshots, then uses current holdings and the existing accounting functions.
  It never starts a provider download. Sold positions are filtered using current
  positions, rather than trusting the preceding minute's holding list.
- The frontend reads snapshots every 15 seconds while visible and keeps server mode as its source even while the first snapshot is incomplete. It does not open
  a provider WebSocket, read/migrate a saved device key, or duplicate provider
  polling in server mode. Browser-direct mode remains the fallback when server
  configuration is absent.
- Trusted server snapshots may reuse the same day's confirmed final close
  after the browser's normal 120-second observation window; original trade and
  fetch times are retained. Browser-submitted observations keep the old limit.

## Validation and activation gate

Core/provider transport, quota, timeouts, snapshots, owner isolation and API
tests are isolated from real accounts. The browser probe executes the actual JS
and panel renderer, intercepts all transport, and rejects provider connections.
Neither constitutes real account/provider verification.

Before activation configure `FUGLE_BACKGROUND_API_KEY` using Render’s Environment
page (Save, rebuild, and deploy), verify server secret ownership, deploy the reviewed commit,
observe an actual successful background cycle with the App closed, then open
the owner's authenticated page and check source/trade/fetch timestamps. Test
foreground/background and position changes without submitting real trades.
Do not claim active background collection until that evidence exists.

## Rollback

Remove `FUGLE_BACKGROUND_API_KEY` and restart/redeploy to return to existing
browser-direct mode. No schema migration is needed. Existing private quote
snapshots can remain; they are inaccessible to other users and are ignored
without the configured service. Revert the feature commit if necessary.

## Continuity and recovery

The browser compares completed-view timestamps with the data already visible.
A retained previous-day snapshot, an older complete response, or a late legacy
response must not overwrite a newer displayed view. The index timestamp is
checked independently so an incomplete batch cannot roll back the index first.
Partial updates keep the current values with an updating notice.

Before private snapshot reads and collector leases, pooled connections are
probed and a failed connection is closed and replaced, with one bounded retry.
The idempotent snapshot upsert also retries once without changing original
quote observation times. A temporary snapshot read failure may serve the last
in-memory snapshot as `storage_unavailable`; it does not pretend to be a new
fetch. Logs include counts and SQLSTATE only, never provider keys or prices.
