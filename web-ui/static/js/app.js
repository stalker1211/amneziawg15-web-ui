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
        this.serverTransportModalState = null;
        this.clientParamsModalState = null;
        this.serverNetworkingModalState = null;
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
    // Used for the AWG 3.0 HeaderProtectionKey, which is a plain symmetric key.
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

    countryCodeToFlagEmoji(countryCode) {
        const code = String(countryCode || '').trim().toUpperCase();
        if (!/^[A-Z]{2}$/.test(code)) return '';
        const A = 0x1F1E6;
        const first = A + (code.charCodeAt(0) - 65);
        const second = A + (code.charCodeAt(1) - 65);
        return String.fromCodePoint(first, second);
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

    collectServerTransportFormState(serverId, fallbackProtocol = window.Protocols.DEFAULT) {
        const toOptionalInt = (raw) => {
            const s = String(raw ?? '').trim();
            if (!s) return null;
            const n = parseInt(s, 10);
            return Number.isFinite(n) ? n : null;
        };

        return {
            protocol: document.getElementById(`serverProtocol-${serverId}`)?.value || fallbackProtocol,
            S1: toOptionalInt(document.getElementById(`serverTransportParam-${serverId}-S1`)?.value),
            S2: toOptionalInt(document.getElementById(`serverTransportParam-${serverId}-S2`)?.value),
            S3: toOptionalInt(document.getElementById(`serverTransportParam-${serverId}-S3`)?.value),
            S4: toOptionalInt(document.getElementById(`serverTransportParam-${serverId}-S4`)?.value),
            H1: (document.getElementById(`serverTransportParam-${serverId}-H1`)?.value || '').trim(),
            H2: (document.getElementById(`serverTransportParam-${serverId}-H2`)?.value || '').trim(),
            H3: (document.getElementById(`serverTransportParam-${serverId}-H3`)?.value || '').trim(),
            H4: (document.getElementById(`serverTransportParam-${serverId}-H4`)?.value || '').trim(),
            HeaderProtectionKey: (document.getElementById(`serverTransportParam-${serverId}-HeaderProtectionKey`)?.value || '').trim(),
        };
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

        return parts.length > 0 ? parts.join(', ') : 'No transport parameters set';
    }

    updateServerConfigPrimaryAction(serverId) {
        const button = document.getElementById(`serverConfigPrimaryAction-${serverId}`);
        if (!button || !this.serverTransportModalState || String(this.serverTransportModalState.serverId) !== String(serverId)) {
            return;
        }

        const currentState = this.collectServerTransportFormState(serverId, this.serverTransportModalState.initial.protocol);
        const isDirty = JSON.stringify(currentState) !== JSON.stringify(this.serverTransportModalState.initial);

        if (isDirty) {
            button.textContent = 'Update & Restart Server';
            button.className = 'btn-pill bg-red-600 text-white px-4 py-2 rounded text-sm hover:bg-red-700';
            button.onclick = () => this.saveServerTransportParams(serverId);
        } else {
            button.textContent = 'Close';
            button.className = 'btn-pill bg-gray-500 text-white px-4 py-2 rounded text-sm hover:bg-gray-600';
            button.onclick = () => this.closeModal();
        }
    }

    setupServerTransportDirtyTracking(serverId, fallbackProtocol = window.Protocols.DEFAULT) {
        const initial = this.collectServerTransportFormState(serverId, fallbackProtocol);
        this.serverTransportModalState = { serverId: String(serverId), initial };

        const fieldIds = [
            `serverProtocol-${serverId}`,
            `serverTransportParam-${serverId}-S1`,
            `serverTransportParam-${serverId}-S2`,
            `serverTransportParam-${serverId}-S3`,
            `serverTransportParam-${serverId}-S4`,
            `serverTransportParam-${serverId}-H1`,
            `serverTransportParam-${serverId}-H2`,
            `serverTransportParam-${serverId}-H3`,
            `serverTransportParam-${serverId}-H4`,
            `serverTransportParam-${serverId}-HeaderProtectionKey`,
        ];

        fieldIds.forEach((id) => {
            const element = document.getElementById(id);
            if (!element) return;
            element.addEventListener('input', () => this.updateServerConfigPrimaryAction(serverId));
            element.addEventListener('change', () => this.updateServerConfigPrimaryAction(serverId));
        });

        this.updateServerConfigPrimaryAction(serverId);
    }

    updateClientConfigPrimaryAction(clientId) {
        const button = document.getElementById(`clientConfigPrimaryAction-${clientId}`);
        if (!button || !this.clientParamsModalState || String(this.clientParamsModalState.clientId) !== String(clientId)) {
            return;
        }

        const currentState = this.collectClientParamsFormState(`clientParam-${clientId}`);
        const isDirty = JSON.stringify(currentState) !== JSON.stringify(this.clientParamsModalState.initial);

        if (isDirty) {
            button.textContent = 'Update Client Config';
            button.className = 'btn-pill bg-red-600 text-white px-4 py-2 rounded text-sm hover:bg-red-700';
            button.onclick = () => this.saveClientParams(this.clientParamsModalState.serverId, clientId);
        } else {
            button.textContent = 'Close';
            button.className = 'btn-pill bg-gray-500 text-white px-4 py-2 rounded text-sm hover:bg-gray-600';
            button.onclick = () => this.closeModal();
        }
    }

    setupClientParamsDirtyTracking(serverId, clientId) {
        const initial = this.collectClientParamsFormState(`clientParam-${clientId}`);
        this.clientParamsModalState = { serverId: String(serverId), clientId: String(clientId), initial };

        const trackedKeys = ['Jc', 'Jmin', 'Jmax'].concat(AmneziaApp.I_PARAM_KEYS)
            .concat(AmneziaApp.AWG3_CLIENT_PARAM_KEYS);
        trackedKeys.forEach((key) => {
            const element = document.getElementById(`clientParam-${clientId}-${key}`);
            if (!element) return;
            element.addEventListener('input', () => this.updateClientConfigPrimaryAction(clientId));
            element.addEventListener('change', () => this.updateClientConfigPrimaryAction(clientId));
        });

        this.updateClientConfigPrimaryAction(clientId);
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
                <div class="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg p-3">
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
                <div class="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg p-3">
                    <div class="font-semibold mb-1">QR code could not be generated</div>
                    <div class="mb-2">Most commonly this happens when the config is too large for a QR code (payload: <span class=\"font-mono\">${bytes}</span> bytes).</div>
                    <div class="text-xs text-red-600 font-mono break-all">${safeMsg}</div>
                    <div class="mt-2">Use “Download Config File (.conf)” instead.</div>
                </div>
            `;
        }
    }

    setupEventListeners() {
        // Create server modal dialog
        const showCreateServerBtn = this.getElement('showCreateServerBtn');
        if (showCreateServerBtn) {
            showCreateServerBtn.addEventListener('click', () => {
                this.openCreateServerModal();
            });
        }

        // ESC closes create-server modal
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                this.closeCreateServerModal();
            }
        });

        // Server form submission
        const serverForm = this.getElement('serverForm');
        if (serverForm) {
            serverForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.createServer();
            });
        }

        const themeToggleBtn = this.getElement('themeToggleBtn');
        if (themeToggleBtn) {
            themeToggleBtn.addEventListener('click', () => {
                this.toggleTheme();
            });
        }

        // Random parameters button
        const randomParamsBtn = this.getElement('randomParamsBtn');
        if (randomParamsBtn) {
            randomParamsBtn.addEventListener('click', () => {
                this.generateRandomParams();
            });
        }

        // Refresh IP button
        const refreshIpBtn = this.getElement('refreshIpBtn');
        if (refreshIpBtn) {
            refreshIpBtn.addEventListener('click', () => {
                this.refreshPublicIp();
            });
        }

        const protocolSelect = this.getElement('serverProtocol');
        if (protocolSelect) {
            // The create form's <select> is left empty in index.html and filled here,
            // so protocols.js is the only place the list is defined.
            protocolSelect.innerHTML = window.Protocols.optionsHtml(window.Protocols.DEFAULT);
            protocolSelect.addEventListener('change', (e) => {
                this.toggleProtocolFields(e.target.value, 'param');
            });
            this.toggleProtocolFields(protocolSelect.value, 'param');
        }

        const genHeaderKeyBtn = this.getElement('genHeaderProtectionKeyBtn');
        if (genHeaderKeyBtn) {
            genHeaderKeyBtn.addEventListener('click', () => {
                const target = this.getElement('paramHeaderProtectionKey');
                if (target) target.value = this.generateBase64Key();
            });
        }

        // Form validation listeners
        this.setupFormValidation();
    }

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
        btn.textContent = isDark ? '☀️' : '🌙';
        btn.title = isDark ? 'Switch to light theme' : 'Switch to dark theme';
        btn.classList.toggle('bg-gray-100', !isDark);
        btn.classList.toggle('bg-gray-800', isDark);
        btn.classList.toggle('text-gray-700', !isDark);
        btn.classList.toggle('text-gray-200', isDark);
        btn.classList.toggle('hover:bg-gray-200', !isDark);
        btn.classList.toggle('hover:bg-gray-700', isDark);
    }

    openCreateServerModal() {
        const modal = this.getElement('createServerModal');
        if (!modal) return;
        modal.classList.remove('hidden');

        // Auto-propose next free port starting from 51820
        const usedPorts = new Set((this.lastServers || []).map(s => Number(s.port)));
        let nextPort = 51820;
        while (usedPorts.has(nextPort)) nextPort++;
        const portEl = this.getElement('serverPort');
        if (portEl) portEl.value = nextPort;

        // Auto-propose next free subnet from 10.10.X.0/24
        const usedThirdOctets = new Set(
            (this.lastServers || [])
                .map(s => s.subnet || '')
                .map(sub => {
                    const m = sub.match(/^10\.10\.(\d+)\./);
                    return m ? Number(m[1]) : null;
                })
                .filter(v => v !== null)
        );
        let thirdOctet = 0;
        while (usedThirdOctets.has(thirdOctet) && thirdOctet < 255) thirdOctet++;
        const subnetEl = this.getElement('serverSubnet');
        if (subnetEl) subnetEl.value = `10.10.${thirdOctet}.0/24`;

        const nameElement = this.getElement('serverName');
        if (nameElement) nameElement.focus();
    }

    closeCreateServerModal() {
        const modal = this.getElement('createServerModal');
        if (!modal) return;
        modal.classList.add('hidden');
    }

    hideError(errorId) {
        const errorElement = this.getElement(errorId);
        if (errorElement) {
            errorElement.classList.add('hidden');
        }
    }

    // Collapse long help text so dialogs stay short enough to show their fields
    // and action buttons without scrolling. Closed by default.
    updateTransportDescription(protocol, prefix = 'param') {
        let descriptionId = 'transportParameterDescription';
        if (prefix !== 'param') {
            const serverId = String(prefix).replace(/^serverTransportParam-/, '').replace(/-$/, '');
            descriptionId = `serverTransportDescription-${serverId}`;
        }

        const description = document.getElementById(descriptionId);
        if (!description) return;
        description.innerHTML = this.getTransportDescriptionHtml(protocol, prefix === 'param' ? 'create' : 'modal');
    }

    collectServerNetworkingFormState(serverId) {
        return {
            enable_nat: !!document.getElementById(`serverEnableNat-${serverId}`)?.checked,
            block_lan_cidrs: !!document.getElementById(`serverBlockLan-${serverId}`)?.checked,
        };
    }

    updateServerNetworkingPrimaryAction(serverId) {
        const button = document.getElementById(`serverNetworkingPrimaryAction-${serverId}`);
        if (!button || !this.serverNetworkingModalState || String(this.serverNetworkingModalState.serverId) !== String(serverId)) {
            return;
        }

        const currentState = this.collectServerNetworkingFormState(serverId);
        const isDirty = JSON.stringify(currentState) !== JSON.stringify(this.serverNetworkingModalState.initial);

        if (isDirty) {
            button.textContent = 'Update Network Settings';
            button.className = 'btn-pill bg-red-600 text-white px-3 py-1 rounded text-xs hover:bg-red-700';
            button.onclick = () => this.saveServerNetworking(serverId);
        } else {
            button.textContent = '';
            button.className = 'hidden';
            button.onclick = null;
        }
    }

    setupServerNetworkingDirtyTracking(serverId) {
        this.serverNetworkingModalState = {
            serverId: String(serverId),
            initial: this.collectServerNetworkingFormState(serverId),
        };

        [`serverEnableNat-${serverId}`, `serverBlockLan-${serverId}`].forEach((id) => {
            const element = document.getElementById(id);
            if (!element) return;
            element.addEventListener('change', () => this.updateServerNetworkingPrimaryAction(serverId));
            element.addEventListener('input', () => this.updateServerNetworkingPrimaryAction(serverId));
        });

        this.updateServerNetworkingPrimaryAction(serverId);
    }

    toggleProtocolFields(protocol, prefix = 'param') {
        const allowS34 = window.Protocols.supportsS34(protocol);
        const allowRanges = window.Protocols.supportsHeaderRanges(protocol);
        const allowAwg3 = window.Protocols.supportsAwg3(protocol);

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

        // AWG 3.0 header protection. Clearing the key on downgrade keeps the
        // submitted payload consistent with the protocol the user picked.
        const keyElement = document.getElementById(`${prefix}HeaderProtectionKey`);
        if (keyElement) {
            keyElement.disabled = !allowAwg3;
            if (!allowAwg3) keyElement.value = '';
            const row = document.getElementById(`${prefix}HeaderProtectionKeyRow`)
                || keyElement.closest('label, div');
            if (row) row.style.display = allowAwg3 ? '' : 'none';
        }

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

    updateStatus(message, isConnected = null) {
        const statusElement = this.getElement('status');
        if (statusElement) {
            statusElement.textContent = message;
        }

        const dot = this.getElement('statusDot');
        if (dot && typeof isConnected === 'boolean') {
            dot.classList.toggle('bg-green-500', isConnected);
            dot.classList.toggle('bg-red-500', !isConnected);
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
            const flag = this.countryCodeToFlagEmoji(this.currentPublicIpCountryCode);
            publicIpElement.textContent = flag ? `${flag} ${nextIp}` : nextIp;
        }
    }

    refreshPublicIp() {
        this.apiFetch('/api/system/refresh-ip')
            .then(response => response.json())
            .then(data => {
                this.updatePublicIp(data.public_ip, data.public_ip_geo_country_code);
                this.loadServers();
            })
            .catch(error => {
                console.error('Error refreshing IP:', error);
            });
    }

    formatProbeTimestamp(epochSeconds) {
        const ts = Number(epochSeconds);
        if (!Number.isFinite(ts) || ts <= 0) return '';
        try {
            return new Date(ts * 1000).toLocaleString();
        } catch (_) {
            return '';
        }
    }

    probeServerEgressIp(serverId, buttonElement = null) {
        const serverCard = buttonElement ? buttonElement.closest('.server-card') : null;
        if (buttonElement) {
            buttonElement.disabled = true;
            buttonElement.classList.add('opacity-60');
        }
        if (serverCard) {
            serverCard.classList.add('egress-probe-updating');
        }

        this.apiFetch(`/api/servers/${serverId}/egress-ip`, { method: 'POST' })
            .then(async (response) => {
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(data?.error || `HTTP ${response.status}`);
                }
                this.loadServers();
            })
            .catch((error) => {
                console.error('Error probing server egress IP:', error);
                alert('Error probing egress IP: ' + error.message);
            })
            .finally(() => {
                if (buttonElement) {
                    buttonElement.disabled = false;
                    buttonElement.classList.remove('opacity-60');
                }
                if (serverCard) {
                    serverCard.classList.remove('egress-probe-updating');
                }
            });
    }

    generateRandomParams() {
        const protocol = this.getElement('serverProtocol')?.value || window.Protocols.DEFAULT;
        const s1Element = this.getElement('paramS1');
        const s2Element = this.getElement('paramS2');
        const s3Element = this.getElement('paramS3');
        const s4Element = this.getElement('paramS4');
        const h1Element = this.getElement('paramH1');
        const h2Element = this.getElement('paramH2');
        const h3Element = this.getElement('paramH3');
        const h4Element = this.getElement('paramH4');
        
        if (s1Element) s1Element.value = Math.floor(Math.random() * 136) + 15;
        if (s2Element) s2Element.value = Math.floor(Math.random() * 136) + 15;
        if (window.Protocols.supportsS34(protocol)) {
            if (s3Element) s3Element.value = Math.floor(Math.random() * 136) + 15;
            // With header protection the daemon slices a 12-byte nonce out of the
            // padding, so S4 cannot go below 12 on AWG 3.0.
            const s4Low = window.Protocols.supportsAwg3(protocol) ? 12 : 0;
            if (s4Element) s4Element.value = Math.floor(Math.random() * (33 - s4Low)) + s4Low;
        } else {
            if (s3Element) s3Element.value = '';
            if (s4Element) s4Element.value = '';
        }

        if (window.Protocols.supportsAwg3(protocol)) {
            const keyElement = this.getElement('paramHeaderProtectionKey');
            if (keyElement && !keyElement.value.trim()) {
                keyElement.value = this.generateBase64Key();
            }
        }
        
        // Generate unique H values
        const hValues = new Set();
        while (hValues.size < 4) {
            hValues.add(Math.floor(Math.random() * 1000000) + 1000);
        }
        const hArray = Array.from(hValues);
        
        if (h1Element) h1Element.value = hArray[0];
        if (h2Element) h2Element.value = hArray[1];
        if (h3Element) h3Element.value = hArray[2];
        if (h4Element) h4Element.value = hArray[3];
    }

    showFormStatus(message, type) {
        const statusDiv = this.getElement('formStatus');
        if (statusDiv) {
            statusDiv.textContent = message;
            statusDiv.className = `text-sm mt-2 ${type === 'success' ? 'text-green-600' : 'text-red-600'}`;
            statusDiv.classList.remove('hidden');
            
            setTimeout(() => {
                statusDiv.classList.add('hidden');
            }, 5000);
        }
    }

    parseHeaderValueJS(value, protocol) {
        const raw = String(value ?? '').trim();
        if (!raw) {
            return { error: 'Header value cannot be empty' };
        }

        const supportsRanges = window.Protocols.supportsHeaderRanges(protocol);

        if (supportsRanges && /^\d+\s*-\s*\d+$/.test(raw)) {
            const [startRaw, endRaw] = raw.split('-', 2).map((part) => part.trim());
            const start = parseInt(startRaw, 10);
            const end = parseInt(endRaw, 10);
            if (start > end) {
                return { error: `Invalid range ${raw}: start must be <= end` };
            }
            return { raw: `${start}-${end}`, start, end };
        }

        if (/^\d+$/.test(raw)) {
            const number = parseInt(raw, 10);
            return { raw: String(number), start: number, end: number };
        }

        return { error: supportsRanges ? `Header value '${raw}' must be an integer or range x-y` : `Header value '${raw}' must be a single integer` };
    }

    validateTransportParamsJS(protocol, params, mtu) {
        let errors = [];
        const hasS1 = Number.isFinite(params.S1);
        const hasS2 = Number.isFinite(params.S2);
        const hasS3 = Number.isFinite(params.S3);
        const hasS4 = Number.isFinite(params.S4);

        if (hasS1 && params.S1 < 0) {
            errors.push(`S1 (${params.S1}) must be non-negative`);
        }
        if (hasS2 && params.S2 < 0) {
            errors.push(`S2 (${params.S2}) must be non-negative`);
        }
        if (hasS3 && params.S3 < 0) {
            errors.push(`S3 (${params.S3}) must be non-negative`);
        }
        if (hasS4 && params.S4 < 0) {
            errors.push(`S4 (${params.S4}) must be non-negative`);
        }
        // S1 + 56 ≠ S2 (only when both present)
        if (hasS1 && hasS2 && (params.S1 + 56 === params.S2)) {
            errors.push(`S1 + 56 (${params.S1 + 56}) must not equal S2 (${params.S2})`);
        }

        if (!window.Protocols.supportsS34(protocol) && (hasS3 || hasS4)) {
            errors.push('S3 and S4 are supported only by AWG 2.0 and AWG 3.0');
        }

        // AWG 3.0: header protection carves a 12-byte cipher nonce out of each
        // S padding, so the daemon rejects any S value below that.
        if (window.Protocols.supportsAwg3(protocol) && String(params.HeaderProtectionKey || '').trim()) {
            if (!/^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$/.test(String(params.HeaderProtectionKey).trim())) {
                errors.push('HeaderProtectionKey must be a base64-encoded 32-byte key');
            }
            [['S1', params.S1], ['S2', params.S2], ['S3', params.S3], ['S4', params.S4]].forEach(([key, value]) => {
                if (!Number.isFinite(value) || value < 12) {
                    errors.push(`${key} must be at least 12 when HeaderProtectionKey is set`);
                }
            });
        }

        const headers = ['H1', 'H2', 'H3', 'H4'].map((key) => ({ key, parsed: this.parseHeaderValueJS(params[key], protocol) }));
        headers.forEach(({ key, parsed }) => {
            if (parsed.error) {
                errors.push(`${key}: ${parsed.error}`);
            }
        });

        if (window.Protocols.supportsHeaderRanges(protocol) && !headers.some(({ parsed }) => parsed.error)) {
            for (let i = 0; i < headers.length; i += 1) {
                for (let j = i + 1; j < headers.length; j += 1) {
                    const left = headers[i].parsed;
                    const right = headers[j].parsed;
                    if (left.start <= right.end && right.start <= left.end) {
                        errors.push(`${headers[i].key} and ${headers[j].key} ranges must not intersect`);
                    }
                }
            }
        }

        return errors;
    }

    getTransportParamWarningsJS(protocol, params, mtu) {
        const warnings = [];
        const hasS1 = Number.isFinite(params.S1);
        const hasS2 = Number.isFinite(params.S2);
        const hasS3 = Number.isFinite(params.S3);
        const hasS4 = Number.isFinite(params.S4);

        if (hasS1 && (params.S1 < 15 || params.S1 > 150)) {
            warnings.push(`S1 (${params.S1}) is outside the common 15-150 range.`);
        }
        if (hasS1 && params.S1 > (mtu - 148)) {
            warnings.push(`S1 (${params.S1}) is above the previous rule-of-thumb bound MTU - 148 (${mtu - 148}). This is practical guidance, not a protocol limit.`);
        }
        if (hasS2 && (params.S2 < 15 || params.S2 > 150)) {
            warnings.push(`S2 (${params.S2}) is outside the common 15-150 range.`);
        }
        if (hasS2 && params.S2 > (mtu - 92)) {
            warnings.push(`S2 (${params.S2}) is above the previous rule-of-thumb bound MTU - 92 (${mtu - 92}). This is practical guidance, not a protocol limit.`);
        }
        const supportsS34 = window.Protocols.supportsS34(protocol);
        if (supportsS34 && hasS3 && (params.S3 < 15 || params.S3 > 150)) {
            warnings.push(`S3 (${params.S3}) is outside the common 15-150 range.`);
        }
        if (supportsS34 && hasS4 && params.S4 > 32) {
            warnings.push(`S4 (${params.S4}) is above a conservative safe range (0-32) and may cause 'message too long' errors.`);
        }

        return warnings;
    }

    validateClientParamsJS(params, mtu) {
        let errors = [];
        if (!(params.Jc > 0)) {
            errors.push(`Jc (${params.Jc}) must be positive`);
        }
        if (!(params.Jmin > 0)) {
            errors.push(`Jmin (${params.Jmin}) must be positive`);
        }
        if (!(params.Jmax > 0)) {
            errors.push(`Jmax (${params.Jmax}) must be positive`);
        }
        if (!(params.Jmin <= params.Jmax)) {
            errors.push(`Jmin (${params.Jmin}) must be less than or equal to Jmax (${params.Jmax})`);
        }

        // AWG 3.0 range params: 'a' or 'a-b', empty means protocol default.
        AmneziaApp.AWG3_CLIENT_PARAM_KEYS.forEach((key) => {
            const raw = String(params[key] ?? '').trim();
            if (!raw) return;
            const match = /^(\d+)(?:\s*-\s*(\d+))?$/.exec(raw);
            if (!match) {
                errors.push(`${key} ('${raw}') must be a number or a range like 22-30`);
                return;
            }
            const low = parseInt(match[1], 10);
            const high = match[2] === undefined ? low : parseInt(match[2], 10);
            if (high < low) {
                errors.push(`${key} range '${raw}' is inverted: start must be <= end`);
            }
        });

        return errors;
    }

    getClientParamWarningsJS(params, mtu) {
        const warnings = [];
        if (!(params.Jc >= 4 && params.Jc <= 12)) {
            warnings.push(`Jc (${params.Jc}) is outside the documented recommended range 4-12.`);
        }
        if (params.Jmax >= mtu) {
            warnings.push(`Jmax (${params.Jmax}) is at or above MTU (${mtu}) and may fragment junk packets.`);
        }
        return warnings;
    }

    // Custom signature packets I1-I5 (AWG 1.5+). Multi-line textarea fields.
    static get I_PARAM_KEYS() {
        return ['I1', 'I2', 'I3', 'I4', 'I5'];
    }

    // AWG 3.0 client-side params. Range-valued ('a' or 'a-b'); empty means the
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

    collectClientParamsFormState(prefix) {
        const state = {
            Jc: parseInt(document.getElementById(`${prefix}-Jc`)?.value || '8', 10),
            Jmin: parseInt(document.getElementById(`${prefix}-Jmin`)?.value || '8', 10),
            Jmax: parseInt(document.getElementById(`${prefix}-Jmax`)?.value || '80', 10),
            I1: document.getElementById(`${prefix}-I1`)?.value || '',
            I2: document.getElementById(`${prefix}-I2`)?.value || '',
            I3: document.getElementById(`${prefix}-I3`)?.value || '',
            I4: document.getElementById(`${prefix}-I4`)?.value || '',
            I5: document.getElementById(`${prefix}-I5`)?.value || '',
        };

        // Only present when the form was rendered for an AWG 3.0 server.
        AmneziaApp.AWG3_CLIENT_PARAM_KEYS.forEach((key) => {
            const element = document.getElementById(`${prefix}-${key}`);
            if (element) state[key] = (element.value || '').trim();
        });

        return state;
    }

    // AWG 3.0 client-side fields. Rendered only for AWG 3.0 servers, matching how
    // S3/S4 are hidden for AWG 1.5.
    autosizeClientParamTextareas(prefix, maxHeightPx = 260) {
        AmneziaApp.I_PARAM_KEYS.forEach((key) => {
            const el = document.getElementById(`${prefix}-${key}`);
            this.enableTextareaAutosize(el, maxHeightPx);
        });
    }

    populateNewClientParamsFromExisting(serverId, clientId) {
        const server = (this.lastServers || []).find((item) => String(item.id) === String(serverId));
        const defaults = server?.client_defaults || {};
        const existingClient = (this.serverClients.get(serverId) || []).find((client) => String(client.id) === String(clientId));
        const params = existingClient?.client_params || defaults;

        const values = {
            Jc: params.Jc ?? defaults.Jc ?? 8,
            Jmin: params.Jmin ?? defaults.Jmin ?? 8,
            Jmax: params.Jmax ?? defaults.Jmax ?? 80,
            I1: params.I1 ?? defaults.I1 ?? '',
            I2: params.I2 ?? defaults.I2 ?? '',
            I3: params.I3 ?? defaults.I3 ?? '',
            I4: params.I4 ?? defaults.I4 ?? '',
            I5: params.I5 ?? defaults.I5 ?? '',
        };

        // Copy AWG 3.0 params too, when the form has those fields.
        AmneziaApp.AWG3_CLIENT_PARAM_KEYS.forEach((key) => {
            values[key] = params[key] ?? defaults[key] ?? '';
        });

        Object.entries(values).forEach(([key, value]) => {
            const el = document.getElementById(`newClientParam-${serverId}-${key}`);
            if (el) {
                el.value = value;
                if (el.tagName === 'TEXTAREA') {
                    this.autosizeTextarea(el, 200);
                }
            }
        });
    }

    validateForm() {
        let isValid = true;

        // Reset errors
        this.hideError('nameError');
        this.hideError('portError');
        this.hideError('subnetError');
        this.hideError('mtuError');
        this.hideError('dnsError');

        // Validate name
        const nameElement = this.getElement('serverName');
        const name = nameElement ? nameElement.value.trim() : '';
        if (!name) {
            this.showError('nameError', 'Server name is required');
            isValid = false;
        }

        // Validate port
        const portElement = this.getElement('serverPort');
        const port = portElement ? parseInt(portElement.value) : 0;
        if (!port || port < 1 || port > 65535) {
            this.showError('portError', 'Port must be between 1 and 65535');
            isValid = false;
        }

        // Validate subnet
        const subnetElement = this.getElement('serverSubnet');
        const subnet = subnetElement ? subnetElement.value : '';
        const subnetRegex = /^(\d{1,3}\.){3}\d{1,3}\/\d{1,2}$/;
        if (!subnet || !subnetRegex.test(subnet)) {
            this.showError('subnetError', 'Valid subnet is required (e.g., 10.0.0.0/24)');
            isValid = false;
        }

        // Validate MTU
        const mtuElement = this.getElement('serverMTU');
        const mtu = mtuElement ? parseInt(mtuElement.value) : 0;
        if (!mtu || mtu < 1280 || mtu > 1440) {
            this.showError('mtuError', 'MTU must be between 1280 and 1440');
            isValid = false;
        }

        // Validate DNS
        const dnsElement = this.getElement('serverDNS');
        const dns = dnsElement ? dnsElement.value.trim() : '';
        const dnsServers = dns.split(',').map(s => s.trim()).filter(s => s);
        const ipRegex = /^(\d{1,3}\.){3}\d{1,3}$/;

        if (!dns || dnsServers.length === 0) {
            this.showError('dnsError', 'At least one DNS server is required');
            isValid = false;
        } else {
            for (const dnsServer of dnsServers) {
                if (!ipRegex.test(dnsServer)) {
                    this.showError('dnsError', `Invalid DNS server IP: ${dnsServer}`);
                    isValid = false;
                    break;
                }
            }
        }

        return isValid;
    }

    // Add DNS input validation listener
    setupFormValidation() {
        const nameElement = this.getElement('serverName');
        const portElement = this.getElement('serverPort');
        const subnetElement = this.getElement('serverSubnet');
        const mtuElement = this.getElement('serverMTU');
        const dnsElement = this.getElement('serverDNS');

        if (nameElement) {
            nameElement.addEventListener('input', () => {
                this.hideError('nameError');
            });
        }

        if (portElement) {
            portElement.addEventListener('input', () => {
                this.hideError('portError');
            });
        }

        if (subnetElement) {
            subnetElement.addEventListener('input', () => {
                this.hideError('subnetError');
            });
        }

        if (mtuElement) {
            mtuElement.addEventListener('input', () => {
                this.hideError('mtuError');
            });
        }

        if (dnsElement) {
            dnsElement.addEventListener('input', () => {
                this.hideError('dnsError');
            });
        }
    }

    showError(errorId, message) {
        const errorElement = this.getElement(errorId);
        if (errorElement) {
            errorElement.textContent = message;
            errorElement.classList.remove('hidden');
        }
    }

    createServer() {
        console.log("Creating server...");

        if (!this.validateForm()) {
            console.log("Form validation failed");
            this.showFormStatus('Please fix the form errors above', 'error');
            return;
        }

        // Safely get form values with fallbacks
        const nameElement = this.getElement('serverName');
        const portElement = this.getElement('serverPort');
        const subnetElement = this.getElement('serverSubnet');
        const mtuElement = this.getElement('serverMTU');
        const dnsElement = this.getElement('serverDNS');
        const protocolElement = this.getElement('serverProtocol');
        const autoStartElement = this.getElement('autoStart');
        const enableNatElement = this.getElement('enableNat');
        const blockLanElement = this.getElement('blockLanCidrs');

        const formData = {
            name: nameElement ? nameElement.value.trim() : 'New Server',
            port: portElement ? parseInt(portElement.value) : 51820,
            subnet: subnetElement ? subnetElement.value : '10.0.0.0/24',
            mtu: mtuElement ? parseInt(mtuElement.value) : 1420,
            dns: dnsElement ? dnsElement.value.trim() : '8.8.8.8,1.1.1.1',
            protocol: protocolElement ? protocolElement.value : window.Protocols.DEFAULT,
            auto_start: autoStartElement ? autoStartElement.checked : true,
            enable_nat: enableNatElement ? enableNatElement.checked : true,
            block_lan_cidrs: blockLanElement ? blockLanElement.checked : true
        };

        console.log("Form data:", formData);

        const toOptionalInt = (raw) => {
            const s = String(raw ?? '').trim();
            if (!s) return null;
            const n = parseInt(s, 10);
            return Number.isFinite(n) ? n : null;
        };

        formData.transport_params = {
            S1: toOptionalInt(this.getElement('paramS1')?.value),
            S2: toOptionalInt(this.getElement('paramS2')?.value),
            S3: toOptionalInt(this.getElement('paramS3')?.value),
            S4: toOptionalInt(this.getElement('paramS4')?.value),
            H1: (this.getElement('paramH1')?.value || '').trim(),
            H2: (this.getElement('paramH2')?.value || '').trim(),
            H3: (this.getElement('paramH3')?.value || '').trim(),
            H4: (this.getElement('paramH4')?.value || '').trim(),
        };

        if (window.Protocols.supportsAwg3(formData.protocol)) {
            formData.transport_params.HeaderProtectionKey =
                (this.getElement('paramHeaderProtectionKey')?.value || '').trim();
        }

        const transportErrors = this.validateTransportParamsJS(formData.protocol, formData.transport_params, formData.mtu);
        if (transportErrors.length > 0) {
            this.showError('obfuscationError', transportErrors.join(' '));
            return;
        } else {
            this.hideError('obfuscationError');
        }

        const transportWarnings = this.getTransportParamWarningsJS(formData.protocol, formData.transport_params, formData.mtu);
        if (transportWarnings.length > 0) {
            const proceed = confirm(`Transport parameter warnings:\n\n- ${transportWarnings.join('\n- ')}\n\nCreate server anyway?`);
            if (!proceed) {
                return;
            }
        }

        // Warn if port/subnet already used by any existing server
        const conflicts = this.getServerConflicts(formData.port, formData.subnet);
        if (conflicts.length > 0) {
            const details = conflicts.map(c => {
                const parts = [];
                if (c.portConflict) parts.push(`port ${c.port}`);
                if (c.subnetConflict) parts.push(`subnet ${c.subnet}`);
                return `- ${c.name} (${c.id}, ${c.status || 'unknown'}): ${parts.join(' & ')}`;
            }).join('\n');

            const msg = `An existing server already uses the same port or subnet:\n\n${details}\n\nCreate anyway?`;
            if (!confirm(msg)) {
                return;
            }
        }

        // Disable button and show loading
        this.setCreateButtonState(true);

        this.apiFetch('/api/servers', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(formData)
        })
        .then(response => {
            console.log("Response received:", response.status);
            if (!response.ok) {
                return response.json().then(err => {
                    throw new Error(err.error || `HTTP ${response.status}`);
                });
            }
            return response.json();
        })
        .then(server => {
            console.log("Server created successfully:", server);
            this.showFormStatus(`Server "${server.name}" created successfully!`, 'success');

            // Reset form
            const serverForm = this.getElement('serverForm');
            if (serverForm) serverForm.reset();

            // Close the modal after success
            this.closeCreateServerModal();

            this.loadServers();
        })
        .catch(error => {
            console.error('Error creating server:', error);
            this.showFormStatus('Error creating server: ' + error.message, 'error');
        })
        .finally(() => {
            // Re-enable button
            this.setCreateButtonState(false);
        });
    }

    setCreateButtonState(loading) {
        const createButton = this.getElement('createButton');
        if (createButton) {
            createButton.disabled = loading;
            createButton.textContent = loading ? 'Creating...' : 'Create Server';
            createButton.classList.toggle('opacity-50', loading);
        }
    }

    loadInitialData() {
        this.loadServers();
        this.loadPublicIp();
    }

    loadPublicIp() {
        this.apiFetch('/api/system/status')
            .then(response => response.json())
            .then(data => {
                this.updatePublicIp(data.public_ip, data.public_ip_geo_country_code);
            })
            .catch(error => {
                console.error('Error loading public IP:', error);
            });
    }

    loadServers() {
        this.apiFetch('/api/servers')
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

    getServerConflicts(port, subnet) {
        const normPort = Number(port);
        const normSubnet = String(subnet || '').trim();
        const servers = Array.isArray(this.lastServers) ? this.lastServers : [];

        return servers
            .filter(s => Number(s.port) === normPort || String(s.subnet || '').trim() === normSubnet)
            .map(s => ({
                id: s.id,
                name: s.name,
                port: s.port,
                subnet: s.subnet,
                status: s.status,
                portConflict: Number(s.port) === normPort,
                subnetConflict: String(s.subnet || '').trim() === normSubnet,
            }));
    }

    renderServers(servers) {
        const serversList = this.getElement('serversList');
        if (!serversList) return;

        serversList.innerHTML = window.ServerUi.renderServersHtml({
            servers,
            escapeHtml: (v) => this.escapeHtml(v),
            formatProbeTimestamp: (ts) => this.formatProbeTimestamp(ts),
            renderServerClients: (serverId, clients) => this.renderServerClients(serverId, clients),
        });

        // Load clients for each server
        servers.forEach(server => {
            this.loadServerClients(server.id);
        });
    }

    renderServerClients(serverId, clients, traffic = {}) {
        return window.ServerUi.renderServerClientsHtml({
            serverId,
            clients,
            traffic,
            escapeHtml: (v) => this.escapeHtml(v),
            isClientActiveFromTraffic: (clientTraffic) => this.isClientActiveFromTraffic(clientTraffic),
            countryCodeToFlagEmoji: (countryCode) => this.countryCodeToFlagEmoji(countryCode),
        });
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
                clientsContainer.innerHTML = this.renderServerClients(serverId, clients, trafficObj);
            }
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
        if (clientsContainer) {
            clientsContainer.innerHTML = this.renderServerClients(serverId, clients, decoratedTraffic);
        }
    }

    showServerError(message) {
        const serversList = this.getElement('serversList');
        if (serversList) {
            serversList.innerHTML = `
                <div class="text-center py-8 text-red-500">
                    ${message}
                </div>
            `;
        }
    }

    // Server management methods
    deleteServer(serverId) {
        if (confirm('Are you sure you want to delete this server and all its clients?')) {
            this.apiFetch(`/api/servers/${serverId}`, { method: 'DELETE' })
                .then(() => this.loadServers())
                .catch(error => {
                    console.error('Error deleting server:', error);
                    alert('Error deleting server: ' + error.message);
                });
        }
    }

    deleteClient(serverId, clientId) {
        if (confirm('Are you sure you want to delete this client?')) {
            this.apiFetch(`/api/servers/${serverId}/clients/${clientId}`, { method: 'DELETE' })
                .then(() => this.loadServers())
                .catch(error => {
                    console.error('Error deleting client:', error);
                    alert('Error deleting client: ' + error.message);
                });
        }
    }

    renameServer(serverId) {
        const server = (this.lastServers || []).find(s => s.id === serverId);
        const currentName = server ? server.name : '';
        const newName = prompt('Rename server:', currentName);
        if (newName === null || newName.trim() === '' || newName.trim() === currentName) return;
        this.apiFetch(`/api/servers/${serverId}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName.trim() })
        })
            .then(() => {
                this.loadServers();
                this.closeModal();
                // Reopen with fresh data from API
                this.showServerConfig(serverId);
            })
            .catch(error => {
                console.error('Error renaming server:', error);
                alert('Error renaming server: ' + error.message);
            });
    }

    renameClient(serverId, clientId) {
        const server = (this.lastServers || []).find(s => s.id === serverId);
        const client = server ? (server.clients || []).find(c => c.id === clientId) : null;
        const currentName = client ? client.name : '';
        const newName = prompt('Rename client:', currentName);
        if (newName === null || newName.trim() === '' || newName.trim() === currentName) return;
        this.apiFetch(`/api/servers/${serverId}/clients/${clientId}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName.trim() })
        })
            .then(() => {
                this.loadServers();
                this.closeModal();
                // Update cached client name so modal shows it immediately
                const cachedClients = this.serverClients.get(serverId);
                if (cachedClients) {
                    const c = cachedClients.find(cl => cl.id === clientId);
                    if (c) c.name = newName.trim();
                }
                this.showClientParamsModal(serverId, clientId);
            })
            .catch(error => {
                console.error('Error renaming client:', error);
                alert('Error renaming client: ' + error.message);
            });
    }

    toggleClientSuspend(serverId, clientId) {
        this.apiFetch(`/api/servers/${serverId}/clients/${clientId}/suspend`, { method: 'POST' })
            .then(() => this.loadServers())
            .catch(error => {
                console.error('Error toggling client suspend:', error);
                alert('Error toggling client suspend: ' + error.message);
            });
    }

    toggleServer(serverId, shouldRun) {
        const action = shouldRun ? 'start' : 'stop';
        this.apiFetch(`/api/servers/${serverId}/${action}`, { method: 'POST' })
            .then(() => this.loadServers())
            .catch(error => {
                console.error(`Error ${action}ing server:`, error);
                alert(`Error ${action}ing server: ` + error.message);
                this.loadServers();
            });
    }

    async submitAddClient(serverId) {
        const server = (this.lastServers || []).find((item) => String(item.id) === String(serverId));
        const mtu = Number(server?.mtu) || 1420;
        const clientName = (document.getElementById(`newClientName-${serverId}`)?.value || '').trim();
        const copyFromClientId = document.getElementById(`newClientCopyFrom-${serverId}`)?.value || '';
        // Shared with the edit dialog; also picks up the AWG 3.0 fields when present.
        const clientParams = this.collectClientParamsFormState(`newClientParam-${serverId}`);

        if (!clientName) {
            this.showTempMessage('Client name is required', 'error');
            return;
        }

        const errors = this.validateClientParamsJS(clientParams, mtu);
        if (errors.length > 0) {
            this.showTempMessage(errors.join(' '), 'error');
            return;
        }

        const warnings = this.getClientParamWarningsJS(clientParams, mtu);
        if (warnings.length > 0) {
            const proceed = confirm(`Client parameter warnings:\n\n- ${warnings.join('\n- ')}\n\nCreate client anyway?`);
            if (!proceed) {
                return;
            }
        }

        try {
            const response = await this.apiFetch(`/api/servers/${serverId}/clients`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    name: clientName,
                    client_params: clientParams,
                    copy_from_client_id: copyFromClientId || null,
                })
            });

            if (!response.ok) {
                let msg = 'Failed to add client';
                try {
                    const err = await response.json();
                    msg = err?.error || msg;
                } catch (_) {
                    // ignore
                }
                throw new Error(msg);
            }

            this.closeModal();
            this.loadServers();
            this.showTempMessage('Client created.', 'success');
        } catch (error) {
            console.error('Error adding client:', error);
            this.showTempMessage('Error adding client: ' + error.message, 'error');
        }
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
            alert('Error downloading client config: ' + error.message);
        }
    }

    showServerConfig(serverId) {
        this.apiFetch(`/api/servers/${serverId}/info`)
            .then(response => response.json())
            .then(serverInfo => {
                this.displayServerConfigModal(serverInfo);
            })
            .catch(error => {
                console.error('Error fetching server info:', error);
                alert('Error loading server configuration: ' + error.message);
            });
    }

    showRawServerConfig(serverId) {
        this.apiFetch(`/api/servers/${serverId}/config`)
            .then(response => response.json())
            .then(config => {
                this.displayRawConfigModal(config);
            })
            .catch(error => {
                console.error('Error fetching server config:', error);
                alert('Error loading server configuration: ' + error.message);
            });
    }

    downloadServerConfig(serverId) {
        this.downloadBlob(`/api/servers/${serverId}/config/download`, `server-${serverId}.conf`)
            .catch((error) => {
                console.error('Error downloading server config:', error);
                alert('Error downloading server config: ' + error.message);
            });
    }

    async saveServerNetworking(serverId) {
        const enableNatEl = document.getElementById(`serverEnableNat-${serverId}`);
        const blockLanEl = document.getElementById(`serverBlockLan-${serverId}`);
        const payload = {
            enable_nat: enableNatEl ? enableNatEl.checked : true,
            block_lan_cidrs: blockLanEl ? blockLanEl.checked : true
        };

        try {
            const response = await this.apiFetch(`/api/servers/${serverId}/networking`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                const text = await response.text();
                throw new Error(text || 'Failed to update networking');
            }

            const data = await response.json();
            const ipt = data?.iptables || 'skipped';
            const msg = ipt === 'failed'
                ? 'Networking updated, but iptables reapply failed.'
                : (ipt === 'reapplied' ? 'Networking updated and iptables reapplied.' : 'Networking updated.');
            this.showTempMessage(msg, ipt === 'failed' ? 'error' : 'success');
            this.loadServers();
        } catch (error) {
            console.error('Error updating server networking:', error);
            this.showTempMessage('Failed to update networking: ' + (error?.message || error), 'error');
        }
    }

    async saveServerTransportParams(serverId) {
        // Pull MTU from cached server list if possible (fallback to 1420)
        const server = (this.lastServers || []).find((s) => String(s.id) === String(serverId));
        const mtu = Number(server?.mtu) || 1420;
        const protocol = document.getElementById(`serverProtocol-${serverId}`)?.value || server?.protocol || window.Protocols.DEFAULT;

        // Same shape the dirty tracking compares against, so the two cannot drift.
        const params = this.collectServerTransportFormState(serverId, protocol);

        const errors = this.validateTransportParamsJS(protocol, params, mtu);
        if (errors.length > 0) {
            this.showTempMessage(errors.join(' '), 'error');
            return;
        }

        const warnings = this.getTransportParamWarningsJS(protocol, params, mtu);
        if (warnings.length > 0) {
            const proceed = confirm(`Transport parameter warnings:\n\n- ${warnings.join('\n- ')}\n\nSave anyway?`);
            if (!proceed) return;
        }

        const ok = confirm('Update protocol and transport parameters and restart the server if it is running? Existing clients will inherit the updated transport profile.');
        if (!ok) return;

        try {
            const response = await this.apiFetch(`/api/servers/${serverId}/transport-params`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(params)
            });

            if (!response.ok) {
                let msg = 'Failed to update server transport parameters';
                try {
                    const data = await response.json();
                    msg = data?.error || msg;
                } catch (_) {
                    const text = await response.text();
                    msg = text || msg;
                }
                throw new Error(msg);
            }

            const data = await response.json();
            const restarted = !!data?.restarted;
            this.showTempMessage(restarted ? 'Protocol and transport updated; server restarted.' : 'Protocol and transport updated.', 'success');
            this.loadServers();
            // Refresh the modal contents
            this.closeModal();
            this.showServerConfig(serverId);
        } catch (error) {
            console.error('Error updating server transport params:', error);
            this.showTempMessage('Failed to update protocol/transport: ' + (error?.message || error), 'error');
        }
    }

    async saveClientParams(serverId, clientId) {
        const server = (this.lastServers || []).find((item) => String(item.id) === String(serverId));
        const mtu = Number(server?.mtu) || 1420;
        const clientParams = this.collectClientParamsFormState(`clientParam-${clientId}`);

        const errors = this.validateClientParamsJS(clientParams, mtu);
        if (errors.length > 0) {
            this.showTempMessage(errors.join(' '), 'error');
            return;
        }

        const warnings = this.getClientParamWarningsJS(clientParams, mtu);
        if (warnings.length > 0) {
            const proceed = confirm(`Client parameter warnings:\n\n- ${warnings.join('\n- ')}\n\nSave anyway?`);
            if (!proceed) {
                return;
            }
        }

        try {
            const response = await this.apiFetch(`/api/servers/${serverId}/clients/${clientId}/client-params`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ client_params: clientParams })
            });
            if (!response.ok) {
                const text = await response.text();
                throw new Error(text || 'Failed to update client params');
            }
            this.closeModal();
            this.loadServers();
            this.showTempMessage('Client parameters updated.', 'success');
        } catch (error) {
            console.error('Error updating client params:', error);
            this.showTempMessage('Failed to update client params: ' + (error?.message || error), 'error');
        }
    }

    closeModal() {
        this.serverTransportModalState = null;
        this.clientParamsModalState = null;
        this.serverNetworkingModalState = null;
        const existingModal = document.getElementById('configModal') || document.getElementById('rawConfigModal');
        if (existingModal) existingModal.remove();

        const logsModal = document.getElementById('logsModal');
        if (logsModal) logsModal.remove();

        if (this.logPoller) {
            clearInterval(this.logPoller);
            this.logPoller = null;
        }

        // Also close the create-server modal (this one is part of the DOM)
        this.closeCreateServerModal();
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
                    <div class="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg p-3">
                        <div class="font-semibold mb-1">Failed to load configuration for QR</div>
                        <div class="text-xs text-red-600 font-mono break-all">${safeMsg}</div>
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

    showTempMessage(message, type) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `fixed top-4 right-4 px-4 py-2 rounded text-white text-sm z-50 ${
            type === 'success' ? 'bg-green-500' : 'bg-red-500'
        }`;
        messageDiv.textContent = message;

        document.body.appendChild(messageDiv);

        setTimeout(() => {
            messageDiv.remove();
        }, 3000);
    }
}

// Initialize the application
const amneziaApp = new AmneziaApp();