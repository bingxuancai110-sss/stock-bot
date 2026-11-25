// Mobile tabs rendered from the normal templates with controlled network responses.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const playwright = require(process.env.STOCKBOT_PLAYWRIGHT_PACKAGE || 'playwright');

const repo = path.resolve(__dirname, '../..');
const fixtures = process.env.STOCKBOT_UI_ARTIFACTS;
const engine = process.argv[2] || 'chromium';
const pages = { '/web/positions': 'positions', '/web/more': 'more', '/web/leaderboard': 'rank', '/web/portfolio': 'home', '/web/workbench': 'workbench' };

async function run() {
  const browser = await playwright[engine].launch({ timeout: 15000 });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const errors = [];
    const fragments = [];
    const holdPaths = new Set();
    const heldFragments = new Map();
    let previewRequests = 0;
    let useStagedHome = false;
    let releaseHomeDetail;
    let releaseWorkbench;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://fixture.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/static/')) {
        const asset = path.join(repo, url.pathname.slice(1));
        return fs.existsSync(asset) ? route.fulfill({ path: asset }) : route.fulfill({ status: 404 });
      }
      if (!pages[url.pathname]) return route.fulfill({ json: { ok: true, rows: [], data: [], market_open: false } });
      if (url.pathname === '/web/workbench' && releaseWorkbench) {
        releaseWorkbench(route);
        return;
      }
      if (url.pathname === '/web/positions' && url.searchParams.get('preview') === '1') {
        assert.equal(url.searchParams.get('fast'), '1');
        assert.equal(url.searchParams.get('fragment'), '1');
        previewRequests += 1;
        return route.fulfill({ path: path.join(fixtures, 'positions-preview.html') });
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

    await page.goto('https://fixture.test/web/portfolio');
    assert.equal(previewRequests, 0);
    const previewCompleted = page.waitForEvent('requestfinished', request => new URL(request.url()).searchParams.get('preview') === '1');
    await page.clock.runFor(1600);
    await previewCompleted;
    assert.equal(previewRequests, 1);
    await page.clock.runFor(200);
    assert.equal(await page.locator('[data-positions-preview]').count(), 0);
    holdPaths.add('/web/positions');
    const historyLength = await page.evaluate(() => history.length);
    const requestedPositions = page.waitForEvent('request', request => {
      const url = new URL(request.url());
      return url.pathname === '/web/positions' && !url.searchParams.has('preview');
    });
    await positions.tap();
    await requestedPositions;
    await page.waitForURL('**/web/positions');
    const preview = page.locator('[data-positions-preview]');
    assert.ok(await preview.isVisible());
    assert.ok(await preview.locator('li').count());
    assert.equal(await preview.locator('form').count(), 0);
    assert.doesNotMatch(await preview.innerText(), /成本|股數|損益/);
    assert.equal(await positions.getAttribute('aria-current'), 'page');
    assert.equal(await content.getAttribute('aria-busy'), 'true');
    await heldFragments.get('/web/positions').fulfill({ status: 503, body: 'temporary error' });
    await page.locator('.ui-feedback-route[data-state=error]').waitFor();
    assert.match(await preview.innerText(), /尚未載入/);
    holdPaths.delete('/web/positions');
    await page.getByRole('button', { name: '重試載入' }).click();
    await content.locator('.position-card').first().waitFor();
    assert.equal(await preview.count(), 0);
    assert.equal(await page.evaluate(() => history.length), historyLength + 1);

    holdPaths.add('/web/leaderboard');
    const rankingRequested = page.waitForEvent('request', request => new URL(request.url()).pathname === '/web/leaderboard');
    await rank.tap();
    await rankingRequested;
    const rankingPreview = page.locator('[data-app-preview]');
    await rankingPreview.waitFor();
    assert.match(await rankingPreview.innerText(), /組合績效排行/);
    assert.equal(await page.locator('.rank-return').count(), 0);
    await heldFragments.get('/web/leaderboard').fulfill({ path: path.join(fixtures, 'rank-fragment.html') });
    await page.locator('.rank-return').first().waitFor();
    assert.equal(await rankingPreview.count(), 0);
    holdPaths.delete('/web/leaderboard');

    useStagedHome = true;
    const heldHomeDetail = new Promise(resolve => { releaseHomeDetail = resolve; });
    const detailRequested = page.waitForEvent('request', request => {
      const url = new URL(request.url());
      return url.pathname === '/web/portfolio' && url.searchParams.get('detail') === '1';
    });
    await page.goto('https://fixture.test/web/portfolio');
    await page.clock.runFor(100);
    await detailRequested;
    const homeDetail = await heldHomeDetail;
    await page.clock.runFor(1600);
    assert.equal(previewRequests, 1);
    const secondPreviewCompleted = page.waitForEvent('requestfinished', request => new URL(request.url()).searchParams.get('preview') === '1');
    await homeDetail.fulfill({ path: path.join(fixtures, 'home-fragment.html') });
    await page.locator('#hv2').waitFor();
    await page.clock.runFor(1600);
    await secondPreviewCompleted;
    const refreshedAfterTrade = page.waitForEvent('requestfinished', request => new URL(request.url()).searchParams.get('preview') === '1');
    await page.evaluate(() => document.dispatchEvent(new CustomEvent('stockbot:positionschanged')));
    await page.clock.runFor(1600);
    await refreshedAfterTrade;
    await page.clock.runFor(31000);
    assert.equal(previewRequests, 3);
    await positions.tap();
    await content.locator('.position-card').first().waitFor();
    assert.equal(await preview.count(), 0);
    assert.deepEqual(errors, []);

    const workbenchResponse = new Promise(resolve => { releaseWorkbench = resolve; });
    const workbenchRequested = page.waitForEvent('request', request => new URL(request.url()).pathname === '/web/workbench');
    const workbenchPreview = await page.evaluate(() => {
      document.querySelector('.app-bottom-nav a[href="/web/workbench"]').click();
      return document.querySelector('[data-document-preview]')?.textContent;
    });
    await page.clock.runFor(120);
    await workbenchRequested;
    const workbench = await workbenchResponse;
    assert.ok(workbench.request().isNavigationRequest());
    assert.match(workbenchPreview, /ETF 與策略研究/);
    await workbench.fulfill({ path: path.join(fixtures, 'workbench.html') });
    await page.locator('#stockbot-workbench').waitFor();
    assert.equal(await page.locator('[data-document-preview]').count(), 0);
  } finally {
    await browser.close();
  }
}

run().then(() => console.log(`${engine}: cached previews, native workbench navigation, holdings refresh and retries passed`))
  .catch(error => { console.error(error); process.exitCode = 1; });
