// AmneziaWG Web UI - the views: QR code, server logs and the raw server config.
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
    displayRawConfigModal(config) {
        const safe = (v) => this.escapeHtml(v);
        const modalHtml = `
            <div id="rawConfigModal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
                <div class="relative top-10 mx-auto p-5 border w-11/12 md:w-3/4 lg:w-2/3 shadow-lg rounded-md bg-white dark:bg-[#1f2937]">
                    <div class="mt-3">
                        <div class="flex justify-between items-center mb-4">
                            <h3 class="text-xl font-bold text-gray-900 dark:text-[#f1f5f9]">Raw Configuration: ${safe(config.server_name)}</h3>
                            <button onclick="amneziaApp.closeModal()" class="text-gray-400 hover:text-gray-600">
                                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                                </svg>
                            </button>
                        </div>

                        <div class="mb-4">
                            <div class="flex justify-between items-center mb-2">
                                <span class="text-sm text-gray-600 dark:text-[#e0e7ff]">Config path: ${safe(config.config_path)}</span>
                                <button onclick="amneziaApp.copyToClipboard('${btoa(JSON.stringify(config))}')"
                                    class="btn-pill bg-gray-500 text-white px-3 py-1 rounded text-xs hover:bg-gray-600">
                                    Copy JSON
                                </button>
                            </div>
                            <pre class="bg-gray-900 dark:bg-[#0f172a] text-green-400 dark:text-[#86efac] p-4 rounded text-sm overflow-x-auto max-h-96 overflow-y-auto">${safe(config.config_content)}</pre>
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
                <div class="relative top-12 mx-auto p-5 border w-11/12 md:w-4/5 lg:w-3/4 shadow-lg rounded-md bg-white dark:bg-[#1f2937]">
                    <div class="mt-2">
                        <div class="flex justify-between items-center mb-3">
                            <div>
                                <h3 class="text-xl font-bold text-gray-900 dark:text-[#f1f5f9]">Server Logs</h3>
                                <div class="text-xs text-gray-500 dark:text-[#94a3b8]">Interface: <span class="font-mono">${safe(interfaceName || 'unknown')}</span></div>
                            </div>
                            <button onclick="amneziaApp.closeModal()" class="text-gray-400 hover:text-gray-600">
                                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                                </svg>
                            </button>
                        </div>

                        <div class="flex items-center justify-between mb-2">
                            <div class="text-xs text-gray-500 dark:text-[#94a3b8]">Auto-refresh: 10s</div>
                            <div class="flex items-center gap-2">
                                <button id="logsManualRefresh"
                                    class="w-7 h-7 rounded-full bg-white/80 text-blue-600 dark:text-[#93c5fd] hover:text-blue-700 shadow-sm border border-blue-200/70 hover:border-blue-300/80 backdrop-blur flex items-center justify-center transition dark:bg-gray-800/80 dark:text-blue-300 dark:border-gray-700/80 dark:hover:border-blue-400/60"
                                        title="Refresh logs">
                                    <span class="text-[12px] leading-none">↻</span>
                                </button>
                                <div id="logsLastUpdate" class="text-xs text-gray-400">Last update: —</div>
                            </div>
                        </div>

                        <pre id="serverLogContent" class="bg-gray-900 dark:bg-[#0f172a] text-emerald-200 p-3 rounded text-xs overflow-x-auto max-h-[60vh] overflow-y-auto"></pre>

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
                <div class="relative p-8 border w-11/12 md:w-3/4 lg:w-2/3 xl:w-1/2 shadow-2xl rounded-2xl bg-white dark:bg-[#1f2937]">
                    <div class="flex flex-col">
                        <div class="flex justify-between items-center w-full mb-6">
                            <h3 class="text-xl font-bold text-gray-900 dark:text-[#f1f5f9]">QR Code for Client: <span class="text-sky-600 dark:text-[#7dd3fc]">${safeClientName}</span></h3>
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
                                <div class="bg-white dark:bg-[#1f2937] p-6 rounded-xl border-2 border-gray-100 shadow-inner">
                                    <div id="qrcode" class="flex justify-center mb-4"></div>
                                    <p class="text-center text-sm text-gray-500 dark:text-[#94a3b8]">Scan with WireGuard app</p>
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
                                        <label class="block text-sm font-medium text-gray-700 dark:text-[#d1d5db]">Configuration</label>
                                        <button onclick="amneziaApp.copyConfigText()"
                                            class="btn-pill bg-gradient-to-r from-blue-500 to-blue-600 hover:from-blue-600 hover:to-blue-700 text-white px-4 py-1.5 rounded-lg text-sm font-medium transition-colors duration-200 shadow hover:shadow-md">
                                            Copy Config
                                        </button>
                                    </div>
                                    <pre id="configText" class="bg-gray-900 dark:bg-[#0f172a] text-green-400 dark:text-[#86efac] p-4 rounded text-sm font-mono overflow-x-auto max-h-96 overflow-y-auto whitespace-pre-wrap">Loading configuration...</pre>
                                </div>
                            </div>
                        </div>
                        
                        <div class="flex justify-end space-x-4 w-full pt-6 border-t border-gray-200 dark:border-[#1f2937]">
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
