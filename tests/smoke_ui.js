// Frontend smoke test: opens every modal in both themes and asserts the fields
// that matter are present and wired. There is no JS unit-test framework in this
// project on purpose; this script is the frontend safety net.
//
// Needs a running container with the source bind-mounted, plus at least one
// server. Usage:
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
    page.on('dialog', (d) => d.accept());

    await page.goto(BASE + '/', { waitUntil: 'networkidle2', timeout: 60000 });
    await new Promise((r) => setTimeout(r, 3000));

    const servers = await page.evaluate(() => (amneziaApp.lastServers || []).map((s) => ({ id: s.id, protocol: s.protocol })));
    check('at least one server exists to test against', servers.length > 0, servers.length);
    if (!servers.length) { await browser.close(); process.exit(1); }

    // Modal methods live in modals.js and are installed onto AmneziaApp.prototype.
    check('modals.js loaded and installed', await page.evaluate(() =>
        typeof window.ModalUi === 'function' && typeof amneziaApp.displayServerConfigModal === 'function'));

    for (const theme of ['light', 'dark']) {
        console.log(`\n[${theme}]`);
        await page.evaluate((t) => document.body.classList.toggle('dark', t === 'dark'), theme);

        for (const server of servers) {
            const { id, protocol } = server;
            const awg3 = protocol === 'AWG 3.0' || protocol === 'AWG 3.1';
            const awg31 = protocol === 'AWG 3.1';

            await page.evaluate((s) => amneziaApp.showServerConfig(s), id);
            await new Promise((r) => setTimeout(r, 1200));
            check(`${protocol} server modal renders`, await page.evaluate(() => !!document.getElementById('configModal')));
            check(`${protocol} protocol select present`, await page.evaluate((s) => !!document.getElementById(`serverProtocol-${s}`), id));
            check(`${protocol} HeaderProtectionKey row ${awg3 ? 'visible' : 'hidden'}`,
                await page.evaluate((s, want) => {
                    const row = document.getElementById(`serverTransportParam-${s}-HeaderProtectionKeyRow`);
                    if (!row) return false;
                    return (getComputedStyle(row).display !== 'none') === want;
                }, id, awg3));
            check(`${protocol} 3.1 options ${awg31 ? 'visible' : 'hidden'}`,
                await page.evaluate((s, want) => {
                    const row = document.getElementById(`serverTransportParam-${s}-Awg31OptionsRow`);
                    if (!row) return false;
                    return (getComputedStyle(row).display !== 'none') === want;
                }, id, awg31));
            check(`${protocol} help is collapsed by default`,
                await page.evaluate(() => [...document.querySelectorAll('#configModal details.param-help')].every((d) => !d.open)));
            // Help text must be readable in both themes (the /60 opacity bug).
            check(`${protocol} help text has non-transparent colour`,
                await page.evaluate(() => {
                    const s = document.querySelector('#configModal details.param-help summary');
                    return s ? !getComputedStyle(s).color.includes('rgba(0, 0, 0, 0)') : false;
                }));
            await page.evaluate(() => amneziaApp.closeModal());
            check(`${protocol} closeModal removes it`, await page.evaluate(() => !document.getElementById('configModal')));

            await page.evaluate((s) => amneziaApp.addClient(s), id);
            await new Promise((r) => setTimeout(r, 600));
            check(`${protocol} add-client Jc field`, await page.evaluate((s) => !!document.getElementById(`newClientParam-${s}-Jc`), id));
            check(`${protocol} add-client AWG3 timings ${awg3 ? 'present' : 'absent'}`,
                await page.evaluate((s, want) => !!document.getElementById(`newClientParam-${s}-KeepaliveTimeout`) === want, id, awg3));
            await page.evaluate(() => amneziaApp.closeModal());

            const clientId = await page.evaluate(async (s) => {
                const r = await amneziaApp.apiFetch(`/api/servers/${s}/clients`);
                const c = await r.json();
                amneziaApp.serverClients.set(s, c);
                return c[0] && c[0].id;
            }, id);
            if (!clientId) { check(`${protocol} has a client to inspect`, false); continue; }

            await page.evaluate((s, c) => amneziaApp.showClientParamsModal(s, c), id, clientId);
            await new Promise((r) => setTimeout(r, 1200));
            check(`${protocol} client params Jc field`, await page.evaluate((c) => !!document.getElementById(`clientParam-${c}-Jc`), clientId));
            check(`${protocol} client AWG3 fields ${awg3 ? 'present' : 'absent'}`,
                await page.evaluate((c, want) => !!document.getElementById(`clientParam-${c}-RekeyAfterTime`) === want, clientId, awg3));
            check(`${protocol} editing a field arms the save button`,
                await page.evaluate((c) => {
                    const input = document.getElementById(`clientParam-${c}-Jc`);
                    input.value = String(Number(input.value || 8) + 1);
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    return /Update/.test(document.getElementById(`clientConfigPrimaryAction-${c}`).textContent);
                }, clientId));
            await page.evaluate(() => amneziaApp.closeModal());

            await page.evaluate((s, c) => amneziaApp.showClientQRCode(s, c), id, clientId);
            await new Promise((r) => setTimeout(r, 1500));
            check(`${protocol} QR code rendered`, await page.evaluate(() => !!document.querySelector('#qrModal canvas, #qrModal img')));
            await page.evaluate(() => amneziaApp.closeQRModal());

            await page.evaluate((s) => amneziaApp.showServerLogs(s, 'wg-' + s), id);
            await new Promise((r) => setTimeout(r, 1200));
            check(`${protocol} logs modal renders`, await page.evaluate(() => !!document.getElementById('serverLogContent')));
            await page.evaluate(() => amneziaApp.closeModal());
        }

        await page.evaluate(() => amneziaApp.openCreateServerModal());
        await new Promise((r) => setTimeout(r, 400));
        check('create form offers all four protocols', await page.evaluate(() =>
            [...document.getElementById('serverProtocol').options].map((o) => o.value).join(',') === 'AWG 1.5,AWG 2.0,AWG 3.0,AWG 3.1'));
        check('AWG 3.0 random params respect the S >= 12 floor', await page.evaluate(() => {
            document.getElementById('serverProtocol').value = 'AWG 3.0';
            amneziaApp.toggleProtocolFields('AWG 3.0', 'param');
            amneziaApp.generateRandomParams();
            return ['S1', 'S2', 'S3', 'S4'].every((k) => Number(document.getElementById('param' + k).value) >= 12);
        }));
        check('AWG 3.1 options become visible', await page.evaluate(() => {
            document.getElementById('serverProtocol').value = 'AWG 3.1';
            amneziaApp.toggleProtocolFields('AWG 3.1', 'param');
            return getComputedStyle(document.getElementById('paramAwg31OptionsRow')).display !== 'none';
        }));
        const createSubmission = await page.evaluate(async () => {
            const originalApiFetch = amneziaApp.apiFetch;
            const originalLoadServers = amneziaApp.loadServers;
            let request = null;

            document.getElementById('serverName').value = 'Smoke Test Server';
            amneziaApp.apiFetch = async (url, options) => {
                request = {
                    url,
                    method: options?.method,
                    payload: JSON.parse(options?.body || '{}'),
                };
                return {
                    ok: true,
                    status: 200,
                    json: async () => ({ name: 'Smoke Test Server' }),
                };
            };
            amneziaApp.loadServers = async () => {};

            try {
                document.getElementById('serverForm').requestSubmit();
                await new Promise((resolve) => setTimeout(resolve, 50));
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
        await page.evaluate(() => amneziaApp.closeCreateServerModal());
    }

    console.log('');
    check('no uncaught page errors', pageErrors.length === 0, pageErrors);
    await browser.close();

    console.log(`\n${failures === 0 ? 'OK' : failures + ' FAILURE(S)'}`);
    process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
    console.error('smoke test crashed:', e);
    process.exit(1);
});
