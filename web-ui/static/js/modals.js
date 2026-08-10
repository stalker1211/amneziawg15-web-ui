// AmneziaWG Web UI - modal dialogs and the HTML helpers they use.
//
// Split out of app.js so each file can be read on its own. These are methods of
// AmneziaApp, installed onto its prototype at the bottom of this file, so `this`
// is the app instance and everything they call (escapeHtml, apiFetch, closeModal,
// the dirty-tracking helpers) keeps working unchanged.
//
// Conventions, unchanged from before the split:
//   * every interpolated value goes through this.escapeHtml()
//   * modals are HTML strings appended to <body>, removed by closeModal()
//   * element ids are suffixed with the server/client id
//   * inline handlers call the global `amneziaApp`, so methods must stay on it
class ModalUi {
    wrapCollapsibleHelp(innerHtml, summaryText, summaryClass) {
        return `
            <details class="param-help mb-3">
                <summary class="${summaryClass} cursor-pointer select-none">${this.escapeHtml(summaryText)}</summary>
                <div class="mt-2">${innerHtml}</div>
            </details>
        `;
    }

    getTransportDescriptionHtml(protocol, variant = 'create') {
        const containerClass = variant === 'modal'
            ? 'text-[12px] text-blue-800/80 space-y-1'
            : 'text-xs text-gray-700 space-y-1';

        const supportsS34 = window.Protocols.supportsS34(protocol);
        const supportsRanges = window.Protocols.supportsHeaderRanges(protocol);

        const baseLines = [
            '<p>S1: Padding for handshake initial traffic. Common starting range is 15-150 but =< (MTU - 148)</p>',
            '<p>S2: Padding for handshake response traffic. Common starting range is 15-150 but =< (MTU - 92); S1 + 56 ≠ S2</p>',
            supportsS34
                ? '<p>S3: Padding of handshake cookie message.</p>'
                : '',
            supportsS34
                ? '<p>S4: Padding for data packets, increase packet size by S4 bytes. Values >32 likely to trigger \'message too long\' errors.</p>'
                : '',
            supportsRanges
                ? '<p>H1-H4: header signature values. Change packet fingerprint, can be single int32 values or ranges (like 1200-1400). Ranges must not overlap.</p>'
                : '<p>H1-H4: header signature values. Change packet fingerprint, can be single int32 values.</p>',
            window.Protocols.supportsAwg3(protocol)
                ? '<p>HeaderProtectionKey: encrypts packet headers instead of only randomising them. Server-side, so it must match on server and clients. When set, each of S1, S2, S3 and S4 must individually be 12 or more (the first 12 bytes of each padding are used as the cipher nonce).</p>'
                : ''
        ];

        const summaryClass = variant === 'modal'
            ? 'text-[12px] font-medium text-blue-800/80'
            : 'text-xs font-medium text-gray-700';

        return this.wrapCollapsibleHelp(
            `<div class="${containerClass}">${baseLines.join('')}</div>`,
            'What do these transport parameters mean?',
            summaryClass,
        );
    }

    getClientParamDescriptionHtml() {
        const inner = `
            <div class="text-[13px] leading-relaxed text-blue-800/80 space-y-1">
                <p>Jc: number of junk packets sent before the handshake starts. Usual range: 4-12</p>
                <p>Jmin / Jmax: minimum and maximum size range for those pre-handshake junk packets. Jmin =< Jmax.</p>
                <p>I1-I5: Optional custom signature packets; see AWG docs for syntax.</p>
                <p>J and I affect handshake camouflage only. They do not change established tunnel transport packets.</p>
            </div>
        `;

        return this.wrapCollapsibleHelp(
            inner,
            'What do these client parameters mean?',
            'text-[13px] font-medium text-blue-900',
        );
    }

    getClientTransportSummaryHtml(protocolValue, transportSummary) {
        const protocol = this.escapeHtml(protocolValue || window.Protocols.DEFAULT);
        const summary = this.escapeHtml(transportSummary || 'Default');

        return `
            <div class="text-sm text-gray-800 space-y-1 mb-3">
                <div><span class="font-semibold text-gray-900">Protocol:</span> <span class="font-mono text-gray-800">${protocol}</span></div>
                <div><span class="font-semibold text-gray-900">Parameters:</span> <span class="font-mono text-gray-800">${summary}</span></div>
            </div>
        `;
    }

