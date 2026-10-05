// The README's screenshots: the main page in both themes, from tests/demo_server.py's
// example data (documentation-range addresses, random keys: nothing real can show).
// --day-time 21:30 plays the demo's invented day at an evening video, whatever the clock
// says, so every run shows the same kind of traffic (at lunch it is nearly idle).
//
//   uv run --no-project --python 3.14 --with-requirements web-ui/requirements.txt tests/demo_server.py --day-time 21:30 &
//   node tests/screenshot.js            # puppeteer: tests/browser.js (npm i -g puppeteer)
//
// Writes screenshot.png (dark) and screenshot-light.png in the current directory (an
// optional second argument names another): 1280 px wide at 2x, the full page. Each
// theme in its own browser context, so the page follows the emulated OS preference
// as a first visit does. Before capturing it changes the demo's password (the
// default-password banner goes), and waits for live traffic in the header's tile (the
// demo's loop ticks every 7 s; a rate needs two readings). It checks the theme and
// the banner after, and exits non-zero without writing a file when anything is off.
const path = require('path');
const puppeteer = require('./browser');

const BASE = process.argv[2] || 'http://127.0.0.1:8099';
const OUT = process.argv[3] || '.';
const SHOTS = [['dark', 'screenshot.png'], ['light', 'screenshot-light.png']];
const TRAFFIC_TIMEOUT_MS = 60000;

(async () => {
    const browser = await puppeteer.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    let failed = false;
    try {
        for (const [theme, file] of SHOTS) {
            const context = await browser.createBrowserContext();
            const page = await context.newPage();
            await page.setViewport({ width: 1280, height: 900, deviceScaleFactor: 2 });
            await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
            await page.goto(BASE, { waitUntil: 'networkidle2' });

            // The demo has no sign-in; a second run finds the password already changed.
            await page.evaluate(() => fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ access: { current_password: 'changeme', password: 'demo-screenshot' } }),
            }));
            await page.goto(BASE, { waitUntil: 'networkidle2' });
            await page.waitForFunction(
                () => parseFloat(document.getElementById('stripTrafficNow')?.textContent) > 0,
                { timeout: TRAFFIC_TIMEOUT_MS, polling: 500 },
            );
            await new Promise((r) => setTimeout(r, 1000)); // the tick's redraw settles

            const state = await page.evaluate(() => ({
                dark: document.body.classList.contains('dark'),
                banner: !document.getElementById('passwordBanner')?.hidden,
                traffic: document.getElementById('stripTrafficNow')?.textContent,
            }));
            const ok = state.dark === (theme === 'dark') && !state.banner;
            console.log(`  ${ok ? 'OK  ' : 'FAIL'}  ${file}: ${JSON.stringify(state)}`);
            if (ok) {
                await page.screenshot({ path: path.join(OUT, file), fullPage: true });
            } else {
                failed = true;
            }
            await context.close();
        }
    } catch (error) {
        console.error(`  FAIL  ${error.message}`);
        failed = true;
    } finally {
        await browser.close();
    }
    process.exit(failed ? 1 : 0);
})();
