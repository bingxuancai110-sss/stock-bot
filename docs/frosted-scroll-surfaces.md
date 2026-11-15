# Frosted scrolling surfaces

The user clarified that the desired upgrade is artistic treatment: soft shadows, frosted surfaces and blurred edges as content scrolls. This release adds that treatment without further typography or layout changes.

On phone widths, the scrolled sticky header uses a 74% theme-colored surface with 18 px backdrop blur and a soft shadow. Its lower 34 px edge fades with a gradient mask and 10 px blur. The bottom navigation uses a 22 px frosted backdrop; its upper 40 px edge has a masked 12 px blur. The top fade disappears at the document start, and the bottom fade disappears at its end. All edge pseudo-elements ignore pointer events.

Blur is bounded to viewport chrome and its edge strips. Data rows and the document receive no filter; native scrolling is unchanged. Scroll updates only coalesce reads into a frame and toggle boundary classes. Blur radius is not animated or recalculated per row. A reduced-transparency preference uses opaque surfaces and removes edge effects; unsupported backdrop filtering also falls back to an opaque surface. Reduced motion disables fade transitions.

Verification uses actual-renderer fixtures with intercepted synthetic data. Chromium and WebKit checks cover the visual surfaces, masks, boundary fades, unchanged document geometry, click-through edges, no page/quote filter, overflow, navigation and existing UI/transaction protections. Preview screenshots contain test companies, not real portfolio performance. Check the real deployed site at phone size after Render is live. Physical-device frame rate is not asserted.

Shared experience CSS/JS version 20261115-2. No database, API, scoring, quote source or transaction change. Revert this release to restore the preceding opaque chrome.
