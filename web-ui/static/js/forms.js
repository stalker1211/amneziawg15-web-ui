// AmneziaWG Web UI - the forms, shown in the side drawer: new server, server
// settings, add client and edit client.
//
// The rules live only in the backend: as you type, the form is sent to
// POST /api/validate (a dry run) and its errors and warnings are shown under the
// fields. The primary button is enabled when nothing is invalid and, for an edit,
// something changed. These are methods of AmneziaApp, installed onto its prototype
// at the bottom of this file, like modals.js. Buttons name their action in
// data-action (AmneziaApp.setupActions); there are no inline handlers (CSP).
//
// Field ids are fixed (one drawer at a time): f-* server basics, t-* transport
// (the prefix toggleProtocolFields works with), s-* settings, c-* client.
class FormUi {
    // --- markup helpers -----------------------------------------------------------
    formField(id, label, value, { type = 'text', hint = '', cls = '', mono = false, placeholder = '', attrs = '' } = {}) {
        const safe = (v) => this.escapeHtml(v ?? '');
        return `
            <div class="${cls}">
                <label class="label" for="${id}">${label}</label>
                <input id="${id}" type="${type}" class="field ${mono ? 'font-mono' : ''}" value="${safe(value)}" placeholder="${safe(placeholder)}" ${attrs}>
                ${hint ? `<p class="hint">${hint}</p>` : ''}
            </div>`;
    }

    formSwitch(id, title, description, checked) {
        return `
            <label class="flex items-start justify-between gap-4 py-1.5 cursor-pointer" for="${id}">
                <span>
                    <span class="block text-sm font-medium text-gray-800 dark:text-[#e5e7eb]">${title}</span>
                    <span class="block text-xs text-gray-500 dark:text-[#94a3b8]">${description}</span>
                </span>
                <span class="switch mt-0.5"><input id="${id}" type="checkbox" ${checked ? 'checked' : ''}><span class="track"></span></span>
            </label>`;
    }

    formSection(title, inner, extra = '') {
        return `
            <section class="flex flex-col gap-3">
                <div class="flex items-center justify-between gap-3"><h3 class="section-title">${title}</h3>${extra}</div>
                ${inner}
            </section>`;
    }

    formCallout(html) {
        return `<p class="rounded-lg border px-3 py-2 text-xs bg-amber-50 border-amber-200 text-amber-900 dark:bg-[#2b1d05] dark:border-[#78350f] dark:text-[#fde68a]">${html}</p>`;
    }

    drawerFooter(primaryLabel, left = '') {
        return `
            ${left}<span class="flex-1"></span>
            <button type="button" id="drawerStatus" hidden data-action="show-checks"
                class="px-1 text-xs font-medium text-red-600 hover:underline dark:text-[#fca5a5]"></button>
            <button type="button" class="btn btn-secondary" data-close="drawer">Cancel</button>
            <button type="submit" id="drawerPrimary" class="btn btn-primary">${primaryLabel}</button>`;
    }

    // Collapsed help under the protocol select; its lines follow the protocol.
    getTransportDescriptionHtml(protocol) {
        const P = window.Protocols;
        const lines = [
            'S1 and S2 add random bytes to the handshake initiation and response. Common values are 15-150; S1 + 56 must not equal S2.',
            P.supportsS34(protocol) ? 'S3 pads cookie replies and S4 every data packet; S4 above 32 tends to cause "message too long" errors.' : '',
            P.supportsHeaderRanges(protocol)
                ? 'H1-H4 replace WireGuard\'s four message-type headers. Each can be a number or a range like 1000-1400, and the four must not overlap.'
                : 'H1-H4 replace WireGuard\'s four message-type headers, one number each.',
            P.supportsAwg3(protocol)
                ? 'Header protection encrypts the headers instead of only changing them. It needs S1-S4 each 12 or more (the first 12 bytes of each padding are the cipher nonce).'
                : '',
            P.supportsAwg31(protocol)
                ? 'Random trailers append a random number of bytes to packets; disabling cookies drops handshake cookie replies and weakens flood protection.'
                : '',
            'Clients copy all of these from the server, so changing them means every client re-imports its config.',
        ].filter(Boolean);
        return `
            <details class="help text-xs text-gray-600 dark:text-[#cbd5e1]">
                <summary class="font-medium">What these values do</summary>
                <div class="mt-2 flex flex-col gap-1 leading-5">${lines.map((l) => `<p>${this.escapeHtml(l)}</p>`).join('')}</div>
            </details>`;
    }

