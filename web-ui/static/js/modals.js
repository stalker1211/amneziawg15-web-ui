// AmneziaWG Web UI - the views, shown as centred dialogs: a client's QR code, why it
// cannot connect, a server's traffic history, its daemon log and its raw .conf, and the
// Activity events.
//
// Methods of AmneziaApp, installed onto its prototype at the bottom of this file
// (like forms.js), so `this` is the app. Every interpolated value is escaped.
class ModalUi {
    // The Activity dialog's filters: an event's `kind` and its label.
    static ACTIVITY_KINDS = [['all', 'All'], ['change', 'Changes'], ['session', 'Sessions'], ['health', 'Health'], ['auth', 'Sign-ins']];

    // The code box every view uses; it follows the theme like the rest of the page.
    codeBoxHtml(text, id) {
        return `<pre id="${id}" class="max-h-[60vh] overflow-auto rounded-lg border border-gray-400 bg-gray-100 text-gray-900 dark:border-gray-900 dark:bg-gray-900 dark:text-gray-100 p-4 text-xs leading-5 font-mono whitespace-pre">${this.escapeHtml(text)}</pre>`;
    }

    // Showing the QR hands the config out: the server records it (POST .../issued),
    // which clears the client's Re-import flag.
    async showClientQRCode(serverId, clientId) {
        const server = (this.lastServers || []).find((s) => s.id === serverId) || {};
        const client = (server.clients || []).find((c) => c.id === clientId) || {};
        const safe = (v) => this.escapeHtml(v ?? '');
        const icon = window.Ui.icon;
        let config;
        try {
            config = await this.getJson(`/api/servers/${serverId}/clients/${clientId}/config-both`);
        } catch (error) {
            console.error('Error fetching config for QR code:', error);
            this.showTempMessage('Could not load the config: ' + error.message, 'error');
            return;
        }

        const issuedOn = window.ServerUi.stamp(client.config_issued_at);
        const notice = client.config_outdated
            ? `<p class="w-full rounded-lg border px-3 py-2 text-sm callout-rose">
                <strong class="font-semibold">This replaces the config on the device.</strong>
                The one handed out${issuedOn ? ` on ${safe(issuedOn)}` : ''} no longer matches. Scan or download it again.</p>`
            : '';
        const box = window.Ui.openDialog(`
            ${window.Ui.dialogHeader(safe(config.client_name || client.name), `<span class="font-mono">${safe(client.client_ip)}</span> · ${safe(server.name)} · ${safe(server.protocol)}`)}
            <div id="qrModal" class="px-5 pb-5 flex flex-col items-center gap-4">
                ${notice}
                <div class="rounded-xl bg-white p-3 border border-gray-400 dark:border-[#475569]">
                    <div id="qrcode" class="flex items-center justify-center"></div>
                </div>
                <p class="text-xs text-center text-gray-700 dark:text-[#bac5d4]">Scan with the AmneziaVPN or AmneziaWG app. The code holds this client's private key.</p>
                <div class="flex flex-wrap justify-center gap-2">
                    <button type="button" class="btn btn-primary" data-qr="copy">${icon('copy')}Copy config</button>
                    <button type="button" class="btn btn-secondary" data-qr="download">${icon('download')}Download .conf</button>
                    <button type="button" class="btn btn-ghost" data-qr="image">${icon('image')}QR image</button>
                </div>
                <details class="help w-full text-sm text-gray-800 dark:text-[#d7dee9]">
                    <summary class="font-medium">Config text</summary>
                    <div class="mt-2">${this.codeBoxHtml(config.clean_config, 'configText')}</div>
                </details>
            </div>`, { size: 'max-w-md' });

        // The QR carries the clean config (no comments): smaller, so it scans more
        // easily, and exactly the text the server fingerprints.
        const size = Math.max(200, Math.min(320, window.innerWidth - 112));
        this.generateQrIntoContainer(box.querySelector('#qrcode'), config.clean_config, size);
        const fileBase = String(config.client_name || client.name || 'client').replace(/[^A-Za-z0-9._-]+/g, '_');
        box.querySelector('[data-qr="copy"]').addEventListener('click', () => this.copyText(config.clean_config, 'Config'));
        box.querySelector('[data-qr="download"]').addEventListener('click', () => this.downloadClientConfig(serverId, clientId));
        box.querySelector('[data-qr="image"]').addEventListener('click', () => {
            const canvas = box.querySelector('#qrcode canvas');
            if (!canvas) return;
            const link = document.createElement('a');
            link.download = `${fileBase}_qr.png`;
            link.href = canvas.toDataURL('image/png');
            link.click();
        });
        this.markConfigIssued(serverId, clientId);
    }

    async markConfigIssued(serverId, clientId) {
        try {
            await this.postJson(`/api/servers/${serverId}/clients/${clientId}/issued`, {});
            this.loadServers();
        } catch (error) {
            console.error('Could not record the config as handed out:', error);
        }
    }

