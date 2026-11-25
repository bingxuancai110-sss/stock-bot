// Mobile tabs rendered from the normal templates with controlled network responses.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const playwright = require(process.env.STOCKBOT_PLAYWRIGHT_PACKAGE || 'playwright');

const repo = path.resolve(__dirname, '../..');
const fixtures = process.env.STOCKBOT_UI_ARTIFACTS;
const engine = process.argv[2] || 'chromium';
const pages = { '/web/positions': 'positions', '/web/more': 'more', '/web/leaderboard': 'rank' };

async function run() {
  const browser = await playwright[engine].launch({ timeout: 15000 });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const errors = [];
    const fragments = [];
    const holdPaths = new Set();
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://fixture.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/static/')) {
        const asset = path.join(repo, url.pathname.slice(1));
        return fs.existsSync(asset) ? route.fulfill({ path: asset }) : route.fulfill({ status: 404 });
      }
      if (!pages[url.pathname]) return route.fulfill({ json: { ok: true, rows: [], data: [], market_open: false } });
      const fragment = url.searchParams.has('fragment');
      if (fragment) {
        fragments.push(url.pathname);
        if (holdPaths.has(url.pathname)) return;
      }
      const name = pages[url.pathname] + (fragment ? '-fragment' : '') + '.html';
      return route.fulfill({ path: path.join(fixtures, name) });
    });

    await page.goto('https://fixture.test/web/positions');
    await page.waitForFunction(() => window.StockBotUI && window.StockBotExperience);
    const nav = page.locator('.app-bottom-nav');
    const positions = nav.locator('a[href="/web/positions"]');
    const more = nav.locator('a[href="/web/more"]');
    const rank = nav.locator('a[href="/web/leaderboard"]');
    const content = page.locator('#app-page-content');
    const dock = nav.locator('.bottom-inner');
    const header = page.locator('.app-header').first();

    const dockAtRest = await dock.boundingBox();
    assert.ok(dockAtRest.x >= 10 && dockAtRest.width < 375);
    await page.evaluate(() => scrollTo(0, 400));
    await page.waitForFunction(() => scrollY > 200);
    await page.waitForFunction(() => document.querySelector('.app-header').getBoundingClientRect().bottom <= 0);
    await page.waitForFunction(rest => document.querySelector('.app-bottom-nav .bottom-inner').getBoundingClientRect().top > rest + 3, dockAtRest.y);
    const dockWhileScrolling = await dock.boundingBox();
    assert.ok(dockWhileScrolling.y - dockAtRest.y <= 6);
    assert.ok(await positions.isVisible());
    await page.screenshot({ path: path.join(fixtures, `${engine}-floating-dock-scrolling.png`) });
    await page.evaluate(() => scrollBy(0, -24));
    await page.waitForFunction(() => document.querySelector('.app-header').getBoundingClientRect().top >= -1);
    assert.ok((await header.boundingBox()).height >= 60);
    await page.screenshot({ path: path.join(fixtures, `${engine}-floating-chrome-return.png`) });
    await positions.tap();
    await page.waitForFunction(() => scrollY < 2);
    assert.deepEqual(fragments, []);
    assert.equal(await positions.getAttribute('aria-current'), 'page');

    await page.clock.install();
    holdPaths.add('/web/more');
    await more.tap();
    await page.waitForFunction(() => document.querySelector('.experience-nav-target')?.pathname === '/web/more');
    assert.equal(await positions.getAttribute('aria-current'), 'page');
    assert.ok(await content.locator('.position-card').count());
    assert.equal(await content.evaluate(el => el.classList.contains('ui-route-arrive')), false);
    await page.clock.runFor(1000);
    const timer = page.locator('#page-nav-progress-card');
    assert.ok(await timer.isVisible());
    assert.ok(await page.locator('#page-nav-loader').isVisible());
    assert.match(await timer.innerText(), /已等待 [1-9]\.\d 秒/);
    await page.clock.runFor(6500);
    const waiting = page.locator('.ui-feedback-route[data-state=waiting]');
    assert.ok(await waiting.isVisible());
    assert.match(await waiting.locator('[data-route-elapsed]').innerText(), /已等待 [7-9]\.\d 秒/);
    assert.equal(await timer.isVisible(), false);
    await positions.tap();
    await page.waitForFunction(() => !document.body.classList.contains('app-route-pending'));
    await page.clock.runFor(200);
    assert.equal(await timer.isVisible(), false);
    assert.ok(page.url().endsWith('/web/positions'));
    assert.equal(await page.locator('.ui-feedback-route[data-state=error]').count(), 0);

    await more.tap();
    await page.waitForFunction(() => document.querySelector('.experience-nav-target')?.pathname === '/web/more');

    await page.evaluate(() => {
      window.fixtureArrivals = [];
      document.getElementById('app-page-content').addEventListener('animationstart', event => {
        if (event.target.id === 'app-page-content') window.fixtureArrivals.push(event.animationName);
      });
      scrollTo(0, 460);
    });
    await page.waitForFunction(() => scrollY > 400);
    await rank.tap();
    await page.waitForURL('**/web/leaderboard');
    assert.equal(await rank.getAttribute('aria-current'), 'page');
    assert.equal(await content.getAttribute('data-app-tab-direction'), 'forward');
    await page.waitForFunction(() => window.fixtureArrivals.includes('app-tab-arrive'));
    assert.equal(await more.getAttribute('aria-busy'), null);

    await page.goBack();
    await page.waitForFunction(() => scrollY > 400 && !!document.querySelector('.position-card'));
    assert.equal(await positions.getAttribute('aria-current'), 'page');
    assert.equal(await content.getAttribute('data-app-tab-direction'), 'backward');

    await page.emulateMedia({ reducedMotion: 'reduce' });
    await rank.tap();
    await page.waitForURL('**/web/leaderboard');
    assert.equal(await content.evaluate(el => getComputedStyle(el).animationName), 'none');
    await page.evaluate(() => scrollTo(0, 400));
    await page.clock.runFor(300);
    assert.equal(await page.locator('body').evaluate(el => el.classList.contains('app-chrome-retracted')), false);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
}

run().then(() => console.log(`${engine}: floating navigation, reverse scroll, elapsed progress, back and reduced motion passed`))
  .catch(error => { console.error(error); process.exitCode = 1; });
