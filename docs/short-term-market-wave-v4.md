# Whole-market coverage and wave holding v4

User-approved intent: more candidates without a six-stock cap; inspect all listed/OTC ordinary equities, rank clearer opportunities first, and hold the wave until trend reversal rather than exiting on every short support touch. Institution activity remains annotation-only, with no price ceiling or lot gate.

## Coverage and persistence

Keep whole-catalogue lightweight quotes to prioritize patterns, sectors and price bands. Append every other ordinary-equity catalogue symbol to the detailed queue, including preliminary rejects and missing-quote names; preliminary ranking affects order only, never removes them from this pass. Each job evaluates <=80 symbols and has a 120-second admission/time budget. Queue the next batch through the existing single-worker executor, yielding behind other background jobs. This adds no worker/service or paid API.

Persist the cycle date/mode, queue, cursor, symbol metadata, distinct evaluated codes, unique source-failure codes and merged qualified rows after every batch. A restart or paused resource budget resumes the saved cursor rather than repeating the same first 80. Save failures keep the previous checkpoint. A total source outage does not advance the checkpoint. A paused/partial pass is explicitly partial. Completion means all symbols were attempted; source failures remain separately counted, never filled with fabricated data. Up to16 existing modeled positions per continuation batch receive a rotating refresh fast lane, sharing the same <=80 cap.

Individual source timestamps still gate live freshness. Old quotes are not made current by completing another batch. Cached valid results remain visible during temporary resource deferral; outdated/mismatched-rule results remain unconfirmed. Intraday/close and day boundaries have independent cycle metadata. Completed close passes do not restart every30 minutes; manual and existing schedules can request refresh. The Radar cadence is a check/resume cadence, not a claim that an entire multi-batch market pass finishes every10 minutes.

## List and opportunity stages

API uses offset/limit pagination (20 default, max50 per response), total/has_more metadata and filters applied before pagination. The stored candidate set has no six/20-stock cap; all qualified symbols are accessible across pages. Sort by opportunity stage, trend clarity, remaining checks and normalized technical priority, without a five-factor score or institutional lots. Keep an expanded-only coverage/price-band diagnostic.

Distinguish current watch, pattern-triggered, and formal entry counts. A watch is not a buy; a pattern trigger may still lack acceptable risk space. Record causal watch/trigger onset from the replay prefixes, avoiding labeling the first available warmup observation as a new formation. Show actual newly observed current-session formations separately. Do not invent daily buy signals just to increase counts.

## Wave exits

Version `short-wave-v4` uses independent close/:intraday snapshot keys; old pattern-v3 snapshots remain untouched. This is a model-rule change, not actual account trading.

Initial trend support uses the prior10-bar low minus0.25ATR (a continuation's compact base uses five prior bars). The separate initial hard stop is below that support and the confirmed swing floor with an ATR buffer. Initial hard-stop downside must remain <=6% and reward/risk >=1.5; no wider risk is hidden to create trades.

A single intraday touch of trend support does not liquidate the position. A close below support with short-term weakness, or two closes below MA20, can reduce half. Merely touching a target while price remains strong does not force a reduction. Confirmed reversal exits require two successive closes below the previously known trend support and MA20, with MA20 flat/falling. The separately known hard stop still exits immediately on touch (gap reference uses lower open, never a fill guarantee).

Trend support trails on a ten-bar basis. Hard stops ratchet only using confirmed structure and remain below trend support with a buffer; all changes apply next session. After a reduction, regaining support and a fresh ten-bar breakout can restore reduced units once, only above entry with volume confirmation and combined downside within the original risk budget. Cost/unit tracking is reduced proportionally for that budget. The user-requested displayed per-share price difference remains initial entry to final exit, independent of units/fees.

UI names trend support and hard stop separately. Preserve dated reasons, historical versus current arrows, institution-only positive annotation, privacy/authentication and all other BOT pages. No database schema migration, portfolio write, real order or LINE message.

## Verification and rollback

Cover170-symbol multi-batch accumulation, persisted cursor/restart, partial time budget, admission/save failures, full pagination47 rows, rank order, institution invariance, support-break recovery without liquidation, two-close trend reversal above hard stop, risk-budgeted restoration, gap hard stop and no future-bar leakage. Test Chromium/WebKit current/history, paging, staged notices and existing full UI regressions. Production must verify progress beyond80 and honest completion/failure counts.

Deploy via existing main auto-deploy. Revert code to roll back; the old v3 keys are retained, with no destructive migration or real account changes.