    renderAwg3ClientParamsHtml(prefix, params) {
        const safe = (value) => this.escapeHtml(value);
        const fields = [
            ['ContentPaddingAddition', 'Extra random bytes added to each data packet.'],
            ['RekeyAfterTime', 'Seconds before a client re-handshakes (default 120).'],
            ['RekeyTimeout', 'Seconds before a handshake is retried (default 5).'],
            ['RejectAfterTime', 'Seconds before a key is rejected (default 180).'],
            ['KeepaliveTimeout', 'Seconds of silence before a keepalive (default 10).'],
            ['MaxHandshakeAttempts', 'Handshake retries before giving up (default 18).'],
        ];

        const help = this.wrapCollapsibleHelp(
            `<div class="text-[12px] leading-relaxed text-blue-800/80 space-y-1">
                <p>All optional and client-side. Accepts a single number or a range like
                <span class="font-mono">22-30</span>; leave empty to use the protocol default.
                Randomising the timings defeats fingerprinting based on WireGuard's fixed intervals.</p>
                ${fields.map(([key, text]) => `<p>${safe(key)}: ${safe(text)}</p>`).join('')}
            </div>`,
            'What do these AWG 3.0 parameters mean?',
            'text-[12px] font-medium text-blue-800/80',
        );

        // Per-field hints live in the title attribute; the full text is one click
        // away in the block above, which keeps this grid compact.
        return `
            <div class="mt-3 pt-3 border-t border-blue-200/70">
                <div class="text-xs font-semibold text-blue-900 mb-1">AWG 3.0 parameters</div>
                ${help}
                <div class="grid grid-cols-2 md:grid-cols-3 gap-2 text-sm">
                    ${fields.map(([key, text]) => `
                        <label class="block" title="${safe(text)}">
                            <div class="text-[11px] font-medium text-blue-900 truncate">${safe(key)}</div>
                            <input id="${prefix}-${safe(key)}" type="text" placeholder="default"
                                class="mt-0.5 w-full px-2 py-1 border border-blue-200 rounded text-sm font-mono bg-white/70"
                                value="${safe(params[key] ?? '')}">
                        </label>
                    `).join('')}
                </div>
            </div>
        `;
    }

    renderClientParamsFormHtml(prefix, params, protocol = window.Protocols.DEFAULT) {
        const safe = (value) => this.escapeHtml(value);
        return `
            <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm mb-3">
                <label class="block">
                    <div class="text-xs font-medium text-blue-900">Jc</div>
                    <input id="${prefix}-Jc" type="number" min="1" class="mt-1 w-full px-2 py-1 border border-blue-200 rounded text-sm" value="${safe(params.Jc ?? 8)}">
                </label>
                <label class="block">
                    <div class="text-xs font-medium text-blue-900">Jmin</div>
                    <input id="${prefix}-Jmin" type="number" min="1" class="mt-1 w-full px-2 py-1 border border-blue-200 rounded text-sm" value="${safe(params.Jmin ?? 8)}">
                </label>
                <label class="block">
                    <div class="text-xs font-medium text-blue-900">Jmax</div>
                    <input id="${prefix}-Jmax" type="number" min="1" class="mt-1 w-full px-2 py-1 border border-blue-200 rounded text-sm" value="${safe(params.Jmax ?? 80)}">
                </label>
            </div>
            <div class="grid grid-cols-1 gap-2">
                ${AmneziaApp.I_PARAM_KEYS.map((key) => `
                    <label class="block text-xs">
                        <div class="font-semibold text-blue-900 mb-1">${safe(key)}</div>
                        <textarea id="${prefix}-${key}" rows="1" class="w-full px-2 py-1 border border-blue-200 rounded text-xs font-mono bg-white/70">${safe(params[key] ?? '')}</textarea>
                    </label>
                `).join('')}
            </div>
            ${window.Protocols.supportsAwg3(protocol) ? this.renderAwg3ClientParamsHtml(prefix, params) : ''}
        `;
    }

