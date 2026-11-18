# Blackhorse refresh and short-term interaction audit

## Confirmed causes

Blackhorse had a30minute due interval but its scheduled entry was only reached when Radar was younger than7minutes. The existing Radar cron ran every10minutes, so Blackhorse could be starved. Warmups at12:00,16:00,20:00Taiwan time explicitly logged Blackhorse memory skips on2026-11-18. Its old280–290MiB reservation assumed an entire180-name historical-price batch; ordinary process memory already exceeded the admission threshold. Blackhorse source payloads also did not mark existing old snapshots stale, and the visible page omitted its calculation time.

Short-term had no retry timer after a failed response, could apply a response for an obsolete filter, rerendered the entire panel for progress-only polls, retained out-of-range pagination after the candidate count shrank, and counted existing held models as watches. A single malformed lightweight timestamp could abort the whole preliminary pass. The short-term CSS also forced OS-dark cards even while the app explicitly selected a light canvas.

## Corrections

- Blackhorse has a dedicated scoped-OIDC workflow/endpoint independent of Radar: every30minutes09:02–13:02Taiwan weekdays; post-close checks14:12,16:12,20:12. GitHub schedule times are best effort. GET is status-only; POST starts at most one admitted job. The runner requires a current saved snapshot, retries deferred starts at most once perminute, and fails after4minutes rather than treating enqueue as completion.
- Both screeners process at most4historical symbols perbatch with at most2quote workers and retain the96MiB reserve. Prefetch lightweight official quotes once for the pool, not once perbatch. Blackhorse history requests use a5second timeout and a180second price-pass budget. A failed/incomplete save does not replace the last saved in-memory result. Candidate quotas, score weights and existing filters remain.
- Separate actual institution date, quote date/range, unknown quote count and computation timestamp in metadata/UI. Quote date comes from source observation time or historical bar dates. Blackhorse observation/index date is calculation date so today's prices cannot be written into yesterday's institution-date score history. Existing historical rows are untouched; no schema migration.
- Short-term retries transient errors after15seconds, discards outdated-filter responses, retains pending force refreshes, clamps pagination, excludes held positions from watch totals, and skips malformed lightweight timestamps while rejecting duplicate/out-of-order dates.
- Metadata-only polling preserves the actual SVG/card nodes. Unchanged cards survive list updates; interaction/scroll pauses defer background rendering. Preserve crosshair date, focus and the expanded-card scroll anchor. Reset navigation when choosing a different stock. Add keyboard crosshair navigation.
- Layout: app-theme surfaces, three-column opportunity summary, compact filters/state tabs, restrained stock cards, explicit waiting conditions in place of premature stop/target grids, two desktop columns and full-width expanded chart. No extra framework.
- Scroll fade/short entrance effects are scoped to the short-term panel; first readable content stays immediate, interaction/focus reveals text immediately, reduced-motion preference leaves all content fully visible. Pressed cards retain text colors and use a subtle surface/border response. Charts fade in on opening, not on progress-only polls.

## Verification and release

Backend tests include independent endpoint auth/status,30minute and post-close freshness, real source quote dating, unchanged candidate coverage with bounded batches, persistence failure preservation and no backdated quote observations; short-term tests cover malformed timestamps, clamped pages, held/watch counts and the existing7791/causal wave cases. Chromium/WebKit cover stable SVG identity, keyboard reading, scroll fade, reduced motion, retry recovery and Blackhorse date/update indicators. Run full shared UI regression as well.

Deploy via existing main auto-deploy. After it is live, manually run the new Blackhorse workflow and inspect both saved source timestamps and production UI; this is necessary to prove today's snapshot actually changed. A nighttime close refresh does not prove next-session intraday timeliness. Roll back by reverting this release including the new workflow; no account/trade changes or destructive migration are involved.
