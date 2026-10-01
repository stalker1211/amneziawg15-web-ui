// Live-update smoke test: the EventSource in app.js, in a real browser.
//
// The stream is what keeps the page current, and a broken one looks fine until the
// panel has been open a while. This drives app.js's own code: the stream opens and
// resyncs, recovers after the browser gives it up, ignores a replaced stream, closes
// in a hidden tab and reopens with a resync when shown, leaves fetches room with more
// tabs than the browser's six connections per host, reopens after a long silence,
// and -- given RESTART_CMD -- comes back after the server restarts.
//
// Usage, against tests/demo_server.py (no login) or a container with the source
// mounted (then pass the user and password):
//
//   NODE_PATH=<dir with puppeteer>/node_modules node tests/smoke_events.js http://127.0.0.1:8099
//   RESTART_CMD='docker restart awg' NODE_PATH=... \
//     node tests/smoke_events.js http://127.0.0.1:8080 admin pw
//
// Exits non-zero on any failed check.
const { execSync } = require('child_process');
const puppeteer = require('puppeteer');

const BASE = (process.argv[2] || 'http://127.0.0.1:8099').replace(/\/$/, '');
const USER = process.argv[3];
const PASS = process.argv[4];
const RESTART_CMD = process.env.RESTART_CMD || '';

let failures = 0;
function check(label, ok, detail) {
    if (!ok) failures++;
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${detail !== undefined ? ' -> ' + JSON.stringify(detail) : ''}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Wait for a condition, polling in the page.
async function waitFor(page, fn, timeoutMs = 15000, everyMs = 200) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (await page.evaluate(fn).catch(() => false)) return true;
        await sleep(everyMs);
    }
    return false;
}

async function openPanel(browser) {
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });
    if (USER) await page.authenticate({ username: USER, password: PASS });
    await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForFunction(() => typeof amneziaApp !== 'undefined', { timeout: 30000 });
    return page;
}

// Passed to page.evaluate whole: puppeteer sends a function's source into the page, so a
// condition there cannot call a helper defined here.
const streamOpen = () => !!(amneziaApp.events && amneziaApp.events.readyState === EventSource.OPEN);