    addClient(serverId) {
        const server = (this.lastServers || []).find((item) => String(item.id) === String(serverId));
        if (!server) {
            this.showTempMessage('Server not found', 'error');
            return;
        }

        const defaults = server.client_defaults || {};
        const clients = this.serverClients.get(serverId) || [];
        this.closeModal();

        const safe = (value) => this.escapeHtml(value);
        const modalHtml = `
            <div id="configModal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
                <div class="relative top-20 mx-auto p-5 border w-11/12 md:w-3/4 lg:w-1/2 shadow-lg rounded-md bg-white">
                    <div class="mt-3">
                        <div class="flex justify-between items-center mb-4">
                            <h3 class="text-xl font-bold text-gray-900">Add Client to Server: <span class="text-purple-600">${safe(server.name)}</span></h3>
                            <button onclick="amneziaApp.closeModal()" class="text-gray-400 hover:text-gray-600">
                                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                                </svg>
                            </button>
                        </div>

                        <div class="space-y-4">
                            <div>
                                <label class="block text-sm font-medium text-gray-700">Client name</label>
                                <input id="newClientName-${serverId}" type="text" class="mt-1 block w-full border border-gray-300 rounded-md px-3 py-2" placeholder="New Client">
                            </div>

                            ${this.getClientTransportSummaryHtml(
                                server.protocol || window.Protocols.DEFAULT,
                                this.formatTransportParamsSummary(server.protocol || window.Protocols.DEFAULT, server.transport_params || {})
                            )}

                            <div class="bg-blue-50 rounded p-3">
                                <div class="text-sm font-medium text-blue-900 mb-2">Client-side parameters</div>

                                <div class="mb-3">
                                    <label class="block text-xs font-medium text-blue-900">Copy from existing client</label>
                                    <select id="newClientCopyFrom-${serverId}" class="mt-1 block w-full border border-blue-200 rounded-md px-2 py-1 text-sm bg-white/70" onchange="amneziaApp.populateNewClientParamsFromExisting('${serverId}', this.value)">
                                        <option value="">Use defaults</option>
                                        ${clients.map((client) => `<option value="${safe(client.id)}">${safe(client.name)} (${safe(client.client_ip)})</option>`).join('')}
                                    </select>
                                </div>

                                ${this.getClientParamDescriptionHtml()}
                                <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm mb-3">
                                    <label class="block">
                                        <div class="text-xs font-medium text-blue-900">Jc</div>
                                        <input id="newClientParam-${serverId}-Jc" type="number" min="4" max="12" class="mt-1 w-full px-2 py-1 border border-blue-200 rounded text-sm" value="${safe(defaults.Jc ?? 8)}">
                                    </label>
                                    <label class="block">
                                        <div class="text-xs font-medium text-blue-900">Jmin</div>
                                        <input id="newClientParam-${serverId}-Jmin" type="number" class="mt-1 w-full px-2 py-1 border border-blue-200 rounded text-sm" value="${safe(defaults.Jmin ?? 8)}">
                                    </label>
                                    <label class="block">
                                        <div class="text-xs font-medium text-blue-900">Jmax</div>
                                        <input id="newClientParam-${serverId}-Jmax" type="number" class="mt-1 w-full px-2 py-1 border border-blue-200 rounded text-sm" value="${safe(defaults.Jmax ?? 80)}">
                                    </label>
                                </div>
                                <div class="grid grid-cols-1 gap-2">
                                    ${AmneziaApp.I_PARAM_KEYS.map((key) => `
                                        <label class="block text-xs">
                                            <div class="font-semibold text-blue-900 mb-1">${safe(key)}</div>
                                            <textarea id="newClientParam-${serverId}-${key}" rows="1" class="w-full px-2 py-1 border border-blue-200 rounded text-xs font-mono bg-white/70">${safe(defaults[key] ?? '')}</textarea>
                                        </label>
                                    `).join('')}
                                </div>
                                ${window.Protocols.supportsAwg3(server.protocol)
                                    ? this.renderAwg3ClientParamsHtml(`newClientParam-${serverId}`, defaults)
                                    : ''}
                            </div>
                        </div>

                        <div class="flex justify-end space-x-3 pt-4 border-t mt-4">
                            <button onclick="amneziaApp.submitAddClient('${serverId}')" class="btn-pill bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">Create Client</button>
                            <button onclick="amneziaApp.closeModal()" class="btn-pill bg-gray-500 text-white px-4 py-2 rounded text-sm hover:bg-gray-600">Close</button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        document.body.insertAdjacentHTML('beforeend', modalHtml);
        AmneziaApp.I_PARAM_KEYS.forEach((key) => {
            const el = document.getElementById(`newClientParam-${serverId}-${key}`);
            this.enableTextareaAutosize(el, 200);
        });
    }

    displayServerConfigModal(serverInfo) {
        const safe = (v) => this.escapeHtml(v);
        const transportParams = serverInfo.transport_params || {};
        const protocol = serverInfo.protocol || window.Protocols.DEFAULT;

        const modalHtml = `
            <div id="configModal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
                <div class="relative top-20 mx-auto p-5 border w-11/12 md:w-3/4 lg:w-1/2 shadow-lg rounded-md bg-white">
                    <div class="mt-3">
                        <div class="flex justify-between items-center mb-4">
                            <h3 class="text-xl font-bold text-gray-900">Server: <span class="text-purple-600 cursor-pointer hover:underline" title="Click to rename"
                                onclick="amneziaApp.renameServer('${serverInfo.id}')">${safe(serverInfo.name)}</span></h3>
                            <button onclick="amneziaApp.closeModal()" class="text-gray-400 hover:text-gray-600">
                                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                                </svg>
                            </button>
                        </div>

                        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
                            <div class="bg-gray-50 p-3 rounded">
                                <h4 class="font-semibold text-sm text-gray-700 mb-2">Basic Information</h4>
                                <div class="space-y-1 text-sm">
                                    <div><span class="font-medium">Interface:</span> ${serverInfo.interface}</div>
                                    <div><span class="font-medium">Port:</span> ${serverInfo.port}</div>
                                    <div><span class="font-medium">Subnet:</span> ${serverInfo.subnet}</div>
                                    <div><span class="font-medium">Server IP:</span> ${serverInfo.server_ip}</div>
                                    <div><span class="font-medium">Public IP:</span> ${serverInfo.public_ip}</div>
                                    <div><span class="font-medium">Status:</span>
                                        <span class="px-2 py-1 rounded-full text-xs ${
                                            serverInfo.status === 'running' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                                        }">${serverInfo.status}</span>
                                    </div>
                                </div>
                            </div>