    transportFieldsHtml(protocol, t) {
        const num = (k) => this.formField(`t-${k}`, k, t[k] ?? '', { type: 'number', mono: true, attrs: 'min="0" inputmode="numeric"' });
        return `
            <div>
                <label class="label" for="t-protocol">Protocol</label>
                <select id="t-protocol" class="field">${window.Protocols.optionsHtml(protocol)}</select>
            </div>
            <div id="t-Description"></div>
            <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">${num('S1')}${num('S2')}${num('S3')}${num('S4')}</div>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                ${['H1', 'H2', 'H3', 'H4'].map((k) => this.formField(`t-${k}`, k, t[k] ?? '', { mono: true })).join('')}
            </div>
            <div id="t-HeaderProtectionKeyRow">
                <label class="label" for="t-HeaderProtectionKey">Header protection key <span class="font-normal text-gray-400 dark:text-[#64748b]">optional</span></label>
                <div class="flex gap-2">
                    <input id="t-HeaderProtectionKey" class="field font-mono text-xs" value="${this.escapeHtml(t.HeaderProtectionKey || '')}" placeholder="empty: header protection off">
                    <button type="button" class="btn btn-secondary" data-action="generate-key">Generate</button>
                </div>
                <p class="hint">Encrypts packet headers. Needs S1-S4 each 12 or more; written into every client config.</p>
            </div>
            <div id="t-Awg31OptionsRow" class="flex flex-col">
                ${this.formSwitch('t-RandomTrailers', 'Random trailers', 'Append a random number of bytes to each packet.', t.RandomTrailers)}
                ${this.formSwitch('t-DisableCookies', 'Disable cookies', 'Do not send handshake cookie replies.', t.DisableCookies)}
            </div>`;
    }

    // Same shape as the transport-params route and /api/validate expect.
    collectTransportForm() {
        const protocol = document.getElementById('t-protocol')?.value || window.Protocols.DEFAULT;
        const P = window.Protocols;
        const value = (k) => (document.getElementById(`t-${k}`)?.value || '').trim();
        const number = (k) => {
            const raw = value(k);
            if (!raw) return null;
            return /^-?\d+$/.test(raw) ? Number(raw) : raw; // the server explains a bad value
        };
        return {
            protocol,
            S1: number('S1'),
            S2: number('S2'),
            S3: P.supportsS34(protocol) ? number('S3') : null,
            S4: P.supportsS34(protocol) ? number('S4') : null,
            H1: value('H1'), H2: value('H2'), H3: value('H3'), H4: value('H4'),
            HeaderProtectionKey: P.supportsAwg3(protocol) ? value('HeaderProtectionKey') : '',
            RandomTrailers: P.supportsAwg31(protocol) ? !!document.getElementById('t-RandomTrailers')?.checked : false,
            DisableCookies: P.supportsAwg31(protocol) ? !!document.getElementById('t-DisableCookies')?.checked : false,
        };
    }

    clientFieldsHtml(server, params) {
        const P = window.Protocols;
        const protocol = server.protocol || P.DEFAULT;
        const safe = (v) => this.escapeHtml(v ?? '');
        const anySignature = AmneziaApp.I_PARAM_KEYS.some((k) => params[k]);
        return `
            <p class="text-xs text-gray-500 dark:text-[#94a3b8]">From the server, the same for every client:
                <span class="text-gray-700 dark:text-[#cbd5e1]">${safe(protocol)} · ${safe(this.formatTransportParamsSummary(protocol, server.transport_params || {}))}</span></p>
            <div class="grid grid-cols-3 gap-3">
                ${this.formField('c-Jc', 'Jc', params.Jc ?? 8, { type: 'number', mono: true, hint: 'junk packets' })}
                ${this.formField('c-Jmin', 'Jmin', params.Jmin ?? 8, { type: 'number', mono: true, hint: 'bytes' })}
                ${this.formField('c-Jmax', 'Jmax', params.Jmax ?? 80, { type: 'number', mono: true, hint: 'bytes' })}
            </div>
            <details class="help text-sm text-gray-600 dark:text-[#cbd5e1]"${anySignature ? ' open' : ''}>
                <summary class="font-medium">Signature packets I1-I5 <span class="font-normal text-gray-400 dark:text-[#64748b]">optional</span></summary>
                <div class="mt-3 flex flex-col gap-3">
                    ${AmneziaApp.I_PARAM_KEYS.map((k) => `
                        <div><label class="label" for="c-${k}">${k}</label>
                        <textarea id="c-${k}" rows="1" class="field field-area" placeholder="e.g. &lt;b 0xc6000000010843&gt;&lt;r 16&gt;">${safe(params[k] || '')}</textarea></div>`).join('')}
                </div>
            </details>
            ${P.supportsAwg3(protocol) ? `
            <div class="flex flex-col gap-3">
                <p class="text-xs font-medium text-gray-600 dark:text-[#cbd5e1]">AWG 3.x timing and padding
                    <span class="font-normal text-gray-400 dark:text-[#64748b]">a number or a range like 22-30; empty keeps the default</span></p>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    ${AmneziaApp.AWG3_CLIENT_PARAM_KEYS.map((k) => this.formField(`c-${k}`, k, params[k] || '', { mono: true, placeholder: 'default' })).join('')}
                </div>
            </div>` : ''}`;
    }

