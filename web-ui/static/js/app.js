// AmneziaWG Web UI - Main Application JavaScript
//
// One class, AmneziaApp; forms.js (the drawer forms) and modals.js (the dialog views)
// add their methods to its prototype, and the instance is the top-level `amneziaApp`.
// What is in here, in file order (DEVELOPMENT.md §12 groups the methods by job):
//
//   state and boot                 constructor, init
//   helpers                        escapeHtml, apiFetch, generateQrIntoContainer
//   the data-action dispatcher     setupActions, setupEventListeners
//   theme                          getPreferredTheme ... updateThemeButton
//   protocol field gating          toggleProtocolFields
//   live updates (/api/events)     setupLiveUpdates, openEvents, recoverEvents, updateStatus
//   public IP and egress probe     updatePublicIp, refreshPublicIp, probeServerEgressIp
//   loading and rendering          loadServers (the one request), renderServers, updateServerTraffic
//   traffic history and charts     loadTrafficHistory, appendTrafficTick, drawTraffic, lastHour, sparks
//   page actions                   deleteServer ... toggleServer, getJson/postJson, showTempMessage
class AmneziaApp {
    constructor() {
        this.api = new window.ApiClient();
        // Live updates (openEvents): the EventSource while one is open, the retry
        // after the browser gave one up, and when the last message arrived.
        this.events = null;
        this.eventsRetryTimer = null;
        this.lastEventAt = 0;
        // The only page state: GET /api/servers, each server with its clients and
        // its last telemetry snapshot (`traffic`), kept current by traffic_update.
        this.lastServers = [];
        // Each running server's last hour for the charts (GET .../traffic?range=1h, then
        // one tick per traffic_update): {t: [epoch s], clients: {id: {down, up}}, loaded},
        // in Mbit/s from the device's side. `trafficAt` is the newest tick's time, by the
        // server's clock. server.traffic stays the snapshot.
        this.trafficHistory = {};
        this.trafficAt = 0;
        this.lastResyncAt = 0;
        this.drawerCtx = null;
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
            if (window.AppConfig.passwordPinned) {
                // The drawer cannot change it; the variable has to.
                document.getElementById('passwordBannerText').innerHTML = 'The panel signs in with <span class="font-mono">changeme</span>, '
                    + 'set by <span class="font-mono">NGINX_PASSWORD</span>. Give the variable a new value and restart.';
            }
            this.setupEventListeners();
            this.setupLiveUpdates();
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
                <div class="rounded-lg border px-3 py-2 text-sm callout-red">
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
                <div class="rounded-lg border px-3 py-2 text-sm callout-red">
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
            'server-traffic': (el) => this.showServerTraffic(el.dataset.server),
            'probe-egress': (el) => this.probeServerEgressIp(el.dataset.server, el),
            'raw-config': (el) => this.showRawServerConfig(el.dataset.server),
            'toggle-client': (el) => this.toggleClientSuspend(...ids(el)),
            'client-qr': (el) => { window.Ui.closeDrawer(); this.showClientQRCode(...ids(el)); },
            'client-edit': (el) => this.showClientParamsModal(...ids(el)),
            'client-menu': (el) => this.openClientMenu(...ids(el), el),
            'randomize': () => this.generateRandomParams(),
            'generate-key': () => this.fillHeaderProtectionKey('t-HeaderProtectionKey'),
            'generate-signatures': () => this.generateSignaturePackets(),
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
        // The charts are drawn to their width.
        let resizeFrame = 0;
        window.addEventListener('resize', () => {
            cancelAnimationFrame(resizeFrame);
            resizeFrame = requestAnimationFrame(() => {
                this.drawTraffic();
                this.redrawServerTraffic();
            });
        });
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

    // Live updates: one EventSource on /api/events (core/events.py) carrying
    // server_status and traffic_update. It is an ordinary request, so the browser sends
    // its cached password with it, reconnects a dropped stream by itself, and every
    // (re)open resyncs. The browser gives a stream up only on an answer that is not a
    // stream -- a 401 after the password changed elsewhere, a 502 while the panel
    // restarts -- and then recoverEvents decides what to do.
    //
    // A hidden tab closes its stream and opens a new one when shown, which also keeps
    // the browser's six connections per host (plain HTTP) from running out across tabs.
    setupLiveUpdates() {
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') this.openEvents();
            else this.closeEvents();
        });
        // Back from the back/forward cache, which closed the stream.
        window.addEventListener('pageshow', (event) => {
            if (event.persisted && document.visibilityState === 'visible') this.openEvents();
        });
        window.addEventListener('online', () => {
            if (document.visibilityState === 'visible' && this.events?.readyState !== EventSource.OPEN) this.openEvents();
        });
        // EventSource has no timeout: a connection that died without a word (a network
        // change while the tab stayed visible) would just go quiet. The server sends
        // something at least every 15 s (a ping when idle), so 45 s of silence means
        // the stream is gone.
        setInterval(() => {
            if (this.events && Date.now() - this.lastEventAt > 45000) this.openEvents();
        }, 15000);
        if (document.visibilityState === 'visible') this.openEvents();
    }

    openEvents() {
        this.closeEvents();
        const source = new EventSource('/api/events');
        this.events = source;
        this.lastEventAt = Date.now();
        // Each handler ignores a replaced stream: a late event from it must not touch
        // the page.
        const on = (type, handler) => source.addEventListener(type, (event) => {
            if (source !== this.events) return;
            this.lastEventAt = Date.now();
            handler(event);
        });
        on('open', () => {
            this.updateStatus('Connected to AmneziaWG Web UI', true);
            this.resyncAppState();
        });
        on('error', () => {
            this.updateStatus('Reconnecting to AmneziaWG Web UI...', false);
            if (source.readyState === EventSource.CLOSED) this.recoverEvents();
        });
        on('ping', () => {});
        on('server_status', () => this.loadServers());
        on('traffic_update', (event) => {
            const data = JSON.parse(event.data);
            this.updateServerTraffic(data.server_id, data.traffic, data.at, data.totals);
        });
    }

    closeEvents() {
        clearTimeout(this.eventsRetryTimer);
        this.eventsRetryTimer = null;
        if (this.events) {
            this.events.close();
            this.events = null;
        }
    }

    // The browser gave the stream up. One request through apiFetch tells why: a 401
    // takes ApiClient's usual way to the sign-in (a reload, where the browser asks);
    // anything else means the panel is restarting or out of reach, so try again.
    async recoverEvents() {
        this.closeEvents();
        try {
            const response = await this.apiFetch('/api/system/status');
            if (response.status === 401) return;
        } catch (_) {
            // Out of reach: the retry below covers it. (Safari reports a 401 this
            // way too; ApiClient tells them apart and is then signing in.)
        }
        // A hidden tab opens its stream when it is shown again.
        if (this.api.signingIn || document.visibilityState !== 'visible' || this.events) return;
        this.eventsRetryTimer = setTimeout(() => this.openEvents(), 3000);
    }

    // pageshow, visibilitychange and a stream opening tend to come together; one
    // reload serves them all.
    resyncAppState() {
        const now = Date.now();
        if (now - this.lastResyncAt < 2000) return;
        this.lastResyncAt = now;
        this.loadServers({ resync: true });
        this.loadPublicIp();
    }

    // The header pill: "Live" while the stream is open (traffic arrives every 7 s),
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

    // Through the throttle: the stream opens a moment later and would load it all again.
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

    // `resync`: the page may have missed ticks (a hidden tab, a new stream), so every
    // running server's last hour is fetched again, not only a newly started one's.
    loadServers({ resync = false } = {}) {
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
                this.loadTrafficHistory(resync);
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
        this.drawTraffic();
    }

    renderServerClients(serverId, clients, traffic = {}) {
        const server = (this.lastServers || []).find((s) => s.id === serverId) || { id: serverId };
        return window.ServerUi.renderServerClientsHtml({
            server,
            clients,
            traffic,
            sparks: this.sparks(server),
            escapeHtml: (v) => this.escapeHtml(v),
            isClientActiveFromTraffic: (clientTraffic) => this.isClientActiveFromTraffic(clientTraffic),
        });
    }

    // The status strip: servers running, clients online (handshake <= 5 min), and every
    // running server's traffic: its rate now and its last hour.
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

        const live = servers.filter((s) => s.status === 'running');
        const rateNow = live.reduce((a, s) => a + Object.values(s.traffic || {})
            .reduce((b, t) => b + (Number(t?.sent_bps) || 0) + (Number(t?.received_bps) || 0), 0), 0) / 1e6;
        set('stripTrafficNow', live.length ? window.Charts.rateNum(rateNow) : '–');
        const chart = document.getElementById('stripTrafficChart');
        if (chart) chart.innerHTML = live.length ? window.Charts.mirrored({ w: 76, h: 22, ...this.lastHour(live, 48) }).svg : '';
    }

    openServerMenu(serverId, anchor) {
        const server = (this.lastServers || []).find((s) => s.id === serverId);
        if (!server) return;
        window.Ui.openMenu(anchor, [
            { label: 'Traffic', icon: 'activity', run: () => this.showServerTraffic(serverId) },
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

    // Every 7 s: the running server's new telemetry, patched into its rows in place, its
    // tick (`at`) appended to the last hour the charts draw, and its interface's totals.
    updateServerTraffic(serverId, traffic, at = null, totals = undefined) {
        const server = (this.lastServers || []).find((s) => s.id === serverId);
        if (!server) return;
        const previous = server.traffic || {};
        server.traffic = (traffic && typeof traffic === 'object') ? traffic : {};
        if (totals !== undefined) server.totals = totals;
        if (Number.isFinite(at)) this.appendTrafficTick(server, at);
        this.patchServerRows(server, previous);
        this.drawBand(server);
        this.renderStrip();
        this.redrawServerTraffic(serverId, 'tick');
    }

    patchServerRows(server, previous = server.traffic || {}) {
        window.ServerUi.patchClients({
            container: this.getElement(`clients-${server.id}`),
            server,
            traffic: server.traffic || {},
            previous,
            sparks: this.sparks(server),
            escapeHtml: (v) => this.escapeHtml(v),
            isClientActiveFromTraffic: (t) => this.isClientActiveFromTraffic(t),
        });
    }

    // --- traffic history: the page's last hour, and the small charts drawn from it ------

    // Each running server's last hour. On a resync every one (ticks may have been
    // missed); otherwise only a server not loaded yet (just started). A stopped server's
    // goes: it has no band, and its next start fetches it again.
    async loadTrafficHistory(all = false) {
        const running = (this.lastServers || []).filter((s) => s.status === 'running');
        Object.keys(this.trafficHistory).forEach((id) => {
            if (!running.some((s) => s.id === id)) delete this.trafficHistory[id];
        });
        const wanted = running.filter((s) => all || !this.trafficHistory[s.id]?.loaded);
        if (!wanted.length) return;
        await Promise.all(wanted.map(async (server) => {
            try {
                const data = window.Charts.fromApi(await this.getJson(`/api/servers/${server.id}/traffic?range=1h`));
                const history = { t: data.t, clients: {}, loaded: true };
                Object.entries(data.clients).forEach(([id, c]) => { history.clients[id] = { down: c.down, up: c.up }; });
                // Ticks that arrived while the request was out.
                const local = this.trafficHistory[server.id];
                const last = data.t[data.t.length - 1] ?? 0;
                local?.t.forEach((t, i) => {
                    if (t <= last) return;
                    history.t.push(t);
                    Object.entries(local.clients).forEach(([id, c]) => {
                        history.clients[id] ??= { down: new Array(history.t.length - 1).fill(null), up: new Array(history.t.length - 1).fill(null) };
                        history.clients[id].down.push(c.down[i] ?? null);
                        history.clients[id].up.push(c.up[i] ?? null);
                    });
                });
                this.padTrafficHistory(history);
                this.trafficHistory[server.id] = history;
                this.trafficAt = Math.max(this.trafficAt, data.now || 0);
            } catch (error) {
                console.error('Error loading traffic history:', error);
            }
        }));
        this.drawTraffic();
    }

    // One tick of a running server, from traffic_update: its clients' rates now, and
    // whatever is over an hour old falls off.
    appendTrafficTick(server, at) {
        const history = (this.trafficHistory[server.id] ??= { t: [], clients: {}, loaded: false });
        if (history.t.length && at <= history.t[history.t.length - 1]) return;
        history.t.push(at);
        (server.clients || []).forEach((client) => {
            const t = server.traffic[client.id];
            // A new client: no data before it.
            const none = () => new Array(history.t.length - 1).fill(null);
            const c = (history.clients[client.id] ??= { down: none(), up: none() });
            c.down.push(t ? (Number(t.sent_bps) || 0) / 1e6 : null);
            c.up.push(t ? (Number(t.received_bps) || 0) / 1e6 : null);
        });
        this.padTrafficHistory(history);
        let drop = 0;
        while (drop < history.t.length && history.t[drop] <= at - 3600) drop += 1;
        if (drop) {
            history.t.splice(0, drop);
            Object.values(history.clients).forEach((c) => { c.down.splice(0, drop); c.up.splice(0, drop); });
        }
        this.trafficAt = Math.max(this.trafficAt, at);
    }

    // Every client's series as long as the times: null where it has no data.
    padTrafficHistory(history) {
        const n = history.t.length;
        Object.values(history.clients).forEach((c) => {
            ['down', 'up'].forEach((key) => {
                for (let i = c[key].length; i < n; i++) c[key][i] = null;
                c[key].length = n;
            });
        });
    }

    // The last hour of these servers together, in `n` buckets: {down, up}.
    lastHour(servers, n) {
        const { buckets, sumSeries } = window.Charts;
        const end = this.trafficAt || Date.now() / 1000;
        const parts = servers.map((s) => this.trafficHistory[s.id]).filter(Boolean).map((h) => {
            const list = Object.values(h.clients);
            return {
                down: buckets(h.t, sumSeries(list.map((c) => c.down), h.t.length), end - 3600, end, n),
                up: buckets(h.t, sumSeries(list.map((c) => c.up), h.t.length), end - 3600, end, n),
            };
        });
        return { down: sumSeries(parts.map((p) => p.down), n), up: sumSeries(parts.map((p) => p.up), n) };
    }

    // Each client's last hour in 60 buckets, for the sparklines in its row.
    sparks(server) {
        const history = this.trafficHistory[server?.id];
        if (!history) return {};
        const { buckets } = window.Charts;
        const end = this.trafficAt || Date.now() / 1000;
        const out = {};
        Object.entries(history.clients).forEach(([id, c]) => {
            out[id] = { down: buckets(history.t, c.down, end - 3600, end, 60), up: buckets(history.t, c.up, end - 3600, end, 60) };
        });
        return out;
    }

    // A running card's band: its last hour across the card's width, its rates now, and
    // the totals box beside it.
    drawBand(server) {
        const { maxOf, mirrored } = window.Charts;
        const plot = document.querySelector(`[data-band-plot="${server.id}"]`);
        if (plot) {
            plot.innerHTML = ''; // measured empty: the chart takes the width the rates leave
            const lh = this.lastHour([server], 120);
            plot.innerHTML = mirrored({ w: plot.clientWidth, h: 44, down: lh.down, up: lh.up,
                dTop: Math.max(maxOf(lh.down), 1), uTop: Math.max(maxOf(lh.up), 0.25) }).svg;
        }
        const now = document.querySelector(`[data-band-now="${server.id}"]`);
        if (now) now.innerHTML = window.ServerUi.bandNowHtml(server.traffic || {});
        const total = document.querySelector(`[data-total="${server.id}"]`);
        if (total) total.innerHTML = window.ServerUi.totalHtml(server.totals);
    }

    // Every chart on the page: the bands, the sparklines and the strip.
    drawTraffic() {
        (this.lastServers || []).filter((s) => s.status === 'running').forEach((server) => {
            this.drawBand(server);
            this.patchServerRows(server);
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