    // Why a client cannot connect: the evidence behind its row's Old config or Maybe
    // blocked pill (its `diagnosis`, DEVELOPMENT.md §10, 2.8 *Contract*), as it stood
    // when the dialog opened.
    showClientDiagnosis(serverId, clientId) {
        const server = (this.lastServers || []).find((s) => s.id === serverId) || {};
        const client = (server.clients || []).find((c) => c.id === clientId) || {};
        const d = (server.traffic || {})[clientId]?.diagnosis;
        const kind = window.ServerUi.DIAGNOSES[d?.verdict];
        if (!kind) {
            this.showTempMessage(`${client.name || 'This client'} has no connection problem now`, 'info');
            return;
        }
        const safe = (v) => this.escapeHtml(v ?? '');
        const old = d.verdict === 'old_config';
        const t = ModalUi.diagnosisText(d);
        const cc = String(d.country || '').toUpperCase();
        const from = `${t.host ? ` from <span class="font-mono">${safe(t.host)}</span>` : ''}${cc ? ` (${window.ServerUi.flag(cc)} ${safe(cc)})` : ''}`;
        const handshakes = (n) => `${n} handshake${n === 1 ? '' : 's'}`;
        const lastDone = t.lastHandshake ? `Last completed handshake ${safe(t.lastHandshake)}.` : 'It has never completed a handshake.';
        const evidence = old
            ? `<strong class="font-semibold">${safe(client.name)}</strong> is trying with parameters this server no longer uses${
                t.items.length ? `: ${t.items.map(safe).join(', ')}` : ''}. Seen since ${safe(t.since)}${from}${
                t.lastAttempt && t.lastAttempt !== t.since ? `, last at ${safe(t.lastAttempt)}` : ''}; ${handshakes(t.attempts)} captured name it. ${lastDone}`
            : `The server read and answered ${handshakes(t.attempts)}${from} since ${safe(t.since)}; none completed, so its replies or the device's next packets are lost on the way. ${lastDone}`;
        const changed = t.paramsChanged
            ? `<p>The server's parameters changed at ${safe(t.paramsChanged)}, after that: the device may still have the old config.</p>` : '';
        const table = old ? `
            <table class="w-full text-sm tabular-nums" data-diagnosis="params">
                <thead><tr class="text-xs text-gray-700 dark:text-[#bac5d4]">
                    <th class="pb-1 pr-3 text-left font-medium">Parameter</th>
                    <th class="pb-1 pr-3 text-left font-medium">Device sends</th>
                    <th class="pb-1 text-left font-medium">Server expects</th>
                </tr></thead>
                <tbody>${t.rows.map(([label, device, expected, differs]) => `
                    <tr class="border-t border-gray-200 dark:border-[#334155]"${differs ? ' data-differs' : ''}>
                        <th scope="row" class="py-1.5 pr-3 text-left font-medium">${safe(label)}</th>
                        <td class="py-1.5 pr-3 font-mono${differs ? ' font-semibold text-red-700 dark:text-[#fca5a5]' : ''}">${safe(device)}</td>
                        <td class="py-1.5 font-mono">${safe(expected)}</td>
                    </tr>`).join('')}</tbody>
            </table>
            <p class="rounded-lg border px-3 py-2 callout-red"><strong class="font-semibold">Re-import its config:</strong> showing the QR is not enough.</p>` : '';
        window.Ui.openDialog(`
            ${window.Ui.dialogHeader(`${kind.label} · ${safe(client.name)}`, `<span class="font-mono">${safe(client.client_ip)}</span> · ${safe(server.name)} · ${safe(server.protocol)}`)}
            <div class="px-5 pb-5 flex flex-col gap-3 text-sm text-gray-800 dark:text-[#d7dee9]" data-diagnosis="${safe(d.verdict)}">
                <p data-diagnosis="evidence">${evidence}</p>
                ${changed}
                ${table}
                <div class="flex flex-wrap justify-end gap-2">
                    ${old ? `<button type="button" class="btn btn-primary" data-action="client-qr" data-server="${safe(serverId)}" data-client="${safe(clientId)}">${window.Ui.icon('qr')}Show the config</button>` : ''}
                    <button type="button" class="btn btn-secondary" data-close="dialog">Close</button>
                </div>
            </div>`, { size: 'max-w-lg' });
    }

    // A diagnosis in words, for the dialog. Times read "23:55" today, else "3 Oct, 23:55";
    // `items` are the evidence's "S1 31 (server 40)", `rows` the parameter table's: S1 and
    // H1 always, trailers and the key when either side has one or they differ.
    static diagnosisText(d) {
        const when = (ts) => {
            const n = Number(ts);
            if (!ts || !Number.isFinite(n)) return '';
            const stamp = window.ServerUi.stamp(n);
            return new Date(n * 1000).toDateString() === new Date().toDateString() ? stamp.split(', ')[1] : stamp;
        };
        const endpoint = String(d.endpoint || '');
        const host = (endpoint.match(/^\[(.+)\]:\d+$/) || endpoint.match(/^([^:]+):\d+$/) || [null, endpoint])[1];
        const dev = d.device || {};
        const srv = d.server || {};
        const mismatch = Array.isArray(d.mismatch) ? d.mismatch : [];
        const onOff = (v) => (v ? 'on' : 'off');
        const range = (v) => String(v ?? '—').replace('-', '–');
        const keyItem = dev.key === 'none' ? 'no header protection key (server has one)'
            : srv.key ? 'an old header protection key (server has a newer one)' : 'a header protection key (server has none)';
        const items = {
            S1: `S1 ${dev.S1} (server ${srv.S1})`,
            H1: `H1 ${dev.H1} (server ${range(srv.H1)})`,
            RandomTrailers: `trailers ${onOff(dev.trailers)} (server ${onOff(srv.trailers)})`,
            HeaderProtectionKey: keyItem,
        };
        const rows = [
            ['S1', dev.S1 ?? '—', srv.S1 ?? '—', true],
            ['H1', dev.H1 ?? '—', range(srv.H1), true],
            ['RandomTrailers', onOff(dev.trailers), onOff(srv.trailers), dev.trailers || srv.trailers],
            ['HeaderProtectionKey', { current: 'current', previous: 'an old one', none: 'none' }[dev.key] || '—',
                srv.key ? 'set' : 'none', (dev.key && dev.key !== 'none') || srv.key],
        ].filter(([label, , , shown]) => shown || mismatch.includes(label))
            .map(([label, device, expected]) => [label, String(device), String(expected), mismatch.includes(label)]);
        return {
            host,
            since: when(d.since),
            lastAttempt: when(d.last_attempt),
            lastHandshake: when(d.last_handshake),
            paramsChanged: when(d.params_changed_at),
            attempts: Math.max(0, Math.round(Number(d.attempts) || 0)),
            items: mismatch.map((m) => items[m] || m),
            rows,
        };
    }

    async downloadClientConfig(serverId, clientId) {
        try {
            await this.downloadBlob(`/api/servers/${serverId}/clients/${clientId}/config`, `client-${clientId}.conf`);
            await this.markConfigIssued(serverId, clientId);
        } catch (error) {
            console.error('Error downloading client config:', error);
            this.showTempMessage('Error downloading client config: ' + error.message, 'error');
        }
    }

