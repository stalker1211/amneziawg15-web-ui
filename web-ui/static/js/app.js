// AmneziaWG Web UI - Main Application JavaScript
class AmneziaApp {
    constructor() {
        this.api = new window.ApiClient();
        this.socket = null;
        this.socketHealthTimer = null;
        this.socketReconnectFailures = 0;
        this.socketLastRebuildAt = 0;
        this.socketLifecycleHandlersInstalled = false;
        // The only page state: GET /api/servers, each server with its clients and
        // its last telemetry snapshot (`traffic`), kept current by traffic_update.
        this.lastServers = [];
        this.lastResyncAt = 0;
        this.drawerCtx = null;
        // New-server defaults: rendered into the page, updated by the settings drawer.
        this.environment = { ...(window.AppConfig.defaults || {}) };
        this.currentPublicIp = '';
        this.currentPublicIpCountryCode = '';
        this.init();
    }

    // Online = a handshake in the last 5 minutes, as the server computed it.
    isClientActiveFromTraffic(clientTraffic) {
        return !!(clientTraffic && clientTraffic.active);
    }

    init() {
        document.addEventListener('DOMContentLoaded', () => {
            console.log("AmneziaWG Web UI initializing...");
            this.applyTheme(this.getPreferredTheme(), false);
            const banner = document.getElementById('passwordBanner');
            if (banner) banner.hidden = !window.AppConfig.passwordIsDefault;
            this.setupEventListeners();
            this.setupSocketLifecycleHandlers();
            this.setupSocketIO();
            this.loadInitialData();
        });
    }

    // Utility function to safely get elements
    getElement(id) {
        const element = document.getElementById(id);
        if (!element) {
            console.warn(`Element with id '${id}' not found`);
        }
        return element;
    }

