# Personal hybrid quote lab

`/web/quote-lab` is an isolated, owner-only trial of Fugle's free stock-trades stream plus rate-limited single-stock REST quotes. It does not replace homepage prices, alter portfolio calculations, persist market data, or subscribe to paid services.

## Enable for one account

Set one service-side access configuration:

- `FUGLE_TEST_USER_ID`: exact existing BOT user ID. Takes precedence when nonempty.
- Otherwise, `FUGLE_TEST_USER_EMAIL`: exact Google login email, case insensitive. The page resolves the existing `user_auth_identities` Google binding. Missing or ambiguous bindings and database failures deny access. It does not create, merge or modify identities.

With neither setting, the route is disabled (404). Unauthenticated requests use the existing login protection; other users receive 403. Responses are private/no-store. Do not put the account email in source control.

The existing Render service uses service environment variables. Patch only these settings; preserve all other variables. When enabling by email, leave the ID setting empty. There is no database migration or new dependency.

## User workflow

1. Register a free Fugle account and request a market-data API key at <https://developer.fugle.tw/docs/key/>.
2. Sign into the configured BOT account and open `/web/quote-lab`.
3. Enter the key locally and start with automatic BOT holdings tracking enabled (default). Up to five held symbols use the `trades` stream; the remaining holdings use direct Fugle Intraday Quote requests. The input specifies up to five preferred stream symbols. Existing eligible slots stay stable; vacancies are filled from preferred held codes then the stored position order. Disable tracking to retain the original manual five-symbol trial.
4. Stop before changing manual preferences. In tracking mode, the owner-only GET `/web/quote-lab/holdings` endpoint refreshes membership every 15 seconds, merging positive lots by code. Fully sold codes are removed, partial reductions stay, and new codes appear automatically. Portfolio-query failures return 503, not an empty portfolio. An empty successful portfolio stops and clears the key. Changes known only to a broker are not visible until recorded in BOT. The key stays in memory when a slot changes while the page remains active. Replacement waits for unsubscribe acknowledgement before subscribing a new symbol, so there is never a temporary sixth stream. Stop before changing symbols. Stop, backgrounding and navigation close the socket and clear the key reference and field. Foregrounding requires entering the key again. Supported browsers use Web Locks to prevent this lab from opening another connection in a second tab; provider connection limits still apply across devices and other programs.

The key goes directly from the browser to `wss://api.fugle.tw/marketdata/v1.0/stock/streaming` and, in the `X-API-KEY` header only, to `https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{symbol}`. Fugle CORS preflight for this application origin was verified. The BOT holdings request sends only the existing BOT login cookie/token, never the Fugle key. Provider requests omit credentials, referrer and redirects; abortable REST requests are cancelled on stop or navigation. It is never sent to this application's backend, stored in Render, included in URLs, printed, or persisted in browser storage. The page is GET-only and contains no key-submission form. Stop removes application references; browser/network internals remain subject to browser behavior.

REST requests rotate serially through non-stream holdings, targeting about 15 seconds per stock when capacity allows. A minimum 1.5-second spacing and a rolling limit of 40 requests per minute leave headroom below the free tier's 60/minute allowance. More holdings and network latency slow the cycle; holdings are not truncated. A provider 429 pauses REST for at least 60 seconds and honors a longer Retry-After without stopping the stream. Other programs using the same key still share provider limits. HTTP 401/403 stop the session. Transient failures preserve the last value with an explicit note.

REST uses `lastTrade.price/time` or confirmed `closePrice/closeTime`, never trial-inclusive `lastPrice`. REST observations do not increment the stream trade counter, and trade age is not labelled transport latency. Name/price fields only update existing rows; the full list is not rebuilt on each holdings refresh.

Network outages retry at 1, 2, 4, 8, 16 and 30 seconds, then stop if still unsuccessful. WebSocket credential, entitlement or subscription-limit errors stop without retry; REST 429 uses the pause described above. Reconnection authenticates and subscribes again. A heartbeat watchdog prevents a dead connection remaining marked live. Authentication/setup has a 15-second timeout.

## Reading the result

- Latest trade price and change from the previous received quote are shown separately from daily return. Red is up and green is down.
- Snapshot messages do not count as new trades. Trial matching prices never overwrite confirmed trade prices. Old/duplicate messages are ignored.
- Fugle `time` is Unix microseconds. The UI shows market and browser-receipt timestamps in Taipei time. Their difference is an observed clock gap, not a guaranteed end-to-end latency. Negative gaps indicate device-clock mismatch. Snapshot age is not labelled transport latency.
- Closed markets may provide only a closing snapshot. Validate actual update speed during a trading session with your own key. No real-key latency claim can be made from simulated tests.

References: [Fugle WebSocket](https://developer.fugle.tw/docs/data/websocket-api/getting-started), [Trades](https://developer.fugle.tw/docs/data/websocket-api/market-data-channels/trades), [pricing](https://developer.fugle.tw/docs/pricing/), [usage terms](https://developer.fugle.tw/docs/data/intro/). Third-party display/redistribution is outside this personal trial and requires separate confirmation.