    // Jc/Jmin/Jmax as numbers when they are numbers (the server explains anything else).
    collectClientForm() {
        const value = (k) => (document.getElementById(`c-${k}`)?.value || '').trim();
        const number = (k) => (/^-?\d+$/.test(value(k)) ? Number(value(k)) : value(k));
        const params = { Jc: number('Jc'), Jmin: number('Jmin'), Jmax: number('Jmax') };
        AmneziaApp.I_PARAM_KEYS.forEach((k) => { params[k] = document.getElementById(`c-${k}`)?.value || ''; });
        AmneziaApp.AWG3_CLIENT_PARAM_KEYS.forEach((k) => {
            if (document.getElementById(`c-${k}`)) params[k] = value(k);
        });
        return params;
    }

    // --- checking as you type -----------------------------------------------------
    // ctx: { collect(), validateBody(), submit(), and optionally snapshot (null for an
    //        edit form), saveLabel, localErrors(), onResult(result), mount(), serverId }
    openFormDrawer({ title, sub, body, primaryLabel, left = '', ctx }) {
        this.drawerCtx = ctx;
        this.drawerCheckSeq = 0;
        window.Ui.openDrawer({
            title,
            sub,
            body: `${body}<div id="checks" class="flex flex-col gap-2"></div>`,
            foot: this.drawerFooter(primaryLabel, left),
            onSubmit: () => this.submitDrawer(),
            onClose: () => { this.drawerCtx = null; clearTimeout(this.drawerCheckTimer); },
        });
        ctx.mount?.();
        // An edit form remembers where it started, so Save arms only on a change.
        if (ctx.snapshot !== undefined) ctx.snapshot = JSON.stringify(ctx.collect());
        this.runDrawerCheck();
    }

    setupDrawerForms() {
        const body = document.getElementById('drawerBody');
        if (!body) return;
        const onEdit = (e) => {
            const ctx = this.drawerCtx;
            if (!ctx) return;
            if (e.target.tagName === 'TEXTAREA') this.autosizeTextarea(e.target, 200);
            if (e.target.id === 'c-copyFrom') this.fillClientFromCopy(e.target.value);
            // Typing marks the transport fields as the user's own; values filled in by
            // the generator fire no input event.
            if (e.type === 'input' && e.target.id.startsWith('t-')) ctx.transportEdited = true;
            if (e.type === 'change' && e.target.id === 't-protocol') {
                this.toggleProtocolFields(e.target.value, 't-');
                ctx.onProtocolChange?.();
            }
            this.scheduleDrawerCheck();
        };
        body.addEventListener('input', onEdit);
        body.addEventListener('change', onEdit);
    }

    scheduleDrawerCheck() {
        const box = document.getElementById('checks');
        if (box) box.innerHTML = '<p class="text-sm text-gray-400 dark:text-[#64748b]">Checking…</p>';
        clearTimeout(this.drawerCheckTimer);
        this.drawerCheckTimer = setTimeout(() => this.runDrawerCheck(), 280);
    }

    isDrawerDirty() {
        const ctx = this.drawerCtx;
        return !ctx || !ctx.snapshot || JSON.stringify(ctx.collect()) !== ctx.snapshot;
    }

