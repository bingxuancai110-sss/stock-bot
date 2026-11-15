# Radar background recovery and desktop alignment

The four-candidate cap described below is superseded by [radar-coverage-batches.md](radar-coverage-batches.md), which preserves the 240-candidate depth while bounding each history-download batch.

## Observed production failure

The signed-in production screener and the user's phone both showed radar rebuilding without results. Render logged rejected radar work due to memory reservation while the source API still returned HTTP 200 with building=true. Container memory was approximately 274 MiB of a 512 MiB limit. A 280/290 MiB reservation therefore prevented background admission. Off-hours page-triggered work also omitted the scheduled deep-candidate limit, leaving a possible 240-candidate request behind that reservation.

## Fix

- Default all page-triggered radar background work to the existing four-candidate scheduled depth, including weekends. Normalize zero/negative limits to the engine's minimum and cap light-producer price workers at two.
- Reserve 200 MiB for this bounded path, retaining the larger reserve for explicit full scans and other modes. Warmup and live radar use the same light budget. Keep the memory guard and single shared executor; recheck headroom when dequeued.
- Release running state on success/failure, reject unsupported empty results without scan evidence, and apply a 60-second failure cooldown. Do not confuse memory rejection with queued/running work.
- Source API and initial metadata expose deferred status; the page stops indefinite waiting and clears the source spinner on completion/defer/failure. Limited deep scans are identified as candidate samples, not full-market rankings.
- Desktop source buttons use readable type and hit areas. At wide desktop widths, quote rows align existing name/price/score/institution/signal fields into columns. Preserve narrower layouts and all existing detail actions. CSS version 20261115-3.

Scoring formulas, candidate-bucket order, manual 240-candidate analysis, transaction writes, database schema and LINE reply protocol are unchanged. This does not guarantee a nonempty candidate list: valid zero-candidate results remain distinct from failed or incomplete market data.

## Verification and rollout

Focused tests use actual extracted producer/API functions, without importing main or starting market jobs. Cover bounded defaults, full-scan reserve, enqueue-time memory changes, failures/cooldown, duplicate requests, scan evidence, API status and candidate-scope metadata. Actual-renderer browser tests cover deferred/ready radar, spinner cleanup and desktop alignment alongside existing UI/navigation regressions.

After deployment, open the real radar tab and confirm a saved snapshot or a specific failure state, checking Render job logs and memory without leaking user tokens. Revert this change to roll back; no data migration is needed.
