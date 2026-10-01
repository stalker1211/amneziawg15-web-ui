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

    formSwitch(id, title, description, checked, disabled = false) {
        return `
            <label class="flex items-start justify-between gap-4 py-1.5 ${disabled ? 'opacity-60' : 'cursor-pointer'}" for="${id}">
                <span>
                    <span class="block text-sm font-medium text-gray-900 dark:text-[#e5e7eb]">${title}</span>
                    <span class="block text-xs text-gray-700 dark:text-[#bac5d4]">${description}</span>
                </span>
                <span class="switch mt-0.5"><input id="${id}" type="checkbox" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''}><span class="track"></span></span>
            </label>`;
    }

    formSelect(id, label, options, selected, { hint = '', disabled = false } = {}) {
        const safe = (v) => this.escapeHtml(v ?? '');
        return `
            <div>
                <label class="label" for="${id}">${label}</label>
                <select id="${id}" class="field" ${disabled ? 'disabled' : ''}>${options.map(([value, text]) =>
                    `<option value="${safe(value)}"${String(value) === String(selected) ? ' selected' : ''}>${safe(text)}</option>`).join('')}</select>
                ${hint ? `<p class="hint">${hint}</p>` : ''}
            </div>`;
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
            <details class="help text-xs text-gray-800 dark:text-[#d7dee9]">
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
                <label class="label" for="t-HeaderProtectionKey">Header protection key <span class="font-normal text-gray-600 dark:text-[#98a6ba]">optional</span></label>
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
            <p class="text-xs text-gray-700 dark:text-[#bac5d4]">From the server, the same for every client:
                <span class="text-gray-800 dark:text-[#d7dee9]">${safe(protocol)} · ${safe(this.formatTransportParamsSummary(protocol, server.transport_params || {}))}</span></p>
            <div class="grid grid-cols-3 gap-3">
                ${this.formField('c-Jc', 'Jc', params.Jc ?? 8, { type: 'number', mono: true, hint: 'junk packets' })}
                ${this.formField('c-Jmin', 'Jmin', params.Jmin ?? 8, { type: 'number', mono: true, hint: 'bytes' })}
                ${this.formField('c-Jmax', 'Jmax', params.Jmax ?? 80, { type: 'number', mono: true, hint: 'bytes' })}
            </div>
            <details class="help text-sm text-gray-800 dark:text-[#d7dee9]"${anySignature ? ' open' : ''}>
                <summary class="font-medium">Signature packets I1-I5 <span class="font-normal text-gray-600 dark:text-[#98a6ba]">optional</span></summary>
                <div class="mt-3 flex flex-col gap-3">
                    <p class="text-xs text-gray-700 dark:text-[#bac5d4]">Sent before every handshake. Generate fills them with packets shaped like
                        QUIC or DNS, or random ones; Save keeps them. Tags: <span class="font-mono">&lt;b 0x…&gt; &lt;t&gt; &lt;r n&gt; &lt;rc n&gt; &lt;rd n&gt;</span>, checked as you type.
                        For other shapes (TLS, DTLS, SIP) try
                        <a href="https://architect.vai-rice.space" target="_blank" rel="noopener noreferrer" class="text-purple-700 hover:underline dark:text-[#c084fc]">AmneziaWG Architect</a>
                        and paste its I1-I5 here.</p>
                    ${this.signatureGeneratorHtml()}
                    ${AmneziaApp.I_PARAM_KEYS.map((k) => `
                        <div><label class="label" for="c-${k}">${k}</label>
                        <textarea id="c-${k}" rows="1" class="field field-area" placeholder="e.g. &lt;b 0xc6000000010843&gt;&lt;r 16&gt;">${safe(params[k] || '')}</textarea></div>`).join('')}
                </div>
            </details>
            ${P.supportsAwg3(protocol) ? `
            <div class="flex flex-col gap-3">
                <p class="text-xs font-medium text-gray-800 dark:text-[#d7dee9]">AWG 3.x timing and padding
                    <span class="font-normal text-gray-600 dark:text-[#98a6ba]">a number or a range like 22-30; empty keeps the default</span></p>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    ${AmneziaApp.AWG3_CLIENT_PARAM_KEYS.map((k) => this.formField(`c-${k}`, k, params[k] || '', { mono: true, placeholder: 'default' })).join('')}
                </div>
            </div>` : ''}`;
    }

    // Generate for I1-I5: a profile (page_config's signatureProfiles), a host for the
    // ones that ask for it, and the server's answer (POST /api/generate).
    signatureGeneratorHtml() {
        const profiles = window.AppConfig.signatureProfiles || [];
        const safe = (v) => this.escapeHtml(v ?? '');
        return `
            <div class="flex flex-wrap items-end gap-2">
                <div><label class="label" for="c-iProfile">Shape</label>
                    <select id="c-iProfile" class="field">${profiles.map((p) => `<option value="${safe(p.id)}">${safe(p.label)}</option>`).join('')}</select></div>
                <div id="c-iHostBox" class="min-w-0 flex-1"${profiles[0]?.host ? '' : ' hidden'}><label class="label" for="c-iHost">Host</label>
                    <input id="c-iHost" type="text" class="field font-mono" placeholder="empty: a common name"></div>
                <button type="button" class="btn btn-secondary" data-action="generate-signatures">Generate</button>
            </div>
            <p id="c-iNote" class="hint" hidden></p>`;
    }

    // The profile picked: show the host field if it asks for one.
    onSignatureProfileChange(profileId) {
        const profile = (window.AppConfig.signatureProfiles || []).find((p) => p.id === profileId);
        const box = document.getElementById('c-iHostBox');
        if (box) box.hidden = !profile?.host;
        const note = document.getElementById('c-iNote');
        if (note) note.hidden = true;
    }

    // Generate: I1-I5 from the server, shaped like the profile, into the fields. Nothing
    // is saved; the drawer checks them like any edit and Save keeps them.
    async generateSignaturePackets() {
        const ctx = this.drawerCtx;
        const profile = document.getElementById('c-iProfile')?.value;
        if (!ctx?.serverId || !profile) return;
        const host = document.getElementById('c-iHost');
        try {
            const data = await this.postJson('/api/generate', {
                server_id: ctx.serverId,
                signature_profile: profile,
                host: (host?.value || '').trim(),
            });
            if (ctx !== this.drawerCtx) return;
            Object.entries(data.signature_packets || {}).forEach(([k, v]) => {
                const el = document.getElementById(`c-${k}`);
                if (!el) return;
                el.value = v;
                this.autosizeTextarea(el, 200);
            });
            if (host && data.signature_host) host.value = data.signature_host;
            const note = document.getElementById('c-iNote');
            if (note) {
                note.textContent = (data.signature_notes || []).join(' ');
                note.hidden = !note.textContent;
            }
            this.scheduleDrawerCheck();
        } catch (error) {
            this.showTempMessage(`Could not generate packets: ${error.message}`, 'error');
        }
    }

    // What the device routes through the tunnel: split tunnelling per client.
    allowedIpsFieldHtml(value) {
        return this.formField('c-allowed_ips', 'Allowed IPs', value, {
            mono: true,
            hint: 'What this device sends through the tunnel. <span class="font-mono">0.0.0.0/0</span> is all '
                + 'IPv4; a narrower list (e.g. <span class="font-mono">192.168.1.0/24</span>) is split tunnelling. '
                + 'Adding <span class="font-mono">::/0</span> sends IPv6 in too, where the server drops it '
                + '(a Linux device with IPv6 switched off cannot bring that up).',
        });
    }

    allowedIps() {
        return (document.getElementById('c-allowed_ips')?.value || '').trim();
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
            if (e.type === 'change' && e.target.id === 'c-iProfile') this.onSignatureProfileChange(e.target.value);
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
        if (box) box.innerHTML = '<p class="text-sm text-gray-600 dark:text-[#98a6ba]">Checking…</p>';
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
        return `${html}<p class="text-xs text-gray-600 dark:text-[#98a6ba]">Checked by the server as you type.</p>`;
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
        const servers = this.lastServers || [];
        const builtIn = window.AppConfig.defaults;
        // The newest server is the template for what a deployment keeps the same: MTU,
        // DNS, NAT and LAN blocking (NAT off where the router routes the tunnel's
        // subnet itself). The built-in values serve only the first server.
        const newest = servers.reduce((a, s) => (!a || (s.created_at || 0) >= (a.created_at || 0) ? s : a), null);
        const newestDns = newest ? [].concat(newest.dns || []).join(', ') : '';
        const base = newest
            ? { mtu: newest.mtu, dns: newestDns || builtIn.dns, enable_nat: newest.enable_nat, block_lan_cidrs: newest.block_lan_cidrs }
            : builtIn;
        const usedPorts = new Set(servers.map((s) => Number(s.port)));
        let port = Number(builtIn.port);
        while (usedPorts.has(port)) port += 1;
        const usedThird = new Set(servers.map((s) => (/^10\.10\.(\d+)\./.exec(s.subnet || '') || [])[1]).filter(Boolean).map(Number));
        let third = 0;
        while (usedThird.has(third) && third < 255) third += 1;

        const protocol = window.Protocols.DEFAULT;
        const mtu = base.mtu;
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
            endpoint_host: (document.getElementById('f-endpoint_host')?.value || '').trim(),
        });
        const transportParams = () => {
            const { protocol: _protocol, ...rest } = this.collectTransportForm();
            return rest;
        };

        this.openFormDrawer({
            title: 'New server',
            sub: newest
                ? `MTU, DNS, NAT and LAN blocking copied from <span class="text-purple-700 dark:text-[#c084fc]">${this.escapeHtml(newest.name)}</span>; clients are added afterwards.`
                : 'Creates an interface and its config; clients are added afterwards.',
            body: `
                ${this.formSection('Server', `
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        ${this.formField('f-name', 'Name', '', { cls: 'sm:col-span-2', placeholder: 'e.g. Home NL' })}
                        ${this.formField('f-port', 'Port (UDP)', port, { type: 'number', mono: true })}
                        ${this.formField('f-subnet', 'Subnet', `10.10.${third}.0/24`, { mono: true })}
                        ${this.formField('f-mtu', 'MTU', mtu, { type: 'number', mono: true, hint: '1280-1440; 1420 suits most links, 1280 the most restrictive ones.' })}
                        ${this.formField('f-dns', 'DNS servers', base.dns, { mono: true, hint: 'Comma-separated, pushed to clients.' })}
                        ${this.formField('f-endpoint_host', 'Endpoint host', '', { cls: 'sm:col-span-2', mono: true, placeholder: `detected: ${this.currentPublicIp || 'public IP'}`,
                            hint: 'Optional: a DNS name (e.g. dynamic DNS) or IPv4 that clients dial. With a name, a new public IP needs no re-import.' })}
                    </div>`)}
                ${this.formSection('Networking', `
                    <div class="flex flex-col">
                        ${this.formSwitch('f-autostart', 'Start after creating', 'Brings the interface up right away.', true)}
                        ${this.formSwitch('f-nat', 'NAT (masquerade)', 'Clients reach the internet through this host.', base.enable_nat !== false)}
                        ${this.formSwitch('f-lan', 'Block private LAN ranges', 'Clients cannot reach 10/8, 172.16/12 or 192.168/16 behind the server, nor this panel.', base.block_lan_cidrs !== false)}
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
        const row = (k, v, mono = true) => `<dt class="text-gray-700 dark:text-[#bac5d4]">${k}</dt><dd class="${mono ? 'font-mono' : ''} text-gray-900 dark:text-[#e5e7eb] min-w-0 truncate">${v}</dd>`;
        const endpointHost = () => (document.getElementById('s-endpoint_host')?.value || '').trim();
        const collect = () => ({
            nat: !!document.getElementById('s-nat')?.checked,
            lan: !!document.getElementById('s-lan')?.checked,
            host: endpointHost(),
            ...this.collectTransportForm(),
        });
        const transportOnly = (state) => { const { nat: _n, lan: _l, host: _h, ...rest } = state; return rest; };

        const ctx = {
            snapshot: null,
            saveLabel: 'Save changes',
            mtu: info.mtu,
            mount: () => this.toggleProtocolFields(info.protocol, 't-'),
            collect,
            validateBody: () => {
                const { protocol, ...transport } = this.collectTransportForm();
                return { server_id: info.id, protocol, transport_params: transport, endpoint_host: endpointHost() };
            },
            onResult: (result) => {
                const impact = document.getElementById('impact');
                if (!impact) return;
                const changed = result.configs_changed || 0;
                const after = result.outdated_after || 0;
                const now = result.outdated_now || 0;
                const n = clients.length;
                const configs = `${changed} client config${changed === 1 ? '' : 's'}`;
                // An endpoint host alone changes client configs without a restart.
                const before = ctx.snapshot ? JSON.parse(ctx.snapshot) : null;
                const transportTouched = !before || JSON.stringify(transportOnly(collect())) !== JSON.stringify(transportOnly(before));
                const restart = running && transportTouched ? ' and restarts the server' : '';
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
                const hostChanged = now.host !== before.host;
                let restarted = false;
                if (hostChanged) {
                    await this.postJson(`/api/servers/${info.id}/endpoint-host`, { endpoint_host: now.host });
                }
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
                if (transportChanged || hostChanged) {
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
                        ${row('DNS', safe((info.dns || []).join(', ')))}${row('MTU', safe(info.mtu))}
                        ${row('Clients', safe(info.clients_count), false)}
                        <dt class="text-gray-700 dark:text-[#bac5d4]">Public key</dt>
                        <dd class="flex items-center gap-1 min-w-0"><span id="s-publicKey" class="font-mono text-xs truncate text-gray-900 dark:text-[#e5e7eb]">${safe(info.public_key)}</span>
                            <button type="button" class="icon-btn icon-btn-sm" aria-label="Copy public key"
                                data-action="copy-public-key">${window.Ui.icon('copy', 'w-3.5 h-3.5')}</button></dd>
                    </dl>`)}
                ${this.formSection('Endpoint', `
                    ${this.formField('s-endpoint_host', 'Endpoint host', info.endpoint_host || '', { mono: true, placeholder: `detected: ${info.public_ip || ''}`,
                        hint: 'What client configs dial: a DNS name (e.g. dynamic DNS) or IPv4; empty uses the detected public IP. Changing it means every client re-imports; with a name, a new public IP needs none.'
                            // With a host set the placeholder is hidden: say what the name should resolve to.
                            + (info.endpoint_host && info.public_ip ? ` Detected public IP: <span class="font-mono">${safe(info.public_ip)}</span>.` : '') })}`)}
                ${this.formSection('Networking', `
                    <div class="flex flex-col">
                        ${this.formSwitch('s-nat', 'NAT (masquerade)', 'Clients reach the internet through this host.', info.enable_nat)}
                        ${this.formSwitch('s-lan', 'Block private LAN ranges', 'Clients cannot reach private networks behind the server, nor this panel.', info.block_lan_cidrs)}
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
                ${this.formSection('Routing', this.allowedIpsFieldHtml('0.0.0.0/0'))}
                ${this.formSection('Client-side parameters', this.clientFieldsHtml(server, defaults))}`,
            primaryLabel: 'Create client',
            ctx: {
                serverId,
                collect: () => ({ name: name(), allowed_ips: this.allowedIps(), ...this.collectClientForm() }),
                localErrors: () => (name() ? [] : ['Enter a client name.']),
                validateBody: () => ({ server_id: serverId, client_params: this.collectClientForm(), allowed_ips: this.allowedIps() }),
                submit: async () => {
                    const data = await this.postJson(`/api/servers/${serverId}/clients`, {
                        name: name(),
                        allowed_ips: this.allowedIps(),
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
            serverId,
            snapshot: null,
            saveLabel: 'Save changes',
            collect: () => ({ allowed_ips: this.allowedIps(), ...this.collectClientForm() }),
            validateBody: () => ({ server_id: serverId, client_params: this.collectClientForm(), allowed_ips: this.allowedIps() }),
            submit: async () => {
                const data = await this.postJson(`/api/servers/${serverId}/clients/${clientId}/client-params`, {
                    client_params: this.collectClientForm(),
                    allowed_ips: this.allowedIps(),
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
                ${this.formSection('Routing', this.allowedIpsFieldHtml(client.allowed_ips || '0.0.0.0/0'))}
                ${this.formSection('Client-side parameters', this.clientFieldsHtml(server, params))}
                ${this.formCallout('Only this client changes. Re-import its QR code or .conf on the device afterwards.')}`,
            primaryLabel: 'No changes',
            left: `<button type="button" class="btn btn-ghost" data-action="client-qr" data-server="${safe(serverId)}" data-client="${safe(clientId)}">${window.Ui.icon('qr')}QR code</button>`,
            ctx,
        });
        this.autosizeClientParamTextareas('c', 200);
    }

    // --- panel settings (⚙ in the header) -------------------------------------------------
    // GET /api/settings, checked through /api/validate, saved by POST /api/settings. A
    // field whose environment variable is set is read-only ("set by LOG_LEVEL").
    async openSettings() {
        let data;
        try {
            data = await this.getJson('/api/settings');
        } catch (error) {
            this.showTempMessage(`Could not load the settings: ${error.message}`, 'error');
            return;
        }
        const values = data.values;
        const access = data.access;
        const banner = document.getElementById('passwordBanner');
        if (banner) banner.hidden = !access.password_is_default;
        const safe = (v) => this.escapeHtml(v ?? '');
        const pinned = (k) => data.sources[k] === 'env';
        const hint = (k, text = '') => (pinned(k) ? this.pinnedHint(data.env[k]) : text);
        const toggle = (k, title, text) => this.formSwitch(`s-${k}`, title, hint(k, text), values[k], pinned(k));
        const choose = (k, label, options, text) => this.formSelect(`s-${k}`, label, options, values[k], { hint: hint(k, text), disabled: pinned(k) });
        const userPinned = access.user_source === 'env';
        const passwordPinned = access.password_source === 'env';
        const row = (k, v) => `<dt class="text-gray-700 dark:text-[#bac5d4]">${k}</dt><dd class="font-mono text-xs text-gray-900 dark:text-[#e5e7eb] min-w-0 break-all">${safe(v || '—')}</dd>`;

        const collectSettings = () => {
            const out = {};
            Object.keys(values).forEach((k) => {
                const el = document.getElementById(`s-${k}`);
                if (!el || el.disabled) return;
                out[k] = el.type === 'checkbox' ? el.checked : (el.type === 'number' ? this.numberOrText(el.id) : el.value.trim());
            });
            return out;
        };
        const collectAccess = () => {
            const user = (document.getElementById('s-user')?.value || '').trim();
            const password = document.getElementById('s-password')?.value || '';
            return {
                ...(!userPinned && user !== access.user ? { user } : {}),
                ...(!passwordPinned && password ? { password } : {}),
            };
        };
        const ctx = {
            snapshot: null,
            saveLabel: 'Save settings',
            collect: () => ({ settings: collectSettings(), access: collectAccess() }),
            validateBody: () => ({ settings: collectSettings(), access: collectAccess() }),
            localErrors: () => {
                const errors = [];
                const changing = Object.keys(collectAccess()).length > 0;
                const password = document.getElementById('s-password')?.value || '';
                if (password && password !== (document.getElementById('s-password2')?.value || '')) {
                    errors.push('The new password and its repeat differ.');
                }
                if (changing && !(document.getElementById('s-current')?.value)) {
                    errors.push('Enter the current password to change the user name or password.');
                }
                return errors;
            },
            submit: async () => {
                const credential = collectAccess();
                const current = document.getElementById('s-current')?.value || '';
                const saved = await this.postJson('/api/settings', {
                    settings: collectSettings(),
                    ...(Object.keys(credential).length ? { access: { ...credential, current_password: current } } : {}),
                });
                window.Ui.closeDrawer();
                this.applySettings(saved);
                if (saved.access_changed) {
                    // Hand the new credential to this browser, so it keeps working
                    // without a sign-in prompt; other browsers ask for it.
                    if (await this.api.rememberCredentials(saved.access.user, credential.password ?? current)) {
                        this.showTempMessage('Sign-in changed. This browser uses the new one; others will ask for it.', 'success');
                        this.loadServers();
                    } else {
                        this.showTempMessage('Sign-in changed. Sign in again with the new one.', 'info');
                        setTimeout(() => window.location.reload(), 1500);
                    }
                    return;
                }
                this.showTempMessage(saved.changed.length ? 'Settings saved' : 'Nothing changed', 'success');
                if (saved.restart_needed) await this.offerDaemonRestart(saved.restart_needed);
            },
        };

        this.openFormDrawer({
            title: 'Panel settings',
            sub: 'Stored with the servers; a field set by an environment variable is read-only.',
            body: `
                ${this.formSection('Access', `
                    ${access.password_is_default ? this.formCallout(passwordPinned
                        ? 'The password is still <span class="font-mono">changeme</span>, and <span class="font-mono">NGINX_PASSWORD</span> sets it. '
                            + 'Give the variable a new value and restart the container, or remove it, restart, and change the password here.'
                        : 'This panel still signs in with the default password <span class="font-mono">changeme</span>. Set a new one below.') : ''}
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        ${this.formField('s-user', 'User name', access.user, { attrs: `autocomplete="username" ${userPinned ? 'disabled' : ''}`,
                            hint: userPinned ? this.pinnedHint('NGINX_USER') : '' })}
                        ${this.formField('s-current', 'Current password', '', { type: 'password', attrs: `autocomplete="current-password" ${access.editable ? '' : 'disabled'}`,
                            hint: access.editable ? 'Needed to change the user name or password.' : 'Nothing to change here: both are set by environment variables.' })}
                        ${this.formField('s-password', 'New password', '', { type: 'password', attrs: `autocomplete="new-password" ${passwordPinned ? 'disabled' : ''}`,
                            hint: passwordPinned ? this.pinnedHint('NGINX_PASSWORD') : 'At least 8 characters.' })}
                        ${this.formField('s-password2', 'Repeat new password', '', { type: 'password', attrs: `autocomplete="new-password" ${passwordPinned ? 'disabled' : ''}` })}
                    </div>
                    <p class="hint">Saving a new one signs other browsers out; this one keeps working.</p>`)}
                ${this.formSection('Logging', `
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                        ${choose('awg_log_level', 'VPN daemon', [['off', 'Off'], ['error', 'Errors only'], ['debug', 'Debug']],
                            'Debug adds every handshake and "unknown type" packets: the trace of a client with outdated parameters. Applies when a server starts.')}
                        ${choose('log_level', 'Web panel', [['ERROR', 'Errors'], ['WARNING', 'Warnings'], ['INFO', 'Info'], ['DEBUG', 'Debug']],
                            'Applies at once.')}
                    </div>`)}
                ${this.formSection('Privacy', `
                    <div class="flex flex-col">
                        ${toggle('geoip', 'Show where addresses are', 'Looks up country and city of endpoint, public and egress IPs at ipapi.co.')}
                    </div>`)}
                ${this.formSection('About', `
                    <dl class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-1.5 text-sm">
                        ${row('Panel', data.about.build_label)}${row('Daemon', data.about.daemon)}${row('Tools', data.about.tools)}
                    </dl>
                    <div class="flex flex-col gap-2">
                        <div class="flex items-center justify-between gap-3">
                            <span class="label mb-0">Firewall rules</span>
                            <button type="button" class="btn btn-ghost btn-sm" data-action="iptables-check">${window.Ui.icon('refresh', 'w-3.5 h-3.5')}Reload</button>
                        </div>
                        <div id="s-iptables" class="flex flex-col gap-3 text-xs"></div>
                    </div>`)}`,
            primaryLabel: 'No changes',
            ctx,
        });
        this.checkIptables();
    }

    // "Set by X": a field an environment variable pins, in a colour that stands out.
    pinnedHint(variable) {
        return `<span class="hint-pinned">${window.Ui.icon('lock', 'inline w-3 h-3 mr-1 -mt-0.5')}Set by <span class="font-mono">${this.escapeHtml(variable)}</span>; `
            + 'remove the variable (and restart) to change it here.</span>';
    }

    // After a save: the banner follows the credential.
    applySettings(saved) {
        const banner = document.getElementById('passwordBanner');
        if (banner) banner.hidden = !saved.access.password_is_default;
    }

    // amneziawg-go reads its log level once, when it starts.
    async offerDaemonRestart(count) {
        const ok = await window.Ui.confirm({
            title: `Restart ${count} running server${count === 1 ? '' : 's'} now?`,
            body: 'The VPN daemon reads its log level when it starts. Connected clients reconnect within about 15 seconds. '
                + 'Otherwise the new level applies at each server\'s next start.',
            confirmLabel: 'Restart now',
            cancelLabel: 'Later',
            danger: false,
        });
        if (!ok) return;
        try {
            const result = await this.postJson('/api/settings/restart-servers', {});
            this.showTempMessage(result.failed.length ? `Could not restart ${result.failed.join(', ')}`
                : `Restarted ${result.restarted.length} server${result.restarted.length === 1 ? '' : 's'}`, result.failed.length ? 'error' : 'success');
        } catch (error) {
            this.showTempMessage(`Restart failed: ${error.message}`, 'error');
        }
        this.loadServers();
    }

    // About → Firewall rules: each server's tagged iptables rules as they are now.
    async checkIptables() {
        const box = document.getElementById('s-iptables');
        if (!box) return;
        const servers = this.lastServers || [];
        if (!servers.length) {
            box.innerHTML = '<p class="text-gray-800 dark:text-[#d7dee9]">No servers yet.</p>';
            return;
        }
        box.innerHTML = '<p class="text-gray-700 dark:text-[#bac5d4]">Reading the rules…</p>';
        const safe = (v) => this.escapeHtml(v ?? '');
        const blocks = await Promise.all(servers.map(async (server) => {
            try {
                const data = await this.getJson(`/api/system/iptables-test?server_id=${encodeURIComponent(server.id)}`);
                const n = data.rules.length;
                // A stopped server has none, by design; a running one should have them all.
                const ok = data.running ? n === data.expected && !data.errors.length : n === 0;
                const state = data.running ? `${n} of ${data.expected} rules` : (n ? `stopped, but ${n} rules left` : 'stopped, no rules');
                const tone = ok ? 'text-green-800 dark:text-[#86efac]' : 'text-red-700 dark:text-[#fca5a5]';
                return `<div class="flex flex-col gap-1">
                    <p><span class="font-medium text-gray-900 dark:text-[#f1f5f9]">${safe(server.name)}</span>
                        <span class="font-mono text-gray-700 dark:text-[#bac5d4]">${safe(data.interface)}</span>
                        · <span class="${tone}">${safe(state)}</span>${data.errors.length ? ` · <span class="text-red-700 dark:text-[#fca5a5]">${safe(data.errors.join('; '))}</span>` : ''}</p>
                    ${n ? this.codeBoxHtml(data.rules.map((r) => r.replace(/ -m comment --comment "awg:[^"]+"/, '')).join('\n'), `rules-${safe(server.id)}`) : ''}
                </div>`;
            } catch (error) {
                return `<p class="text-red-700 dark:text-[#fca5a5]">${safe(server.name)}: ${safe(error.message)}</p>`;
            }
        }));
        box.innerHTML = blocks.join('');
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
