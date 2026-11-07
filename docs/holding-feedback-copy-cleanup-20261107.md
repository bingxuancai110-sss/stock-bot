# Holding feedback and explanatory-copy cleanup — 2026-11-07

The user paused the proposed brokerage redesign and requested the existing live
layout be retained. That exploration is saved only in the local branch
`codex/brokerage-experience-20261107` at `98fdade`; it is not part of this release.
This branch starts from production PR #88 (`55ccc9a`).

## Reproduced defect

In the authenticated production holdings page, expanding the first holding made
the name and price disappear behind a blank 70px area. The summary remains an
absolute hit target over that area after expansion, but the previous transparent
override covered only `:not([open])`. The generic hover/press rule therefore
painted a solid surface on the expanded hit target. There was no numeric tween
active in the observed failing card.

Keep the summary's background transparent for both open and closed states and
all interaction states. Preserve the whole-card feedback and existing mobile
transparent summary text, and preserve desktop disclosure text. No holding,
return, price-source, transaction or data-fetch behavior changes.

## Copy cleanup

Hide only selected repetitive helper blocks in the existing presentation layer:
home contribution/shortcut instructions; holding/allocation instructions; target
allocation subtitle; static screener intro/disclaimer and detail subtitles;
ranking chart descriptions; market welcome/drag instructions; tool-directory
descriptions; static rotation section introductions; and repeated global footer.

Do not apply a global rule to `small`, paragraphs, source labels, or numeric text.
Keep live status, dates, data sources, missing-data warnings, fees, transaction
confirmation and errors, risk evidence and optional methodology disclosures.
No buttons, routes or data fields are removed. The CSS asset version is
`20261107-1`; refreshing the original page loads it.

## Verification

Use the original actual-renderer fixtures (not the paused redesign fixtures).
Add explicit checks for expanded hover/press transparency and retained name
color, hidden helper blocks, and visible source/date/quality information.
After deployment, repeat the name/quote expansion path in the authenticated
production browser and confirm no brokerage assets are loaded.

Chromium and WebKit each passed 213 isolated actual-renderer checks with no
page errors. The checks include the expanded-hover and expanded-press regression,
unchanged original layout, privacy, chart tracking, form confirmation and error
states, plus retained market source/date information after copy cleanup.

Rollback: revert this UI-only commit. No database migration or external-data
change accompanies the release.
