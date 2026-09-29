// AmneziaWG Web UI - server cards and client rows.
//
// Pure renderers: they take the API payloads and return HTML. Every value goes
// through escapeHtml; handlers call the global `amneziaApp`. Ids are 6 hex chars
// from the backend, escaped anyway.
class ServerUi {
    static flag(countryCode) {
        const cc = String(countryCode || '').trim().toUpperCase();
        return /^[A-Z]{2}$/.test(cc)
            ? String.fromCodePoint(0x1F1E6 + (cc.charCodeAt(0) - 65), 0x1F1E6 + (cc.charCodeAt(1) - 65))
            : '';
    }

    // "5 min ago" style, for the egress check time.
    static ago(epochSeconds) {
        const ts = Number(epochSeconds);
        if (!Number.isFinite(ts) || ts <= 0) return '';
        const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
        if (s < 45) return 'just now';
        if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
        if (s < 86400) return `${Math.round(s / 3600)} h ago`;
        return `${Math.round(s / 86400)} d ago`;
    }

    // "3 Sep, 21:15" for when a config was handed out.
    static stamp(epochSeconds) {
        const ts = Number(epochSeconds);
        if (!Number.isFinite(ts) || ts <= 0) return '';
        const d = new Date(ts * 1000);
        const month = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getMonth()];
        return `${d.getDate()} ${month}, ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    }

    // Byte counts as `awg show` prints them: "0 B", "1.39 MiB".
    static bytes(value) {
        let n = Math.max(0, Number(value) || 0);
        const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
        let unit = 0;
        while (n >= 1024 && unit < units.length - 1) {
            n /= 1024;
            unit += 1;
        }
        return unit === 0 ? `${n} B` : `${n.toFixed(2)} ${units[unit]}`;
    }

    // "57 s ago", "3 min 5 s ago", "2 h 10 min ago", "4 d ago": a handshake's age.
    static since(seconds) {
        const s = Number(seconds);
        if (seconds === null || seconds === undefined || !Number.isFinite(s) || s < 0) return '';
        const part = (big, bigUnit, small, smallUnit) => `${big} ${bigUnit}${small ? ` ${small} ${smallUnit}` : ''} ago`;
        if (s < 60) return `${s} s ago`;
        if (s < 3600) return part(Math.floor(s / 60), 'min', s % 60, 's');
        if (s < 86400) return part(Math.floor(s / 3600), 'h', Math.floor((s % 3600) / 60), 'min');
        return `${Math.floor(s / 86400)} d ago`;
    }

    static isOnline(server, client, clientTraffic, isActive) {
        return server.status === 'running' && !client.suspended && isActive(clientTraffic);
    }

    static renderServersHtml({ servers, escapeHtml, renderServerClients }) {
        if (!Array.isArray(servers) || servers.length === 0) {
            return `<p class="rounded-xl border border-dashed border-gray-300 dark:border-[#334155] p-8 text-center text-sm text-gray-500 dark:text-[#94a3b8]">
                No servers yet. Create one with New server.</p>`;
        }
        return servers.map((server) => ServerUi.serverCardHtml({ server, escapeHtml, renderServerClients })).join('');
    }

    static serverCardHtml({ server, escapeHtml, renderServerClients }) {
        const safe = (v) => escapeHtml(v ?? '');
        const icon = window.Ui.icon;
        const id = safe(server.id);
        const running = server.status === 'running';
        const sep = '<span aria-hidden="true" class="text-gray-300 dark:text-[#475569]">·</span>';
        const facts = [
            `<span class="font-medium text-gray-800 dark:text-[#e5e7eb]">${safe(server.protocol || window.Protocols.DEFAULT)}</span>`,
            `<span>UDP <span class="font-mono">${safe(server.port)}</span></span>`,
            `<span class="font-mono">${safe(server.subnet)}</span>`,
            `<span class="font-mono">${safe(server.interface)}</span>`,
            `<span>NAT ${server.enable_nat ? 'on' : 'off'}</span>`,
            `<span>LAN ${server.block_lan_cidrs ? 'blocked' : 'allowed'}</span>`,
        ].join(sep);

        const probe = server.egress_probe && typeof server.egress_probe === 'object' ? server.egress_probe : null;
        let egress;
        if (!running) {
            egress = '<span class="text-gray-500 dark:text-[#94a3b8]">Egress not checked while the server is stopped</span>';
        } else if (probe && probe.external_ip) {
            const cc = String(probe.external_ip_geo_country_code || '').toUpperCase();
            const where = [cc, probe.external_ip_geo].filter(Boolean).join(' / ');
            const checked = ServerUi.ago(probe.checked_at);
            const via = probe.service_name || probe.service;
            egress = `<span class="text-gray-500 dark:text-[#94a3b8]">Egress</span>
                <span class="font-mono text-gray-800 dark:text-[#e5e7eb]">${safe(probe.external_ip)}</span>
                ${where ? `<span>${ServerUi.flag(cc)} ${safe(where)}</span>` : ''}
                ${checked || via ? `<span class="text-gray-400 dark:text-[#64748b]">${checked ? `checked ${safe(checked)}` : ''}${via ? ` via ${safe(via)}` : ''}</span>` : ''}`;
        } else if (probe) {
            egress = `<span class="text-red-600 dark:text-[#fca5a5]" title="${safe(probe.error || '')}">No external access</span>`;
        } else {
            egress = '<span class="text-gray-500 dark:text-[#94a3b8]">Egress not checked yet</span>';
        }

        const status = running
            ? '<span class="pill bg-green-100 text-green-800 dark:bg-[#14532d] dark:text-[#86efac]">Running</span>'
            : '<span class="pill bg-gray-200 text-gray-700 dark:bg-[#334155] dark:text-[#cbd5e1]">Stopped</span>';

        return `
        <article class="server-card rounded-xl border border-gray-300 bg-white shadow-sm dark:bg-[#1f2937] dark:border-[#334155]" aria-label="${safe(server.name)}" data-server-id="${id}">
            <div class="p-4 sm:p-5 flex flex-col gap-2.5">
                <div class="flex flex-wrap items-center justify-between gap-x-4 gap-y-3">
                    <div class="flex items-center gap-3 min-w-0">
                        <h3 class="text-lg font-semibold text-purple-700 dark:text-[#c084fc] truncate" data-name="${id}">${safe(server.name)}</h3>
                        <label class="switch" title="${running ? 'Stop server' : 'Start server'}">
                            <input type="checkbox" ${running ? 'checked' : ''} aria-label="${safe(server.name)} running"
                                onchange="amneziaApp.toggleServer('${id}', this.checked)">
                            <span class="track"></span>
                        </label>
                        ${status}
                    </div>
                    <div class="flex items-center gap-1">
                        <button type="button" class="btn btn-secondary btn-sm" onclick="amneziaApp.addClient('${id}')">${icon('userPlus', 'w-3.5 h-3.5')}Client</button>
                        <button type="button" class="icon-btn" onclick="amneziaApp.showServerConfig('${id}')" aria-label="Server settings" title="Server settings">${icon('gear')}</button>
                        <button type="button" class="icon-btn" onclick="amneziaApp.openServerMenu('${id}', this)" aria-label="More actions for ${safe(server.name)}" aria-haspopup="menu" title="More">${icon('dots')}</button>
                    </div>
                </div>
                <div class="${running ? '' : 'opacity-60'} flex flex-col gap-1.5">
                    <p class="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-gray-600 dark:text-[#cbd5e1]">${facts}</p>
                    <p class="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm text-gray-700 dark:text-[#cbd5e1]">
                        ${egress}
                        <button type="button" class="icon-btn icon-btn-sm" onclick="amneziaApp.probeServerEgressIp('${id}', this)" aria-label="Check egress IP again" title="Check egress IP again">${icon('refresh', 'w-3.5 h-3.5')}</button>
                    </p>
                </div>
            </div>
            <div id="clients-${id}" class="border-t border-gray-200 dark:border-[#2b3647] ${running ? '' : 'opacity-60'}">
                ${renderServerClients(server.id, server.clients || [])}
            </div>
        </article>`;
    }

    // The Clients header's summary: "4 · 2 online · 1 to re-import".
    static summaryHtml({ server, clients, traffic, isClientActiveFromTraffic }) {
        const online = clients.filter((c) => ServerUi.isOnline(server, c, traffic[c.id], isClientActiveFromTraffic)).length;
        const stale = clients.filter((c) => c.config_outdated).length;
        return `${clients.length} · ${online} online${stale
            ? ` · <span class="font-semibold text-rose-700 dark:text-[#fda4af]">${stale} to re-import</span>` : ''}`;
    }

    // The Clients header and rows of one card, rendered with the servers. Telemetry
    // then patches the data-cell elements in place (patchClients), so the buttons,
    // keyboard focus, a text selection and an open ⋯ menu survive every update.
    static renderServerClientsHtml({ server, clients, traffic, escapeHtml, isClientActiveFromTraffic }) {
        const safe = (v) => escapeHtml(v ?? '');
        const header = `
            <div class="px-4 sm:px-5 pt-3 pb-1 flex items-center gap-2">
                <span class="section-title">Clients</span>
                <span class="text-xs text-gray-500 dark:text-[#94a3b8] tabular-nums" data-cell="summary">${ServerUi.summaryHtml({ server, clients, traffic, isClientActiveFromTraffic })}</span>
            </div>`;
        if (clients.length === 0) {
            return `${header}<p class="px-4 sm:px-5 pb-4 text-sm text-gray-500 dark:text-[#94a3b8]">No clients yet. Add one with + Client.</p>`;
        }
        const rows = clients.map((client) => ServerUi.clientRowHtml({
            server, client, clientTraffic: traffic[client.id] || {}, safe, isClientActiveFromTraffic,
        })).join('');
        return `${header}<ul class="divide-y divide-gray-200 dark:divide-[#2b3647]">${rows}</ul>`;
    }

    // What the telemetry-driven cells of a row show; shared by the render and the patch.
    static liveCells({ server, client, clientTraffic, safe, isClientActiveFromTraffic }) {
        const suspended = !!client.suspended;
        const on = ServerUi.isOnline(server, client, clientTraffic, isClientActiveFromTraffic);
        const endpoint = clientTraffic.endpoint || '';
        const cc = String(clientTraffic.geo_country_code || '').toUpperCase();
        const where = [cc, clientTraffic.geo].filter(Boolean).join(' / ');
        const age = ServerUi.since(clientTraffic.latest_handshake_seconds);
        return {
            dotClass: `w-2.5 h-2.5 rounded-full flex-none ${suspended ? 'bg-amber-400'
                : on ? 'bg-green-500' : 'ring-1 ring-inset ring-gray-400 dark:ring-[#64748b]'}`,
            dotTitle: suspended ? 'Suspended' : on ? 'Online: handshake in the last 5 minutes' : 'Offline',
            place: endpoint
                ? `${ServerUi.flag(cc)} <span class="font-mono">${safe(endpoint)}</span>${where ? ` <span class="text-gray-500 dark:text-[#94a3b8]">${safe(where)}</span>` : ''}`
                : '<span class="text-gray-400 dark:text-[#64748b]">Not connected</span>',
            handshake: endpoint && age ? `handshake ${age}` : '',
            rx: ServerUi.bytes(clientTraffic.received_bytes),
            tx: ServerUi.bytes(clientTraffic.sent_bytes),
        };
    }

    static clientRowHtml({ server, client, clientTraffic, safe, isClientActiveFromTraffic }) {
        const icon = window.Ui.icon;
        const sid = safe(server.id);
        const cid = safe(client.id);
        const suspended = !!client.suspended;
        const cells = ServerUi.liveCells({ server, client, clientTraffic, safe, isClientActiveFromTraffic });
        const dim = suspended ? 'opacity-55' : '';

        const suspendedPill = suspended
            ? '<span class="pill bg-amber-100 text-amber-800 dark:bg-[#451a03] dark:text-[#fcd34d]">Suspended</span>' : '';
        const issuedOn = ServerUi.stamp(client.config_issued_at);
        const reimportPill = client.config_outdated
            ? `<button type="button" class="pill flex-none bg-rose-100 text-rose-800 hover:bg-rose-200 dark:bg-[#4c0519] dark:text-[#fda4af] dark:hover:bg-[#6b0f2a]"
                   onclick="amneziaApp.showClientQRCode('${sid}', '${cid}')"
                   title="The config changed after it was handed out${issuedOn ? ` on ${safe(issuedOn)}` : ''}. Show the new one.">${icon('refresh', 'w-3 h-3')}Re-import</button>`
            : '';

        return `
        <li class="px-4 sm:px-5 py-3 grid gap-x-4 gap-y-1.5 grid-cols-[minmax(0,1fr)_auto] md:grid-cols-[minmax(0,13rem)_minmax(0,1fr)_7.5rem_auto] items-center" data-client-id="${cid}">
            <div class="col-span-2 md:col-span-1 flex items-center gap-2 min-w-0 ${dim}">
                <span data-cell="dot" class="${cells.dotClass}" title="${cells.dotTitle}"></span>
                <span class="text-sm font-medium text-sky-700 dark:text-[#7dd3fc] truncate" data-name="${cid}">${safe(client.name)}</span>
                <span class="font-mono text-xs text-gray-500 dark:text-[#94a3b8]">${safe(client.client_ip)}</span>
            </div>
            <div class="col-span-2 md:col-span-1 flex flex-col items-start gap-0.5 text-xs text-gray-600 dark:text-[#cbd5e1] min-w-0">
                <span class="flex items-center gap-2 min-w-0 max-w-full">${reimportPill}${suspendedPill}<span data-cell="place" class="min-w-0 truncate ${dim}">${cells.place}</span></span>
                <span data-cell="handshake" class="text-gray-500 dark:text-[#94a3b8] ${dim}"${cells.handshake ? '' : ' hidden'}>${safe(cells.handshake)}</span>
            </div>
            <div class="flex flex-col whitespace-nowrap text-xs font-mono tabular-nums text-gray-600 dark:text-[#cbd5e1] md:text-right ${dim}">
                <span title="Received"><span class="traffic-arrow" data-cell="rx-arrow">↓</span> <span data-cell="rx">${safe(cells.rx)}</span></span>
                <span title="Sent"><span class="traffic-arrow" data-cell="tx-arrow">↑</span> <span data-cell="tx">${safe(cells.tx)}</span></span>
            </div>
            <div class="flex items-center gap-1 justify-end">
                <label class="switch switch-sm switch-amber mr-1.5" title="${suspended ? 'Reactivate client' : 'Suspend client'}">
                    <input type="checkbox" ${suspended ? '' : 'checked'} aria-label="${safe(client.name)} active"
                        onchange="amneziaApp.toggleClientSuspend('${sid}', '${cid}')">
                    <span class="track"></span>
                </label>
                <button type="button" class="btn btn-ghost btn-sm" onclick="amneziaApp.showClientQRCode('${sid}', '${cid}')">${icon('qr', 'w-3.5 h-3.5')}QR</button>
                <button type="button" class="btn btn-ghost btn-sm" onclick="amneziaApp.showClientParamsModal('${sid}', '${cid}')">${icon('edit', 'w-3.5 h-3.5')}Edit</button>
                <button type="button" class="icon-btn icon-btn-sm" onclick="amneziaApp.openClientMenu('${sid}', '${cid}', this)" aria-label="More actions for ${safe(client.name)}" aria-haspopup="menu">${icon('dots')}</button>
            </div>
        </li>`;
    }

    // Apply a telemetry update to a rendered card: only the data-cell elements change.
    // `previous` is the traffic the rows show now, to flash the arrows of totals that grew.
    static patchClients({ container, server, traffic, previous, escapeHtml, isClientActiveFromTraffic }) {
        if (!container) return;
        const safe = (v) => escapeHtml(v ?? '');
        const clients = server.clients || [];
        const summary = container.querySelector('[data-cell="summary"]');
        if (summary) summary.innerHTML = ServerUi.summaryHtml({ server, clients, traffic, isClientActiveFromTraffic });
        container.querySelectorAll('li[data-client-id]').forEach((row) => {
            const client = clients.find((c) => c.id === row.dataset.clientId);
            if (!client) return;
            const now = traffic[client.id] || {};
            const before = previous[client.id] || {};
            const cells = ServerUi.liveCells({ server, client, clientTraffic: now, safe, isClientActiveFromTraffic });
            const cell = (name) => row.querySelector(`[data-cell="${name}"]`);
            const dot = cell('dot');
            if (dot) {
                dot.className = cells.dotClass;
                dot.title = cells.dotTitle;
            }
            const place = cell('place');
            if (place && place.innerHTML !== cells.place) place.innerHTML = cells.place;
            const handshake = cell('handshake');
            if (handshake) {
                handshake.textContent = cells.handshake;
                handshake.hidden = !cells.handshake;
            }
            [['rx', 'received_bytes'], ['tx', 'sent_bytes']].forEach(([name, key]) => {
                const value = cell(name);
                if (value) value.textContent = cells[name];
                const arrow = cell(`${name}-arrow`);
                if (arrow && before[key] !== undefined && now[key] !== before[key]) {
                    arrow.classList.remove('traffic-flash');
                    void arrow.offsetWidth; // restart the animation
                    arrow.classList.add('traffic-flash');
                }
            });
        });
    }
}

window.ServerUi = ServerUi;
