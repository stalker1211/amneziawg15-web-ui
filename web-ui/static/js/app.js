// AmneziaWG Web UI - Main Application JavaScript
class AmneziaApp {
    constructor() {
        this.api = new window.ApiClient();
        this.socket = null;
        this.socketHealthTimer = null;
        this.socketReconnectFailures = 0;
        this.socketLastRebuildAt = 0;
        this.socketLifecycleHandlersInstalled = false;
        this.lastServers = [];
        this.serverClients = new Map();
        this.lastTrafficByServer = new Map();
        this.drawerCtx = null;
        this.environment = {};
        this.currentPublicIp = '';
        this.currentPublicIpCountryCode = '';
        this.logPoller = null;
        this.init();
    }

    isClientActiveFromTraffic(clientTraffic) {
        if (!clientTraffic || typeof clientTraffic !== 'object') return false;
        if (typeof clientTraffic.active === 'boolean') return clientTraffic.active;
        const seconds = clientTraffic.latest_handshake_seconds;
        if (typeof seconds === 'number' && Number.isFinite(seconds)) return seconds <= 300;
        const hs = String(clientTraffic.latest_handshake || '').toLowerCase();
        if (!hs || hs.includes('never')) return false;
        // Very small fallback parser (covers the common 'N seconds/minutes ago' format).
        let total = 0;
        const unitSeconds = { second: 1, minute: 60, hour: 3600, day: 86400 };
        const re = /(\d+)\s+(second|minute|hour|day)s?/g;
        let m;
        while ((m = re.exec(hs)) !== null) {
            total += Number(m[1]) * (unitSeconds[m[2]] || 0);
        }
        return total > 0 && total <= 300;
    }

    // Generate a base64-encoded 32-byte key, matching `awg genkey` output format.
    // Used for the AWG 3.x HeaderProtectionKey, which is a plain symmetric key.
    generateBase64Key() {
        const bytes = new Uint8Array(32);
        crypto.getRandomValues(bytes);
        let binary = '';
        bytes.forEach((b) => { binary += String.fromCharCode(b); });
        return btoa(binary);
    }

    // Fill a HeaderProtectionKey input with a freshly generated key. Used by the
    // Generate buttons in both the create form and the server config modal.
    fillHeaderProtectionKey(elementId) {
        const element = document.getElementById(elementId);
        if (!element) return;
        element.value = this.generateBase64Key();
        element.dispatchEvent(new Event('input', { bubbles: true }));
    }

