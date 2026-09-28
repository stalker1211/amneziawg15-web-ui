// AmneziaWG Web UI - the views, shown as centred dialogs: a client's QR code, a
// server's daemon log and its raw .conf.
//
// Methods of AmneziaApp, installed onto its prototype at the bottom of this file
// (like forms.js), so `this` is the app. Every interpolated value is escaped.
class ModalUi {
    // The code box every view uses; it follows the theme like the rest of the page.
    codeBoxHtml(text, id) {
        return `<pre id="${id}" class="max-h-[60vh] overflow-auto rounded-lg border border-gray-300 bg-gray-100 text-gray-900 dark:border-gray-900 dark:bg-gray-900 dark:text-gray-100 p-4 text-xs leading-5 font-mono whitespace-pre">${this.escapeHtml(text)}</pre>`;
    }

    // Showing the QR hands the config out: the server records it (POST .../issued),
    // which clears the client's Re-import flag.
    async showClientQRCode(serverId, clientId) {
        const server = (this.lastServers || []).find((s) => s.id === serverId) || {};
        const client = (this.serverClients.get(serverId) || server.clients || []).find((c) => c.id === clientId) || {};
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
                <div class="rounded-xl bg-white p-3 border border-gray-200 dark:border-[#475569]">
                    <div id="qrcode" class="flex items-center justify-center"></div>
                </div>
                <p class="text-xs text-center text-gray-500 dark:text-[#94a3b8]">Scan with the AmneziaVPN or AmneziaWG app. The code holds this client's private key.</p>
                <div class="flex flex-wrap justify-center gap-2">
                    <button type="button" class="btn btn-primary" data-qr="copy">${icon('copy')}Copy config</button>
                    <button type="button" class="btn btn-secondary" data-qr="download">${icon('download')}Download .conf</button>
                    <button type="button" class="btn btn-ghost" data-qr="image">${icon('image')}QR image</button>
                </div>
                <details class="help w-full text-sm text-gray-600 dark:text-[#cbd5e1]">
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

// Install onto AmneziaApp so call sites and inline onclick= handlers work.
Object.getOwnPropertyNames(ModalUi.prototype)
    .filter((name) => name !== 'constructor')
    .forEach((name) => {
        AmneziaApp.prototype[name] = ModalUi.prototype[name];
    });

window.ModalUi = ModalUi;
