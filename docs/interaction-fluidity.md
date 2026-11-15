# Interaction fluidity

Production inspection confirmed the existing portfolio disclosure and its transparent phone hit target. The code still applied a fixed 170 ms pressed timer to every pointer-down, including the start of a scroll; shrank rows and navigation; rescanned all UI controls/charts/disclosures after quote text mutations; and observed every disclosure size to restart height animations after async content changes.

Changes:
- Track a single pointer press, clear it after an 8 px movement, native scroll, pointer cancellation, blur, page departure or hidden document. Preserve visible feedback while holding, a short minimum response for quick taps and a two-second recovery timeout. Cancel old release timers before a new press.
- Use a neutral surface and inset border, without scaling rows/navigation or recoloring financial values. Gate hover to fine pointers with hover support. Keep the existing transparent portfolio hit-target protection. Respect reduced motion durations.
- Coalesce quote mutations by their actual number host. Update those numbers only; retain full initialization for new DOM controls and existing privacy masking. Do not add new network requests.
- Animate explicit disclosure toggles, cancelling an in-flight animation before measuring a rapid next toggle. Remove the per-disclosure ResizeObserver and its repeated layout reads/async height animations.

Validation uses actual rendered pages with intercepted test data, including gestures, rapid press/release timers, quote color/geometry, localized number updates, new-control initialization, nested disclosures, reduced motion, charts, navigation and transaction protections. No production transaction is submitted during acceptance. Production checks must verify the new asset versions and read-only tap/scroll/expand flows after Render is live. This is a presentation/DOM-work improvement, not a claim that backend response time or phone frame rate was measured.

Shared CSS v20261115-5 and JavaScript v20261115-2. No schema, price source, scoring, portfolio or transaction changes. Roll back by reverting the release commit and restoring its asset references.

Desktop action labels also retain at least 4.5:1 text contrast during hover in Chromium and WebKit. Final validation: each engine passed 214 existing UI, 15 navigation, 23 presentation and 20 focused interaction checks (272 checks per engine).