    // The daemon log for one interface, oldest first, reloaded every 10 s while open.
    showServerLogs(serverId, iface) {
        const server = (this.lastServers || []).find((s) => s.id === serverId) || {};
        const interfaceName = String(iface || server.interface || '').trim();
        const safe = (v) => this.escapeHtml(v ?? '');
        let poller = null;
        const box = window.Ui.openDialog(`
            ${window.Ui.dialogHeader(`Logs · <span class="font-mono">${safe(interfaceName)}</span>`,
                `${safe(server.name)} · oldest first · <span id="logsLastUpdate">loading…</span>`)}
            <div class="px-5 pb-5 flex flex-col gap-3">
                ${this.codeBoxHtml('', 'serverLogContent')}
                <div class="flex justify-end gap-2">
                    <button type="button" class="btn btn-secondary" id="logsManualRefresh">${window.Ui.icon('refresh')}Reload</button>
                    <button type="button" class="btn btn-primary" data-close="dialog">Close</button>
                </div>
            </div>`, { size: 'max-w-3xl', onClose: () => clearInterval(poller) });

        const refresh = async () => {
            const logEl = box.querySelector('#serverLogContent');
            const tsEl = box.querySelector('#logsLastUpdate');
            if (!logEl || !logEl.isConnected) return;
            try {
                const data = await this.getJson(`/api/system/awg-log?interface=${encodeURIComponent(interfaceName)}&lines=400`);
                const lines = Array.isArray(data?.lines) ? data.lines : [];
                logEl.textContent = lines.join('\n') || 'No log lines yet.';
                logEl.scrollTop = logEl.scrollHeight;
                if (tsEl) tsEl.textContent = `updated ${new Date().toLocaleTimeString()}`;
            } catch (error) {
                logEl.textContent = `Failed to load logs: ${error?.message || error}`;
                if (tsEl) tsEl.textContent = 'update failed';
            }
        };
        box.querySelector('#logsManualRefresh').addEventListener('click', refresh);
        refresh();
        poller = setInterval(refresh, 10000);
    }

    // A server's traffic history (GET .../traffic): 1 h every 7 s, 6 h and 24 h per
    // minute; a summary, the chart, and a row per client with its presence on the same
    // time axis and its totals. The crosshair's readout lists every client with traffic
    // at that moment; hovering a client draws its share, a click (a tap) pins it. While
    // open, 1 h follows each tick and every range is fetched again each minute.
    showServerTraffic(serverId, range = '24h') {
        const server = (this.lastServers || []).find((s) => s.id === serverId);
        if (!server) return;
        const safe = (v) => this.escapeHtml(v ?? '');
        const { C } = window.Charts;
        const view = { serverId, range, data: null, hover: null, emph: null, pinned: null, seq: 0, timer: null };
        this.trafficView = view;
        const count = (server.clients || []).length;
        const box = window.Ui.openDialog(`
            ${window.Ui.dialogHeader(`Traffic · ${safe(server.name)}`,
                `<span class="font-mono">${safe(server.interface)}</span> · ${count} client${count === 1 ? '' : 's'} · ${server.status === 'running' ? 'live, every 7 s' : 'stopped'}`)}
            <div class="px-5 pb-5 flex flex-col gap-3">
                <div class="flex flex-wrap items-center justify-between gap-3">
                    <div class="inline-flex rounded-lg border border-gray-400 p-0.5 dark:border-[#3b4a60]" role="radiogroup" aria-label="Range">
                        ${['1h', '6h', '24h'].map((r) => `<button type="button" role="radio" data-range="${r}" class="h-7 px-3 rounded-md text-xs font-medium">${r.replace('h', ' h')}</button>`).join('')}
                    </div>
                    <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs ${C.text2}">
                        <span class="inline-flex items-center gap-1.5"><span class="w-3.5 h-0.5 rounded-full ${C.dKey}"></span>Download, to the devices</span>
                        <span class="inline-flex items-center gap-1.5"><span class="w-3.5 h-0.5 rounded-full ${C.uKey}"></span>Upload, from the devices</span>
                    </div>
                </div>
                <p id="trafficSummary" class="text-sm text-gray-800 dark:text-[#d7dee9]">Loading…</p>
                <div id="trafficBlock" class="relative select-none"></div>
                <div class="flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-gray-300 dark:border-[#334155]">
                    <p id="trafficSince" class="text-xs ${C.muted} max-w-xl"></p>
                    <button type="button" class="btn btn-primary" data-close="dialog">Close</button>
                </div>
            </div>`, {
            size: 'max-w-4xl',
            onClose: () => {
                clearInterval(view.timer);
                if (this.trafficView === view) this.trafficView = null;
            },
        });
        box.querySelectorAll('[data-range]').forEach((button) => button.addEventListener('click', () => {
            if (view.range === button.dataset.range) return;
            view.range = button.dataset.range;
            view.hover = null;
            this.loadServerTraffic(view, true);
        }));
        view.timer = setInterval(() => this.loadServerTraffic(view), 60000);
        this.loadServerTraffic(view, true);
    }

    // Fetch the view's range; `rebuild` lays the chart and the rows out anew (on open
    // and on a range switch), otherwise the data is replaced in place. An answer for a
    // range no longer shown is dropped.
    async loadServerTraffic(view, rebuild = false) {
        if (this.trafficView !== view) return;
        view.seq += 1;
        const seq = view.seq;
        const range = view.range;
        if (rebuild) this.renderTrafficRange(view);
        try {
            const body = await this.getJson(`/api/servers/${view.serverId}/traffic?range=${range}`);
            if (this.trafficView !== view || view.seq !== seq) return;
            view.data = window.Charts.fromApi(body);
            if (rebuild) this.renderTrafficBlock(view);
            this.updateTrafficView(view);
        } catch (error) {
            if (this.trafficView !== view || view.seq !== seq) return;
            const summary = document.getElementById('trafficSummary');
            if (summary) summary.textContent = `Could not load the traffic: ${error.message}`;
        }
    }

    // From the app: a tick (the 1 h view follows it) or a resize (redrawn to its width).
    redrawServerTraffic(serverId = null, reason = 'resize') {
        const view = this.trafficView;
        if (!view?.data) return;
        if (reason === 'tick') {
            if (serverId === view.serverId && view.range === '1h') this.loadServerTraffic(view);
            return;
        }
        this.drawTrafficPlot(view);
    }

    renderTrafficRange(view) {
        document.querySelectorAll('#dialog [data-range]').forEach((button) => {
            const on = button.dataset.range === view.range;
            button.setAttribute('aria-checked', String(on));
            button.className = `h-7 px-3 rounded-md text-xs font-medium ${on
                ? 'bg-gray-200 text-gray-900 dark:bg-[#334155] dark:text-white'
                : 'text-gray-700 hover:bg-gray-100 dark:text-[#bac5d4] dark:hover:bg-[#273449]'}`;
        });
    }

    // The clients of the server as it is now, each with its slice of the history.
    trafficRows(view) {
        const server = (this.lastServers || []).find((s) => s.id === view.serverId) || {};
        const n = view.data.t.length;
        const none = () => new Array(n).fill(null);
        return (server.clients || []).map((client) => {
            const c = view.data.clients[client.id] || { down: none(), up: none(), state: new Array(n).fill(' ') };
            return { client, ...c, total: view.data.totals[client.id] || { down: 0, up: 0 } };
        });
    }

