# In-place trade completion

Previously the client replaced the entire app content with the lightweight POST receipt and scrolled to the top. Successful buy and sell bookkeeping now opens an accessible confirmation dialog above the existing holdings page. A separate GET refreshes the portfolio region without document navigation, a page loader, or replacing the app shell. Sorting, expanded holding, chart period, and viewport anchor are retained. The receipt waits for the user to dismiss it.

The committed POST outcome is independent of the subsequent read. Failed synchronization leaves success explicit and offers a GET-only retry. The original successful form remains locked until refreshed data supplies a new form. Retry remains disabled during reads and does not disappear under a rapid second tap. Late responses cannot replace a different page. Unknown or rejected POST responses retain the existing safeguards and do not display success.

No backend trade, cost, tax, user-isolation, idempotency, database or API contract changes. The no-JavaScript receipt remains available. New static assets: `trade-completion.js` and `trade-completion.css`; `main.py` loads the assets and delegates confirmed outcomes.

Validation: Chromium and WebKit each passed 214 existing shared UI checks and 14 focused trade-completion checks. Focused coverage includes buy/sell cards, a held background read, centered mobile layout, preserved viewport and shell, fresh form after sync, retained sort/expanded stock/chart period, failed read with repeat-click retry, and late responses after leaving. All writes were intercepted fixture requests; no production trades were created. Physical iPhone verification remains outstanding.

Deploy by merging into main and waiting for the matching Render deploy to become live. Verify the new asset references and the existing holdings page. Revert this commit to roll back, then let Render deploy the revert. No data migration or database rollback is required.
