# Personal quote lab

`/web/quote-lab` is an isolated, owner-only trial of Fugle's free stock-trades stream. It does not replace homepage prices, alter portfolio calculations, persist market data, or subscribe to paid services.

## Enable for one account

Set one service-side access configuration:

- `FUGLE_TEST_USER_ID`: exact existing BOT user ID. Takes precedence when nonempty.
- Otherwise, `FUGLE_TEST_USER_EMAIL`: exact Google login email, case insensitive. The page resolves the existing `user_auth_identities` Google binding. Missing or ambiguous bindings and database failures deny access. It does not create, merge or modify identities.

With neither setting, the route is disabled (404). Unauthenticated requests use the existing login protection; other users receive 403. Responses are private/no-store. Do not put the account email in source control.

The existing Render service uses service environment variables. Patch only these settings; preserve all other variables. When enabling by email, leave the ID setting empty. There is no database migration or new dependency.

## User workflow

1. Register a free Fugle account and request a market-data API key at <https://developer.fugle.tw/docs/key/>.
2. Sign into the configured BOT account and open `/web/quote-lab`.
3. Enter the key locally, choose up to five different listed/OTC stock symbols, and start. Each stock uses only the `trades` channel; regular-board trades only.
4. Stop before changing symbols. Stop, backgrounding and navigation close the socket and clear the key reference and field. Foregrounding requires entering the key again. Supported browsers use Web Locks to prevent this lab from opening another connection in a second tab; provider connection limits still apply across devices and other programs.

The key goes directly from the browser to `wss://api.fugle.tw/marketdata/v1.0/stock/streaming`. It is never sent to this application's backend, stored in Render, included in URLs, printed, or persisted in browser storage. The page is GET-only and contains no key-submission form. Stop removes application references; browser/network internals remain subject to browser behavior.

Network outages retry at 1, 2, 4, 8, 16 and 30 seconds, then stop if still unsuccessful. Credential/entitlement/quota errors stop without retry. Reconnection authenticates and subscribes again. A heartbeat watchdog prevents a dead connection remaining marked live. Authentication/setup has a 15-second timeout.

## Reading the result

- Latest trade price and change from the previous accepted trade are shown separately from daily return. Red is up and green is down.
- Snapshot messages do not count as new trades. Trial matching prices never overwrite confirmed trade prices. Old/duplicate messages are ignored.
- Fugle `time` is Unix microseconds. The UI shows market and browser-receipt timestamps in Taipei time. Their difference is an observed clock gap, not a guaranteed end-to-end latency. Negative gaps indicate device-clock mismatch. Snapshot age is not labelled transport latency.
- Closed markets may provide only a closing snapshot. Validate actual update speed during a trading session with your own key. No real-key latency claim can be made from simulated tests.

References: [Fugle WebSocket](https://developer.fugle.tw/docs/data/websocket-api/getting-started), [Trades](https://developer.fugle.tw/docs/data/websocket-api/market-data-channels/trades), [pricing](https://developer.fugle.tw/docs/pricing/), [usage terms](https://developer.fugle.tw/docs/data/intro/). Third-party display/redistribution is outside this personal trial and requires separate confirmation.