    // The chart, the axes and a row per client; drawTrafficPlot fills them.
    renderTrafficBlock(view) {
        const block = document.getElementById('trafficBlock');
        if (!block) return;
        const safe = (v) => this.escapeHtml(v ?? '');
        const { C, hatchSwatch } = window.Charts;
        const cols = 'grid grid-cols-[minmax(0,1fr)_auto] sm:grid-cols-[7.5rem_minmax(0,1fr)_6.5rem] gap-x-3';
        block.innerHTML = `
            <div class="grid grid-cols-[minmax(0,1fr)] sm:grid-cols-[7.5rem_minmax(0,1fr)_6.5rem] gap-x-3">
                <div class="hidden sm:block relative h-[220px]" id="trafficY"></div>
                <div class="relative h-[220px] touch-pan-y cursor-crosshair" id="trafficPlot"></div>
                <div class="hidden sm:block"></div>
                <div class="hidden sm:block"></div>
                <div class="relative h-6" id="trafficX"></div>
                <div class="hidden sm:block"></div>
            </div>
            <div class="border-t border-gray-300 dark:border-[#334155]">
                <div class="${cols} items-center pt-3 pb-1">
                    <span class="section-title">Clients</span>
                    <span class="hidden sm:flex items-center gap-3 text-[11px] ${C.text2}">
                        <span class="inline-flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm ${C.onlineKey}"></span>online</span>
                        <span class="inline-flex items-center gap-1.5">${hatchSwatch()}suspended</span>
                        <span class="inline-flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-sm ${C.trackKey}"></span>offline</span>
                    </span>
                    <span class="text-[11px] text-right ${C.text2}">in ${view.range.replace('h', ' h')}</span>
                </div>
                ${this.trafficRows(view).map(({ client }) => `
                    <div class="${cols} gap-y-1 items-center -mx-2 px-2 py-1.5 rounded-md cursor-pointer" data-traffic-client="${safe(client.id)}" title="Show this client's share; click to keep it">
                        <span class="flex items-center gap-2 min-w-0"><span data-traffic-dot class="w-2.5 h-2.5 rounded-full flex-none"></span><span class="text-sm font-medium text-sky-700 dark:text-[#7dd3fc] truncate">${safe(client.name)}</span></span>
                        <span data-traffic-presence class="order-3 sm:order-none col-span-2 sm:col-span-1 h-3"></span>
                        <span data-traffic-total class="flex flex-col text-xs font-mono tabular-nums text-right whitespace-nowrap text-gray-800 dark:text-[#d7dee9]"></span>
                    </div>`).join('') || `<p class="py-2 text-sm ${C.text2}">No clients.</p>`}
            </div>
            <div id="trafficCross" class="absolute top-0 w-px bg-gray-500/70 dark:bg-[#94a3b8]/70 pointer-events-none" hidden></div>
            <span id="trafficDotDown" class="absolute w-2.5 h-2.5 -ml-[5px] -mt-[5px] rounded-full ring-2 ring-white dark:ring-[#1f2937] pointer-events-none ${C.dKey}" hidden></span>
            <span id="trafficDotUp" class="absolute w-2.5 h-2.5 -ml-[5px] -mt-[5px] rounded-full ring-2 ring-white dark:ring-[#1f2937] pointer-events-none ${C.uKey}" hidden></span>
            <div id="trafficTip" class="absolute z-10 pointer-events-none" hidden></div>`;

        // A drag on touch moves the crosshair (the plot pans only vertically), and a
        // lifted finger keeps the readout; a mouse leaving the plot clears it.
        const plot = document.getElementById('trafficPlot');
        const follow = (e) => {
            const r = plot.getBoundingClientRect();
            view.hover = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
            this.trafficHover(view);
        };
        plot.addEventListener('pointerdown', follow);
        plot.addEventListener('pointermove', follow);
        plot.addEventListener('pointerleave', (e) => {
            if (e.pointerType !== 'mouse') return;
            view.hover = null;
            this.trafficHover(view);
        });
        block.querySelectorAll('[data-traffic-client]').forEach((row) => {
            const id = row.dataset.trafficClient;
            row.addEventListener('pointerenter', (e) => { if (e.pointerType === 'mouse') { view.emph = id; this.drawTrafficPlot(view); } });
            row.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') { view.emph = view.pinned; this.drawTrafficPlot(view); } });
            row.addEventListener('click', () => {
                view.pinned = view.pinned === id ? null : id;
                view.emph = view.pinned;
                this.drawTrafficPlot(view);
            });
        });
    }

    // The summary, each client's totals and dot, the footer; then the chart.
    updateTrafficView(view) {
        const { C, rate, sumSeries, hhmm } = window.Charts;
        const bytes = window.ServerUi.bytes;
        const rows = this.trafficRows(view);
        const { t, since } = view.data;
        const down = sumSeries(rows.map((r) => r.down), t.length);
        const label = view.range.replace('h', ' h');
        const summary = document.getElementById('trafficSummary');
        if (summary) {
            const total = (key) => rows.reduce((a, r) => a + r.total[key], 0);
            let peak = -1;
            down.forEach((v, i) => { if (v !== null && (peak < 0 || v > down[peak])) peak = i; });
            summary.innerHTML = `↓ <strong class="font-semibold ${C.text}">${bytes(total('down'))}</strong>
                &nbsp;↑ <strong class="font-semibold ${C.text}">${bytes(total('up'))}</strong> in the last ${label}
                ${peak >= 0 && down[peak] > 0 ? `<span class="${C.muted}">·</span> peak <strong class="font-semibold ${C.text}">${rate(down[peak])}</strong> at ${hhmm(t[peak])}` : ''}`;
        }
        const server = (this.lastServers || []).find((s) => s.id === view.serverId) || {};
        rows.forEach(({ client, total }) => {
            const row = document.querySelector(`[data-traffic-client="${CSS.escape(client.id)}"]`);
            if (!row) return;
            row.querySelector('[data-traffic-total]').innerHTML = `<span>↓ ${bytes(total.down)}</span><span>↑ ${bytes(total.up)}</span>`;
            const on = window.ServerUi.isOnline(server, client, (server.traffic || {})[client.id], (x) => this.isClientActiveFromTraffic(x));
            row.querySelector('[data-traffic-dot]').className = `w-2.5 h-2.5 rounded-full flex-none ${client.suspended ? 'bg-amber-400'
                : on ? 'bg-green-500' : 'ring-1 ring-inset ring-gray-400 dark:ring-[#64748b]'}`;
        });
        const sinceEl = document.getElementById('trafficSince');
        if (sinceEl) {
            sinceEl.textContent = since
                ? `Kept in the panel's memory since it started ${window.ServerUi.ago(since)}: every 7 s for the last hour, per minute for 24 h. A restart of the container starts it over.`
                : 'Kept in the panel\'s memory: nothing recorded since it started. A restart of the container starts it over.';
        }
        this.drawTrafficPlot(view);
    }

