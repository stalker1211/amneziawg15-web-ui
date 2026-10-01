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
    // nginx sends script-src 'self'; a violation is reported on the console, not as an error.
    const cspViolations = [];
    page.on('console', (m) => { if (/Content Security Policy/i.test(m.text())) cspViolations.push(m.text()); });
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

    // One request draws the page: the servers carry their clients and traffic; then each
    // running server's last hour, once, for the charts. (The live-update stream,
    // /api/events, is opened once too, and loads no data itself.)
    const apiRequests = [];
    const recordApi = (r) => { if (new URL(r.url()).pathname.startsWith('/api/')) apiRequests.push(new URL(r.url()).pathname); };
    page.on('request', recordApi);
    await page.reload({ waitUntil: 'networkidle2', timeout: 60000 });
    await new Promise((r) => setTimeout(r, 1500));
    page.off('request', recordApi);
    const runningIds = await page.evaluate(() => amneziaApp.lastServers.filter((s) => s.status === 'running').map((s) => s.id).sort());
    const historyRequests = apiRequests.filter((u) => /^\/api\/servers\/[^/]+\/traffic$/.test(u)).sort();
    check('a page load asks /api/servers once, and each running server\'s last hour once',
        apiRequests.filter((u) => u === '/api/servers').length === 1 && apiRequests.filter((u) => u === '/api/events').length === 1
            && JSON.stringify(historyRequests) === JSON.stringify(runningIds.map((id) => `/api/servers/${id}/traffic`))
            && apiRequests.every((u) => ['/api/servers', '/api/system/status', '/api/events'].includes(u) || historyRequests.includes(u)),
        apiRequests);

    // Telemetry patches the rows in place: buttons, focus and an open menu survive.
    check('a traffic update patches the rows in place', await page.evaluate(() => {
        const server = amneziaApp.lastServers.find((s) => s.status === 'running' && (s.clients || []).some((c) => !c.suspended));
        if (!server) return false;
        const client = server.clients.find((c) => !c.suspended);
        const row = () => document.querySelector(`#clients-${server.id} li[data-client-id="${client.id}"]`);
        const edit = [...row().querySelectorAll('button')].find((b) => /Edit/.test(b.textContent));
        edit.focus();
        const before = server.traffic[client.id] || {};
        // A live tick since the render leaves its flash on; start from none.
        row().querySelectorAll('.traffic-flash').forEach((a) => a.classList.remove('traffic-flash'));
        amneziaApp.updateServerTraffic(server.id, { ...server.traffic, [client.id]: {
            ...before, endpoint: '198.51.100.99:4242', latest_handshake_seconds: 3, active: true,
            sent_bytes: (before.sent_bytes || 0) + 5 * 1024 * 1024 } });
        // ↓ is the device's download: what the server sent it.
        return edit.isConnected && document.activeElement === edit && /198\.51\.100\.99/.test(row().textContent)
            && /handshake 3 s ago/.test(row().textContent)
            && row().querySelector('[data-cell="down-arrow"]').classList.contains('traffic-flash')
            && !row().querySelector('[data-cell="up-arrow"]').classList.contains('traffic-flash');
    }));
    await page.evaluate(() => amneziaApp.loadServers());
    await new Promise((r) => setTimeout(r, 500));

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

        // Traffic (step 13): a band on running cards, a sparkline on online rows, the
        // dialog from the band, from ⋯ and from the totals, its ranges, readout, highlight and pin.
        check('a traffic band on running cards only, with a chart', await page.evaluate(() => amneziaApp.lastServers.every((s) => {
            const band = document.querySelector(`article[data-server-id="${s.id}"] [data-action="server-traffic"]`);
            return s.status === 'running' ? !!band?.querySelector('[data-band-plot] svg path') : !band;
        })));
        // Beside it, the interface's totals since it came up (2.6): a size and a start time.
        check('a totals box on running cards only, with its sizes and start', await page.evaluate(() => amneziaApp.lastServers.every((s) => {
            const box = document.querySelector(`article[data-server-id="${s.id}"] [data-total]`);
            if (s.status !== 'running') return !box;
            const text = box?.textContent.replace(/\s+/g, ' ') || '';
            return /Total/i.test(text) && /since \S/.test(text) && /↓ [\d.]+ (B|KiB|MiB|GiB|TiB)/.test(text) && /↑ [\d.]+ /.test(text);
        })));
        check('a sparkline under the totals on online rows only', await page.evaluate(() => amneziaApp.lastServers.every((s) =>
            (s.clients || []).every((c) => {
                const cell = document.querySelector(`li[data-client-id="${c.id}"] [data-cell="spark"]`);
                const online = window.ServerUi.isOnline(s, c, (s.traffic || {})[c.id], (t) => !!t?.active);
                return online ? !!cell?.querySelector('svg') && /bit\/s/.test(cell.textContent) && cell.offsetHeight > 0
                    : !!cell && cell.offsetHeight === 0 && !cell.textContent.trim();
            }))));
        check('the strip shows the traffic now and its last hour', await page.evaluate(() =>
            /^\d/.test(document.getElementById('stripTrafficNow').textContent)
            && !!document.querySelector('#stripTrafficChart svg')));
        const dialog = await page.evaluate(async () => {
            const server = amneziaApp.lastServers.find((s) => s.status === 'running' && (s.clients || []).length);
            const wait = (ms) => new Promise((r) => setTimeout(r, ms));
            document.querySelector(`[data-action="server-traffic"][data-server="${server.id}"]`).click();
            await wait(900);
            const out = { fromBand: /Traffic/.test(document.getElementById('dialogTitle')?.textContent || '') };
            out.rows = document.querySelectorAll('[data-traffic-client]').length === server.clients.length;
            out.chart = !!document.querySelector('#trafficPlot svg path[d]');
            const ranges = {};
            for (const r of ['1h', '6h', '24h']) {
                document.querySelector(`#dialog [data-range="${r}"]`).click();
                await wait(700);
                ranges[r] = document.querySelector(`#dialog [data-range="${r}"]`).getAttribute('aria-checked') === 'true'
                    && amneziaApp.trafficView?.data?.range === r
                    && new RegExp(`in the last ${r.replace('h', ' h')}`).test(document.getElementById('trafficSummary').textContent);
            }
            out.ranges = ranges;
            // The readout at the newest tick, the one point every history has (a fresh
            // container's starts at its boot).
            document.querySelector('#dialog [data-range="1h"]').click();
            await wait(700);
            const plot = document.getElementById('trafficPlot');
            const box = plot.getBoundingClientRect();
            const at = { clientX: box.right - 2, clientY: box.top + 50, bubbles: true, pointerType: 'mouse' };
            plot.dispatchEvent(new PointerEvent('pointermove', at));
            const tip = document.getElementById('trafficTip');
            out.readout = !tip.hidden && !document.getElementById('trafficCross').hidden && /download/.test(tip.textContent);
            const row = document.querySelector('[data-traffic-client]');
            row.dispatchEvent(new PointerEvent('pointerenter', { pointerType: 'mouse' }));
            out.highlight = amneziaApp.trafficView.emph === row.dataset.trafficClient && row.classList.contains('bg-gray-100')
                && document.querySelectorAll('#trafficPlot path').length > 6;
            row.click();
            row.dispatchEvent(new PointerEvent('pointerleave', { pointerType: 'mouse' }));
            out.pinned = amneziaApp.trafficView.pinned === row.dataset.trafficClient && amneziaApp.trafficView.emph === row.dataset.trafficClient;
            plot.dispatchEvent(new PointerEvent('pointerleave', { pointerType: 'mouse', bubbles: true }));
            out.cleared = document.getElementById('trafficTip').hidden;
            window.Ui.closeDialog();
            out.closed = amneziaApp.trafficView === null;
            amneziaApp.openServerMenu(server.id, document.querySelector(`[data-action="server-menu"][data-server="${server.id}"]`));
            [...document.querySelectorAll('#menu button')].find((b) => /Traffic/.test(b.textContent))?.click();
            await wait(700);
            out.fromMenu = amneziaApp.trafficView?.serverId === server.id && !!document.querySelector('#trafficPlot svg');
            window.Ui.closeDialog();
            // The totals box beside the band opens the same dialog (2.6).
            document.querySelector(`[data-total="${server.id}"]`).click();
            await wait(700);
            out.fromTotals = amneziaApp.trafficView?.serverId === server.id && !!document.querySelector('#trafficPlot svg');
            window.Ui.closeDialog();
            return out;
        });
        check('the Traffic dialog: from the band, every range, the readout, highlight, pin, from ⋯ and from the totals',
            Object.values(dialog).every((v) => (typeof v === 'object' ? Object.values(v).every(Boolean) : v)), dialog);

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
                await amneziaApp.loadServers();
                const c = (amneziaApp.lastServers.find((x) => x.id === s) || {}).clients || [];
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
            // Generate for I1-I5: QUIC first (no host), then DNS for a host.
            check(`${protocol} I1-I5 generator starts on QUIC without a host`, await page.evaluate(() =>
                document.getElementById('c-iProfile')?.value === 'quic' && document.getElementById('c-iHostBox').hidden));
            await page.evaluate(() => document.querySelector('[data-action="generate-signatures"]').click());
            await new Promise((r) => setTimeout(r, 1200));
            check(`${protocol} Generate fills I1-I5 with QUIC packets`, await page.evaluate(() =>
                /^<b 0xc[0-3]00000001/.test(document.getElementById('c-I1').value)
                && /^<b 0x[4-7]/.test(document.getElementById('c-I5').value)));
            await page.evaluate(() => {
                const select = document.getElementById('c-iProfile');
                select.value = 'dns';
                select.dispatchEvent(new Event('change', { bubbles: true }));
                document.getElementById('c-iHost').value = 'ya.ru';
                document.querySelector('[data-action="generate-signatures"]').click();
            });
            await new Promise((r) => setTimeout(r, 1200));
            check(`${protocol} DNS shows the host and asks for it`, await page.evaluate(() =>
                !document.getElementById('c-iHostBox').hidden
                && document.getElementById('c-I1').value.includes('02796102727500')  // ya.ru as labels
                && document.getElementById('c-I3').value === ''));
            check(`${protocol} the port hint shows off port 53`, await page.evaluate(() =>
                !document.getElementById('c-iNote').hidden && /UDP 53/.test(document.getElementById('c-iNote').textContent)));
            check(`${protocol} generated packets pass the check and can be saved`, await page.evaluate(() =>
                !document.getElementById('drawerPrimary').disabled));
            // Generate per group: the junk row, and on AWG 3.x the timers and padding;
            // each fills its own fields only.
            const groups = await page.evaluate(async (want3) => {
                const wait = (ms) => new Promise((r) => setTimeout(r, ms));
                const val = (k) => document.getElementById(`c-${k}`)?.value ?? null;
                const button = (g) => document.querySelector(`[data-action="generate-client-group"][data-group="${g}"]`);
                const i1 = val('I1');
                const timers = val('RekeyAfterTime');
                button('junk').click();
                await wait(1200);
                const [jc, jmin, jmax] = ['Jc', 'Jmin', 'Jmax'].map((k) => Number(val(k)));
                const out = {
                    junk: jc >= 4 && jc <= 12 && jmax - jmin >= 64 && jmax <= 160,
                    junkOnly: val('I1') === i1 && val('RekeyAfterTime') === timers,
                    awg3Button: !!button('awg3') === want3,
                };
                if (want3) {
                    button('awg3').click();
                    await wait(1200);
                    out.awg3 = /^\d+-\d+$/.test(val('RejectAfterTime')) && /^\d+-\d+$/.test(val('ContentPaddingAddition'))
                        && Number(val('RekeyAfterTime').split('-')[1]) < Number(val('RejectAfterTime').split('-')[0]);
                    out.awg3Only = Number(val('Jc')) === jc && val('I1') === i1;
                }
                out.saveable = !document.getElementById('drawerPrimary').disabled;
                return out;
            }, awg3);
            check(`${protocol} Generate per group fills its own fields, and they pass the check`,
                Object.values(groups).every(Boolean), groups);
            await page.evaluate(() => window.Ui.closeDrawer());

            await page.evaluate((s, c) => amneziaApp.showClientQRCode(s, c), id, clientId);
            await new Promise((r) => setTimeout(r, 1500));
            check(`${protocol} QR code rendered`, await page.evaluate(() => !!document.querySelector('#qrModal canvas, #qrModal img')));
            // Showing the QR hands the config out, which the server records.
            check(`${protocol} showing the QR records the config as issued`, await page.evaluate(async (s, c) => {
                const r = await amneziaApp.apiFetch('/api/servers');
                const client = ((await r.json()).find((x) => x.id === s)?.clients || []).find((x) => x.id === c);
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
        const startsFrom = await page.evaluate(() => {
            const servers = amneziaApp.lastServers;
            const newest = servers.reduce((a, s) => (s.created_at >= a.created_at ? s : a));
            const form = {
                mtu: Number(document.getElementById('f-mtu').value), dns: document.getElementById('f-dns').value,
                nat: document.getElementById('f-nat').checked, lan: document.getElementById('f-lan').checked,
            };
            const want = { mtu: newest.mtu, dns: [].concat(newest.dns).join(', '), nat: newest.enable_nat, lan: newest.block_lan_cidrs };
            const sub = document.getElementById('drawerSub').textContent;
            return { ok: JSON.stringify(form) === JSON.stringify(want) && sub.includes(`copied from ${newest.name}`), form, want, newest: newest.name };
        });
        check('create form starts from the newest server (MTU, DNS, NAT, LAN) and says so', startsFrom.ok, startsFrom);
        check('create form offers all four protocols', await page.evaluate(() =>
            [...document.getElementById('t-protocol').options].map((o) => o.value).join(',') === 'AWG 1.5,AWG 2.0,AWG 3.0,AWG 3.1'));
        check('create form opens with generated parameters, not a fixed set', await page.evaluate(() => {
            const v = (k) => document.getElementById('t-' + k).value;
            return /^\d+$/.test(v('S1')) && v('S1') !== '50' && Number(v('H1')) > 4000 && Number(v('H4')) > Number(v('H1'));
        }));
        check('AWG 3.0 random params respect the S >= 12 floor and bring a key', await page.evaluate(async () => {
            const select = document.getElementById('t-protocol');
            select.value = 'AWG 3.0';
            select.dispatchEvent(new Event('change', { bubbles: true })); // regenerates: nothing was typed
            await new Promise((r) => setTimeout(r, 500));
            return ['S1', 'S2', 'S3', 'S4'].every((k) => Number(document.getElementById('t-' + k).value) >= 12)
                && /^[A-Za-z0-9+/]{43}=$/.test(document.getElementById('t-HeaderProtectionKey').value)
                && /^\d+-\d+$/.test(document.getElementById('t-H1').value);
        }));
        check('Randomize keeps a header protection key that is already there', await page.evaluate(async () => {
            const key = document.getElementById('t-HeaderProtectionKey').value;
            const h1 = document.getElementById('t-H1').value;
            await amneziaApp.generateRandomParams();
            return document.getElementById('t-HeaderProtectionKey').value === key && document.getElementById('t-H1').value !== h1;
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

    // At phone width nothing scrolls sideways, the page or the Traffic dialog.
    await page.setViewport({ width: 390, height: 900 });
    await new Promise((r) => setTimeout(r, 500));
    const narrow = await page.evaluate(async () => {
        const page = document.documentElement.scrollWidth;
        const server = amneziaApp.lastServers.find((s) => s.status === 'running');
        amneziaApp.showServerTraffic(server.id);
        await new Promise((r) => setTimeout(r, 900));
        const dialog = document.documentElement.scrollWidth;
        window.Ui.closeDialog();
        return { page, dialog };
    });
    check('no horizontal scroll at 390 px, with the dialog open too', narrow.page <= 390 && narrow.dialog <= 390, narrow);
    await page.setViewport({ width: 1280, height: 1000 });

    // AllowedIPs edits arm Save without a warning; an endpoint host counts every
    // client config it changes, without the restart a transport change needs.
    check('AllowedIPs edit arms Save, no warning', await page.evaluate(async () => {
        const server = amneziaApp.lastServers.find((s) => (s.clients || []).length);
        const client = server.clients[0];
        amneziaApp.showClientParamsModal(server.id, client.id);
        await new Promise((r) => setTimeout(r, 700));
        const field = document.getElementById('c-allowed_ips');
        field.value = field.value.includes('::/0') ? '0.0.0.0/0' : '0.0.0.0/0, ::/0';
        field.dispatchEvent(new Event('input', { bubbles: true }));
        await new Promise((r) => setTimeout(r, 900));
        const warned = /IPv6/.test(document.getElementById('checks').textContent);
        const armed = !document.getElementById('drawerPrimary').disabled;
        window.Ui.closeDrawer();
        return armed && !warned;
    }));
    check('switching the protocol re-checks the whole form', await page.evaluate(async () => {
        const server = amneziaApp.lastServers.find((s) => s.protocol === 'AWG 2.0');
        amneziaApp.showServerConfig(server.id);
        await new Promise((r) => setTimeout(r, 900));
        const set = (id, v) => { const el = document.getElementById(id); el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); };
        const select = document.getElementById('t-protocol');
        select.value = 'AWG 2.0';
        select.dispatchEvent(new Event('change', { bubbles: true }));
        set('t-S4', '1');
        await new Promise((r) => setTimeout(r, 900));
        select.value = 'AWG 3.0';
        select.dispatchEvent(new Event('change', { bubbles: true }));
        await new Promise((r) => setTimeout(r, 900));
        const off = /header protection is off.*S4 \(1\) is below/.test(document.getElementById('checks').textContent);
        await amneziaApp.fillHeaderProtectionKey('t-HeaderProtectionKey');
        await new Promise((r) => setTimeout(r, 900));
        const refused = /S4 must be at least 12/.test(document.getElementById('checks').textContent)
            && document.getElementById('drawerPrimary').disabled;
        window.Ui.closeDrawer();
        return off && refused;
    }));
    check('an endpoint host previews its re-imports without a restart', await page.evaluate(async () => {
        const server = amneziaApp.lastServers.find((s) => s.status === 'running' && (s.clients || []).length);
        amneziaApp.showServerConfig(server.id);
        await new Promise((r) => setTimeout(r, 900));
        const host = document.getElementById('s-endpoint_host');
        host.value = 'vpn.example.com';
        host.dispatchEvent(new Event('input', { bubbles: true }));
        await new Promise((r) => setTimeout(r, 900));
        const impact = document.getElementById('impact').textContent;
        window.Ui.closeDrawer();
        return /client config/.test(impact) && !/restarts/.test(impact);
    }));

    // Panel settings: pinned fields are read-only, a save reaches the new-server
    // defaults, and the credential needs its repeat and the current password.
    const settingsPinned = await page.evaluate(async () => {
        const data = await amneziaApp.getJson('/api/settings');
        await amneziaApp.openSettings();
        await new Promise((r) => setTimeout(r, 800));
        const disabled = [...document.querySelectorAll('#drawerBody input:disabled, #drawerBody select:disabled')]
            .map((e) => e.id).filter((id) => !['s-user', 's-password', 's-password2'].includes(id)).sort();
        const pinned = Object.keys(data.sources).filter((k) => data.sources[k] === 'env').map((k) => `s-${k}`).sort();
        return { disabled, pinned, banner: !document.getElementById('passwordBanner').hidden, isDefault: data.access.password_is_default };
    });
    check('settings drawer shows exactly the env-pinned fields read-only, and the banner follows the credential',
        JSON.stringify(settingsPinned.disabled) === JSON.stringify(settingsPinned.pinned)
            && settingsPinned.banner === settingsPinned.isDefault, settingsPinned);
    const settingsSaved = await page.evaluate(async () => {
        if (document.getElementById('s-default_port')) return 'the retired New servers section is still there';
        const level = document.getElementById('s-log_level');
        if (!level || level.disabled) return 'panel level missing or pinned';
        level.value = level.value === 'WARNING' ? 'INFO' : 'WARNING';
        level.dispatchEvent(new Event('change', { bubbles: true }));
        await new Promise((r) => setTimeout(r, 900));
        const button = document.getElementById('drawerPrimary');
        if (button.disabled) return `save not armed: ${document.getElementById('checks').textContent}`;
        document.getElementById('drawerForm').requestSubmit();
        await new Promise((r) => setTimeout(r, 900));
        if (window.Ui.isDrawerOpen()) return 'drawer still open';
        const saved = await amneziaApp.getJson('/api/settings');
        return saved.values.log_level === level.value ? 'saved' : `stored ${saved.values.log_level}`;
    });
    check('saving settings stores the change (and the New servers section is gone)', settingsSaved === 'saved', settingsSaved);
    check('the credential needs its repeat and the current password', await page.evaluate(async () => {
        await amneziaApp.openSettings();
        await new Promise((r) => setTimeout(r, 600));
        const password = document.getElementById('s-password');
        password.value = 'n3w-secret-1';
        password.dispatchEvent(new Event('input', { bubbles: true }));
        await new Promise((r) => setTimeout(r, 900));
        const text = document.getElementById('checks').textContent;
        const blocked = document.getElementById('drawerPrimary').disabled;
        window.Ui.closeDrawer();
        return blocked && /repeat differ/.test(text) && /current password/.test(text);
    }));

    console.log('');
    check('no request left the panel\'s origin', foreignRequests.size === 0, [...foreignRequests]);
    check('no uncaught page errors', pageErrors.length === 0, pageErrors);
    check('no Content-Security-Policy violation', cspViolations.length === 0, cspViolations);
    check('no alert/confirm/prompt appeared', nativeDialogs.length === 0, nativeDialogs);
    await browser.close();

    console.log(`\n${failures === 0 ? 'OK' : failures + ' FAILURE(S)'}`);
    process.exit(failures === 0 ? 0 : 1);
})().catch((e) => {
    console.error('smoke test crashed:', e);
    process.exit(1);
});
