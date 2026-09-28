// Frontend smoke test: opens every form (in the side drawer) and view in both
// themes and asserts the fields that matter are present and wired. There is no JS unit-test framework in this
// project on purpose; this script is the frontend safety net.
//
// Needs a running panel with at least one server: a container with the source
// bind-mounted, or tests/demo_server.py (then pass its URL, e.g.
// http://127.0.0.1:8099, and run node with puppeteer on NODE_PATH). Usage:
//
//   docker run -d --name awg --cap-add NET_ADMIN --device /dev/net/tun \
//     -e NGINX_USER=admin -e NGINX_PASSWORD=pw -p 18093:80 \
//     -v "$PWD/web-ui":/app/web-ui:ro amneziawg-web-ui:local
//
//   docker run --rm --add-host host.docker.internal:host-gateway \
//     -v "$PWD/tests/smoke_ui.js":/home/pptruser/s.js:ro -w /home/pptruser \
//     ghcr.io/puppeteer/puppeteer:latest node s.js http://host.docker.internal:18093 admin pw
//
// Exits non-zero if any check fails or the page logs an uncaught error.
const puppeteer = require('puppeteer');

const BASE = process.argv[2] || 'http://host.docker.internal:18093';
const USER = process.argv[3] || 'admin';
const PASS = process.argv[4] || 'pw';