    // The span the chart shows: the last hour up to the newest tick, or the minute grid.
    trafficSpan(view) {
        const { t, now, range } = view.data;
        if (range === '1h' || t.length < 2) {
            const end = Math.max(now || 0, t[t.length - 1] || 0);
            return [end - 3600 * Number.parseInt(range, 10), end];
        }
        return [t[0], t[t.length - 1]];
    }

    drawTrafficPlot(view) {
        const plot = document.getElementById('trafficPlot');
        if (!plot || !view.data) return;
        const { C, maxOf, mirrored, niceCeil, sumSeries, withGaps, presenceSvg, tickNum, hhmm } = window.Charts;
        const rows = this.trafficRows(view);
        const { t, step } = view.data;
        const down = sumSeries(rows.map((r) => r.down), t.length);
        const up = sumSeries(rows.map((r) => r.up), t.length);
        const emphRow = view.emph ? rows.find((r) => r.client.id === view.emph) : null;
        const [t0, t1] = this.trafficSpan(view);
        const w = plot.clientWidth;
        const xOf = (tt) => ((tt - t0) / Math.max(1e-9, t1 - t0)) * w;
        const H = 220;

        // One scale for both sides; upload gets only the room it needs.
        const dTop = niceCeil(maxOf(down));
        const tickStep = niceCeil(dTop / 5);
        const uTop = niceCeil(Math.max(maxOf(up), dTop * 0.05));
        const split = Math.min(0.85, Math.max(0.6, dTop / (dTop + uTop)));
        const g0 = window.Charts.geometry(H, dTop, uTop, 2, split);
        const ticksDown = [];
        for (let v = tickStep; v <= dTop + 1e-9; v += tickStep) ticksDown.push(v);
        const room = (H - g0.base - 2) / g0.k;
        const ticksUp = [];
        for (let v = tickStep; v <= room + 1e-9; v += tickStep) ticksUp.push(v);
        const gridY = [...ticksDown.map((v) => Math.round(g0.base - v * g0.k) + 0.5), ...ticksUp.map((v) => Math.round(g0.base + v * g0.k) + 0.5)];

        const gapped = withGaps(t, [down, up, emphRow?.down || down, emphRow?.up || up], step);
        const m = mirrored({
            w, h: H, xs: gapped.t.map(xOf), down: gapped.series[0], up: gapped.series[1],
            emph: emphRow ? { down: gapped.series[2], up: gapped.series[3] } : null,
            dTop, uTop, grid: gridY, width: 2, split,
        });
        view.geo = { g: m.g, xOf, w, down, up, rows };
        plot.innerHTML = m.svg;

        // The y axis: ↓ values above the line, ↑ below; the top one carries the unit. At
        // phone width the labels sit inside the plot.
        const label = (v, dir, top) => `${dir} ${tickNum(v)}${top ? ' Mbit/s' : ''}`;
        const yLabels = [
            ...ticksDown.map((v, i) => [m.g.base - v * m.g.k, label(v, '↓', i === ticksDown.length - 1)]),
            [m.g.base, '0'],
            ...ticksUp.map((v) => [m.g.base + v * m.g.k, label(v, '↑', false)]),
        ];
        const yAxis = document.getElementById('trafficY');
        if (yAxis) yAxis.innerHTML = yLabels.map(([y, s]) => `<span class="absolute right-0 -translate-y-1/2 text-[11px] tabular-nums whitespace-nowrap ${C.muted}" style="top:${y}px">${s}</span>`).join('');
        plot.insertAdjacentHTML('beforeend', yLabels.filter(([, s]) => s !== '0').map(([y, s]) => `<span class="sm:hidden absolute left-1 -translate-y-1/2 px-1 rounded bg-white/80 text-[10px] tabular-nums ${C.muted} dark:bg-[#1f2937]/80" style="top:${y}px">${s}</span>`).join(''));

        // The x axis: round local times, every 10 min, 1 h or 4 h; none clipped at an edge.
        const every = { '1h': 10, '6h': 60, '24h': 240 }[view.range] * 60;
        const first = new Date(t0 * 1000);
        first.setSeconds(0, 0);
        if (every >= 3600) {
            first.setMinutes(0);
            first.setHours(Math.ceil(first.getHours() / (every / 3600)) * (every / 3600));
        } else {
            first.setMinutes(Math.ceil(first.getMinutes() / (every / 60)) * (every / 60));
        }
        const xTicks = [];
        for (let tt = first.getTime() / 1000; tt <= t1; tt += every) {
            const x = xOf(tt);
            if (x > 16 && x < w - 16) xTicks.push([x, hhmm(tt)]);
        }
        const xAxis = document.getElementById('trafficX');
        if (xAxis) xAxis.innerHTML = xTicks.map(([x, s]) => `<span class="absolute top-1 -translate-x-1/2 text-[11px] tabular-nums ${C.muted}" style="left:${x}px">${s}</span>`).join('');

        rows.forEach((row) => {
            const el = document.querySelector(`[data-traffic-client="${CSS.escape(row.client.id)}"]`);
            if (!el) return;
            const strip = el.querySelector('[data-traffic-presence]');
            strip.innerHTML = presenceSvg({ t, states: row.state, t0, t1, step, w: strip.clientWidth });
            const on = row.client.id === view.emph;
            'bg-gray-100 dark:bg-[#273449]'.split(' ').forEach((cls) => el.classList.toggle(cls, on));
        });
        this.trafficHover(view);
    }

