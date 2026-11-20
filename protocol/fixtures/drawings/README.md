# Shared drawing fixtures (the library contract)

One page per bundled library, in the `drawing` object's own shape (`{drawing_id, title, summary, kind, html, data}`),
written to CORE's sealed CSP. Each sets `document.documentElement.dataset.rendered = 'yes'` once it drew (or
`'error: …'`). Both lanes render them in a test: the phone in `PisaTests/CanvasAppTests.swift`
(`eachLibraryRendersUnderTheSealedCSP`), CORE through its check and seal, and in Chromium.

The contract:
- a page loads a library as `<script src="pisa-bundle://lib/<file>">`, files and versions as the app pins them: `d3.min.js` 7.9.0, `mermaid.min.js` 12.2.0, `vega.min.js` 6.4.0,
  `vega-lite.min.js` 6.5.0, `vega-embed.min.js` 7.3.0, `vega-interpreter.min.js` 2.3.2;
- no eval anywhere: Vega-Lite is embedded with `{ast: true, expr: vega.expressionInterpreter, actions: false}`;
- `PISA_DATA` is frozen: a library that changes its input gets a copy (`JSON.parse(JSON.stringify(…))`);
- Mermaid renders from the element's text (`el.textContent = …; mermaid.run({nodes: [el]})`), `securityLevel:
  'strict'`.

CORE hashes inline scripts into the CSP (no `'unsafe-inline'` for scripts), so these pages are
**resealed by CORE** (`pisa.domain.drawing.seal`): each one's meta CSP carries its scripts' `sha256-` hashes. CORE's
tests check that each page passes the check, that `seal(unseal(html))` gives it back byte for byte, that its
libraries are the pins above, and that it draws in Chromium under that CSP (`tests/test_core_canvas1.py`,
`test_each_library_draws_under_the_sealed_csp`; the bundle from `PISA_CANVAS_LIBS` or the iOS tree). A page edited
here must be resealed by CORE's `seal`, or the hashes no longer match and its scripts don't run.