    // Sends the form to /api/validate and shows the answer. Resolves the result, or
    // null when the drawer closed or a newer check overtook this one.
    async runDrawerCheck() {
        const ctx = this.drawerCtx;
        if (!ctx) return null;
        const seq = ++this.drawerCheckSeq;
        let result;
        try {
            result = await this.postJson('/api/validate', ctx.validateBody());
        } catch (error) {
            result = { errors: [`Could not check the form: ${error.message}`], warnings: [] };
        }
        if (ctx !== this.drawerCtx || seq !== this.drawerCheckSeq) return null;
        result.errors = [...(ctx.localErrors?.() || []), ...(result.errors || [])];
        result.warnings = result.warnings || [];

        const box = document.getElementById('checks');
        if (box) box.innerHTML = this.renderChecks(result);
        const status = document.getElementById('drawerStatus');
        if (status) {
            status.hidden = !result.errors.length;
            status.textContent = result.errors.length === 1 ? '1 thing to fix' : `${result.errors.length} things to fix`;
        }
        ctx.onResult?.(result);
        const primary = document.getElementById('drawerPrimary');
        const dirty = this.isDrawerDirty();
        if (primary) {
            primary.disabled = result.errors.length > 0 || !dirty;
            if (ctx.snapshot) primary.textContent = dirty ? ctx.saveLabel : 'No changes';
        }
        return result;
    }

    renderChecks({ errors, warnings }) {
        const list = (items) => `<ul class="mt-1 list-disc pl-5 flex flex-col gap-0.5">${items.map((x) => `<li>${this.escapeHtml(x)}</li>`).join('')}</ul>`;
        let html = '';
        if (errors.length) {
            html += `<div class="rounded-lg border px-3 py-2 text-sm bg-red-50 border-red-200 text-red-800 dark:bg-[#3b1219] dark:border-[#7f1d1d] dark:text-[#fecaca]" role="alert">
                <strong class="font-semibold">${errors.length === 1 ? 'One thing to fix' : `${errors.length} things to fix`}</strong>${list(errors)}</div>`;
        }
        if (warnings.length) {
            html += `<div class="rounded-lg border px-3 py-2 text-sm bg-amber-50 border-amber-200 text-amber-900 dark:bg-[#2b1d05] dark:border-[#78350f] dark:text-[#fde68a]">
                <strong class="font-semibold">Worth a look</strong> <span class="text-xs">(saving still works)</span>${list(warnings)}</div>`;
        }
        if (!html) {
            html = `<p class="flex items-center gap-1.5 text-sm text-green-700 dark:text-[#86efac]">${window.Ui.icon('check')}No problems found.</p>`;
        }
        return `${html}<p class="text-xs text-gray-400 dark:text-[#64748b]">Checked by the server as you type.</p>`;
    }

    async submitDrawer() {
        const ctx = this.drawerCtx;
        const primary = document.getElementById('drawerPrimary');
        if (!ctx || !primary || primary.disabled) return;
        primary.disabled = true;
        clearTimeout(this.drawerCheckTimer);
        const result = await this.runDrawerCheck();
        if (!result || result.errors.length || ctx !== this.drawerCtx) {
            document.getElementById('checks')?.scrollIntoView({ block: 'nearest' });
            return;
        }
        try {
            await ctx.submit();
        } catch (error) {
            console.error('Saving failed:', error);
            this.showTempMessage(error.message, 'error');
            if (ctx === this.drawerCtx) primary.disabled = false;
        }
    }

