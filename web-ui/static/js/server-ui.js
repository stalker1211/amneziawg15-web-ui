// AmneziaWG Web UI - Server rendering helpers
class ServerUi {
    static renderServersHtml({ servers, escapeHtml, formatProbeTimestamp, renderServerClients }) {
        const safe = (value) => escapeHtml(value);
        const geoSuffix = (geo, countryCode) => {
            const geoText = String(geo || '').trim();
            const cc = String(countryCode || '').trim().toUpperCase();
            if (!geoText && !cc) return '';

            const flag = /^[A-Z]{2}$/.test(cc)
                ? String.fromCodePoint(0x1F1E6 + (cc.charCodeAt(0) - 65), 0x1F1E6 + (cc.charCodeAt(1) - 65))
                : '';

            const details = cc && geoText ? `${cc} / ${geoText}` : (cc || geoText);
            const prefix = flag ? `${safe(flag)} ` : '';
            return ` <span class="text-gray-500">(${prefix}${safe(details)})</span>`;
        };

        if (!Array.isArray(servers) || servers.length === 0) {
            return `
                <div class="text-center py-8 text-gray-500">
                    No servers created yet. Create your first server above.
                </div>
            `;
        }

        return servers.map((server) => {
            const probe = (server && typeof server === 'object') ? server.egress_probe : null;
            const hasExternalAccess = !!(probe && probe.external_ip);
            const finalExternalIp = hasExternalAccess ? probe.external_ip : 'No external access';
            const checkedAt = probe?.checked_at ? formatProbeTimestamp(probe.checked_at) : '';
            const egressGeo = geoSuffix(probe?.external_ip_geo, probe?.external_ip_geo_country_code);

            return `
            <div class="server-card bg-white rounded-lg shadow-md p-6">
                <div class="flex justify-between items-center mb-4">
                    <div class="flex items-start gap-3">
                        <div class="w-9 h-9 flex items-center justify-center bg-blue-100 text-blue-600 rounded-full mt-0.5">
                            <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                                <rect x="3" y="4" width="18" height="6" rx="1.5" stroke-width="2"></rect>
                                <rect x="3" y="14" width="18" height="6" rx="1.5" stroke-width="2"></rect>
                                <circle cx="7" cy="7" r="0.9" fill="currentColor" stroke="none"></circle>
                                <circle cx="7" cy="17" r="0.9" fill="currentColor" stroke="none"></circle>
                            </svg>
                        </div>
                        <div>
                            <h3 class="text-lg font-semibold">${safe(server.name)}</h3>
                            <p class="text-sm text-gray-600">
                                ID: ${safe(server.id)} | Port: ${safe(server.port)} | Subnet: ${safe(server.subnet)}
                                ${server.obfuscation_enabled ? '| 🔒 Obfuscated' : ''}
                                | NAT: ${server.enable_nat ? 'On' : 'Off'}
                                | LAN Block: ${server.block_lan_cidrs ? 'On' : 'Off'}
                            </p>
                            <p class="text-sm text-gray-500 flex items-center gap-2">Client's egress IP: <span class="egress-probe-value font-medium ${hasExternalAccess ? 'text-gray-700' : 'text-red-400'}">${safe(finalExternalIp)}</span>${egressGeo}${checkedAt ? ` <span class="egress-probe-ts text-xs text-gray-400">(${safe(checkedAt)})</span>` : ''}
                            <button onclick="amneziaApp.probeServerEgressIp('${server.id}', this)"
                                class="egress-refresh-btn w-7 h-7 rounded-full bg-white/80 text-blue-600 hover:text-blue-700 shadow-sm border border-blue-200/70 hover:border-blue-300/80 backdrop-blur flex items-center justify-center transition"
                                title="Refresh final external IP">
                                <span class="text-[12px] leading-none">↻</span>
                            </button>
                            </p>
                        </div>
                    </div>
                    <div class="flex items-center space-x-2">
                        <span class="px-3 py-1 rounded-full text-sm ${
                            server.status === 'running' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                        }">${server.status}</span>
                        <button onclick="amneziaApp.deleteServer('${server.id}')"
                            class="inline-flex items-center gap-1.5 rounded-full bg-red-50/70 text-red-600 hover:text-red-700 border border-red-200/70 hover:border-red-300 px-3 py-1.5 text-xs font-medium shadow-sm transition">
                            <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                                <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path>
                                <line x1="10" y1="11" x2="10" y2="17"></line>
                                <line x1="14" y1="11" x2="14" y2="17"></line>
                            </svg>
                            Delete
                        </button>
                    </div>
                </div>
                <div class="space-x-2 mb-4">
                    <button onclick="amneziaApp.startServer('${server.id}')" class="btn-pill bg-green-500 text-white px-3.5 py-1.5 rounded-full text-sm hover:bg-green-600">
                        Start
                    </button>
                    <button onclick="amneziaApp.stopServer('${server.id}')" class="btn-pill bg-red-500 text-white px-3.5 py-1.5 rounded-full text-sm hover:bg-red-600">
                        Stop
                    </button>
                    <button onclick="amneziaApp.addClient('${server.id}')" class="btn-pill bg-blue-500 text-white px-3.5 py-1.5 rounded-full text-sm hover:bg-blue-600">
                        Add Client
                    </button>
                    <button onclick="amneziaApp.showServerConfig('${server.id}')" class="btn-pill bg-purple-500 text-white px-3.5 py-1.5 rounded-full text-sm hover:bg-purple-600">
                        Show Config
                    </button>
                    <button onclick="amneziaApp.showServerLogs('${server.id}', '${server.interface || ''}')" class="btn-pill bg-slate-700 text-white px-3.5 py-1.5 rounded-full text-sm hover:bg-slate-800">
                        View Logs
                    </button>
                </div>
                <div id="clients-${server.id}">
                    ${renderServerClients(server.id, server.clients || [])}
                </div>
            </div>
        `;
        }).join('');
    }

