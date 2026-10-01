// AmneziaWG Web UI - the views, shown as centred dialogs: a client's QR code, a
// server's traffic history, its daemon log and its raw .conf.
//
// Methods of AmneziaApp, installed onto its prototype at the bottom of this file
// (like forms.js), so `this` is the app. Every interpolated value is escaped.
class ModalUi {
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
            ? `<p class="w-full rounded-lg border px-3 py-2 text-sm bg-rose-50 border-rose-200 text-rose-900 dark:bg-[#3b0a1a] dark:border-[#881337] dark:text-[#fecdd3]">
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