    // --- new server -----------------------------------------------------------------
    async openCreateServerModal() {
        const env = this.environment || {};
        const servers = this.lastServers || [];
        const usedPorts = new Set(servers.map((s) => Number(s.port)));
        let port = Number(env.port) || 51820;
        while (usedPorts.has(port)) port += 1;
        const usedThird = new Set(servers.map((s) => (/^10\.10\.(\d+)\./.exec(s.subnet || '') || [])[1]).filter(Boolean).map(Number));
        let third = 0;
        while (usedThird.has(third) && third < 255) third += 1;

        const protocol = window.Protocols.DEFAULT;
        const mtu = env.mtu || 1420;
        // Every server gets its own random parameters (it used to be one fixed set
        // unless Randomize was pressed).
        const generated = await this.fetchGenerated(protocol, mtu).catch(() => null);
        const transport = generated?.transport_params || {};
        const basics = () => ({
            name: (document.getElementById('f-name')?.value || '').trim(),
            port: this.numberOrText('f-port'),
            subnet: (document.getElementById('f-subnet')?.value || '').trim(),
            mtu: this.numberOrText('f-mtu'),
            dns: (document.getElementById('f-dns')?.value || '').trim(),
        });
        const transportParams = () => {
            const { protocol: _protocol, ...rest } = this.collectTransportForm();
            return rest;
        };

        this.openFormDrawer({
            title: 'New server',
            sub: 'Creates an interface and its config; clients are added afterwards.',
            body: `
                ${this.formSection('Server', `
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        ${this.formField('f-name', 'Name', '', { cls: 'sm:col-span-2', placeholder: 'e.g. Home NL' })}
                        ${this.formField('f-port', 'Port (UDP)', port, { type: 'number', mono: true })}
                        ${this.formField('f-subnet', 'Subnet', `10.10.${third}.0/24`, { mono: true })}
                        ${this.formField('f-mtu', 'MTU', mtu, { type: 'number', mono: true, hint: '1280-1440; 1420 suits most links, 1280 the most restrictive ones.' })}
                        ${this.formField('f-dns', 'DNS servers', env.dns || '1.1.1.1, 9.9.9.9', { mono: true, hint: 'Comma-separated, pushed to clients.' })}
                    </div>`)}
                ${this.formSection('Networking', `
                    <div class="flex flex-col">
                        ${this.formSwitch('f-autostart', 'Start after creating', 'Brings the interface up right away.', true)}
                        ${this.formSwitch('f-nat', 'NAT (masquerade)', 'Clients reach the internet through this host.', env.enable_nat !== false)}
                        ${this.formSwitch('f-lan', 'Block private LAN ranges', 'Clients cannot reach 10/8, 172.16/12 or 192.168/16 behind the server.', env.block_lan_cidrs !== false)}
                    </div>`)}
                ${this.formSection('Protocol and transport', this.transportFieldsHtml(protocol, transport),
                    '<button type="button" class="btn btn-secondary btn-sm" data-action="randomize">Randomize</button>')}`,
            primaryLabel: 'Create server',
            ctx: {
                mount: () => this.toggleProtocolFields(protocol, 't-'),
                // A new protocol gets a fresh set, unless the fields were typed in.
                onProtocolChange: () => { if (!this.drawerCtx?.transportEdited) this.generateRandomParams(); },
                collect: () => ({ ...basics(), ...this.collectTransportForm() }),
                validateBody: () => ({
                    server: { ...basics(), protocol: this.collectTransportForm().protocol, transport_params: transportParams() },
                }),
                submit: async () => {
                    const payload = {
                        ...basics(),
                        protocol: this.collectTransportForm().protocol,
                        transport_params: transportParams(),
                        auto_start: !!document.getElementById('f-autostart')?.checked,
                        enable_nat: !!document.getElementById('f-nat')?.checked,
                        block_lan_cidrs: !!document.getElementById('f-lan')?.checked,
                    };
                    const server = await this.postJson('/api/servers', payload);
                    window.Ui.closeDrawer();
                    this.showTempMessage(`${server.name} created${payload.auto_start ? ' and started' : ''}`, 'success');
                    this.loadServers();
                },
            },
        });
    }

    numberOrText(id) {
        const raw = (document.getElementById(id)?.value || '').trim();
        return /^\d+$/.test(raw) ? Number(raw) : raw;
    }

    // Random parameters from the server (POST /api/generate), the one generator:
    // every value it draws passes the validators without a warning.
    async fetchGenerated(protocol, mtu) {
        const n = Number(mtu);
        return this.postJson('/api/generate', { protocol, ...(n >= 1280 && n <= 1440 ? { mtu: n } : {}) });
    }

    fillTransportFields(params, { keepKey = false } = {}) {
        ['S1', 'S2', 'S3', 'S4', 'H1', 'H2', 'H3', 'H4'].forEach((k) => {
            const el = document.getElementById(`t-${k}`);
            if (el && !el.disabled) el.value = params[k] ?? '';
        });
        const key = document.getElementById('t-HeaderProtectionKey');
        if (key && !key.disabled && params.HeaderProtectionKey && !(keepKey && key.value.trim())) {
            key.value = params.HeaderProtectionKey;
        }
    }

