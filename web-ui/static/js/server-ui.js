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
        const probeServiceSuffix = (serviceName) => {
            const serviceText = String(serviceName || '').trim();
            if (!serviceText) return '';
            return ` <span class="egress-probe-service text-xs text-gray-400">via ${safe(serviceText)}</span>`;
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
            const serviceSuffix = probeServiceSuffix(probe?.service_name || probe?.service);

            return `
            <div class="server-card bg-white rounded-lg shadow-md p-6 ${server.status !== 'running' ? 'opacity-60' : ''}">
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
                            <div class="flex items-center gap-3">
                                <h3 class="text-lg font-semibold"><span class="text-purple-600">${safe(server.name)}</span></h3>
                                <label class="relative inline-flex items-center cursor-pointer" title="${server.status === 'running' ? 'Stop server' : 'Start server'}">
                                    <input type="checkbox" class="sr-only peer" ${server.status === 'running' ? 'checked' : ''}
                                        onchange="amneziaApp.toggleServer('${server.id}', this.checked)">
                                    <div class="w-9 h-5 bg-gray-300 peer-focus:outline-none peer-focus:ring-2 peer-focus:ring-green-300 rounded-full peer peer-checked:after:translate-x-full rtl:peer-checked:after:-translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-green-500"></div>
                                </label>
                                <span class="text-sm font-bold ${server.status === 'running' ? 'text-green-600' : 'text-red-500'}">${server.status === 'running' ? 'Running' : 'Stopped'}</span>
                            </div>
                            <p class="text-sm text-gray-600">
                                ID: ${safe(server.id)} | Port: ${safe(server.port)} | Subnet: ${safe(server.subnet)}
                                | Protocol: ${safe(server.protocol || 'AWG 1.5')}
                                | NAT: ${server.enable_nat ? 'On' : 'Off'}
                                | LAN Block: ${server.block_lan_cidrs ? 'On' : 'Off'}
                            </p>
                            <p class="text-sm text-gray-500 flex items-center gap-2">Client's egress IP: <span class="egress-probe-value font-medium ${hasExternalAccess ? 'text-gray-700' : 'text-red-400'}">${safe(finalExternalIp)}</span>${egressGeo}${checkedAt ? ` <span class="egress-probe-ts text-xs text-gray-400">(${safe(checkedAt)})</span>` : ''}${serviceSuffix}
                            <button onclick="amneziaApp.probeServerEgressIp('${server.id}', this)"
                                class="egress-refresh-btn w-7 h-7 rounded-full bg-white/80 text-blue-600 hover:text-blue-700 shadow-sm border border-blue-200/70 hover:border-blue-300/80 backdrop-blur flex items-center justify-center transition"
                                title="Refresh final external IP">
                                <span class="text-[12px] leading-none">↻</span>
                            </button>
                            </p>
                        </div>
                    </div>
                    <div class="flex items-center gap-1.5">
                        <button onclick="amneziaApp.addClient('${server.id}')"
                            class="w-8 h-8 rounded-full bg-blue-50 text-blue-600 hover:bg-blue-100 border border-blue-200/70 hover:border-blue-300 flex items-center justify-center transition shadow-sm"
                            title="Add Client">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M16 21v-2a4 4 0 00-4-4H5a4 4 0 00-4-4v2"></path>
                                <circle cx="8.5" cy="7" r="4"></circle>
                                <line x1="20" y1="8" x2="20" y2="14"></line>
                                <line x1="23" y1="11" x2="17" y2="11"></line>
                            </svg>
                        </button>
                        <button onclick="amneziaApp.showServerConfig('${server.id}')"
                            class="w-8 h-8 rounded-full bg-purple-50 text-purple-600 hover:bg-purple-100 border border-purple-200/70 hover:border-purple-300 flex items-center justify-center transition shadow-sm"
                            title="Edit Config">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <circle cx="12" cy="12" r="3"></circle>
                                <path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"></path>
                            </svg>
                        </button>
                        <button onclick="amneziaApp.showServerLogs('${server.id}', '${server.interface || ''}')"
                            class="w-8 h-8 rounded-full bg-slate-50 text-slate-600 hover:bg-slate-100 border border-slate-200/70 hover:border-slate-300 flex items-center justify-center transition shadow-sm"
                            title="View Logs">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"></path>
                                <polyline points="14 2 14 8 20 8"></polyline>
                                <line x1="16" y1="13" x2="8" y2="13"></line>
                                <line x1="16" y1="17" x2="8" y2="17"></line>
                            </svg>
                        </button>
                        <span class="w-px h-5 bg-gray-200 mx-0.5"></span>
                        <button onclick="amneziaApp.deleteServer('${server.id}')"
                            class="w-8 h-8 rounded-full bg-red-50 text-red-500 hover:bg-red-100 border border-red-200/70 hover:border-red-300 flex items-center justify-center transition shadow-sm"
                            title="Delete Server">
                            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="3 6 5 6 21 6"></polyline>
                                <path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"></path>
                                <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"></path>
                            </svg>
                        </button>
                    </div>
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
                    const isSuspended = !!client.suspended;
                    const rowOpacity = isSuspended ? 'opacity-50' : '';
                    const statusDotClass = isSuspended ? 'bg-gray-400' : (isActive ? 'bg-green-500' : 'bg-red-500');
                    const statusDotTitle = isSuspended ? 'suspended' : (isActive ? 'active (≤ 5 minutes)' : 'inactive');
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
                    <div class="flex justify-between items-center bg-gray-50 p-3 rounded-lg hover:bg-gray-100 transition-colors duration-200 ${rowOpacity}">
                        <div class="flex items-center">
                            <div class="w-8 h-8 flex items-center justify-center bg-blue-100 text-blue-600 rounded-full mr-3">
                                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/>
                                </svg>
                            </div>
                            <div class="flex items-center space-x-2">
                                <div class="flex flex-col">
                                    <span class="font-medium flex items-center gap-2 flex-wrap">
                                        <span class="inline-block w-2 h-2 rounded-full ${statusDotClass}" title="${statusDotTitle}"></span>
                                        <span><span class="text-sky-600">${safe(client.name)}</span> <span class="text-sm text-gray-600">(${safe(client.client_ip)})</span></span>
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
                        <div class="flex items-center gap-1.5">
                                <label class="relative inline-flex items-center cursor-pointer" title="${isSuspended ? 'Reactivate client' : 'Suspend client'}">
                                    <input type="checkbox" class="sr-only peer" ${isSuspended ? '' : 'checked'}
                                        onchange="amneziaApp.toggleClientSuspend('${serverId}', '${client.id}')">
                                    <div class="w-8 h-[18px] bg-amber-300 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-[14px] rtl:peer-checked:after:-translate-x-[14px] peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:start-[2px] after:bg-white after:border-gray-300 after:border after:rounded-full after:h-[14px] after:w-[14px] after:transition-all peer-checked:bg-green-500"></div>
                                </label>
                                <button onclick="amneziaApp.showClientQRCode('${serverId}', '${client.id}')"
                                    class="inline-flex items-center gap-1 rounded-full bg-amber-50 text-amber-600 hover:bg-amber-100 border border-amber-200/70 hover:border-amber-300 px-2.5 py-1 text-xs font-medium shadow-sm transition"
                                    title="Show QR Code">
                                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2">
                                    <path stroke-linecap="round" stroke-linejoin="round" d="M12 4v1m6 11h2m-6 0h-2v4m0-11v3m0 0h.01M12 12h4.01M16 20h4M4 12h4m12 0h.01M5 8h2a1 1 0 001-1V5a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1zm12 0h2a1 1 0 001-1V5a1 1 0 00-1-1h-2a1 1 0 00-1 1v2a1 1 0 001 1zM5 20h2a1 1 0 001-1v-2a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1z"/>
                                </svg>
                                QR
                            </button>
                                <button onclick="amneziaApp.showClientParamsModal('${serverId}', '${client.id}')"
                                    class="inline-flex items-center gap-1 rounded-full bg-sky-50 text-sky-600 hover:bg-sky-100 border border-sky-200/70 hover:border-sky-300 px-2.5 py-1 text-xs font-medium shadow-sm transition"
                                    title="Edit client config parameters">
                                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <path d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l9.586-9.586z"/>
                                </svg>
                                Edit
                            </button>
                                <span class="w-px h-4 bg-gray-200"></span>
                                <button onclick="amneziaApp.deleteClient('${serverId}', '${client.id}')"
                                    class="w-7 h-7 rounded-full bg-red-50 text-red-500 hover:bg-red-100 border border-red-200/70 hover:border-red-300 flex items-center justify-center transition shadow-sm"
                                    title="Delete client">
                                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                    <polyline points="3 6 5 6 21 6"></polyline>
                                    <path d="M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2"></path>
                                    <path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"></path>
                                </svg>
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