    // Escape user-controlled strings for safe HTML rendering
    escapeHtml(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    formatTransportParamsSummary(protocol, transportParams = {}) {
        const orderedKeys = (window.Protocols.supportsS34(protocol))
            ? ['S1', 'S2', 'S3', 'S4', 'H1', 'H2', 'H3', 'H4']
            : ['S1', 'S2', 'H1', 'H2', 'H3', 'H4'];
        const parts = orderedKeys
            .filter((key) => transportParams[key] !== undefined && transportParams[key] !== null && transportParams[key] !== '')
            .map((key) => `${key}=${transportParams[key]}`);

        // Never render the key itself: this summary is shown in the client dialogs.
        if (window.Protocols.supportsAwg3(protocol) && String(transportParams.HeaderProtectionKey || '').trim()) {
            parts.push('HeaderProtection=on');
        }
        if (window.Protocols.supportsAwg31(protocol) && transportParams.RandomTrailers) {
            parts.push('RandomTrailers=on');
        }
        if (window.Protocols.supportsAwg31(protocol) && transportParams.DisableCookies) {
            parts.push('DisableCookies=on');
        }

        return parts.length > 0 ? parts.join(', ') : 'No transport parameters set';
    }

    async apiFetch(input, init = {}) {
        return this.api.fetch(input, init);
    }

    async downloadBlob(url, fallbackFilename) {
        return this.api.downloadBlob(url, fallbackFilename);
    }

    autosizeTextarea(textarea, maxHeightPx = 260) {
        if (!textarea) return;
        // Let it shrink too (set to auto first)
        textarea.style.height = 'auto';
        const next = Math.min(textarea.scrollHeight, maxHeightPx);
        textarea.style.height = `${next}px`;
        textarea.style.overflowY = textarea.scrollHeight > maxHeightPx ? 'auto' : 'hidden';
    }

    enableTextareaAutosize(textarea, maxHeightPx = 260) {
        if (!textarea) return;
        textarea.style.resize = 'none';
        textarea.style.whiteSpace = 'pre-wrap';
        textarea.style.overflowWrap = 'anywhere';

        const handler = () => this.autosizeTextarea(textarea, maxHeightPx);
        textarea.removeEventListener('input', handler);
        textarea.addEventListener('input', handler);
        // Initial sizing
        handler();
    }

    // UTF-8 byte size, for the QR payload diagnostics.
    getUtf8ByteLength(text) {
        return new TextEncoder().encode(String(text)).length;
    }

    generateQrIntoContainer(qrContainer, text, size = 300) {
        if (!qrContainer) return;

        const value = String(text ?? '');
        qrContainer.innerHTML = '';

        if (!value.trim()) {
            qrContainer.innerHTML = `
                <div class="rounded-lg border px-3 py-2 text-sm bg-red-50 border-red-200 text-red-800 dark:bg-[#3b1219] dark:border-[#7f1d1d] dark:text-[#fecaca]">
                    No configuration text to encode.
                </div>
            `;
            return;
        }

        // M, then L for a config too large for M. H, the densest, turned a 450-byte
        // config into 109 modules a side (81 at M): harder to scan off a screen, and
        // a screen does not get dirty.
        const levels = [QRCode?.CorrectLevel?.M, QRCode?.CorrectLevel?.L].filter((l) => l !== undefined);

        let lastError = null;
        for (const level of levels) {
            try {
                qrContainer.innerHTML = '';
                new QRCode(qrContainer, {
                    text: value,
                    width: size,
                    height: size,
                    colorDark: "#000000",
                    colorLight: "#ffffff",
                    correctLevel: level,
                    margin: 1
                });
                lastError = null;
                break;
            } catch (err) {
                lastError = err;
            }
        }

        if (lastError) {
            const bytes = this.getUtf8ByteLength(value);
            const safeMsg = this.escapeHtml(lastError?.message || String(lastError));
            qrContainer.innerHTML = `
                <div class="rounded-lg border px-3 py-2 text-sm bg-red-50 border-red-200 text-red-800 dark:bg-[#3b1219] dark:border-[#7f1d1d] dark:text-[#fecaca]">
                    <div class="font-semibold mb-1">QR code could not be generated</div>
                    <div class="mb-2">Most commonly this happens when the config is too large for a QR code (payload: <span class=\"font-mono\">${bytes}</span> bytes).</div>
                    <div class="text-xs text-red-600 dark:text-[#fca5a5] font-mono break-all">${safeMsg}</div>
                    <div class="mt-2">Use Download .conf instead.</div>
                </div>
            `;
        }
    }

    // Every button and switch in generated markup names its action in data-action,
    // with data-server / data-client; one listener per event type runs it. There are
    // no inline handlers, so nginx can send script-src 'self'. Buttons act on click,
    // switches and selects on change.
    setupActions() {
        const ids = (el) => [el.dataset.server, el.dataset.client];
        const actions = {
            'toggle-server': (el) => this.toggleServer(el.dataset.server, el.checked),
            'add-client': (el) => this.addClient(el.dataset.server),
            'server-settings': (el) => this.showServerConfig(el.dataset.server),
            'server-menu': (el) => this.openServerMenu(el.dataset.server, el),
            'probe-egress': (el) => this.probeServerEgressIp(el.dataset.server, el),
            'raw-config': (el) => this.showRawServerConfig(el.dataset.server),
            'toggle-client': (el) => this.toggleClientSuspend(...ids(el)),
            'client-qr': (el) => { window.Ui.closeDrawer(); this.showClientQRCode(...ids(el)); },
            'client-edit': (el) => this.showClientParamsModal(...ids(el)),
            'client-menu': (el) => this.openClientMenu(...ids(el), el),
            'randomize': () => this.generateRandomParams(),
            'generate-key': () => this.fillHeaderProtectionKey('t-HeaderProtectionKey'),
            'copy-public-key': () => this.copyText(document.getElementById('s-publicKey')?.textContent, 'Public key'),
            'show-checks': () => document.getElementById('checks')?.scrollIntoView({ block: 'nearest' }),
            'open-settings': () => this.openSettings(),
            'iptables-check': () => this.checkIptables(),
        };
        const run = (event) => {
            const el = event.target.closest('[data-action]');
            if (!el || !actions[el.dataset.action]) return;
            const onChange = el.tagName === 'INPUT' || el.tagName === 'SELECT';
            if ((event.type === 'change') !== onChange) return;
            actions[el.dataset.action](el, event);
        };
        document.addEventListener('click', run);
        document.addEventListener('change', run);
    }

    setupEventListeners() {
        this.setupActions();
        this.getElement('showCreateServerBtn')?.addEventListener('click', () => this.openCreateServerModal());
        this.getElement('themeToggleBtn')?.addEventListener('click', () => this.toggleTheme());
        this.getElement('refreshIpBtn')?.addEventListener('click', () => this.refreshPublicIp());
        this.setupDrawerForms();
    }

    // Light/dark toggle. The OS preference only picks the first theme; after a click
    // the choice is remembered. The theme is a `dark` class on <body>, which the
    // Tailwind dark: classes and the few body.dark rules in style.css key off.
    getPreferredTheme() {
        try {
            const saved = localStorage.getItem('amnezia_theme');
            if (saved === 'dark') return true;
            if (saved === 'light') return false;
        } catch (_) {
            // ignore
        }

        return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    }

    applyTheme(isDark, persist = true) {
        document.body.classList.toggle('dark', !!isDark);
        this.updateThemeButton(!!isDark);

        if (persist) {
            try {
                localStorage.setItem('amnezia_theme', isDark ? 'dark' : 'light');
            } catch (_) {
                // ignore
            }
        }
    }

    toggleTheme() {
        const isDark = document.body.classList.contains('dark');
        this.applyTheme(!isDark, true);
    }

    updateThemeButton(isDark) {
        const btn = this.getElement('themeToggleBtn');
        if (!btn) return;
        btn.innerHTML = window.Ui.icon(isDark ? 'sun' : 'moon');
        btn.title = isDark ? 'Switch to light theme' : 'Switch to dark theme';
        btn.setAttribute('aria-label', btn.title);
    }

    // Collapse long help text so dialogs stay short enough to show their fields
    // and action buttons without scrolling. Closed by default.
    updateTransportDescription(protocol, prefix = 't-') {
        const description = document.getElementById(`${prefix}Description`);
        if (!description) return;
        description.innerHTML = this.getTransportDescriptionHtml(protocol);
    }

    toggleProtocolFields(protocol, prefix = 't-') {
        const allowS34 = window.Protocols.supportsS34(protocol);
        const allowRanges = window.Protocols.supportsHeaderRanges(protocol);
        const allowAwg3 = window.Protocols.supportsAwg3(protocol);
        const allowAwg31 = window.Protocols.supportsAwg31(protocol);

        ['S3', 'S4'].forEach((key) => {
            const element = document.getElementById(`${prefix}${key}`);
            if (!element) return;
            element.disabled = !allowS34;
            if (!allowS34) {
                element.value = '';
            }
            const container = element.closest('label, div');
            if (container) {
                container.style.display = allowS34 ? '' : 'none';
            }
        });

        ['H1', 'H2', 'H3', 'H4'].forEach((key) => {
            const element = document.getElementById(`${prefix}${key}`);
            if (!element) return;
            element.placeholder = allowRanges ? '123 or 123-456' : '123';
        });

        // AWG 3.x header protection. Clearing the key on downgrade keeps the
        // submitted payload consistent with the protocol the user picked.
        const keyElement = document.getElementById(`${prefix}HeaderProtectionKey`);
        if (keyElement) {
            keyElement.disabled = !allowAwg3;
            if (!allowAwg3) keyElement.value = '';
            const row = document.getElementById(`${prefix}HeaderProtectionKeyRow`)
                || keyElement.closest('label, div');
            if (row) row.style.display = allowAwg3 ? '' : 'none';
        }

        ['RandomTrailers', 'DisableCookies'].forEach((key) => {
            const element = document.getElementById(`${prefix}${key}`);
            if (!element) return;
            element.disabled = !allowAwg31;
            if (!allowAwg31) element.checked = false;
        });
        const awg31Row = document.getElementById(`${prefix}Awg31OptionsRow`);
        if (awg31Row) awg31Row.style.display = allowAwg31 ? '' : 'none';

        this.updateTransportDescription(protocol, prefix);
    }

    setupSocketLifecycleHandlers() {
        if (this.socketLifecycleHandlersInstalled) return;
        this.socketLifecycleHandlersInstalled = true;

        window.addEventListener('pageshow', () => {
            this.handleSocketResume('pageshow');
        });

        window.addEventListener('focus', () => {
            this.handleSocketResume('focus');
        });

        window.addEventListener('online', () => {
            this.handleSocketResume('online');
        });

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                this.handleSocketResume('visibilitychange');
            }
        });
    }

    clearSocketHealthTimer() {
        if (this.socketHealthTimer) {
            clearTimeout(this.socketHealthTimer);
            this.socketHealthTimer = null;
        }
    }

    teardownSocket() {
        this.clearSocketHealthTimer();

        if (!this.socket) return;

        try {
            this.socket.off();
        } catch (_) {
            // ignore
        }

        try {
            this.socket.disconnect();
        } catch (_) {
            // ignore
        }

        this.socket = null;
    }

    createSocket() {
        const socketUrl = window.location.origin;
        return io(socketUrl, {
            path: '/socket.io',
            transports: ['websocket'],
            forceNew: true,
            reconnection: true,
            reconnectionAttempts: Infinity,
            reconnectionDelay: 1000,
            reconnectionDelayMax: 5000,
            timeout: 5000,
        });
    }

    // pageshow, focus and visibilitychange tend to fire together, and a reconnect right
    // after them; one reload serves them all.
    resyncAppState() {
        const now = Date.now();
        if (now - this.lastResyncAt < 2000) return;
        this.lastResyncAt = now;
        this.loadServers();
        this.loadPublicIp();
    }

    scheduleSocketHealthCheck(reason, delayMs = 3000) {
        this.clearSocketHealthTimer();
        this.socketHealthTimer = setTimeout(() => {
            if (!this.socket || this.socket.connected) {
                return;
            }

            console.warn(`Socket health check failed after ${reason}, rebuilding connection`);
            this.rebuildSocket(`health-check:${reason}`);
        }, delayMs);
    }

    bindSocketHandlers(socket) {
        socket.on('connect', () => {
            if (socket !== this.socket) return;

            this.socketReconnectFailures = 0;
            this.clearSocketHealthTimer();
            console.log("✅ Connected to server via WebSocket");
            this.updateStatus('Connected to AmneziaWG Web UI', true);
            this.resyncAppState();
        });

        socket.on('disconnect', (reason) => {
            if (socket !== this.socket) return;

            console.log("❌ Disconnected from server", reason ? `(${reason})` : '');
            this.updateStatus('Reconnecting to AmneziaWG Web UI...', false);

            if (reason !== 'io client disconnect') {
                this.scheduleSocketHealthCheck(`disconnect:${reason || 'unknown'}`);
            }
        });

        socket.on('connect_error', (error) => {
            if (socket !== this.socket) return;

            this.socketReconnectFailures += 1;
            console.error("❌ WebSocket connection error:", error);
            this.updateStatus('Connection error - retrying...', false);

            if (document.visibilityState === 'visible' && this.socketReconnectFailures >= 2) {
                this.rebuildSocket(`connect-error:${error?.message || 'unknown'}`);
                return;
            }

            this.scheduleSocketHealthCheck(`connect-error:${error?.message || 'unknown'}`, 2500);
        });

        socket.on('status', (data) => {
            if (socket !== this.socket) return;

            console.log("Status update:", data);
            if (data.public_ip) {
                this.updatePublicIp(data.public_ip, data.public_ip_geo_country_code);
            }
        });

        socket.on('server_status', (data) => {
            if (socket !== this.socket) return;

            console.log("Server status update:", data);
            this.loadServers();
        });

        socket.on('traffic_update', (data) => {
            if (socket !== this.socket) return;
            this.updateServerTraffic(data.server_id, data.traffic);
        });
    }

    rebuildSocket(reason = 'manual') {
        const now = Date.now();
        if ((now - this.socketLastRebuildAt) < 1500) {
            return;
        }

        this.socketLastRebuildAt = now;
        this.teardownSocket();
        this.socket = this.createSocket();
        this.bindSocketHandlers(this.socket);

        console.log(`Rebuilt Socket.IO connection (${reason})`);
        this.updateStatus('Reconnecting to AmneziaWG Web UI...', false);
    }

    handleSocketResume(trigger) {
        if (this.socket && this.socket.connected) {
            this.resyncAppState();
            return;
        }

        if (!this.socket) {
            this.rebuildSocket(`resume:${trigger}`);
            return;
        }

        console.log(`Socket resume check triggered by ${trigger}`);
        this.updateStatus('Reconnecting to AmneziaWG Web UI...', false);

        try {
            this.socket.connect();
        } catch (_) {
            this.rebuildSocket(`resume-connect:${trigger}`);
            return;
        }

        this.scheduleSocketHealthCheck(`resume:${trigger}`, 2500);
    }

    setupSocketIO() {
        this.rebuildSocket('initial');
    }

    // The header pill: "Live" while the socket is up (traffic arrives every 7 s),
    // "Reconnecting…" otherwise; the full message is its tooltip.
    updateStatus(message, isConnected = null) {
        const frame = this.getElement('statusFrame');
        const label = this.getElement('status');
        const dot = this.getElement('statusDot');
        if (typeof isConnected !== 'boolean') return;
        if (frame) {
            frame.dataset.state = isConnected ? 'connected' : 'reconnecting';
            frame.title = isConnected ? `${message}; traffic refreshes every 7 s` : message;
        }
        if (label) label.textContent = isConnected ? 'Live' : 'Reconnecting…';
        if (dot) {
            dot.classList.remove('bg-gray-400', 'bg-green-500', 'bg-amber-400');
            dot.classList.add(isConnected ? 'bg-green-500' : 'bg-amber-400');
        }
    }

    updatePublicIp(ip, countryCode = null) {
        const publicIpElement = this.getElement('publicIp');
        const nextIp = String(ip || '').trim();
        if (!nextIp) return;

        const normalizedCountryCode = typeof countryCode === 'string'
            ? countryCode.trim().toUpperCase()
            : '';

        if (normalizedCountryCode) {
            this.currentPublicIpCountryCode = normalizedCountryCode;
        } else if (this.currentPublicIp && this.currentPublicIp !== nextIp) {
            this.currentPublicIpCountryCode = '';
        }

        this.currentPublicIp = nextIp;

        if (publicIpElement) {
            publicIpElement.textContent = nextIp;
        }
    }

    // A new public IP is written into every client config's Endpoint, so say how
    // many devices now hold a config pointing at the old address.
    async refreshPublicIp() {
        const button = this.getElement('refreshIpBtn');
        const before = this.currentPublicIp;
        if (button) button.disabled = true;
        try {
            // A 502 means detection failed and nothing was changed.
            const data = await this.postJson('/api/system/refresh-ip', {});
            this.updatePublicIp(data.public_ip, data.public_ip_geo_country_code);
            await this.loadServers();
            if (data.public_ip === before) {
                this.showTempMessage(`Public IP is still ${data.public_ip}`, 'success');
            } else {
                const stale = this.lastServers.flatMap((s) => s.clients || []).filter((c) => c.config_outdated).length;
                this.showTempMessage(stale
                    ? `Public IP changed to ${data.public_ip}. ${stale} client config${stale === 1 ? '' : 's'} to re-import.`
                    : `Public IP changed to ${data.public_ip}`, stale ? 'info' : 'success');
            }
        } catch (error) {
            console.error('Error refreshing IP:', error);
            this.showTempMessage(error.message, 'error');
        } finally {
            if (button) button.disabled = false;
        }
    }

    async probeServerEgressIp(serverId, buttonElement = null) {
        const server = (this.lastServers || []).find((s) => s.id === serverId);
        if (server && server.status !== 'running') {
            this.showTempMessage(`${server.name} is stopped. Start it to check its egress IP.`, 'error');
            return;
        }
        if (buttonElement) buttonElement.disabled = true;
        try {
            const probe = await this.postJson(`/api/servers/${serverId}/egress-ip`, {});
            const name = server?.name || 'Server';
            if (probe.external_ip) {
                this.showTempMessage(`${name} reaches the internet as ${probe.external_ip}`, 'success');
            } else {
                this.showTempMessage(`${name}: no external access${probe.error ? ` (${probe.error})` : ''}`, 'error');
            }
        } catch (error) {
            console.error('Error probing server egress IP:', error);
            this.showTempMessage('Error probing egress IP: ' + error.message, 'error');
        } finally {
            if (buttonElement) buttonElement.disabled = false;
        }
        this.loadServers();
    }

    // Custom signature packets I1-I5 (AWG 1.5+). Multi-line textarea fields.
    static get I_PARAM_KEYS() {
        return window.AppConfig.params?.signature || [];
    }

    // AWG 3.x client-side params. Range-valued ('a' or 'a-b'); empty means the
    // daemon keeps its built-in WireGuard default.
    static get AWG3_CLIENT_PARAM_KEYS() {
        return window.AppConfig.params?.awg3Client || [];
    }

    // AWG 3.x client-side fields. Rendered only for AWG 3.x servers, matching how
    // S3/S4 are hidden for AWG 1.5.
    autosizeClientParamTextareas(prefix, maxHeightPx = 260) {
        AmneziaApp.I_PARAM_KEYS.forEach((key) => {
            const el = document.getElementById(`${prefix}-${key}`);
            this.enableTextareaAutosize(el, maxHeightPx);
        });
    }

    // Through the throttle: the socket connects a moment later and would load it all again.
    loadInitialData() {
        this.resyncAppState();
    }

    loadPublicIp() {
        this.apiFetch('/api/system/status')
            .then(response => response.json())
            .then(data => {
                if (data.public_ip) {
                    this.updatePublicIp(data.public_ip, data.public_ip_geo_country_code);
                } else {
                    const el = this.getElement('publicIp');
                    if (el) el.textContent = 'Not detected';
                }
            })
            .catch(error => {
                console.error('Error loading public IP:', error);
            });
    }

    loadServers() {
        return this.apiFetch('/api/servers')
            .then(response => {
                if (!response.ok) {
                    return response.json().then(err => {
                        throw new Error(err?.error || `HTTP ${response.status}`);
                    });
                }
                return response.json();
            })
            .then(servers => {
                this.lastServers = Array.isArray(servers) ? servers : [];
                this.renderServers(servers);
            })
            .catch(error => {
                console.error('Error loading servers:', error);
                this.showServerError('Failed to load servers');
            });
    }

    renderServers(servers) {
        const serversList = this.getElement('serversList');
        if (!serversList) return;

        // A full render replaces every button, so a ⋯ menu would stay anchored to a
        // detached one.
        window.Ui.closeMenu();
        serversList.innerHTML = window.ServerUi.renderServersHtml({
            servers,
            escapeHtml: (v) => this.escapeHtml(v),
            renderServerClients: (serverId, clients) => {
                const server = servers.find((s) => s.id === serverId) || {};
                return this.renderServerClients(serverId, clients, server.traffic || {});
            },
        });
        this.renderStrip();
    }

    renderServerClients(serverId, clients, traffic = {}) {
        const server = (this.lastServers || []).find((s) => s.id === serverId) || { id: serverId };
        return window.ServerUi.renderServerClientsHtml({
            server,
            clients,
            traffic,
            escapeHtml: (v) => this.escapeHtml(v),
            isClientActiveFromTraffic: (clientTraffic) => this.isClientActiveFromTraffic(clientTraffic),
        });
    }

    // The status strip: servers running and clients online (handshake <= 5 min).
    renderStrip() {
        const servers = this.lastServers || [];
        const running = servers.filter((s) => s.status === 'running').length;
        let total = 0;
        let online = 0;
        servers.forEach((server) => {
            const clients = server.clients || [];
            const traffic = server.traffic || {};
            total += clients.length;
            online += clients.filter((c) => window.ServerUi.isOnline(server, c, traffic[c.id],
                (t) => this.isClientActiveFromTraffic(t))).length;
        });
        const set = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = String(value); };
        set('stripServersRunning', running);
        set('stripServersTotal', servers.length);
        set('stripClientsOnline', online);
        set('stripClientsTotal', total);
    }

    openServerMenu(serverId, anchor) {
        const server = (this.lastServers || []).find((s) => s.id === serverId);
        if (!server) return;
        window.Ui.openMenu(anchor, [
            { label: 'Logs', icon: 'logs', run: () => this.showServerLogs(serverId, server.interface) },
            { label: 'Full config', icon: 'code', run: () => this.showRawServerConfig(serverId) },
            { label: 'Rename', icon: 'edit', run: () => this.renameServer(serverId, document.querySelector(`[data-name="${serverId}"]`)) },
            '-',
            { label: 'Delete server', icon: 'trash', danger: true, run: () => this.deleteServer(serverId) },
        ]);
    }

    openClientMenu(serverId, clientId, anchor) {
        window.Ui.openMenu(anchor, [
            { label: 'Rename', icon: 'edit', run: () => this.renameClient(serverId, clientId, document.querySelector(`[data-name="${clientId}"]`)) },
            { label: 'Download .conf', icon: 'download', run: () => this.downloadClientConfig(serverId, clientId) },
            '-',
            { label: 'Delete client', icon: 'trash', danger: true, run: () => this.deleteClient(serverId, clientId) },
        ]);
    }

    // Every 7 s: the running server's new telemetry, patched into its rows in place.
    updateServerTraffic(serverId, traffic) {
        const server = (this.lastServers || []).find((s) => s.id === serverId);
        if (!server) return;
        const previous = server.traffic || {};
        server.traffic = (traffic && typeof traffic === 'object') ? traffic : {};
        window.ServerUi.patchClients({
            container: this.getElement(`clients-${serverId}`),
            server,
            traffic: server.traffic,
            previous,
            escapeHtml: (v) => this.escapeHtml(v),
            isClientActiveFromTraffic: (t) => this.isClientActiveFromTraffic(t),
        });
        this.renderStrip();
    }

    showServerError(message) {
        const serversList = this.getElement('serversList');
        if (serversList) {
            serversList.innerHTML = `
                <div class="text-center py-8 text-red-500 dark:text-[#fca5a5]">
                    ${message}
                </div>
            `;
        }
    }

    // Server management methods
    async deleteServer(serverId) {
        const server = (this.lastServers || []).find(s => s.id === serverId);
        const count = (server?.clients || []).length;
        const ok = await window.Ui.confirm({
            title: `Delete ${this.escapeHtml(server?.name || 'this server')}?`,
            body: `The interface <span class="font-mono">${this.escapeHtml(server?.interface || '')}</span> stops and `
                + `${count === 1 ? 'its client config stops' : `its ${count} client configs stop`} working. This cannot be undone.`,
            confirmLabel: 'Delete server',
        });
        if (!ok) return;
        try {
            await this.postJson(`/api/servers/${serverId}`, undefined, 'DELETE');
            this.showTempMessage(`${server?.name || 'Server'} deleted`, 'success');
        } catch (error) {
            console.error('Error deleting server:', error);
            this.showTempMessage('Error deleting server: ' + error.message, 'error');
        }
        this.loadServers();
    }

    async deleteClient(serverId, clientId) {
        const server = (this.lastServers || []).find(s => s.id === serverId);
        const client = (server?.clients || []).find(c => c.id === clientId);
        const ok = await window.Ui.confirm({
            title: `Delete ${this.escapeHtml(client?.name || 'this client')}?`,
            body: `Its config stops working on the device right away and `
                + `<span class="font-mono">${this.escapeHtml(client?.client_ip || '')}</span> becomes free. This cannot be undone.`,
            confirmLabel: 'Delete client',
        });
        if (!ok) return;
        try {
            await this.postJson(`/api/servers/${serverId}/clients/${clientId}`, undefined, 'DELETE');
            this.showTempMessage(`${client?.name || 'Client'} deleted`, 'success');
        } catch (error) {
            console.error('Error deleting client:', error);
            this.showTempMessage('Error deleting client: ' + error.message, 'error');
        }
        this.loadServers();
    }

    // Inline rename: `target` is the element showing the name; it becomes a text field.
    renameServer(serverId, target) {
        const server = (this.lastServers || []).find(s => s.id === serverId);
        window.Ui.startRename(target, {
            value: server ? server.name : (target?.textContent || '').trim(),
            label: 'New server name',
            onSave: async (name) => {
                await this.postJson(`/api/servers/${serverId}/rename`, { name });
                this.showTempMessage(`Renamed to ${name}`, 'success');
                this.loadServers();
            },
        });
    }

    renameClient(serverId, clientId, target) {
        const client = this.findClient(serverId, clientId);
        window.Ui.startRename(target, {
            value: client ? client.name : (target?.textContent || '').trim(),
            label: 'New client name',
            inputClass: 'text-sm font-medium',
            onSave: async (name) => {
                await this.postJson(`/api/servers/${serverId}/clients/${clientId}/rename`, { name });
                if (client) client.name = name;
                this.showTempMessage(`Renamed to ${name}`, 'success');
                this.loadServers();
            },
        });
    }

    findClient(serverId, clientId) {
        const server = (this.lastServers || []).find((s) => s.id === serverId);
        return (server?.clients || []).find((c) => c.id === clientId);
    }

    // GET JSON; throws with the server's error message on a non-2xx answer.
    async getJson(url) {
        const response = await this.apiFetch(url);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
        return data;
    }

    // POST (or another verb) JSON and return the parsed reply; throws with the
    // server's error message on a non-2xx answer.
    async postJson(url, body, method = 'POST') {
        const response = await this.apiFetch(url, {
            method,
            body: body === undefined ? undefined : JSON.stringify(body),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
        return data;
    }

    async toggleClientSuspend(serverId, clientId) {
        try {
            const data = await this.postJson(`/api/servers/${serverId}/clients/${clientId}/suspend`, {});
            const name = data?.client?.name || 'Client';
            this.showTempMessage(data?.suspended
                ? `${name} suspended; its config cannot connect until reactivated`
                : `${name} reactivated`, 'success');
        } catch (error) {
            console.error('Error toggling client suspend:', error);
            this.showTempMessage('Error toggling client suspend: ' + error.message, 'error');
        }
        this.loadServers();
    }

    async toggleServer(serverId, shouldRun) {
        const action = shouldRun ? 'start' : 'stop';
        const server = (this.lastServers || []).find((s) => s.id === serverId);
        try {
            await this.postJson(`/api/servers/${serverId}/${action}`, {});
            this.showTempMessage(`${server?.name || 'Server'} ${shouldRun ? 'started' : 'stopped'}`, 'success');
        } catch (error) {
            console.error(`Error ${action}ing server:`, error);
            this.showTempMessage(`Error ${action}ing server: ` + error.message, 'error');
        }
        this.loadServers();
    }

    async copyText(text, what = 'Text') {
        try {
            await navigator.clipboard.writeText(String(text || ''));
            this.showTempMessage(`${what} copied`, 'success');
        } catch (_) {
            this.showTempMessage('This browser blocked the clipboard; select the text and copy it instead.', 'error');
        }
    }

    showTempMessage(message, type) {
        window.Ui.toast(message, ['error', 'info'].includes(type) ? type : 'success');
    }
}

// Initialize the application
const amneziaApp = new AmneziaApp();