    static renderServerClientsHtml({ serverId, clients, traffic, escapeHtml, isClientActiveFromTraffic, countryCodeToFlagEmoji }) {
        if (clients.length === 0) {
            return '<p class="text-gray-500 text-sm">No clients yet.</p>';
        }

        const safe = (value) => escapeHtml(value);

        return `
            <h4 class="font-medium mb-2">Clients (${clients.length}):</h4>
            <div class="space-y-2">
                ${clients.map((client) => {
                    const clientTraffic = traffic[client.id] || { received: '0 B', sent: '0 B' };
                    const isActive = isClientActiveFromTraffic(clientTraffic);
                    const endpoint = clientTraffic.endpoint;
                    const geo = clientTraffic.geo;
                    const geoCountryCode = clientTraffic.geo_country_code;
                    const flag = geoCountryCode ? countryCodeToFlagEmoji(geoCountryCode) : '';
                    const flagPrefix = flag ? `${safe(flag)} ` : '';
                    const latestHandshake = clientTraffic.latest_handshake;
                    const endpointLine = endpoint && endpoint !== '(none)'
                        ? `${flagPrefix}${safe(endpoint)}${geo ? ` <span class=\"text-gray-500\">(${safe(geo)})</span>` : ''}`
                        : '<span class="text-gray-400">(not connected)</span>';
                    const handshakeLine = (endpoint && endpoint !== '(none)' && latestHandshake)
                        ? `<span class="text-xs text-gray-500">latest handshake: ${safe(latestHandshake)}</span>`
                        : '';
                    const rxFlashClass = clientTraffic._rx_changed ? 'traffic-flash' : '';
                    const txFlashClass = clientTraffic._tx_changed ? 'traffic-flash' : '';

                    return `
                    <div class="flex justify-between items-center bg-gray-50 p-3 rounded-lg hover:bg-gray-100 transition-colors duration-200">
                        <div class="flex items-center">
                            <div class="w-8 h-8 flex items-center justify-center bg-blue-100 text-blue-600 rounded-full mr-3">
                                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/>
                                </svg>
                            </div>
                            <div class="flex items-center space-x-2">
                                <div class="flex flex-col">
                                    <span class="font-medium flex items-center gap-2 flex-wrap">
                                        <span class="inline-block w-2 h-2 rounded-full ${isActive ? 'bg-green-500' : 'bg-red-500'}" title="${isActive ? 'active (≤ 5 minutes)' : 'inactive'}"></span>
                                        <span>${safe(client.name)} <span class="text-sm text-gray-600">(${safe(client.client_ip)})</span></span>
                                        <span class="text-xs text-gray-500">
                                            <span class="traffic-arrow ${rxFlashClass}" aria-label="received">
                                                <svg class="traffic-arrow-icon traffic-arrow-icon-rx" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                                                    <path fill-rule="evenodd" d="M10 15a1 1 0 01-.707-.293l-5-5a1 1 0 111.414-1.414L9 11.586V3a1 1 0 112 0v8.586l3.293-3.293a1 1 0 111.414 1.414l-5 5A1 1 0 0110 15z" clip-rule="evenodd" />
                                                </svg>
                                            </span>
                                            ${safe(clientTraffic.received)}
                                            &nbsp;
                                            <span class="traffic-arrow ${txFlashClass}" aria-label="sent">
                                                <svg class="traffic-arrow-icon traffic-arrow-icon-tx" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                                                    <path fill-rule="evenodd" d="M10 5a1 1 0 01.707.293l5 5a1 1 0 11-1.414 1.414L11 8.414V17a1 1 0 11-2 0V8.414L5.707 11.707a1 1 0 01-1.414-1.414l5-5A1 1 0 0110 5z" clip-rule="evenodd" />
                                                </svg>
                                            </span>
                                            ${safe(clientTraffic.sent)}
                                        </span>
                                    </span>
                                    <span class="text-xs text-gray-600">${endpointLine}</span>
                                    ${handshakeLine}
                                </div>
                            </div>
                        </div>
                        <div class="flex space-x-2">
                                <button onclick="amneziaApp.showClientQRCode('${serverId}', '${client.id}')"
                                    class="btn-pill bg-gradient-to-r from-purple-500 to-indigo-500 hover:from-purple-600 hover:to-indigo-600 text-white px-3.5 py-1.5 rounded-full text-sm font-medium transition-all duration-200 shadow hover:shadow-md flex items-center"
                                    title="Show QR Code">
                                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v1m6 11h2m-6 0h-2v4m0-11v3m0 0h.01M12 12h4.01M16 20h4M4 12h4m12 0h.01M5 8h2a1 1 0 001-1V5a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1zm12 0h2a1 1 0 001-1V5a1 1 0 00-1-1h-2a1 1 0 00-1 1v2a1 1 0 001 1zM5 20h2a1 1 0 001-1v-2a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1z"/>
                                </svg>
                                QR Code
                            </button>
                                <button onclick="amneziaApp.showClientIParamsModal('${serverId}', '${client.id}')"
                                    class="btn-pill bg-gradient-to-r from-sky-500 to-blue-600 hover:from-sky-600 hover:to-blue-700 text-white px-3.5 py-1.5 rounded-full text-sm font-medium transition-all duration-200 shadow hover:shadow-md flex items-center"
                                    title="Edit I1–I5 (client-only)">
                                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l9.586-9.586z"/>
                                </svg>
                                I1–I5
                            </button>
                                <button onclick="amneziaApp.downloadClientConfig('${serverId}', '${client.id}')"
                                    class="btn-pill bg-gradient-to-r from-blue-500 to-cyan-500 hover:from-blue-600 hover:to-cyan-600 text-white px-3.5 py-1.5 rounded-full text-sm font-medium transition-all duration-200 shadow hover:shadow-md flex items-center">
                                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                                </svg>
                                Download
                            </button>
                                <button onclick="amneziaApp.deleteClient('${serverId}', '${client.id}')"
                                    class="btn-pill bg-gradient-to-r from-red-500 to-pink-500 hover:from-red-600 hover:to-pink-600 text-white px-3.5 py-1.5 rounded-full text-sm font-medium transition-all duration-200 shadow hover:shadow-md flex items-center">
                                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                                </svg>
                                Delete
                            </button>
                        </div>
                    </div>
                    `;
                }).join('')}
            </div>
        `;
    }
}

window.ServerUi = ServerUi;
