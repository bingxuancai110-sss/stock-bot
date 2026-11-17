# Portfolio visual feedback

The holdings view has a high-contrast selected tab, a persistent neutral selected-stock edge, and a restrained neutral press state. Transparent disclosure hit targets remain transparent so they cannot cover stock names or quotes. Native disclosure behavior and the existing height transition remain intact; detail content clarifies with a 180ms opacity animation, disabled for reduced motion.

Expanded holdings group cost, value and return data into a two-column surface with a full-width source row. A repeated weight bar is omitted because weight remains in the detail grid and allocation donut. Buy and sell actions have separate equal-width 48px buttons; stock details uses a separate full-width link. Research disclosures share a consistent 48px target and panel treatment. Existing financial colors, privacy behavior, trade confirmation cards, and chart gestures are unchanged.

Changes: new scoped `static/portfolio-polish.css`; one stylesheet reference in `main.py`. No API, database, calculation or JavaScript runtime changes.

Validation: Chromium and WebKit each passed 16 visual/layout checks and 214 shared interaction checks. Coverage includes 320/390/1280px layouts, financial values remaining readable while pressed, transparent hit targets, usable buy/sell controls, changing selected holdings, reduced motion, and existing transaction/navigation behavior. Writes were intercepted fixture requests. Physical device testing remains separate.

Deploy by merging into main and verifying the matching Render deploy is live. Check the authenticated mobile holdings page, disclosure state and trade entry. Roll back by reverting the commit; no data rollback is required.