let failures = 0;
function check(label, condition, detail) {
    const ok = !!condition;
    if (!ok) failures++;
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${detail !== undefined ? ' -> ' + JSON.stringify(detail) : ''}`);
}

(async () => {
    const browser = await puppeteer.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 1000 });
    await page.authenticate({ username: USER, password: PASS });

    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));
    // The panel has no alert/confirm/prompt any more; one appearing is a failure.
    const nativeDialogs = [];
    page.on('dialog', (d) => { nativeDialogs.push(d.message()); d.dismiss(); });
    // Everything is served from the panel itself (no CDN): record every origin asked.
    const foreignRequests = new Set();
    page.on('request', (r) => {
        const url = r.url();
        if (!url.startsWith('data:') && !url.startsWith('blob:') && new URL(url).origin !== new URL(BASE).origin) {
            foreignRequests.add(new URL(url).origin);
        }
    });

    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }]);
    await page.evaluateOnNewDocument(() => localStorage.removeItem('amnezia_theme'));
    await page.goto(BASE + '/', { waitUntil: 'networkidle2', timeout: 60000 });
    await new Promise((r) => setTimeout(r, 3000));

    // Theme: two states. With nothing saved the OS preference picks the first one;
    // the button toggles light/dark and remembers the choice.
    const themeState = () => page.evaluate(() => `${document.body.classList.contains('dark') ? 'dark' : 'light'}:${localStorage.getItem('amnezia_theme')}`);
    check('first visit follows the OS dark preference', await themeState() === 'dark:null', await themeState());
    const toggles = [];
    for (let i = 0; i < 2; i++) {
        await page.click('#themeToggleBtn');
        toggles.push(await themeState());
    }
    check('theme button toggles light/dark and remembers the choice',
        toggles.join(' ') === 'light:light dark:dark', toggles);

    const servers = await page.evaluate(() => (amneziaApp.lastServers || []).map((s) => ({ id: s.id, protocol: s.protocol })));
    check('at least one server exists to test against', servers.length > 0, servers.length);
    if (!servers.length) { await browser.close(); process.exit(1); }

    // Views live in modals.js, forms in forms.js; both install onto AmneziaApp.prototype.
    check('modals.js and forms.js loaded and installed', await page.evaluate(() =>
        typeof window.ModalUi === 'function' && typeof window.FormUi === 'function'
        && typeof amneziaApp.openServerSettings === 'function' && typeof amneziaApp.showServerLogs === 'function'));
    const drawerOpen = () => page.evaluate(() => !document.getElementById('drawerRoot').hidden);
    const visible = (id) => page.evaluate((i) => {
        const el = document.getElementById(i);
        return !!el && getComputedStyle(el).display !== 'none';
    }, id);

    for (const theme of ['light', 'dark']) {
        console.log(`\n[${theme}]`);
        await page.evaluate((t) => document.body.classList.toggle('dark', t === 'dark'), theme);

        for (const server of servers) {
            const { id, protocol } = server;
            const awg3 = protocol === 'AWG 3.0' || protocol === 'AWG 3.1';
            const awg31 = protocol === 'AWG 3.1';

            await page.evaluate((s) => amneziaApp.showServerConfig(s), id);
            await new Promise((r) => setTimeout(r, 1200));
            check(`${protocol} settings drawer opens`, await drawerOpen());
            check(`${protocol} protocol select present`, await page.evaluate((p) =>
                document.getElementById('t-protocol')?.value === p, protocol));
            check(`${protocol} header protection key ${awg3 ? 'visible' : 'hidden'}`, await visible('t-HeaderProtectionKeyRow') === awg3);
            check(`${protocol} 3.1 options ${awg31 ? 'visible' : 'hidden'}`, await visible('t-Awg31OptionsRow') === awg31);
            check(`${protocol} help is collapsed by default`,
                await page.evaluate(() => [...document.querySelectorAll('#drawerBody details.help')].every((d) => !d.open)));
            // Help text must be readable in both themes (the /60 opacity bug).
            check(`${protocol} help text has non-transparent colour`,
                await page.evaluate(() => {
                    const s = document.querySelector('#drawerBody details.help summary');
                    return s ? !getComputedStyle(s).color.includes('rgba(0, 0, 0, 0)') : false;
                }));
            check(`${protocol} settings start with nothing to save`, await page.evaluate(() => {
                const b = document.getElementById('drawerPrimary');
                return b.disabled && /No changes/.test(b.textContent);
            }));
            await page.evaluate(() => window.Ui.closeDrawer());
            check(`${protocol} closing the drawer hides it`, !(await drawerOpen()));

            await page.evaluate((s) => amneziaApp.addClient(s), id);
            await new Promise((r) => setTimeout(r, 600));
            check(`${protocol} add-client Jc field`, await page.evaluate(() => !!document.getElementById('c-Jc')));
            check(`${protocol} add-client AWG3 timings ${awg3 ? 'present' : 'absent'}`,
                await page.evaluate((want) => !!document.getElementById('c-KeepaliveTimeout') === want, awg3));
            await page.evaluate(() => window.Ui.closeDrawer());

            const clientId = await page.evaluate(async (s) => {
                const r = await amneziaApp.apiFetch(`/api/servers/${s}/clients`);
                const c = await r.json();
                amneziaApp.serverClients.set(s, c);
                return c[0] && c[0].id;
            }, id);
            if (!clientId) { check(`${protocol} has a client to inspect`, false); continue; }

            await page.evaluate((s, c) => amneziaApp.showClientParamsModal(s, c), id, clientId);
            await new Promise((r) => setTimeout(r, 1200));
            check(`${protocol} client params Jc field`, await page.evaluate(() => !!document.getElementById('c-Jc')));
            check(`${protocol} client AWG3 fields ${awg3 ? 'present' : 'absent'}`,
                await page.evaluate((want) => !!document.getElementById('c-RekeyAfterTime') === want, awg3));
            await page.evaluate(() => {
                const input = document.getElementById('c-Jc');
                input.value = String(Number(input.value || 8) + 1);
                input.dispatchEvent(new Event('input', { bubbles: true }));
            });
            await new Promise((r) => setTimeout(r, 1000));  // the server checks the change
            check(`${protocol} editing a field arms the save button`, await page.evaluate(() => {
                const b = document.getElementById('drawerPrimary');
                return !b.disabled && /Save changes/.test(b.textContent);
            }));
            await page.evaluate(() => window.Ui.closeDrawer());

            await page.evaluate((s, c) => amneziaApp.showClientQRCode(s, c), id, clientId);
            await new Promise((r) => setTimeout(r, 1500));
            check(`${protocol} QR code rendered`, await page.evaluate(() => !!document.querySelector('#qrModal canvas, #qrModal img')));
            // Showing the QR hands the config out, which the server records.
            check(`${protocol} showing the QR records the config as issued`, await page.evaluate(async (s, c) => {
                const r = await amneziaApp.apiFetch(`/api/servers/${s}/clients`);
                const client = (await r.json()).find((x) => x.id === c);
                return !!client && client.config_issued_at !== null && client.config_outdated === false;
            }, id, clientId));
            await page.evaluate(() => window.Ui.closeDialog());

            await page.evaluate((s) => amneziaApp.showServerLogs(s), id);
            await new Promise((r) => setTimeout(r, 1200));
            check(`${protocol} logs view renders`, await page.evaluate(() => !!document.getElementById('serverLogContent')));
            // The code box follows the theme (it used to stay dark on a light page).
            check(`${protocol} log box matches the ${theme} theme`, await page.evaluate((t) => {
                const [r, g, b] = getComputedStyle(document.getElementById('serverLogContent')).backgroundColor.match(/\d+/g).map(Number);
                return (r + g + b > 384) === (t === 'light');
            }, theme));
            await page.evaluate(() => window.Ui.closeDialog());

            await page.evaluate((s) => amneziaApp.showRawServerConfig(s), id);
            await new Promise((r) => setTimeout(r, 800));
            check(`${protocol} full config view shows the .conf`, await page.evaluate(() =>
                /\[Interface\]/.test(document.getElementById('rawConfigText')?.textContent || '')));
            await page.evaluate(() => window.Ui.closeDialog());
            check(`${protocol} closing a view hides it`, await page.evaluate(() => document.getElementById('dialogRoot').hidden));
        }

        await page.evaluate(() => amneziaApp.openCreateServerModal());
        await new Promise((r) => setTimeout(r, 600));
        check('create form offers all four protocols', await page.evaluate(() =>
            [...document.getElementById('t-protocol').options].map((o) => o.value).join(',') === 'AWG 1.5,AWG 2.0,AWG 3.0,AWG 3.1'));
        check('AWG 3.0 random params respect the S >= 12 floor', await page.evaluate(() => {
            document.getElementById('t-protocol').value = 'AWG 3.0';
            amneziaApp.toggleProtocolFields('AWG 3.0', 't-');
            amneziaApp.generateRandomParams();
            return ['S1', 'S2', 'S3', 'S4'].every((k) => Number(document.getElementById('t-' + k).value) >= 12);
        }));
        check('AWG 3.1 options become visible', await page.evaluate(() => {
            document.getElementById('t-protocol').value = 'AWG 3.1';
            amneziaApp.toggleProtocolFields('AWG 3.1', 't-');
            return getComputedStyle(document.getElementById('t-Awg31OptionsRow')).display !== 'none';
        }));
        const createSubmission = await page.evaluate(async () => {
            const originalApiFetch = amneziaApp.apiFetch;
            const originalLoadServers = amneziaApp.loadServers;
            let request = null;

            const name = document.getElementById('f-name');
            name.value = 'Smoke Test Server';
            name.dispatchEvent(new Event('input', { bubbles: true }));
            // Validation still goes to the server; only the create call is caught.
            amneziaApp.apiFetch = async (url, options) => {
                if (url !== '/api/servers') return originalApiFetch.call(amneziaApp, url, options);
                request = { url, method: options?.method, payload: JSON.parse(options?.body || '{}') };
                return { ok: true, status: 200, json: async () => ({ name: 'Smoke Test Server' }) };
            };
            amneziaApp.loadServers = async () => {};

            try {
                await new Promise((resolve) => setTimeout(resolve, 900));
                document.getElementById('drawerForm').requestSubmit();
                await new Promise((resolve) => setTimeout(resolve, 900));
                return request;
            } finally {
                amneziaApp.apiFetch = originalApiFetch;
                amneziaApp.loadServers = originalLoadServers;
            }
        });
        check('create form submits the selected AWG 3.1 payload',
            createSubmission?.url === '/api/servers'
                && createSubmission?.method === 'POST'
                && createSubmission?.payload?.protocol === 'AWG 3.1'
                && createSubmission?.payload?.transport_params?.RandomTrailers === false
                && createSubmission?.payload?.transport_params?.DisableCookies === false,
            createSubmission);
        await page.evaluate(() => window.Ui.closeDrawer());
    }

    console.log('');
    check('no request left the panel\'s origin', foreignRequests.size === 0, [...foreignRequests]);
    check('no uncaught page errors', pageErrors.length === 0, pageErrors);
    check('no alert/confirm/prompt appeared', nativeDialogs.length === 0, nativeDialogs);
    await browser.close();

    console.log(`\n${failures === 0 ? 'OK' : failures + ' FAILURE(S)'}`);
    process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
    console.error('smoke test crashed:', e);
    process.exit(1);
});