    // Randomize: a fresh set for the selected protocol. A header protection key that
    // is already there stays (Generate replaces it).
    async generateRandomParams() {
        const ctx = this.drawerCtx;
        const protocol = document.getElementById('t-protocol')?.value || window.Protocols.DEFAULT;
        const mtu = document.getElementById('f-mtu')?.value || ctx?.mtu;
        try {
            const data = await this.fetchGenerated(protocol, mtu);
            if (ctx !== this.drawerCtx) return;
            this.fillTransportFields(data.transport_params, { keepKey: true });
            if (ctx) ctx.transportEdited = false;
            this.scheduleDrawerCheck();
        } catch (error) {
            this.showTempMessage(`Could not generate parameters: ${error.message}`, 'error');
        }
    }

    // Generate: a new 32-byte header protection key, from the server like the rest.
    async fillHeaderProtectionKey(elementId) {
        const element = document.getElementById(elementId);
        const protocol = document.getElementById('t-protocol')?.value;
        if (!element || !window.Protocols.supportsAwg3(protocol)) return;
        try {
            const data = await this.fetchGenerated(protocol);
            element.value = data.transport_params.HeaderProtectionKey || '';
            element.dispatchEvent(new Event('input', { bubbles: true }));
        } catch (error) {
            this.showTempMessage(`Could not generate a key: ${error.message}`, 'error');
        }
    }

    // --- server settings --------------------------------------------------------------
    openServerSettings(info) {
        const server = (this.lastServers || []).find((s) => s.id === info.id) || {};
        const safe = (v) => this.escapeHtml(v ?? '');
        const running = info.status === 'running';
        const clients = server.clients || [];
        const defaultImpact = 'Changing anything here rewrites every client config, and a running server restarts. '
            + 'Each client then has to re-import its QR code or .conf.';
        const row = (k, v, mono = true) => `<dt class="text-gray-500 dark:text-[#94a3b8]">${k}</dt><dd class="${mono ? 'font-mono' : ''} text-gray-800 dark:text-[#e5e7eb] min-w-0 truncate">${v}</dd>`;
        const collect = () => ({
            nat: !!document.getElementById('s-nat')?.checked,
            lan: !!document.getElementById('s-lan')?.checked,
            ...this.collectTransportForm(),
        });
        const transportOnly = (state) => { const { nat: _n, lan: _l, ...rest } = state; return rest; };

        const ctx = {
            snapshot: null,
            saveLabel: 'Save changes',
            mtu: info.mtu,
            mount: () => this.toggleProtocolFields(info.protocol, 't-'),
            collect,
            validateBody: () => {
                const { protocol, ...transport } = this.collectTransportForm();
                return { server_id: info.id, protocol, transport_params: transport };
            },
            onResult: (result) => {
                const impact = document.getElementById('impact');
                if (!impact) return;
                const changed = result.configs_changed || 0;
                const after = result.outdated_after || 0;
                const now = result.outdated_now || 0;
                const n = clients.length;
                const configs = `${changed} client config${changed === 1 ? '' : 's'}`;
                const restart = running ? ' and restarts the server' : '';
                let text = this.escapeHtml(defaultImpact);
                if (changed && after) {
                    text = `Saving changes ${configs}${restart}. <strong>${after} of ${n} client${n === 1 ? '' : 's'} will need to re-import</strong> (QR code or .conf) and stay marked until then.`;
                } else if (changed && now) {
                    text = `Saving changes ${configs}${restart}. <strong>Every config will match its device again</strong>, so the re-import marks go away.`;
                } else if (changed) {
                    text = `Saving changes ${configs}${restart}.`;
                }
                impact.innerHTML = this.formCallout(text);
            },
            submit: async () => {
                const now = collect();
                const before = JSON.parse(ctx.snapshot);
                const networkChanged = now.nat !== before.nat || now.lan !== before.lan;
                const transportChanged = JSON.stringify(transportOnly(now)) !== JSON.stringify(transportOnly(before));
                let restarted = false;
                if (networkChanged) {
                    const data = await this.postJson(`/api/servers/${info.id}/networking`, { enable_nat: now.nat, block_lan_cidrs: now.lan });
                    if (data.iptables === 'failed') this.showTempMessage('Networking saved, but reapplying iptables failed.', 'error');
                }
                if (transportChanged) {
                    const data = await this.postJson(`/api/servers/${info.id}/transport-params`, transportOnly(now));
                    restarted = !!data.restarted;
                }
                window.Ui.closeDrawer();
                await this.loadServers();
                if (transportChanged) {
                    const fresh = (this.lastServers || []).find((s) => s.id === info.id);
                    const stale = (fresh?.clients || []).filter((c) => c.config_outdated).length;
                    this.showTempMessage(`${info.name} saved${restarted ? ' and restarted' : ''}. ${stale
                        ? `${stale} client config${stale === 1 ? '' : 's'} to re-import.`
                        : 'Every client config matches its device.'}`, stale ? 'info' : 'success');
                } else if (networkChanged) {
                    this.showTempMessage(`Networking for ${info.name} updated`, 'success');
                }
            },
        };

        this.openFormDrawer({
            title: `<span class="text-purple-700 dark:text-[#c084fc]">${safe(info.name)}</span> settings`,
            sub: `${safe(info.protocol)} · <span class="font-mono">${safe(info.interface)}</span> · ${running ? 'running' : 'stopped'}`,
            body: `
                ${this.formSection('Overview', `
                    <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-1.5 text-sm">
                        ${row('Port', `${safe(info.port)}/udp`)}${row('Subnet', safe(info.subnet))}${row('Server IP', safe(info.server_ip))}
                        ${row('Public IP', safe(info.public_ip))}${row('DNS', safe((info.dns || []).join(', ')))}${row('MTU', safe(info.mtu))}
                        ${row('Clients', safe(info.clients_count), false)}
                        <dt class="text-gray-500 dark:text-[#94a3b8]">Public key</dt>
                        <dd class="flex items-center gap-1 min-w-0"><span id="s-publicKey" class="font-mono text-xs truncate text-gray-800 dark:text-[#e5e7eb]">${safe(info.public_key)}</span>
                            <button type="button" class="icon-btn icon-btn-sm" aria-label="Copy public key"
                                data-action="copy-public-key">${window.Ui.icon('copy', 'w-3.5 h-3.5')}</button></dd>
                    </dl>`)}
                ${this.formSection('Networking', `
                    <div class="flex flex-col">
                        ${this.formSwitch('s-nat', 'NAT (masquerade)', 'Clients reach the internet through this host.', info.enable_nat)}
                        ${this.formSwitch('s-lan', 'Block private LAN ranges', 'Clients cannot reach private networks behind the server.', info.block_lan_cidrs)}
                    </div>
                    <p class="hint">Applied to iptables immediately while the server is running.</p>`)}
                ${this.formSection('Protocol and transport', `
                    <div id="impact">${this.formCallout(this.escapeHtml(defaultImpact))}</div>
                    ${this.transportFieldsHtml(info.protocol, info.transport_params || {})}`,
                    '<button type="button" class="btn btn-secondary btn-sm" data-action="randomize">Randomize</button>')}`,
            primaryLabel: 'No changes',
            left: `<button type="button" class="btn btn-ghost" data-action="raw-config" data-server="${safe(info.id)}">${window.Ui.icon('code')}Full config</button>`,
            ctx,
        });
    }

