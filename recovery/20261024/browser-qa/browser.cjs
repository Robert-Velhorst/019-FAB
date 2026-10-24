const { chromium } = require('C:/Users/NO/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert = require('node:assert/strict');
const data = JSON.parse(process.env.FAB_REHEARSAL_BROWSER);
const output = 'C:/Users/NO/AppData/Local/Temp/fab-review-qa-20261020';
const origin = 'https://fab-rehearsal.invalid';
const proxy = { 'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'fab-rehearsal.invalid', Origin: origin, Cookie: data.cookie };

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const cookieValue = data.cookie.slice(data.cookie.indexOf('=') + 1);
    await context.addCookies([{ name: '__Host-fab_operator', value: cookieValue, domain: 'fab-rehearsal.invalid', path: '/', secure: true, httpOnly: true, sameSite: 'Strict' }]);
    let active = 0, peak = 0, pageCalls = 0;
    let holdNextPage = false, releaseHeldPage, notifyHeldPage;
    let heldPage = new Promise(resolve => { notifyHeldPage = resolve; });
    const errors = [];
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort('blockedbyclient');
      const methods = decodeURIComponent(url.pathname).split('/').pop().split(',');
      const reads = methods.filter(name => name === 'fab.reviewPage').length;
      active += reads; peak = Math.max(peak, active); pageCalls += reads;
      try {
        if (reads && holdNextPage) {
          holdNextPage = false;
          notifyHeldPage();
          await new Promise(resolve => { releaseHeldPage = resolve; });
        }
        if (reads) await new Promise(resolve => setTimeout(resolve, 180));
        const response = await route.fetch({ url: data.webBase + url.pathname + url.search, headers: { ...route.request().headers(), ...proxy } });
        await route.fulfill({ response });
      } finally { active -= reads; }
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(String(error)));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    await page.clock.install();
    await page.goto(origin + '/admin/operations', { waitUntil: 'networkidle' });
    await page.locator('#review-workspace .fab-review-table tbody tr').first().waitFor();
    const rows = page.locator('#review-workspace .fab-review-table tbody tr');
    assert.equal(await rows.count(), 12);
    const pageSize = parseInt(await page.locator('#review-workspace .fab-result-count').innerText(), 10);
    assert(pageSize > 0);
    for (let index = 0; index < 6; index++) {
      await page.locator('#review-workspace').getByRole('button', { name: /^Load next/ }).click();
      await page.waitForFunction(expected => document.querySelector('#review-workspace .fab-result-count')?.textContent.includes(`${expected} shown from ${expected} loaded`), pageSize * (index + 2));
    }
    const loadedReviews = pageSize * 7;
    assert.match(await page.locator('#review-workspace .fab-result-count').innerText(), new RegExp(`${loadedReviews} shown from ${loadedReviews} loaded`));
    const before = await rows.allTextContents();
    const response = await fetch(data.webBase + '/api/trpc/fab.refreshControlCenter', { method: 'POST', headers: { ...proxy, 'Content-Type': 'application/json' }, body: JSON.stringify({ json: null }) });
    assert.equal(response.status, 200);
    pageCalls = 0; peak = 0;
    const refreshRead = page.waitForResponse(response => response.url().includes('fab.reviewPage'));
    await page.clock.fastForward(60000);
    await refreshRead;
    const deadline = Date.now() + 20000;
    while (pageCalls < 6 || active > 0) {
      assert(Date.now() < deadline, `refresh did not finish: ${pageCalls}/${active}`);
      await page.waitForTimeout(80);
    }
    assert(peak <= 4 && peak > 0, `unexpected logical RPC peak ${peak}`);
    assert.equal(pageCalls, 6);
    assert.deepEqual(await rows.allTextContents(), before);
    const backgroundCalls = pageCalls, backgroundPeak = peak;
    holdNextPage = true;
    await page.locator('#review-workspace').getByRole('button', { name: /^Load next/ }).click();
    await heldPage;
    await page.getByRole('button', { name: 'Refresh control center', exact: true }).click();
    await page.waitForFunction(expected => document.querySelector('#review-workspace .fab-result-count')?.textContent.includes(`${expected} shown from ${expected} loaded`), pageSize);
    const lateRead = page.waitForResponse(response => response.url().includes('fab.reviewPage'));
    releaseHeldPage();
    await lateRead;
    await page.locator('#review-workspace').getByRole('button', { name: /^Load next/ }).waitFor();
    assert.equal(await page.locator('#review-workspace .fab-result-count').innerText(), `${pageSize} shown from ${pageSize} loaded`, 'superseded load-more must not append to a refreshed queue');
    heldPage = new Promise(resolve => { notifyHeldPage = resolve; });
    holdNextPage = true;
    await page.locator('#review-workspace').getByRole('button', { name: /^Load next/ }).click();
    await heldPage;
    const forced = await fetch(data.webBase + '/api/trpc/fab.refreshControlCenter', { method: 'POST', headers: { ...proxy, 'Content-Type': 'application/json' }, body: JSON.stringify({ json: null }) });
    assert.equal(forced.status, 200);
    const firstPageRead = page.waitForResponse(response => response.url().includes('fab.controlCenter'));
    await page.clock.fastForward(60000);
    await firstPageRead;
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const lateBackgroundRead = page.waitForResponse(response => response.url().includes('fab.reviewPage'));
    releaseHeldPage();
    await lateBackgroundRead;
    await page.locator('#review-workspace').getByRole('button', { name: /^Load next/ }).waitFor();
    assert.equal(await page.locator('#review-workspace .fab-result-count').innerText(), `${pageSize} shown from ${pageSize} loaded`, 'a first-page poll must also supersede pending load-more');
    await page.locator('#review-workspace').getByRole('button', { name: /^Load next/ }).click();
    await page.waitForFunction(expected => document.querySelector('#review-workspace .fab-result-count')?.textContent.includes(`${expected} shown from ${expected} loaded`), pageSize * 2);
    assert.equal(await page.locator('vite-error-overlay').count(), 0);
    assert.equal(await page.getByRole('button', { name: 'Close navigation', exact: true }).isVisible(), false, 'mobile close control must be hidden on desktop');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.locator('#review-workspace').scrollIntoViewIfNeeded();
    await page.screenshot({ path: output + '/desktop.png' });
    await page.getByRole('button', { name: 'Collapse sidebar', exact: true }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector('#fab-sidebar').getBoundingClientRect().right <= 1);
    await page.getByRole('button', { name: 'Open navigation', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#fab-sidebar').getBoundingClientRect().left >= -1);
    await page.getByRole('button', { name: 'Close navigation', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('#fab-sidebar').getBoundingClientRect().right <= 1);
    await page.locator('#review-workspace').evaluate(element => element.scrollIntoView({ block: 'start' }));
    assert.equal(await page.locator('#review-workspace tbody tr:first-child td:first-child > span').evaluate(element => getComputedStyle(element).gridColumnStart), '2');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: output + '/mobile.png' });
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ url: page.url(), title: await page.title(), loadedReviewsBeforeReset: loadedReviews, loadedReviewsAfterReset: pageSize, loadedReviewsAfterFreshLoad: pageSize * 2, staleLoadMoreDiscarded: true, firstPagePollSupersedesLoadMore: true, visibleRowsPerPage: 12, loadedPages: 7, refreshedAdditionalPages: backgroundCalls, peakLogicalPageReads: backgroundPeak, desktopMobileControlsHidden: true, collapsedDesktopToMobileNavigationWorks: true, consoleErrors: errors, desktopOverflow: false, mobileOverflow: false, screenshots: [output + '/desktop.png', output + '/mobile.png'], syntheticOnly: true, providerWrites: false }));
    await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
