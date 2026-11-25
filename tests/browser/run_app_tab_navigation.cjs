// Mobile tabs rendered from the normal templates with controlled network responses.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const playwright = require(process.env.STOCKBOT_PLAYWRIGHT_PACKAGE || 'playwright');

const repo = path.resolve(__dirname, '../..');
const fixtures = process.env.STOCKBOT_UI_ARTIFACTS;
const engine = process.argv[2] || 'chromium';
const pages = { '/web/positions': 'positions', '/web/more': 'more', '/web/leaderboard': 'rank', '/web/portfolio': 'home', '/web/workbench': 'workbench', '/web/market': 'market', '/web/us-rotation': 'us_rotation' };

async function run() {
  const browser = await playwright[engine].launch({ timeout: 15000 });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const errors = [];
    const fragments = [];
    const holdPaths = new Set();
    const heldFragments = new Map();
    const background = [];
    const backgroundData = [];
    const foregroundData = [];
    let allowPreloads = false;
    let useStagedHome = false;
    let releaseHomeDetail;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://fixture.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/static/')) {
        const asset = path.join(repo, url.pathname.slice(1));
        return fs.existsSync(asset) ? route.fulfill({ path: asset }) : route.fulfill({ status: 404 });
      }
      const defaultSource = url.pathname === '/web/api/workbench/source' && url.searchParams.get('source') === '黑馬';
      if (defaultSource || url.pathname === '/web/api/us-rotation') {
        const prefetch = route.request().headers()['x-stockbot-preload'] === '1';
        (prefetch ? backgroundData : foregroundData).push(url.pathname);
        return route.fulfill({ json: defaultSource ? { ok:true, rows:[], meta:{market_open:false} } :
          { ok:true, snapshot:{as_of:'2026-11-25', industries:[]} } });
      }
      if (!pages[url.pathname]) return route.fulfill({ json: { ok: true, rows: [], data: [], market_open: false } });
      if (route.request().headers()['x-stockbot-preload'] === '1') {
        if (!allowPreloads) return route.fulfill({ status: 429 });
        assert.equal(url.searchParams.get('fragment'), '1');
        background.push(url.pathname);
        return route.fulfill({ path: path.join(fixtures, pages[url.pathname] + '-fragment.html') });
      }
      const fragment = url.searchParams.has('fragment');
      if (url.pathname === '/web/portfolio' && useStagedHome) {
        if (!fragment) return route.fulfill({ path: path.join(fixtures, 'home-loading.html') });
        if (url.searchParams.get('fast') === '1') {
          return route.fulfill({ contentType: 'text/html', body: '<section class="daily-fast-sync">等待首頁完整資料</section>' });
        }
        if (url.searchParams.get('detail') === '1') {
          releaseHomeDetail(route);
          return;
        }
      }
      if (fragment) {
        fragments.push(url.pathname);
        if (holdPaths.has(url.pathname)) { heldFragments.set(url.pathname, route); return; }
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
    const headerAtRest = await header.boundingBox();
    assert.ok(dockAtRest.x >= 10 && dockAtRest.width < 375);
    await page.evaluate(() => scrollTo(0, 400));
    await page.waitForFunction(() => scrollY > 200);
    await page.waitForFunction(rest => document.querySelector('.app-header').getBoundingClientRect().height < rest * .86, headerAtRest.height);
    await page.waitForFunction(rest => document.querySelector('.app-bottom-nav .bottom-inner').getBoundingClientRect().width < rest * .94, dockAtRest.width);
    const dockWhileScrolling = await dock.boundingBox();
    const headerWhileScrolling = await header.boundingBox();
    assert.ok(headerWhileScrolling.y >= -1 && headerWhileScrolling.height >= 55);
    assert.ok(Math.abs((dockWhileScrolling.y + dockWhileScrolling.height) - (dockAtRest.y + dockAtRest.height)) < 1);
    assert.ok(await positions.isVisible());
    await page.screenshot({ path: path.join(fixtures, `${engine}-floating-dock-scrolling.png`) });
    await page.evaluate(() => scrollBy(0, -24));
    await page.waitForFunction(rest => document.querySelector('.app-header').getBoundingClientRect().height >= rest - 1, headerAtRest.height);
    await page.waitForFunction(rest => document.querySelector('.app-bottom-nav .bottom-inner').getBoundingClientRect().width >= rest - 1, dockAtRest.width);
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
    assert.equal(await page.locator('body').evaluate(el => el.classList.contains('app-chrome-compact')), false);

    allowPreloads = true;
    useStagedHome = true;
    const heldHomeDetail = new Promise(resolve => { releaseHomeDetail = resolve; });
    await page.goto('https://fixture.test/web/portfolio');
    await page.clock.runFor(100);
    const homeDetail = await heldHomeDetail;
    await page.clock.runFor(1600);
    assert.deepEqual(background, []);
    await homeDetail.fulfill({ path: path.join(fixtures, 'home-fragment.html') });
    await page.locator('#hv2').waitFor();

    for (const destination of ['/web/positions', '/web/market', '/web/leaderboard', '/web/us-rotation', '/web/more', '/web/workbench']) {
      if (!background.includes(destination)) {
        const ready = page.waitForEvent('requestfinished', request =>
          request.headers()['x-stockbot-preload'] === '1' && new URL(request.url()).pathname === destination);
        await page.clock.runFor(1200);
        await ready;
      }
    }
    assert.deepEqual(background, ['/web/positions', '/web/market', '/web/leaderboard', '/web/us-rotation', '/web/more', '/web/workbench']);
    if (!backgroundData.includes('/web/api/workbench/source')) {
      await page.waitForEvent('requestfinished', request => new URL(request.url()).pathname === '/web/api/workbench/source');
    }
    assert.deepEqual(backgroundData, ['/web/api/us-rotation', '/web/api/workbench/source']);
    await page.clock.runFor(200);
    const priorFragments = fragments.length;
    const warmRoutes = [
      ['/web/positions', '.position-card'],
      ['/web/market', '.market-chip-grid'],
      ['/web/leaderboard', '.rank-return'],
      ['/web/us-rotation', '#us-radar'],
      ['/web/more', '.more-group'],
      ['/web/workbench', '#stockbot-workbench']
    ];
    for (const [destination, selector] of warmRoutes) {
      await nav.locator('a[href="' + destination + '"]').tap();
      await content.locator(selector).first().waitFor({ timeout:3000 });
      assert.equal(fragments.length, priorFragments);
      assert.equal(await content.locator('[data-app-preview]').count(), 0);
      if (destination === '/web/us-rotation') await page.getByText('美股交易日 2026-11-25', { exact:true }).waitFor();
    }
    assert.deepEqual(foregroundData, []);
    await page.clock.runFor(31000);
    holdPaths.add('/web/positions');
    const freshPositionRequest = page.waitForEvent('request', request =>
      !request.headers()['x-stockbot-preload'] && new URL(request.url()).pathname === '/web/positions');
    await positions.tap();
    await freshPositionRequest;
    assert.ok(await content.locator('#stockbot-workbench').isVisible());
    assert.equal(await content.locator('[data-app-preview]').count(), 0);
    await heldFragments.get('/web/positions').fulfill({ path: path.join(fixtures, 'positions-fragment.html') });
    await content.locator('.position-card').first().waitFor();
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
}

run().then(() => console.log(`${engine}: full warm pages, no duplicate foreground fetch, expired fallback passed`))
  .catch(error => { console.error(error); process.exitCode = 1; });