    // The crosshair through the chart and the strips, and its readout: the total (or the
    // highlighted client) and every client with traffic at that moment.
    trafficHover(view) {
        const cross = document.getElementById('trafficCross');
        if (!cross) return;
        const tip = document.getElementById('trafficTip');
        const dotDown = document.getElementById('trafficDotDown');
        const dotUp = document.getElementById('trafficDotUp');
        const parts = [cross, tip, dotDown, dotUp];
        const { t } = view.data || { t: [] };
        if (view.hover === null || !t.length || !view.geo) {
            parts.forEach((el) => { el.hidden = true; });
            return;
        }
        const { C, rate, rateNum, hhmm } = window.Charts;
        const safe = (v) => this.escapeHtml(v ?? '');
        const { g, xOf, w, down, up, rows } = view.geo;
        // The point nearest the pointer, by time.
        const [t0, t1] = this.trafficSpan(view);
        const at = t0 + view.hover * (t1 - t0);
        let i = 0;
        t.forEach((tt, k) => { if (Math.abs(tt - at) < Math.abs(t[i] - at)) i = k; });
        const block = document.getElementById('trafficBlock');
        const plot = document.getElementById('trafficPlot');
        const left = plot.getBoundingClientRect().left - block.getBoundingClientRect().left;
        const x = left + xOf(t[i]);
        const sel = view.emph ? rows.find((r) => r.client.id === view.emph) : null;
        const dv = sel ? sel.down[i] : down[i];
        const uv = sel ? sel.up[i] : up[i];
        const noData = dv === null && uv === null;
        Object.assign(cross.style, { left: `${x}px`, height: `${block.scrollHeight}px` });
        Object.assign(dotDown.style, { left: `${x}px`, top: `${g.base - (dv || 0) * g.k}px` });
        Object.assign(dotUp.style, { left: `${x}px`, top: `${g.base + (uv || 0) * g.k}px` });
        const per = rows
            .map((r) => ({ name: r.client.name, d: r.down[i] || 0, u: r.up[i] || 0 }))
            .filter((r) => r.d + r.u > 0.0005)
            .sort((a, b) => b.d + b.u - (a.d + a.u));
        tip.innerHTML = `
            <div class="min-w-[12.5rem] rounded-lg border border-gray-300 bg-white px-3 py-2 shadow-lg text-xs dark:border-[#334155] dark:bg-[#111827]">
                <div class="mb-1 font-medium ${C.text2}">${hhmm(t[i])}${sel ? ` · ${safe(sel.client.name)}` : ''}</div>
                ${noData ? `<div class="${C.muted}">No data</div>` : `
                <div class="flex items-center gap-2"><span class="w-3 h-0.5 rounded-full ${C.dKey}"></span><strong class="font-semibold tabular-nums ${C.text}">${rate(dv || 0)}</strong><span class="${C.muted}">download</span></div>
                <div class="flex items-center gap-2"><span class="w-3 h-0.5 rounded-full ${C.uKey}"></span><strong class="font-semibold tabular-nums ${C.text}">${rate(uv || 0)}</strong><span class="${C.muted}">upload</span></div>`}
                ${!sel && per.length ? `<div class="mt-1.5 pt-1.5 border-t border-gray-200 dark:border-[#334155] grid grid-cols-[minmax(0,1fr)_auto_auto] gap-x-3 gap-y-0.5 tabular-nums">
                    ${per.map((r) => `<span class="truncate text-gray-800 dark:text-[#d7dee9]">${safe(r.name)}</span><span class="text-right ${C.text2}">↓ ${rateNum(r.d)}</span><span class="text-right ${C.text2}">↑ ${rateNum(r.u)}</span>`).join('')}
                </div>` : ''}
            </div>`;
        dotDown.hidden = dotUp.hidden = noData;
        cross.hidden = tip.hidden = false;
        const tipW = tip.offsetWidth || 214;
        const tipLeft = xOf(t[i]) > w * 0.6 ? x - tipW - 14 : x + 14;
        Object.assign(tip.style, { left: `${Math.max(0, Math.min(tipLeft, block.clientWidth - tipW))}px`, top: '6px' });
    }

    // What the panel recorded since it started (GET /api/activity; DEVELOPMENT.md §3,
    // Activity events), newest first, filtered here by kind and server: from the
    // header for all of it, from a server's ⋯ for that server. While open, each SSE
    // `activity` event is prepended (receiveActivity), and a stream that (re)opens loads
    // it again (loadActivity), since a restarted panel counts seq from 1 again.
    showActivity(serverId = null) {
        const view = { serverId, kind: 'all', events: [], since: null, lastSeq: 0, pending: null, error: '', options: '' };
        this.activityView = view;
        const box = window.Ui.openDialog(`
            ${window.Ui.dialogHeader('Activity', '<span id="activitySub">loading…</span>')}
            <div class="px-5 pb-5 flex flex-col gap-3">
                <div class="flex flex-wrap items-center justify-between gap-3">
                    <div class="inline-flex rounded-lg border border-gray-400 p-0.5 dark:border-[#3b4a60]" role="radiogroup" aria-label="Kind">
                        ${ModalUi.ACTIVITY_KINDS.map(([kind, label]) => `<button type="button" role="radio" data-activity-kind="${kind}">${label}</button>`).join('')}
                    </div>
                    <select id="activityServer" class="field sm:w-56" aria-label="Server"></select>
                </div>
                <ol id="activityList" class="max-h-[60vh] overflow-y-auto -mx-2 px-2"></ol>
                <div class="flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-gray-300 dark:border-[#334155]">
                    <p id="activitySince" class="text-xs text-gray-600 dark:text-[#98a6ba] max-w-xl"></p>
                    <button type="button" class="btn btn-primary" data-close="dialog">Close</button>
                </div>
            </div>`, {
            size: 'max-w-3xl',
            onClose: () => {
                if (this.activityView === view) this.activityView = null;
            },
        });
        box.querySelectorAll('[data-activity-kind]').forEach((button) => button.addEventListener('click', () => {
            view.kind = button.dataset.activityKind;
            this.renderActivity(view);
        }));
        box.querySelector('#activityServer').addEventListener('change', (event) => {
            view.serverId = event.target.value || null;
            this.renderActivity(view);
        });
        this.renderActivity(view);
        this.loadActivity(view);
    }

    // The whole ring. Events the stream brings meanwhile wait in `pending` and are kept
    // only above the ring's newest seq, so none is shown twice or lost.
    async loadActivity(view = this.activityView) {
        if (!view || this.activityView !== view) return;
        const pending = [];
        view.pending = pending;
        try {
            const body = await this.getJson('/api/activity');
            if (this.activityView !== view || view.pending !== pending) return;
            const events = Array.isArray(body?.events) ? body.events : [];
            const top = events.length ? events[0].seq : 0;
            const later = pending.filter((e) => e.seq > top).sort((a, b) => b.seq - a.seq);
            view.events = [...later, ...events];
            view.lastSeq = view.events.length ? view.events[0].seq : 0;
            view.since = body?.since || null;
            view.error = '';
        } catch (error) {
            if (this.activityView !== view || view.pending !== pending) return;
            view.error = error.message;
        }
        view.pending = null;
        this.renderActivity(view);
    }