    init() {
        document.addEventListener('DOMContentLoaded', () => {
            console.log("AmneziaWG Web UI initializing...");
            this.applyTheme(this.getPreferredTheme(), false);
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

    // Estimate UTF-8 byte size for QR payload diagnostics
    getUtf8ByteLength(text) {
        try {
            if (typeof TextEncoder !== 'undefined') {
                return new TextEncoder().encode(String(text)).length;
            }
        } catch (_) {
            // fall through
        }
        // Fallback (older browsers)
        return unescape(encodeURIComponent(String(text))).length;
    }

    generateQrIntoContainer(qrContainer, text) {
        if (!qrContainer) return;

        const value = String(text ?? '');
        qrContainer.innerHTML = '';

        if (!value.trim()) {
            qrContainer.innerHTML = `
                <div class="text-sm text-red-600 dark:text-[#fca5a5] bg-red-50 dark:bg-[#7f1d1d] border border-red-200 rounded-lg p-3">
                    No configuration text to encode.
                </div>
            `;
            return;
        }

        // Try higher error correction first, then fall back to fit larger payloads.
        const levels = [
            QRCode?.CorrectLevel?.H,
            QRCode?.CorrectLevel?.Q,
            QRCode?.CorrectLevel?.M,
            QRCode?.CorrectLevel?.L
        ].filter((l) => l !== undefined);

        let lastError = null;
        for (const level of levels) {
            try {
                qrContainer.innerHTML = '';
                new QRCode(qrContainer, {
                    text: value,
                    width: 300,
                    height: 300,
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
                <div class="text-sm text-red-700 bg-red-50 dark:bg-[#7f1d1d] border border-red-200 rounded-lg p-3">
                    <div class="font-semibold mb-1">QR code could not be generated</div>
                    <div class="mb-2">Most commonly this happens when the config is too large for a QR code (payload: <span class=\"font-mono\">${bytes}</span> bytes).</div>
                    <div class="text-xs text-red-600 dark:text-[#fca5a5] font-mono break-all">${safeMsg}</div>
                    <div class="mt-2">Use “Download Config File (.conf)” instead.</div>
                </div>
            `;
        }
    }

    setupEventListeners() {
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

    resyncAppState() {
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
            const response = await this.apiFetch('/api/system/refresh-ip');
            const data = await response.json();
            if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
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
            this.showTempMessage('Could not detect the public IP: ' + error.message, 'error');
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
        return ['I1', 'I2', 'I3', 'I4', 'I5'];
    }

    // AWG 3.x client-side params. Range-valued ('a' or 'a-b'); empty means the
    // daemon keeps its built-in WireGuard default.
    static get AWG3_CLIENT_PARAM_KEYS() {
        return [
            'ContentPaddingAddition',
            'RekeyAfterTime',
            'RekeyTimeout',
            'RejectAfterTime',
            'KeepaliveTimeout',
            'MaxHandshakeAttempts',
        ];
    }

    // AWG 3.x client-side fields. Rendered only for AWG 3.x servers, matching how
    // S3/S4 are hidden for AWG 1.5.
    autosizeClientParamTextareas(prefix, maxHeightPx = 260) {
        AmneziaApp.I_PARAM_KEYS.forEach((key) => {
            const el = document.getElementById(`${prefix}-${key}`);
            this.enableTextareaAutosize(el, maxHeightPx);
        });
    }

    loadInitialData() {
        this.loadServers();
        this.loadPublicIp();
    }

    loadPublicIp() {
        this.apiFetch('/api/system/status')
            .then(response => response.json())
            .then(data => {
                this.environment = data.environment || {};
                this.updatePublicIp(data.public_ip, data.public_ip_geo_country_code);
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

        // Rows start with the last known traffic, so a reload does not blank them.
        serversList.innerHTML = window.ServerUi.renderServersHtml({
            servers,
            escapeHtml: (v) => this.escapeHtml(v),
            renderServerClients: (serverId, clients) =>
                this.renderServerClients(serverId, clients, this.lastTrafficByServer.get(serverId) || {}),
        });
        this.renderStrip();

        // Load clients for each server
        servers.forEach(server => {
            this.loadServerClients(server.id);
        });
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
            const clients = this.serverClients.get(server.id) || server.clients || [];
            const traffic = this.lastTrafficByServer.get(server.id) || {};
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

    loadServerClients(serverId) {
        Promise.all([
            this.apiFetch(`/api/servers/${serverId}/clients`).then(res => res.json()),
            this.apiFetch(`/api/servers/${serverId}/traffic`).then(res => res.ok ? res.json() : {})
        ]).then(([clients, traffic]) => {
            this.serverClients.set(serverId, Array.isArray(clients) ? clients : []);
            const trafficObj = (traffic && typeof traffic === 'object') ? traffic : {};
            // Initial load: store snapshot but do not flash.
            this.lastTrafficByServer.set(serverId, trafficObj);
            const clientsContainer = this.getElement(`clients-${serverId}`);
            if (clientsContainer) {
                clientsContainer.innerHTML = this.renderServerClients(serverId, this.serverClients.get(serverId), trafficObj);
            }
            this.renderStrip();
        }).catch(error => {
            console.error(`Error loading clients or traffic for server ${serverId}:`, error);
        });
    }

    updateServerTraffic(serverId, traffic) {
        // Update traffic without full reload - only if clients are already loaded
        const clients = this.serverClients.get(serverId);
        if (!clients) return;

        const nextTraffic = (traffic && typeof traffic === 'object') ? traffic : {};
        const prevTraffic = this.lastTrafficByServer.get(serverId) || {};

        // Decorate traffic entries with change flags so the UI can flash rx/tx updates.
        const decoratedTraffic = {};
        for (const [clientId, info] of Object.entries(nextTraffic)) {
            const prev = prevTraffic[clientId] || {};
            const received = info?.received;
            const sent = info?.sent;
            decoratedTraffic[clientId] = {
                ...(info || {}),
                _rx_changed: typeof received !== 'undefined' && received !== prev.received,
                _tx_changed: typeof sent !== 'undefined' && sent !== prev.sent,
            };
        }

        this.lastTrafficByServer.set(serverId, nextTraffic);

        const clientsContainer = this.getElement(`clients-${serverId}`);
        // Leave a row alone while its name is being edited in place.
        if (clientsContainer && !clientsContainer.querySelector('input[aria-label="New client name"]')) {
            clientsContainer.innerHTML = this.renderServerClients(serverId, clients, decoratedTraffic);
        }
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
        const client = (this.serverClients.get(serverId) || []).find(c => c.id === clientId);
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

    async downloadClientConfig(serverId, clientId) {
        try {
            const url = `/api/servers/${serverId}/clients/${clientId}/config`;
            const resp = await this.apiFetch(url);
            if (!resp.ok) {
                let msg = `HTTP ${resp.status}`;
                try {
                    const err = await resp.json();
                    msg = err?.error || msg;
                } catch (_) {
                    // ignore
                }
                throw new Error(msg);
            }

            const text = await resp.text();
            const w = window.open('', '_blank');
            if (w) {
                w.document.title = `client-${clientId}.conf`;
                w.document.body.innerHTML = `<pre style="white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace; padding: 16px;">${this.escapeHtml(text)}</pre>`;
            } else {
                const blob = new Blob([text], { type: 'text/plain' });
                const blobUrl = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = blobUrl;
                a.download = `client-${clientId}.conf`;
                document.body.appendChild(a);
                a.click();
                a.remove();
                setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
            }
        } catch (error) {
            console.error('Error downloading client config:', error);
            this.showTempMessage('Error downloading client config: ' + error.message, 'error');
        }
    }

    showRawServerConfig(serverId) {
        this.apiFetch(`/api/servers/${serverId}/config`)
            .then(response => response.json())
            .then(config => {
                this.displayRawConfigModal(config);
            })
            .catch(error => {
                console.error('Error fetching server config:', error);
                this.showTempMessage('Error loading server configuration: ' + error.message, 'error');
            });
    }

    downloadServerConfig(serverId) {
        this.downloadBlob(`/api/servers/${serverId}/config/download`, `server-${serverId}.conf`)
            .catch((error) => {
                console.error('Error downloading server config:', error);
                this.showTempMessage('Error downloading server config: ' + error.message, 'error');
            });
    }

    closeModal() {
        const existingModal = document.getElementById('configModal') || document.getElementById('rawConfigModal');
        if (existingModal) existingModal.remove();

        const logsModal = document.getElementById('logsModal');
        if (logsModal) logsModal.remove();

        if (this.logPoller) {
            clearInterval(this.logPoller);
            this.logPoller = null;
        }
    }

    closeQRModal() {
        const existingModal = document.getElementById('qrModal');
        if (existingModal) {
            existingModal.remove();
        }
    }

    async fetchAndGenerateQRCode(serverId, clientId) {
        try {
            this.qrServerId = serverId;
            this.qrClientId = clientId;
            
            // Use the efficient endpoint that returns both versions
            const response = await this.apiFetch(`/api/servers/${serverId}/clients/${clientId}/config-both`);
            if (!response.ok) {
                throw new Error('Failed to fetch config');
            }
            
            const data = await response.json();
            this.currentCleanConfig = data.clean_config;
            this.currentFullConfig = data.full_config;
            this.currentClientName = data.client_name;
            
            // Display full config text
            const configTextEl = document.getElementById('configText');
            if (configTextEl) {
                configTextEl.textContent = this.currentFullConfig;
            }
            
            // Generate QR code from full config
            const qrContainer = document.getElementById('qrcode');
            if (qrContainer) {
                this.generateQrIntoContainer(qrContainer, this.currentFullConfig);
            }
        } catch (error) {
            console.error('Error fetching config for QR code:', error);
            this.showTempMessage('Failed to fetch/generate QR code: ' + error.message, 'error');
            const qrContainer = document.getElementById('qrcode');
            if (qrContainer) {
                const safeMsg = this.escapeHtml(error?.message || String(error));
                qrContainer.innerHTML = `
                    <div class="text-sm text-red-700 bg-red-50 dark:bg-[#7f1d1d] border border-red-200 rounded-lg p-3">
                        <div class="font-semibold mb-1">Failed to load configuration for QR</div>
                        <div class="text-xs text-red-600 dark:text-[#fca5a5] font-mono break-all">${safeMsg}</div>
                    </div>
                `;
            }
        }
    }

    updateConfigTypeLabel() {
        const configTypeLabel = document.getElementById('configType');
        if (configTypeLabel) {
            configTypeLabel.textContent = this.currentConfigType === 'clean' ? 'Clean Config' : 'Full Config';
        }
    }

    toggleConfigView() {
        const configTextArea = document.getElementById('configText');
        const qrContainer = document.getElementById('qrcode');
        
        if (this.currentConfigType === 'clean') {
            // Switch to full config
            configTextArea.value = this.currentFullConfig;
            this.currentConfigType = 'full';
        } else {
            // Switch to clean config
            configTextArea.value = this.currentCleanConfig;
            this.currentConfigType = 'clean';
        }
        
        this.updateConfigTypeLabel();

        // Keep QR aligned with what the user sees.
        if (qrContainer) {
            const text = this.currentConfigType === 'clean' ? this.currentCleanConfig : this.currentFullConfig;
            this.generateQrIntoContainer(qrContainer, text);
        }
    }

    downloadQRCode() {
        const qrContainer = document.getElementById('qrcode');
        if (!qrContainer) return;
        
        const canvas = qrContainer.querySelector('canvas');
        if (!canvas) return;
        
        // Create a temporary link to download the canvas as PNG
        const link = document.createElement('a');
        link.download = `${this.currentClientName.replace(/[^a-z0-9]/gi, '_')}_qr_code.png`;
        link.href = canvas.toDataURL('image/png');
        link.click();
    }

    copyConfigText() {
        const configTextEl = document.getElementById('configText');
        if (configTextEl) {
            const text = configTextEl.textContent || '';
            navigator.clipboard.writeText(text).then(() => {
                this.showTempMessage('Configuration copied to clipboard!', 'success');
            }).catch(() => {
                // Fallback: use a temporary textarea
                const tmp = document.createElement('textarea');
                tmp.value = text;
                document.body.appendChild(tmp);
                tmp.select();
                document.execCommand('copy');
                tmp.remove();
                this.showTempMessage('Configuration copied to clipboard!', 'success');
            });
        }
    }

    copyToClipboard(text) {
        // Decode base64 text if it's the JSON data
        try {
            const decodedText = atob(text);
            const jsonData = JSON.parse(decodedText);
            text = jsonData.config_content || decodedText;
        } catch (e) {
            // If it's not base64 JSON, use the text as is
        }

        navigator.clipboard.writeText(text).then(() => {
            // Show a temporary notification
            this.showTempMessage('Configuration copied to clipboard!', 'success');
        }).catch(err => {
            console.error('Failed to copy: ', err);
            this.showTempMessage('Failed to copy to clipboard', 'error');
        });
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