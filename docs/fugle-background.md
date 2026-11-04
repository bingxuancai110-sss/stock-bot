# Private Fugle background collection

Status: implementation under validation; not yet activated in production.

## Deployment configuration

Set `FUGLE_BACKGROUND_API_KEY` in the existing Render service's protected
environment-variable settings. Set `FUGLE_TEST_USER_ID` to the same authorized
owner's existing BOT user ID. Do not put the key in chat, source control, a URL,
logs or shared quote snapshots. Do not copy a key out of browser storage.
Missing either value leaves the current browser-direct mode intact.

The new module does not create paid Render resources or change the compute
plan. It runs one lightweight timer in the existing web process, every 60
seconds, independently of page requests. A suspended/free Render service cannot
run Python timers. The existing external health keep-alive must continue to
wake/keep the service available during trading hours. This is not a guarantee
of continuous availability on a free instance.

## Behavior

- Current owner holdings only, through the existing positions/cost logic.
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
- The frontend reads snapshots every 15 seconds while visible. It does not open
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

Before activation verify server secret ownership, deploy the reviewed commit,
observe an actual successful background cycle with the App closed, then open
the owner's authenticated page and check source/trade/fetch timestamps. Test
foreground/background and position changes without submitting real trades.
Do not claim active background collection until that evidence exists.

## Rollback

Remove `FUGLE_BACKGROUND_API_KEY` and restart/redeploy to return to existing
browser-direct mode. No schema migration is needed. Existing private quote
snapshots can remain; they are inaccessible to other users and are ignored
without the configured service. Revert the feature commit if necessary.