    // --- add and edit client ------------------------------------------------------------
    addClient(serverId) {
        const server = (this.lastServers || []).find((s) => String(s.id) === String(serverId));
        if (!server) {
            this.showTempMessage('Server not found', 'error');
            return;
        }
        const safe = (v) => this.escapeHtml(v ?? '');
        const clients = server.clients || [];
        const defaults = server.client_defaults || {};
        const name = () => (document.getElementById('c-name')?.value || '').trim();

        this.openFormDrawer({
            title: 'Add client',
            sub: `to <span class="text-purple-700 dark:text-[#c084fc]">${safe(server.name)}</span>`,
            body: `
                ${this.formSection('Client', `
                    ${this.formField('c-name', 'Name', '', { placeholder: 'e.g. Phone' })}
                    <div>
                        <label class="label" for="c-copyFrom">Start from</label>
                        <select id="c-copyFrom" class="field">
                            <option value="">Server defaults</option>
                            ${clients.map((c) => `<option value="${safe(c.id)}">Copy ${safe(c.name)} (${safe(c.client_ip)})</option>`).join('')}
                        </select>
                    </div>`)}
                ${this.formSection('Client-side parameters', this.clientFieldsHtml(server, defaults))}`,
            primaryLabel: 'Create client',
            ctx: {
                serverId,
                collect: () => ({ name: name(), ...this.collectClientForm() }),
                localErrors: () => (name() ? [] : ['Enter a client name.']),
                validateBody: () => ({ server_id: serverId, client_params: this.collectClientForm() }),
                submit: async () => {
                    const data = await this.postJson(`/api/servers/${serverId}/clients`, {
                        name: name(),
                        client_params: this.collectClientForm(),
                        copy_from_client_id: document.getElementById('c-copyFrom')?.value || null,
                    });
                    window.Ui.closeDrawer();
                    this.showTempMessage(`${data.client?.name || name()} added with ${data.client?.client_ip || 'a new address'}`, 'success');
                    this.loadServers();
                },
            },
        });
        this.autosizeClientParamTextareas('c', 200);
    }

