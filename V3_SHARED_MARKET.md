# V3 shared market engine — test branch only

No code in main.py is changed. The existing BOT startup behavior remains the default.
Do not enable the new engine in production until the gates below are satisfied.

## Wiring contract (future opt-in host integration)

1. Use one PostgreSQL database and one namespace per provider-key quota scope.
   Run StateStore.init_schema() only as an approved migration. Supply fresh dedicated
   non-autocommit DB connections, a synchronized Asia/Taipei clock, a trusted exchange
   trading-calendar predicate, and a server-side HTTP client.
2. Construct Engine(store, session, api_key, now=..., is_trading_day=...).
   Pass engine.collector('bot') as shared_collector to
   fugle_background.start_server_service. The existing environment key/owner checks
   and owner-specific snapshot storage remain unchanged. Without that optional
   argument the legacy Collector remains in use.
3. Feed set_pool with verified same-day active exchange symbols, V3 ledger holdings,
   and ranked candidates from the BOT research pipeline. The pool is capped at 200.
   Candidates not in the supplied active list are dropped; unknown holdings fail closed.
4. Construct DynamicMarket(engine) and explicitly register_v3_market_api(app, market,
   token=...). Schedule market.refresh() internally; HTTP reads never call Fugle.
   Each refresh handles up to 20 symbols, including up to 10 prioritized holdings.
5. All other consumers of the same Fugle API key must route through the quota gate
   or be given a separately verified budget. A separately configured namespace,
   direct provider client, browser, or external process can bypass this gate.

## Proposed limits (not proof of actual plan)
Rolling 60 seconds: BOT 30, V3 20, combined 50, leaving 10 of a hypothetical 60.
The state database serializes reservations and caches public quotes, not account
positions or tokens. 429 triggers a common cooldown; 401/403 trigger credential
cooldown except index-specific 403. Fresh cache reuse is 45 seconds; V3 rejects
trade/observation age >180 seconds during the session. Final same-day closes
require the provider close flag. Prior-day prices require a separate historical
endpoint; this intraday engine intentionally does not relabel them as current.

## Release gates
- Complete Flask, PostgreSQL, and actual Fugle parser CI results.
- Audit all existing direct Fugle callers and verify account quota before activation.
- Wire verified listed-symbol feed and official holiday calendar; no synthetic symbols
  in real trading. Wire V3 independent ledger and approved scheduler credential.
- Check shared DB clock synchronization, request lease/timeout behavior and load,
  production BOT cadence, process restart, and error propagation.
- Validate true provider quotes, authenticated scheduled HTTP reads, and paper ledger
  consistency in a separate authorized environment.
- No production migration, secret changes, deployment, or main merge in this branch.