                            <div class="bg-gray-50 p-3 rounded">
                                <h4 class="font-semibold text-sm text-gray-700 mb-2">Configuration</h4>
                                <div class="space-y-1 text-sm">
                                    <div><span class="font-medium">Protocol:</span> ${serverInfo.protocol}</div>
                                    <div><span class="font-medium">Clients:</span> ${serverInfo.clients_count}</div>
                                    <div><span class="font-medium">DNS:</span> ${safe(serverInfo.dns.join(', '))}</div>
                                    <div><span class="font-medium">MTU:</span> ${serverInfo.mtu}</div>
                                    <div class="truncate"><span class="font-medium">Public Key:</span>
                                        <span class="font-mono text-xs">${safe(serverInfo.public_key)}</span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div class="bg-gray-50 p-3 rounded mb-4">
                            <h4 class="font-semibold text-sm text-gray-700 mb-2">Networking</h4>
                            <div class="space-y-2 text-sm">
                                <label class="flex items-center gap-2">
                                    <input type="checkbox" id="serverEnableNat-${serverInfo.id}" ${serverInfo.enable_nat ? 'checked' : ''}>
                                    <span>Enable NAT/MASQUERADE</span>
                                </label>
                                <label class="flex items-center gap-2">
                                    <input type="checkbox" id="serverBlockLan-${serverInfo.id}" ${serverInfo.block_lan_cidrs ? 'checked' : ''}>
                                    <span>Block access to private LAN ranges</span>
                                </label>
                                <div class="text-xs text-gray-500">Requires iptables reapply if the server is running.</div>
                                <div>
                                        <button id="serverNetworkingPrimaryAction-${serverInfo.id}"
                                            class="hidden">
                                    </button>
                                </div>
                            </div>
                        </div>