    // "Start from" another client: copy its parameters into the fields.
    fillClientFromCopy(clientId) {
        const serverId = this.drawerCtx?.serverId;
        const server = (this.lastServers || []).find((s) => s.id === serverId);
        const source = (server?.clients || []).find((c) => c.id === clientId);
        const params = source ? source.client_params || {} : server?.client_defaults || {};
        ['Jc', 'Jmin', 'Jmax', ...AmneziaApp.I_PARAM_KEYS, ...AmneziaApp.AWG3_CLIENT_PARAM_KEYS].forEach((k) => {
            const el = document.getElementById(`c-${k}`);
            if (!el) return;
            el.value = params[k] ?? '';
            if (el.tagName === 'TEXTAREA') this.autosizeTextarea(el, 200);
        });
    }

    showClientParamsModal(serverId, clientId) {
        const server = (this.lastServers || []).find((s) => String(s.id) === String(serverId));
        const client = (server?.clients || []).find((c) => c.id === clientId);
        if (!server || !client) {
            this.showTempMessage('Client not found', 'error');
            return;
        }
        const safe = (v) => this.escapeHtml(v ?? '');
        const traffic = (server.traffic || {})[clientId] || {};
        const age = window.ServerUi.since(traffic.latest_handshake_seconds);
        const seen = traffic.endpoint && age ? `handshake ${safe(age)}` : 'not connected';
        const params = { ...(server.client_defaults || {}), ...(client.client_params || {}) };
        const ctx = {
            snapshot: null,
            saveLabel: 'Save changes',
            collect: () => this.collectClientForm(),
            validateBody: () => ({ server_id: serverId, client_params: this.collectClientForm() }),
            submit: async () => {
                const data = await this.postJson(`/api/servers/${serverId}/clients/${clientId}/client-params`, {
                    client_params: this.collectClientForm(),
                });
                window.Ui.closeDrawer();
                const saved = data.client || {};
                const issued = !!saved.config_issued_at || saved.config_outdated;
                this.showTempMessage(!issued ? `${client.name} saved.`
                    : saved.config_outdated ? `${client.name} saved. Its config changed: re-import it on the device.`
                        : `${client.name} saved. Its config matches the device again.`, saved.config_outdated ? 'info' : 'success');
                this.loadServers();
            },
        };
        this.openFormDrawer({
            title: `<span class="text-sky-700 dark:text-[#7dd3fc]">${safe(client.name)}</span>`,
            sub: `<span class="font-mono">${safe(client.client_ip)}</span> · ${safe(server.name)} · ${seen}`,
            body: `
                ${this.formSection('Client-side parameters', this.clientFieldsHtml(server, params))}
                ${this.formCallout('Only this client changes. Re-import its QR code or .conf on the device afterwards.')}`,
            primaryLabel: 'No changes',
            left: `<button type="button" class="btn btn-ghost" data-action="client-qr" data-server="${safe(serverId)}" data-client="${safe(clientId)}">${window.Ui.icon('qr')}QR code</button>`,
            ctx,
        });
        this.autosizeClientParamTextareas('c', 200);
    }

    showServerConfig(serverId) {
        this.apiFetch(`/api/servers/${serverId}/info`)
            .then((response) => response.json())
            .then((info) => this.openServerSettings(info))
            .catch((error) => {
                console.error('Error fetching server info:', error);
                this.showTempMessage('Error loading server configuration: ' + error.message, 'error');
            });
    }
}

// Install onto AmneziaApp, so `this` is the app in every method.
Object.getOwnPropertyNames(FormUi.prototype)
    .filter((name) => name !== 'constructor')
    .forEach((name) => {
        AmneziaApp.prototype[name] = FormUi.prototype[name];
    });

window.FormUi = FormUi;