    // From openEvents: one SSE `activity` event. Without the dialog there is nothing to
    // keep; one at or below the last seq shown is a duplicate.
    receiveActivity(event) {
        const view = this.activityView;
        if (!view || !Number.isFinite(event?.seq)) return;
        if (view.pending) {
            view.pending.push(event);
            return;
        }
        if (event.seq <= view.lastSeq) return;
        view.lastSeq = event.seq;
        view.events.unshift(event);
        if (view.events.length > 1000) view.events.length = 1000;
        this.renderActivity(view);
    }

    renderActivity(view) {
        const list = document.getElementById('activityList');
        if (!list || this.activityView !== view) return;
        const safe = (v) => this.escapeHtml(v ?? '');
        const ofServer = view.serverId ? view.events.filter((e) => e.server_id === view.serverId) : view.events;
        const shown = view.kind === 'all' ? ofServer : ofServer.filter((e) => e.kind === view.kind);

        document.querySelectorAll('#dialog [data-activity-kind]').forEach((button) => {
            const kind = button.dataset.activityKind;
            const on = kind === view.kind;
            const count = kind === 'all' ? ofServer.length : ofServer.filter((e) => e.kind === kind).length;
            const label = ModalUi.ACTIVITY_KINDS.find(([k]) => k === kind)[1];
            button.setAttribute('aria-checked', String(on));
            button.className = `h-7 px-2.5 rounded-md text-xs font-medium whitespace-nowrap ${on
                ? 'bg-gray-200 text-gray-900 dark:bg-[#334155] dark:text-white'
                : 'text-gray-700 hover:bg-gray-100 dark:text-[#bac5d4] dark:hover:bg-[#273449]'}`;
            button.innerHTML = `${label}<span class="hidden sm:inline ml-1 tabular-nums opacity-70">${count}</span>`;
        });

        // The servers there are now and any only the events still name (deleted ones);
        // rebuilt only when that set changes, so an open select is left alone.
        const names = new Map((this.lastServers || []).map((s) => [s.id, s.name]));
        view.events.forEach((e) => { if (e.server_id && !names.has(e.server_id)) names.set(e.server_id, e.server); });
        if (view.serverId && !names.has(view.serverId)) names.set(view.serverId, view.serverId);
        const options = `<option value="">All servers</option>${[...names].map(([id, name]) =>
            `<option value="${safe(id)}">${safe(name)}</option>`).join('')}`;
        const select = document.getElementById('activityServer');
        if (view.options !== options) {
            view.options = options;
            select.innerHTML = options;
        }
        select.value = view.serverId || '';

        const sub = document.getElementById('activitySub');
        if (sub) {
            sub.textContent = view.error ? `Could not load it: ${view.error}`
                : view.pending && !view.events.length ? 'loading…' : 'newest first · live';
        }
        const since = document.getElementById('activitySince');
        if (since) {
            since.textContent = `Kept in the panel's memory${view.since ? ` since it started ${window.ServerUi.ago(Date.parse(view.since) / 1000)}` : ''}; `
                + 'a restart of the container empties it. Each event is also a line in docker logs.';
        }

        if (!shown.length) {
            list.innerHTML = view.pending ? '' : `<li class="py-6 text-center text-sm text-gray-700 dark:text-[#bac5d4]">${view.events.length
                ? 'Nothing of this kind here.' : 'Nothing recorded since the panel started.'}</li>`;
            return;
        }
        // A heading for each day: the ring covers the time since boot, often days.
        let day = '';
        list.innerHTML = shown.map((e) => {
            const at = new Date(e.ts);
            const label = this.activityDay(at);
            const heading = label !== day
                ? `<li class="sticky top-0 z-[1] pt-3 pb-1 bg-white dark:bg-[#1f2937]"><span class="section-title">${label}</span></li>` : '';
            day = label;
            return heading + this.activityRowHtml(e, at);
        }).join('');
    }

    activityDay(at) {
        const start = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
        const days = Math.round((start(new Date()) - start(at)) / 86400000);
        if (days === 0) return 'Today';
        if (days === 1) return 'Yesterday';
        return window.ServerUi.stamp(at.getTime() / 1000).split(',')[0];
    }

    activityRowHtml(e, at) {
        const safe = (v) => this.escapeHtml(v ?? '');
        const [icon, tone, title, detail] = this.activityText(e);
        const two = (n) => String(n).padStart(2, '0');
        const clock = `${two(at.getHours())}:${two(at.getMinutes())}:${two(at.getSeconds())}`;
        return `
            <li class="flex items-start gap-3 py-2 border-b border-gray-200 last:border-b-0 dark:border-[#334155]" data-activity-seq="${Number(e.seq)}" data-activity-event="${safe(e.event)}">
                <span class="flex-none mt-0.5 w-7 h-7 rounded-full flex items-center justify-center ${tone}">${window.Ui.icon(icon, 'w-3.5 h-3.5')}</span>
                <div class="min-w-0 flex-1">
                    <p class="text-sm text-gray-900 dark:text-[#e5e7eb] break-words">${title}</p>
                    ${detail ? `<p class="text-xs text-gray-700 dark:text-[#bac5d4] break-words">${detail}</p>` : ''}
                </div>
                <time datetime="${safe(e.ts)}" title="${safe(at.toLocaleString())}" class="flex-none text-xs tabular-nums text-gray-600 dark:text-[#98a6ba]">${clock}</time>
            </li>`;
    }

