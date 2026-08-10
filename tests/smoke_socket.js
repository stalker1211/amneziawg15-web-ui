// Socket lifecycle smoke test.
//
// The reconnect/resync logic in app.js is the most intricate code in the project and
// the easiest to break silently: a broken reconnect looks fine until the panel has
// been open for a while. This drives the real state machine in a real browser.
//
// Usage (needs a running container with the source mounted, and >=1 server):
//
//   docker run --rm --add-host host.docker.internal:host-gateway \
//     -v "$PWD/tests/smoke_socket.js":/home/pptruser/s.js:ro -w /home/pptruser \
//     ghcr.io/puppeteer/puppeteer:latest node s.js http://host.docker.internal:8080 admin pw
//
// Exits non-zero on any failed check.
const puppeteer = require('puppeteer');

const BASE = process.argv[2] || 'http://host.docker.internal:18093';
const USER = process.argv[3] || 'admin';
const PASS = process.argv[4] || 'pw';

let failures = 0;
function check(label, ok, detail) {
    if (!ok) failures++;
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${detail !== undefined ? ' -> ' + JSON.stringify(detail) : ''}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Wait for a condition, polling in the page. Avoids fixed sleeps where possible.
async function waitFor(page, fn, timeoutMs = 15000, everyMs = 250) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (await page.evaluate(fn)) return true;
        await sleep(everyMs);
    }
    return false;
}

(async () => {
    const browser = await puppeteer.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });
    await page.authenticate({ username: USER, password: PASS });

    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));
    page.on('dialog', (d) => d.accept());

    await page.goto(BASE + '/', { waitUntil: 'networkidle2', timeout: 60000 });

    // --- baseline -----------------------------------------------------------
    check('socket connects on load', await waitFor(page, () => !!(amneziaApp.socket && amneziaApp.socket.connected)));
    check('status pill shows connected', await page.evaluate(() =>
        /connected/i.test(document.getElementById('status')?.textContent || '')));
    check('servers loaded over the socket-triggered resync', await page.evaluate(() =>
        Array.isArray(amneziaApp.lastServers) && amneziaApp.lastServers.length > 0));

    // --- disconnect is noticed ---------------------------------------------
    await page.evaluate(() => {
        window.__resyncCount = 0;
        const original = amneziaApp.resyncAppState.bind(amneziaApp);
        amneziaApp.resyncAppState = () => { window.__resyncCount += 1; return original(); };
        // Simulate the transport dropping, as a laptop sleep or network blip would.
        amneziaApp.socket.io.engine.close();
    });
    check('disconnect is observed', await waitFor(page, () => !amneziaApp.socket.connected, 10000));
    check('status pill reflects the drop', await page.evaluate(() =>
        !/^connected/i.test((document.getElementById('status')?.textContent || '').trim())));

    // --- socket.io reconnects by itself, and the app resyncs ----------------
    check('reconnects automatically', await waitFor(page, () => !!amneziaApp.socket.connected, 20000));
    check('resync ran after reconnect', await waitFor(page, () => window.__resyncCount > 0, 10000),
        await page.evaluate(() => window.__resyncCount));
    check('status pill back to connected', await page.evaluate(() =>
        /connected/i.test(document.getElementById('status')?.textContent || '')));

    // --- rebuildSocket replaces the instance and rebinds -------------------
    const rebuilt = await page.evaluate(async () => {
        const before = amneziaApp.socket;
        amneziaApp.socketLastRebuildAt = 0;          // bypass the 1.5s throttle
        amneziaApp.rebuildSocket('test');
        return { replaced: amneziaApp.socket !== before, hasSocket: !!amneziaApp.socket };
    });
    check('rebuildSocket swaps in a new instance', rebuilt.replaced && rebuilt.hasSocket, rebuilt);
    check('rebuilt socket connects', await waitFor(page, () => !!amneziaApp.socket?.connected, 20000));

    // --- the rebuild throttle -----------------------------------------------
    const throttled = await page.evaluate(() => {
        const current = amneziaApp.socket;
        amneziaApp.socketLastRebuildAt = Date.now();  // pretend we just rebuilt
        amneziaApp.rebuildSocket('should-be-throttled');
        return amneziaApp.socket === current;
    });
    check('rebuilds within 1.5s are throttled', throttled);

    // --- stale handlers must not touch app state ---------------------------
    const staleIgnored = await page.evaluate(() => {
        // Emit on an orphaned socket: handlers guard with `socket !== this.socket`.
        const orphan = amneziaApp.createSocket();
        amneziaApp.bindSocketHandlers(orphan);
        const before = window.__resyncCount;
        orphan.emit = () => {};
        // Fire the handler directly, as a late event from a replaced socket would.
        orphan.listeners('connect').forEach((fn) => fn());
        const unchanged = window.__resyncCount === before;
        orphan.disconnect();
        return unchanged;
    });
    check('events from a replaced socket are ignored', staleIgnored);

    // --- resume path (tab focus / wake) ------------------------------------
    const resumed = await page.evaluate(async () => {
        const before = window.__resyncCount;
        amneziaApp.handleSocketResume('test-focus');   // connected -> resync only
        return { delta: window.__resyncCount - before, connected: !!amneziaApp.socket.connected };
    });
    check('resume while connected resyncs without reconnecting',
        resumed.delta >= 1 && resumed.connected, resumed);

    const resumedCold = await page.evaluate(async () => {
        amneziaApp.teardownSocket();                  // no socket at all
        amneziaApp.socketLastRebuildAt = 0;
        amneziaApp.handleSocketResume('test-cold');
        return !!amneziaApp.socket;
    });
    check('resume with no socket builds one', resumedCold);
    check('rebuilt-from-cold socket connects', await waitFor(page, () => !!amneziaApp.socket?.connected, 20000));

    // --- health-check timer -------------------------------------------------
    const healthTimer = await page.evaluate(() => {
        amneziaApp.scheduleSocketHealthCheck('test', 500);
        const armed = !!amneziaApp.socketHealthTimer;
        amneziaApp.clearSocketHealthTimer();
        return { armed, cleared: !amneziaApp.socketHealthTimer };
    });
    check('health check arms and clears', healthTimer.armed && healthTimer.cleared, healthTimer);

    // --- live traffic still flows ------------------------------------------
    const traffic = await page.evaluate(() => new Promise((resolve) => {
        const timer = setTimeout(() => resolve('timeout'), 12000);
        amneziaApp.socket.on('traffic_update', (d) => { clearTimeout(timer); resolve(d?.server_id || 'no-id'); });
    }));
    // Only meaningful when a server is running; report either way.
    console.log(`  INFO  traffic_update after reconnect: ${traffic}`);

    console.log('');
    check('no uncaught page errors', pageErrors.length === 0, pageErrors);
    await browser.close();

    console.log(`\n${failures === 0 ? 'OK' : failures + ' FAILURE(S)'}`);
    process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
    console.error('socket smoke test crashed:', e);
    process.exit(1);
});
