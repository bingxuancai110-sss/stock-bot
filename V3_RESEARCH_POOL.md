# V3 BOT research snapshot adapter (test branch only)

This is a research-priority adapter, not an order generator. No main.py wiring,
production migration, scheduler update or live deployment is performed.

## Host wiring contract

Call v3_research_pool.load_and_refresh explicitly from the future approved
scheduler. Pass the same V3 shared market Engine, a verified active TWSE/TPEx
catalogue for today, independent V3 ledger holdings, the actual last confirmed
close date, the most recent published institutional data date, and the session
flag derived from the official calendar. No guessing dates, no use of personal
watchlist or holdings.

Supply existing saved-snapshot readers:
- screener_reader = _load_persisted_screener_snapshot (blackhorse, radar)
- live_radar_reader = _load_recent_live_radar_snapshot
- chips_reader = build_chips_payload (adapter passes allow_compute=False,
  persist=False)
- short_store = _short_term_service.store or its equivalent read-only store;
  short_key and short_version = short_term.KEY and short_term.SELECTION_VERSION
  (observed version 15 in base commit). Read KEY and KEY:intraday directly,
  not ShortTermService.payload, which may enqueue quote refreshes.

Blackhorse and five-factor evidence share one source. No score recalculation.
Radar live observations must be same-day and at most 10 minutes old; prior
confirmed close radar is labeled separately. Blackhorse live snapshots must
be at most 40 minutes old. Chips must match the actual latest published
institutional date. Short-term live snapshot and row observation must be at
most 20 minutes old; version and session must match. Long/short and historical
entry/add/reduce/exit/hold/wait remain distinct. Prior-close research is not
an executable intraday signal.

Research priority sorts by number of distinct source families, then sum of
1/(1+rank/10) using the best rank per source, then code. This is a provisional
research queue heuristic, not validated alpha or expected return. Multiple
chip groups do not multiply source count; conflicting directions are exposed.
Holdings stay first, with total pool capped at 200. Refreshing the pool
preserves rotation position if a prior remaining symbol survives. No valid
research evidence means no new ranked pool; the previous pool is filtered
against the verified active catalogue and current V3 holdings.

## Outstanding release gates

The adapter was tested using fixture schemas and isolated SQLite; it has not
read production snapshots end-to-end. Verify current main snapshot contracts
(the full main.py was too large for the GitHub connector; the schema was
inspected in the user's uploaded main (3)(1).py and short_term.py from base).
The host must provide the authoritative catalogue and dates and persist/serve
research provenance independently of live quote cache. Confirm actual CI
results, PostgreSQL locking, Flask endpoint authentication, actual provider
quota, and scheduled authenticated reads before activating. No live prices,
short execution or simulated orders are produced by this module.
