# Restore radar candidate coverage

The previous recovery release incorrectly treated four as both the total candidate cap and the price-download batch size. It produced a two-row result, including ETF 0050 in the equity source. A successful API response was therefore insufficient acceptance evidence.

This change restores the existing 240-candidate depth after the broad lightweight market pass, excludes ETFs from the equity universe before ranking, and filters ETFs defensively when normalizing saved rows. It does not change factor weights, liquidity thresholds, ranking formulas, trade records or portfolio values.

History quotes are consumed lazily in batches of four with at most two workers, rather than retaining every pool quote in one local map. The shared quote cache retains its existing bounded LRU. Initial 200 MiB headroom and single background executor remain; each new batch also checks 120 MiB headroom. If a batch fails the guard, no partial result is published. Blackhorse still uses its original bulk call and worker settings.

New radar diagnostics record scan policy version 2 and the configured depth. Older four-candidate or ETF-contaminated snapshots are rejected for rebuilding, without deletion or schema migration. The page polls queued scans for up to eight minutes so the larger candidate pass can complete in the background. A completed result still reports candidate coverage rather than claiming every listed stock received deep analysis.

Tests exercise the real producer, iterator, universe filter, snapshot policy and complete compute function with isolated inputs. They verify all 240 selected candidates are visited in 60 bounded batches, ETF exclusion, laziness, memory rejection without snapshot replacement, preserved Blackhorse concurrency and rebuild of old snapshots. Market data in tests is synthetic only; production verification must use the real website and saved real scan output.

Rollout: merge the change to the existing main auto-deploy branch, wait for Render live, then open the real radar and check coverage, returned rows, ETF absence, detail interaction and memory metrics. Roll back by reverting this commit; no database migration is required. The former four-candidate cap returns after rollback.
