// Render drawings in Chromium under CORE's sealed CSP, as the phone would.
// stdin: {"libs": "<dir with the bundled .min.js>", "pages": [{"name", "html", "data"}]}; stdout: one JSON
// list of {name, rendered, leak, violations, errors}.
// Chromium has no pisa-bundle: scheme, so the bundle is served on a local port and both the page's
// <script src="pisa-bundle://lib/…"> and the CSP's `pisa-bundle:` source name that port instead: nothing else of the
// page or its policy changes (its inline scripts still run only by their hashes). PISA_DATA is set before the
// page runs, frozen, as the phone's user script does.
const fs = require('fs');
const http = require('http');
const path = require('path');
const { chromium } = require('playwright');

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const pages = new Map();
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://x');
    const lib = /^\/lib\/([a-z0-9-]+\.min\.js)$/.exec(url.pathname);
    if (lib && fs.existsSync(path.join(input.libs, lib[1]))) {
      res.writeHead(200, { 'content-type': 'text/javascript; charset=utf-8' });
      res.end(fs.readFileSync(path.join(input.libs, lib[1])));
      return;
    }
    const page = /^\/page\/(\d+)$/.exec(url.pathname);
    if (page && pages.has(page[1])) {
      res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
      res.end(pages.get(page[1]));
      return;
    }
    res.writeHead(404);
    res.end();
  });
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch();
  const out = [];
  try {
    for (const [i, item] of input.pages.entries()) {
      const html = item.html
        .split('pisa-bundle://lib/').join(`${origin}/lib/`)
        .replace(/(Content-Security-Policy" content="[^"]*)/, (m) => m.split('pisa-bundle:').join(origin));
      pages.set(String(i), html);
      const context = await browser.newContext();
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(String(e.message).slice(0, 200)));
      await page.addInitScript((data) => {
        const freeze = (v) => {
          if (v && typeof v === 'object') { Object.values(v).forEach(freeze); Object.freeze(v); }
          return v;
        };
        Object.defineProperty(window, 'PISA_DATA', { value: freeze(data), writable: false });
        window.__violations = [];
        document.addEventListener('securitypolicyviolation', (e) => {
          window.__violations.push(`${e.violatedDirective} ${e.blockedURI}`.slice(0, 200));
        });
      }, item.data || {});
      await page.goto(`${origin}/page/${i}`);
      let rendered = null;
      for (let t = 0; t < 200; t++) {
        rendered = await page.evaluate(() => document.documentElement.dataset.rendered || null);
        if (rendered) break;
        await page.waitForTimeout(100);
      }
      await page.waitForTimeout(300);
      const leak = await page.evaluate(() => document.documentElement.dataset.leak || null);
      const violations = await page.evaluate(() => window.__violations);
      out.push({ name: item.name, rendered, leak, violations, errors });
      await context.close();
    }
  } finally {
    await browser.close();
    server.close();
  }
  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => {
  process.stderr.write(String(e && e.stack));
  process.exit(1);
});