    // [icon, badge tone, title, detail] for one event; every value escaped. Traffic is
    // flipped to the device's side as everywhere on the page: ↓ is what the server sent.
    activityText(e) {
        const safe = (v) => this.escapeHtml(v ?? '');
        const d = e.detail || {};
        const tones = {
            change: 'bg-sky-100 text-sky-700 dark:bg-[#0c2a3d] dark:text-[#7dd3fc]',
            good: 'bg-green-100 text-green-700 dark:bg-[#052e16] dark:text-[#86efac]',
            quiet: 'bg-gray-100 text-gray-600 dark:bg-[#273449] dark:text-[#bac5d4]',
            warn: 'bg-amber-100 text-amber-800 dark:bg-[#451a03] dark:text-[#fcd34d]',
            net: 'bg-indigo-100 text-indigo-700 dark:bg-[#1e1b4b] dark:text-[#a5b4fc]',
            bad: 'bg-red-100 text-red-600 dark:bg-[#3b1219] dark:text-[#fca5a5]',
        };
        const name = (v) => `<span class="font-semibold">${safe(v)}</span>`;
        const mono = (v) => `<span class="font-mono">${safe(v)}</span>`;
        const muted = (v) => `<span class="text-gray-600 dark:text-[#98a6ba]"> · ${safe(v)}</span>`;
        const server = e.server ? name(e.server) : 'a server';
        const client = e.client ? name(e.client) : 'a client';
        const onServer = e.server ? muted(e.server) : '';
        const value = (v) => {
            if (v === true) return 'on';
            if (v === false) return 'off';
            if (v === null || v === undefined || v === '') return 'none';
            return Array.isArray(v) ? v.join(', ') : typeof v === 'object' ? JSON.stringify(v) : String(v);
        };
        const changes = (Array.isArray(d.changes) ? d.changes : []).map((c) => ('old' in c || 'new' in c)
            ? `${safe(c.field)} ${mono(value(c.old))} → ${mono(value(c.new))}` : safe(c.field)).join(' · ');
        const duration = (s) => (s < 60 ? `${Math.max(0, Math.round(s))} s` : window.ServerUi.uptime(s));
        const bytes = window.ServerUi.bytes;
        const where = d.endpoint ? `${mono(d.endpoint)}${d.country ? ` ${window.ServerUi.flag(d.country)} ${safe(d.country)}` : ''}` : '';
        const from = where ? `from ${where} · ` : '';
        const count = (n, noun) => `${Number(n) || 0} ${noun}${Number(n) === 1 ? '' : 's'}`;
        const table = {
            'server.create': ['plus', tones.change, `Server ${server} created`],
            'server.delete': ['trash', tones.change, `Server ${server} deleted`],
            'server.start': ['power', tones.good, `Server ${server} started`],
            'server.stop': ['power', tones.quiet, `Server ${server} stopped`],
            'server.rename': ['edit', tones.change, `Server ${server} renamed`],
            'server.transport': ['edit', tones.change, `Transport parameters of ${server} changed`],
            'server.networking': ['edit', tones.change, `Networking of ${server} changed`],
            'server.endpoint': ['edit', tones.change, `Endpoint host of ${server} changed`],
            'client.add': ['userPlus', tones.change, `Client ${client} added${onServer}`],
            'client.delete': ['trash', tones.change, `Client ${client} deleted${onServer}`],
            'client.suspend': ['lock', tones.warn, `Client ${client} suspended${onServer}`],
            'client.resume': ['check', tones.good, `Client ${client} resumed${onServer}`],
            'client.rename': ['edit', tones.change, `Client ${client} renamed${onServer}`],
            'client.params': ['edit', tones.change, `Parameters of ${client} changed${onServer}`],
            'settings.save': ['gear', tones.change, 'Panel settings saved'],
            'access.change': ['key', tones.warn, d.user_changed ? 'Sign-in user and password changed' : 'Sign-in password changed'],
            'client.online': ['activity', tones.good, `${client} came online${onServer}`,
                where ? `from ${where}` : ''],
            'client.offline': ['activity', tones.quiet, `${client} went offline${onServer}`,
                `after ${duration(Number(d.duration_s) || 0)} · ↓ ${bytes(d.sent_bytes)} ↑ ${bytes(d.received_bytes)}`],
            'client.old_config': ['alert', tones.bad, `${client} is trying with an old config${onServer}`,
                `${from}${safe((Array.isArray(d.mismatch) ? d.mismatch : []).join(', ') || 'Parameters')} differ · ${count(d.attempts, 'handshake')} captured`],
            'client.maybe_blocked': ['alert', tones.warn, `${client} may be blocked${onServer}`,
                `${from}${count(d.attempts, 'handshake')} answered, none completed`],
            'client.recovered': ['check', tones.good, `${client} connects again${onServer}`,
                `${safe(window.ServerUi.DIAGNOSES[d.verdict]?.label || d.verdict)} cleared after ${duration(Number(d.duration_s) || 0)}`],
            'health.problem': ['alert', tones.warn, 'Health check failing', safe(d.problem)],
            'health.clear': ['check', tones.good, 'Health problem cleared', safe(d.problem)],
            'egress.change': ['globe', tones.net, `Egress IP of ${server} changed`,
                `${mono(value(d.old))} → ${mono(value(d.new))}${d.label ? ` · ${safe(d.label)}` : ''}`],
            'auth.fail': ['lock', tones.bad, `Failed sign-in${Number(d.count) > 1 ? `, ${Number(d.count)} times` : ''}`,
                `from ${mono(d.address)} · ${d.user === null || d.user === undefined ? 'no user' : `user ${mono(d.user)}`}`],
        };
        const [icon, tone, title, detail = changes] = table[e.event] || ['dots', tones.quiet, safe(e.event)];
        return [icon, tone, title, detail];
    }

    // The server's .conf as the panel generated it. It holds the server's private key.
    async showRawServerConfig(serverId) {
        let config;
        try {
            config = await this.getJson(`/api/servers/${serverId}/config`);
        } catch (error) {
            console.error('Error fetching server config:', error);
            this.showTempMessage('Error loading server configuration: ' + error.message, 'error');
            return;
        }
        const safe = (v) => this.escapeHtml(v ?? '');
        const box = window.Ui.openDialog(`
            ${window.Ui.dialogHeader(`<span class="font-mono">${safe(config.interface)}.conf</span>`,
                `${safe(config.server_name)} · generated from the panel's settings, read-only`)}
            <div id="rawConfigModal" class="px-5 pb-5 flex flex-col gap-3">
                ${this.codeBoxHtml(config.config_content, 'rawConfigText')}
                <div class="flex flex-wrap justify-end gap-2">
                    <button type="button" class="btn btn-secondary" data-raw="copy">${window.Ui.icon('copy')}Copy</button>
                    <button type="button" class="btn btn-secondary" data-raw="download">${window.Ui.icon('download')}Download</button>
                    <button type="button" class="btn btn-primary" data-close="dialog">Close</button>
                </div>
            </div>`, { size: 'max-w-3xl' });
        box.querySelector('[data-raw="copy"]').addEventListener('click', () => this.copyText(config.config_content, 'Server config'));
        box.querySelector('[data-raw="download"]').addEventListener('click', () => this.downloadServerConfig(serverId));
    }

    downloadServerConfig(serverId) {
        this.downloadBlob(`/api/servers/${serverId}/config/download`, `server-${serverId}.conf`)
            .catch((error) => {
                console.error('Error downloading server config:', error);
                this.showTempMessage('Error downloading server config: ' + error.message, 'error');
            });
    }
}

// Install onto AmneziaApp, so `this` is the app in every method.
Object.getOwnPropertyNames(ModalUi.prototype)
    .filter((name) => name !== 'constructor')
    .forEach((name) => {
        AmneziaApp.prototype[name] = ModalUi.prototype[name];
    });

window.ModalUi = ModalUi;