                        <div class="bg-blue-50 p-3 rounded mb-4">
                            <div class="flex items-center justify-between mb-1">
                                <h4 class="font-semibold text-sm text-blue-700">Protocol and Transport</h4>
                            </div>
                            <div class="text-xs text-gray-500 mb-2">
                                These values are server-side: changing any of them rewrites every client
                                config, so clients must re-import theirs to keep connecting.
                            </div>

                            <div class="mb-3 text-xs">
                                <label class="block">
                                    <select id="serverProtocol-${serverInfo.id}"
                                        class="w-full md:w-56 px-2 py-1 border border-blue-200 rounded text-xs bg-white/70"
                                        onchange="amneziaApp.toggleProtocolFields(this.value, 'serverTransportParam-${serverInfo.id}-')">
                                        ${window.Protocols.optionsHtml(protocol)}
                                    </select>
                                </label>
                            </div>

                            <div id="serverTransportDescription-${serverInfo.id}">${this.getTransportDescriptionHtml(protocol, 'modal')}</div>

                            <div class="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs mb-3">
                                ${['S1','S2','S3','S4'].map((key) => `
                                    <label class="block">
                                        <div class="font-medium text-blue-800/80">${safe(key)}</div>
                                        <input id="serverTransportParam-${serverInfo.id}-${key}" type="number"
                                            class="mt-1 w-full px-2 py-1 border border-blue-200 rounded text-xs font-mono bg-white/70"
                                            value="${safe(transportParams[key] ?? '')}" />
                                    </label>
                                `).join('')}
                            </div>

                            <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs mb-3">
                                ${['H1','H2','H3','H4'].map((key) => `
                                    <label class="block">
                                        <div class="font-medium text-blue-800/80">${safe(key)}</div>
                                        <input id="serverTransportParam-${serverInfo.id}-${key}" type="text"
                                            class="mt-1 w-full px-2 py-1 border border-blue-200 rounded text-xs font-mono bg-white/70"
                                            value="${safe(transportParams[key] ?? '')}" />
                                    </label>
                                `).join('')}
                            </div>

                            <!-- AWG 3.0 only; shown/hidden by toggleProtocolFields() -->
                            <div id="serverTransportParam-${serverInfo.id}-HeaderProtectionKeyRow" class="text-xs">
                                <div class="font-medium text-blue-800/80">HeaderProtectionKey (optional, server-side)</div>
                                <div class="mt-1 flex items-center gap-2">
                                    <input id="serverTransportParam-${serverInfo.id}-HeaderProtectionKey" type="text"
                                        class="w-full px-2 py-1 border border-blue-200 rounded text-xs font-mono bg-white/70"
                                        placeholder="leave empty to disable header protection"
                                        value="${safe(transportParams.HeaderProtectionKey ?? '')}" />
                                    <button type="button"
                                        onclick="amneziaApp.fillHeaderProtectionKey('serverTransportParam-${serverInfo.id}-HeaderProtectionKey')"
                                        class="btn-pill bg-gray-500 text-white px-3 py-1 rounded text-xs hover:bg-gray-600 whitespace-nowrap">
                                        Generate
                                    </button>
                                </div>
                                <p class="mt-1 text-blue-800/80">
                                    Requires S1, S2, S3 and S4 to each be 12 or more.
                                </p>
                            </div>
                        </div>