(async () => {
    const browser = await puppeteer.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    const page = await openPanel(browser);
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));
    const pillState = () => page.evaluate(() => document.getElementById('statusFrame')?.dataset.state);

    // --- baseline -----------------------------------------------------------
    check('the stream opens on load', await waitFor(page, streamOpen));
    check('status pill shows connected', await waitFor(page, () =>
        document.getElementById('statusFrame')?.dataset.state === 'connected', 5000), await pillState());
    check('servers loaded', await waitFor(page, () => amneziaApp.lastServers.length > 0));
    await page.evaluate(() => {
        window.__resyncs = 0;
        const resync = amneziaApp.resyncAppState.bind(amneziaApp);
        amneziaApp.resyncAppState = () => { window.__resyncs += 1; return resync(); };
        window.__traffic = 0;
        const traffic = amneziaApp.updateServerTraffic.bind(amneziaApp);
        amneziaApp.updateServerTraffic = (...args) => { window.__traffic += 1; return traffic(...args); };
    });
    check('traffic_update events arrive (a running server)', await waitFor(page, () => window.__traffic > 0, 20000));

    // --- the browser gives the stream up ------------------------------------
    // As after a 502 while the panel restarts: closed, then an error. recoverEvents
    // asks /api/system/status, gets 200 and opens a new stream 3 s later.
    const givenUp = await page.evaluate(() => {
        window.__resyncs = 0;
        amneziaApp.lastResyncAt = 0;
        const source = amneziaApp.events;
        source.close();
        source.dispatchEvent(new Event('error'));
        window.__givenUp = source;
        return amneziaApp.events === null;
    });
    check('a given-up stream is dropped', givenUp);
    check('status pill reflects it', await pillState() === 'reconnecting', await pillState());
    check('a new stream opens after the retry', await waitFor(page, () =>
        amneziaApp.events?.readyState === EventSource.OPEN && amneziaApp.events !== window.__givenUp, 10000));
    check('and the page resyncs', await waitFor(page, () => window.__resyncs > 0, 5000),
        await page.evaluate(() => window.__resyncs));
    check('status pill back to connected', await waitFor(page, () =>
        document.getElementById('statusFrame')?.dataset.state === 'connected', 5000), await pillState());

    // --- a replaced stream is ignored ----------------------------------------
    const staleIgnored = await page.evaluate(() => {
        const old = amneziaApp.events;
        amneziaApp.openEvents();
        amneziaApp.lastResyncAt = 0;
        const before = window.__resyncs;
        old.dispatchEvent(new Event('open'));  // a late event from the replaced stream
        return { replaced: amneziaApp.events !== old, unchanged: window.__resyncs === before };
    });
    check('events from a replaced stream are ignored', staleIgnored.replaced && staleIgnored.unchanged, staleIgnored);
    check('the replacing stream opens', await waitFor(page, streamOpen, 10000));

    // --- hidden and shown -----------------------------------------------------
    const other = await openPanel(browser);
    await other.bringToFront();
    const hidden = await waitFor(page, () => document.visibilityState === 'hidden', 5000);
    if (!hidden) {
        // A headless browser that keeps every tab visible: drive the handler instead.
        console.log('  INFO  tabs stay visible here; emulating visibilitychange');
        await page.evaluate(() => {
            window.__vis = 'hidden';
            Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => window.__vis });
            document.dispatchEvent(new Event('visibilitychange'));
        });
    }
    check('a hidden tab closes its stream', await waitFor(page, () => amneziaApp.events === null, 5000));
    await page.evaluate(() => { window.__resyncs = 0; amneziaApp.lastResyncAt = 0; });
    if (hidden) {
        await page.bringToFront();
    } else {
        await page.evaluate(() => { window.__vis = 'visible'; document.dispatchEvent(new Event('visibilitychange')); });
    }
    check('shown again, it opens a stream', await waitFor(page, streamOpen, 10000));
    check('and resyncs', await waitFor(page, () => window.__resyncs > 0, 5000));

    // --- more tabs than the browser's six connections per host ---------------
    if (hidden) {
        const tabs = [other];
        let loaded = true;
        try {
            for (let i = 0; i < 6; i++) tabs.push(await openPanel(browser));
        } catch (e) {
            // Hidden tabs that kept their streams hold all six connections: the next
            // tab cannot even load the page.
            loaded = false;
            check('eight tabs load', false, e.message);
        }
        if (loaded) {
            for (const tab of tabs) await tab.bringToFront();  // each shown once, then hidden
            const last = tabs[tabs.length - 1];
            await waitFor(last, streamOpen, 10000);
            const open = [];
            for (const tab of [page, ...tabs]) open.push(await tab.evaluate(() => !!amneziaApp.events));
            check('only the visible tab of 8 holds a stream', open.filter(Boolean).length === 1, open);
            const ms = await last.evaluate(async () => {
                const t0 = performance.now();
                await fetch('/api/servers');
                return Math.round(performance.now() - t0);
            });
            check('a fetch is not left waiting for a connection', ms < 2000, `${ms} ms`);
        }
        for (const tab of tabs) await tab.close();
        await page.bringToFront();
        await waitFor(page, streamOpen, 10000);
    } else {
        console.log('  INFO  many-tabs check skipped (tabs stay visible here)');
        await other.close();
    }

    // --- a long silence reopens the stream -----------------------------------
    // A connection that died without a word: the page holds a stream that never
    // delivers anything (the real one closed, a stand-in left), quiet for a minute.
    await page.evaluate(() => {
        amneziaApp.events.close();
        amneziaApp.events = window.__quiet = { readyState: EventSource.OPEN, close() {} };
        amneziaApp.lastEventAt = Date.now() - 60000;
    });
    check('after 45 s of silence a new stream opens', await waitFor(page, () =>
        amneziaApp.events?.readyState === EventSource.OPEN && amneziaApp.events !== window.__quiet, 25000));

    // --- a server restart ------------------------------------------------------
    if (RESTART_CMD) {
        await page.evaluate(() => { window.__resyncs = 0; window.__traffic = 0; amneziaApp.lastResyncAt = 0; });
        execSync(RESTART_CMD, { stdio: 'ignore' });
        check('the restart is noticed', await waitFor(page, () =>
            document.getElementById('statusFrame')?.dataset.state === 'reconnecting', 30000));
        check('the stream comes back after the restart', await waitFor(page, streamOpen, 90000));
        check('and the page resyncs', await waitFor(page, () => window.__resyncs > 0, 10000));
        check('traffic flows again', await waitFor(page, () => window.__traffic > 0, 20000));
    } else {
        console.log('  INFO  restart check skipped (set RESTART_CMD)');
    }

    console.log('');
    check('no uncaught page errors', pageErrors.length === 0, pageErrors);
    await browser.close();

    console.log(`\n${failures === 0 ? 'OK' : failures + ' FAILURE(S)'}`);
    process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
    console.error('events smoke test crashed:', e);
    process.exit(1);
});