                        <div class="flex justify-end space-x-3 pt-4 border-t">
                                <button onclick="amneziaApp.showRawServerConfig('${serverInfo.id}')"
                                    class="btn-pill bg-blue-500 text-white px-4 py-2 rounded text-sm hover:bg-blue-600">
                                View Full Config
                            </button>
                                <button id="serverConfigPrimaryAction-${serverInfo.id}"
                                    class="btn-pill bg-gray-500 text-white px-4 py-2 rounded text-sm hover:bg-gray-600">
                                Close
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        document.body.insertAdjacentHTML('beforeend', modalHtml);
        this.toggleProtocolFields(protocol, `serverTransportParam-${serverInfo.id}-`);
        this.setupServerTransportDirtyTracking(serverInfo.id, protocol);
        this.setupServerNetworkingDirtyTracking(serverInfo.id);
    }

    async showClientParamsModal(serverId, clientId) {
        const cached = (this.serverClients.get(serverId) || []).find((c) => c.id === clientId);
        const safeName = this.escapeHtml(cached?.name || clientId);
        const safe = (value) => this.escapeHtml(value);

        // Close any existing modal first
        this.closeModal();

        const modalHtml = `
            <div id="configModal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
                <div class="relative top-20 mx-auto p-5 border w-11/12 md:w-3/4 lg:w-1/2 shadow-lg rounded-md bg-white">
                    <div class="mt-3">
                        <div class="flex justify-between items-center mb-4">
                            <h3 class="text-xl font-bold text-gray-900">Client: <span class="text-sky-600 cursor-pointer hover:underline" title="Click to rename"
                                onclick="amneziaApp.renameClient('${serverId}', '${clientId}')">${safeName}</span></h3>
                            <button onclick="amneziaApp.closeModal()" class="text-gray-400 hover:text-gray-600">
                                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                                </svg>
                            </button>
                        </div>

                        <div id="clientIParamsBody" class="space-y-3">
                            <div class="text-sm text-gray-500">Loading…</div>
                        </div>

                        <div class="flex justify-end space-x-3 pt-4 border-t mt-4">
                                <button id="clientConfigPrimaryAction-${clientId}"
                                    class="btn-pill bg-gray-500 text-white px-4 py-2 rounded text-sm hover:bg-gray-600">
                                Close
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        document.body.insertAdjacentHTML('beforeend', modalHtml);

        try {
            const res = await this.apiFetch(`/api/servers/${serverId}/clients`);
            if (!res.ok) throw new Error('Failed to load client list');
            const clients = await res.json();
            const client = (clients || []).find((c) => c.id === clientId);
            const server = (this.lastServers || []).find((item) => String(item.id) === String(serverId));
            const defaults = server?.client_defaults || {};
            const params = client?.client_params || defaults;
            const transportSummary = this.formatTransportParamsSummary(
                client?.protocol || server?.protocol || window.Protocols.DEFAULT,
                server?.transport_params || {}
            );
            const protocolValue = client?.protocol || server?.protocol || window.Protocols.DEFAULT;

            const body = document.getElementById('clientIParamsBody');
            if (!body) return;
            body.innerHTML = `
                ${this.getClientTransportSummaryHtml(protocolValue, transportSummary)}
                <div class="bg-blue-50 rounded p-3">
                    <div class="text-sm font-medium text-blue-900 mb-2">Client-side parameters</div>
                    ${this.getClientParamDescriptionHtml()}
                    ${this.renderClientParamsFormHtml(`clientParam-${clientId}`, params, protocolValue)}
                </div>
            `;
            this.autosizeClientParamTextareas(`clientParam-${clientId}`, 260);
            this.setupClientParamsDirtyTracking(serverId, clientId);
        } catch (e) {
            const body = document.getElementById('clientIParamsBody');
            if (body) {
                body.innerHTML = `<div class="text-sm text-red-600">Failed to load current values: ${this.escapeHtml(e?.message || String(e))}</div>`;
            }
        }
    }

    displayRawConfigModal(config) {
        const safe = (v) => this.escapeHtml(v);
        const modalHtml = `
            <div id="rawConfigModal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
                <div class="relative top-10 mx-auto p-5 border w-11/12 md:w-3/4 lg:w-2/3 shadow-lg rounded-md bg-white">
                    <div class="mt-3">
                        <div class="flex justify-between items-center mb-4">
                            <h3 class="text-xl font-bold text-gray-900">Raw Configuration: ${safe(config.server_name)}</h3>
                            <button onclick="amneziaApp.closeModal()" class="text-gray-400 hover:text-gray-600">
                                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                                </svg>
                            </button>
                        </div>

                        <div class="mb-4">
                            <div class="flex justify-between items-center mb-2">
                                <span class="text-sm text-gray-600">Config path: ${safe(config.config_path)}</span>
                                <button onclick="amneziaApp.copyToClipboard('${btoa(JSON.stringify(config))}')"
                                    class="btn-pill bg-gray-500 text-white px-3 py-1 rounded text-xs hover:bg-gray-600">
                                    Copy JSON
                                </button>
                            </div>
                            <pre class="bg-gray-900 text-green-400 p-4 rounded text-sm overflow-x-auto max-h-96 overflow-y-auto">${safe(config.config_content)}</pre>
                        </div>

                        <div class="flex justify-end space-x-3 pt-4 border-t">
                                <button onclick="amneziaApp.downloadServerConfig('${config.server_id}')"
                                    class="btn-pill bg-green-500 text-white px-4 py-2 rounded text-sm hover:bg-green-600">
                                Download Config
                            </button>
                                <button onclick="amneziaApp.closeModal()"
                                    class="btn-pill bg-gray-500 text-white px-4 py-2 rounded text-sm hover:bg-gray-600">
                                Close
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        // Close any existing modal first
        this.closeModal();
        document.body.insertAdjacentHTML('beforeend', modalHtml);
    }

    async showServerLogs(serverId, iface) {
        const safe = (v) => this.escapeHtml(v);
        const interfaceName = String(iface || '').trim();

        // Close any existing modal first
        this.closeModal();

        const modalHtml = `
            <div id="logsModal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
                <div class="relative top-12 mx-auto p-5 border w-11/12 md:w-4/5 lg:w-3/4 shadow-lg rounded-md bg-white">
                    <div class="mt-2">
                        <div class="flex justify-between items-center mb-3">
                            <div>
                                <h3 class="text-xl font-bold text-gray-900">Server Logs</h3>
                                <div class="text-xs text-gray-500">Interface: <span class="font-mono">${safe(interfaceName || 'unknown')}</span></div>
                            </div>
                            <button onclick="amneziaApp.closeModal()" class="text-gray-400 hover:text-gray-600">
                                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                                </svg>
                            </button>
                        </div>

                        <div class="flex items-center justify-between mb-2">
                            <div class="text-xs text-gray-500">Auto-refresh: 10s</div>
                            <div class="flex items-center gap-2">
                                <button id="logsManualRefresh"
                                    class="w-7 h-7 rounded-full bg-white/80 text-blue-600 hover:text-blue-700 shadow-sm border border-blue-200/70 hover:border-blue-300/80 backdrop-blur flex items-center justify-center transition dark:bg-gray-800/80 dark:text-blue-300 dark:border-gray-700/80 dark:hover:border-blue-400/60"
                                        title="Refresh logs">
                                    <span class="text-[12px] leading-none">↻</span>
                                </button>
                                <div id="logsLastUpdate" class="text-xs text-gray-400">Last update: —</div>
                            </div>
                        </div>

                        <pre id="serverLogContent" class="bg-gray-900 text-emerald-200 p-3 rounded text-xs overflow-x-auto max-h-[60vh] overflow-y-auto"></pre>

                        <div class="flex justify-end space-x-3 pt-4 border-t mt-4">
                            <button onclick="amneziaApp.closeModal()" class="btn-pill bg-gray-600 text-white px-4 py-2 rounded text-sm hover:bg-gray-700">
                                Close
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        document.body.insertAdjacentHTML('beforeend', modalHtml);

        const refreshLogs = async () => {
            const logEl = document.getElementById('serverLogContent');
            const tsEl = document.getElementById('logsLastUpdate');
            if (!logEl) return;

            try {
                const url = `/api/system/awg-log?interface=${encodeURIComponent(interfaceName)}&lines=400`;
                const resp = await this.apiFetch(url);
                if (!resp.ok) {
                    const text = await resp.text();
                    throw new Error(text || `HTTP ${resp.status}`);
                }
                const data = await resp.json();
                const lines = Array.isArray(data?.lines) ? data.lines : [];
                logEl.textContent = lines.join('\n') || 'No log lines yet.';
                if (tsEl) tsEl.textContent = `Last update: ${new Date().toLocaleTimeString()}`;
            } catch (error) {
                logEl.textContent = `Failed to load logs: ${error?.message || error}`;
                if (tsEl) tsEl.textContent = `Last update: error`;
            }
        };

        const btn = document.getElementById('logsManualRefresh');
        if (btn) {
            btn.addEventListener('click', () => refreshLogs());
        }

        // Initial load + 10s polling
        await refreshLogs();
        this.logPoller = setInterval(refreshLogs, 10000);
    }

    showClientQRCode(serverId, clientId) {
        const cached = (this.serverClients.get(serverId) || []).find((c) => c.id === clientId);
        const safeClientName = this.escapeHtml(cached?.name || clientId);
        // Create modal for QR code
        const modalHtml = `
            <div id="qrModal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50 flex items-center justify-center">
                <div class="relative p-8 border w-11/12 md:w-3/4 lg:w-2/3 xl:w-1/2 shadow-2xl rounded-2xl bg-white">
                    <div class="flex flex-col">
                        <div class="flex justify-between items-center w-full mb-6">
                            <h3 class="text-xl font-bold text-gray-900">QR Code for Client: <span class="text-sky-600">${safeClientName}</span></h3>
                            <button onclick="amneziaApp.closeQRModal()"
                                    class="text-gray-400 hover:text-gray-600 transition-colors duration-200 p-1 rounded-full hover:bg-gray-100">
                                <svg class="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                                </svg>
                            </button>
                        </div>
                        
                        <div class="flex flex-col lg:flex-row gap-8 mb-6">
                            <!-- Left side: QR Code -->
                            <div class="lg:w-2/5">
                                <div class="bg-white p-6 rounded-xl border-2 border-gray-100 shadow-inner">
                                    <div id="qrcode" class="flex justify-center mb-4"></div>
                                    <p class="text-center text-sm text-gray-500">Scan with WireGuard app</p>
                                </div>
                                <!-- Download QR Code button outside the box -->
                                <div class="mt-4 text-center">
                                        <button onclick="amneziaApp.downloadQRCode()"
                                            class="btn-pill inline-flex items-center bg-gradient-to-r from-blue-500 to-cyan-500 hover:from-blue-600 hover:to-cyan-600 text-white px-5 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 shadow hover:shadow-lg transform hover:-translate-y-0.5">
                                        <svg class="w-5 h-5 mr-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
                                        </svg>
                                        Download QR Code Image
                                    </button>
                                </div>
                            </div>
                            
                            <!-- Right side: Configuration Text -->
                            <div class="lg:w-3/5">
                                <div class="mb-4">
                                    <div class="flex items-center justify-between mb-2">
                                        <label class="block text-sm font-medium text-gray-700">Configuration</label>
                                        <button onclick="amneziaApp.copyConfigText()"
                                            class="btn-pill bg-gradient-to-r from-blue-500 to-blue-600 hover:from-blue-600 hover:to-blue-700 text-white px-4 py-1.5 rounded-lg text-sm font-medium transition-colors duration-200 shadow hover:shadow-md">
                                            Copy Config
                                        </button>
                                    </div>
                                    <pre id="configText" class="bg-gray-900 text-green-400 p-4 rounded text-sm font-mono overflow-x-auto max-h-96 overflow-y-auto whitespace-pre-wrap">Loading configuration...</pre>
                                </div>
                            </div>
                        </div>
                        
                        <div class="flex justify-end space-x-4 w-full pt-6 border-t border-gray-200">
                                <button onclick="amneziaApp.downloadClientConfig('${serverId}', '${clientId}')"
                                    class="btn-pill bg-gradient-to-r from-green-500 to-green-600 hover:from-green-600 hover:to-green-700 text-white px-6 py-3 rounded-xl text-sm font-medium transition-all duration-200 shadow hover:shadow-lg transform hover:-translate-y-0.5">
                                <svg class="w-5 h-5 inline mr-2 -mt-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                                </svg>
                                Download Config File (.conf)
                            </button>
                                <button onclick="amneziaApp.closeQRModal()"
                                    class="btn-pill bg-gradient-to-r from-gray-500 to-gray-600 hover:from-gray-600 hover:to-gray-700 text-white px-6 py-3 rounded-xl text-sm font-medium transition-all duration-200 shadow hover:shadow-lg">
                                Close
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        // Close any existing modal first
        this.closeQRModal();
        document.body.insertAdjacentHTML('beforeend', modalHtml);

        // Fetch client config and generate QR code
        this.fetchAndGenerateQRCode(serverId, clientId);
    }

}

// Install onto AmneziaApp so existing call sites and inline onclick= handlers work.
Object.getOwnPropertyNames(ModalUi.prototype)
    .filter((name) => name !== 'constructor')
    .forEach((name) => {
        AmneziaApp.prototype[name] = ModalUi.prototype[name];
    });

window.ModalUi = ModalUi;
