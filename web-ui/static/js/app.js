// AmneziaWG Web UI - Main Application JavaScript
class AmneziaApp {
    constructor() {
        this.socket = null;
        this.lastServers = [];
        this.serverClients = new Map();
        this.lastTrafficByServer = new Map();
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
            this.setupEventListeners();
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

    getApiToken() {
        try {
            return String(localStorage.getItem('amnezia_api_token') || '').trim();
        } catch (_) {
            return '';
        }
    }

    setApiToken(token) {
        try {
            const v = String(token || '').trim();
            if (v) localStorage.setItem('amnezia_api_token', v);
            else localStorage.removeItem('amnezia_api_token');
        } catch (_) {
            // ignore
        }
    }

    buildApiHeaders(existingHeaders) {
        const headers = new Headers(existingHeaders || {});
        const token = this.getApiToken();
        if (token) headers.set('X-API-Token', token);
        return headers;
    }

    async apiFetch(input, init = {}) {
        const nextInit = { ...(init || {}) };
        nextInit.headers = this.buildApiHeaders(nextInit.headers);

        let resp = await fetch(input, nextInit);

        if (resp.status === 401) {
            const entered = prompt('API token required. Paste API_TOKEN value:', this.getApiToken());
            if (entered && String(entered).trim()) {
                this.setApiToken(String(entered).trim());
                const retryInit = { ...(init || {}) };
                retryInit.headers = this.buildApiHeaders(retryInit.headers);
                resp = await fetch(input, retryInit);
            }
        }

        return resp;
    }

    _filenameFromContentDisposition(cd, fallback) {
        const value = String(cd || '');
        const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(value);
        const raw = (m && (m[1] || m[2])) ? (m[1] || m[2]) : '';
        try {
            const decoded = raw ? decodeURIComponent(raw) : '';
            return decoded || fallback;
        } catch (_) {
            return raw || fallback;
        }
    }

    async downloadBlob(url, fallbackFilename) {
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

        const blob = await resp.blob();
        const filename = this._filenameFromContentDisposition(resp.headers.get('Content-Disposition'), fallbackFilename);

        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        a.download = filename || 'download';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
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

        // Obfuscation toggle
        const obfuscationCheckbox = this.getElement('enableObfuscation');
        if (obfuscationCheckbox) {
            obfuscationCheckbox.addEventListener('change', (e) => {
                this.toggleObfuscationParams(e.target.checked);
            });
            // Initialize visibility
            this.toggleObfuscationParams(obfuscationCheckbox.checked);
        }

        // Form validation listeners
        this.setupFormValidation();
    }

    openCreateServerModal() {
        const modal = this.getElement('createServerModal');
        if (!modal) return;
        modal.classList.remove('hidden');

        const nameElement = this.getElement('serverName');
        if (nameElement) nameElement.focus();
    }

    closeCreateServerModal() {
        const modal = this.getElement('createServerModal');
        if (!modal) return;
        modal.classList.add('hidden');
    }

    setupFormValidation() {
        const nameElement = this.getElement('serverName');
        const portElement = this.getElement('serverPort');
        const subnetElement = this.getElement('serverSubnet');
        
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
    }

    hideError(errorId) {
        const errorElement = this.getElement(errorId);
        if (errorElement) {
            errorElement.classList.add('hidden');
        }
    }

    toggleObfuscationParams(show) {
        const obfuscationParams = this.getElement('obfuscationParams');
        if (obfuscationParams) {
            obfuscationParams.style.display = show ? 'block' : 'none';
        }
    }

    setupSocketIO() {
        // Use the exact same URL as the current page to avoid CORS issues
        const socketUrl = window.location.origin;

        this.socket = io(socketUrl, {
            path: '/socket.io',
            transports: ['websocket'],
        });

        this.socket.on('connect', () => {
            console.log("✅ Connected to server via WebSocket");
            this.updateStatus('Connected to AmneziaWG Web UI');
        });

        this.socket.on('disconnect', () => {
            console.log("❌ Disconnected from server");
            this.updateStatus('Disconnected from AmneziaWG Web UI');
        });

        this.socket.on('connect_error', (error) => {
            console.error("❌ WebSocket connection error:", error);
            this.updateStatus('Connection error - retrying...');
        });

        this.socket.on('status', (data) => {
            console.log("Status update:", data);
            if (data.public_ip) {
                this.updatePublicIp(data.public_ip);
            }
        });

        this.socket.on('server_status', (data) => {
            console.log("Server status update:", data);
            this.loadServers();
        });

        this.socket.on('traffic_update', (data) => {
            this.updateServerTraffic(data.server_id, data.traffic);
        });
    }

    updateStatus(message) {
        const statusElement = this.getElement('status');
        if (statusElement) {
            statusElement.textContent = message;
        }
    }

    updatePublicIp(ip) {
        const publicIpElement = this.getElement('publicIp');
        if (publicIpElement) {
            publicIpElement.textContent = ip;
        }
    }

    refreshPublicIp() {
        this.apiFetch('/api/system/refresh-ip')
            .then(response => response.json())
            .then(data => {
                this.updatePublicIp(data.public_ip);
                this.loadServers();
            })
            .catch(error => {
                console.error('Error refreshing IP:', error);
            });
    }

    generateRandomParams() {
        // Generate random values within recommended ranges
        const jcElement = this.getElement('paramJc');
        const s1Element = this.getElement('paramS1');
        const s2Element = this.getElement('paramS2');
        const h1Element = this.getElement('paramH1');
        const h2Element = this.getElement('paramH2');
        const h3Element = this.getElement('paramH3');
        const h4Element = this.getElement('paramH4');
        
        if (jcElement) jcElement.value = Math.floor(Math.random() * 9) + 4; // 4-12
        if (s1Element) s1Element.value = Math.floor(Math.random() * 136) + 15; // 15-150
        if (s2Element) s2Element.value = Math.floor(Math.random() * 136) + 15; // 15-150
        
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

    validateObfuscationParamsJS(params, mtu) {
        let errors = [];

        // Jmin < Jmax ≤ mtu
        if (!(params.Jmin < params.Jmax && params.Jmax <= mtu)) {
            errors.push(`Jmin (${params.Jmin}) must be less than Jmax (${params.Jmax}), and Jmax ≤ MTU (${mtu})`);
        }
        // Jmax > Jmin < mtu
        if (!(params.Jmax > params.Jmin && params.Jmin < mtu)) {
            errors.push(`Jmax (${params.Jmax}) must be greater than Jmin (${params.Jmin}), and Jmin < MTU (${mtu})`);
        }
        // S1 ≤ (mtu - 148) and in the range from 15 to 150
        if (!(params.S1 <= (mtu - 148) && params.S1 >= 15 && params.S1 <= 150)) {
            errors.push(`S1 (${params.S1}) must be in [15, 150] and ≤ (MTU - 148) (${mtu - 148})`);
        }
        // S2 ≤ (mtu - 92) and in the range from 15 to 150
        if (!(params.S2 <= (mtu - 92) && params.S2 >= 15 && params.S2 <= 150)) {
            errors.push(`S2 (${params.S2}) must be in [15, 150] and ≤ (MTU - 92) (${mtu - 92})`);
        }
        // S1 + 56 ≠ S2
        if (params.S1 + 56 === params.S2) {
            errors.push(`S1 + 56 (${params.S1 + 56}) must not equal S2 (${params.S2})`);
        }

        return errors;
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
        const obfuscationElement = this.getElement('enableObfuscation');
        const autoStartElement = this.getElement('autoStart');

        const formData = {
            name: nameElement ? nameElement.value.trim() : 'New Server',
            port: portElement ? parseInt(portElement.value) : 51820,
            subnet: subnetElement ? subnetElement.value : '10.0.0.0/24',
            mtu: mtuElement ? parseInt(mtuElement.value) : 1420,
            dns: dnsElement ? dnsElement.value.trim() : '8.8.8.8,1.1.1.1',
            obfuscation: obfuscationElement ? obfuscationElement.checked : true,
            auto_start: autoStartElement ? autoStartElement.checked : true
        };

        console.log("Form data:", formData);

        // Add obfuscation parameters if enabled
        if (formData.obfuscation) {
            formData.obfuscation_params = {
                Jc: parseInt(this.getElement('paramJc')?.value || '8'),
                Jmin: parseInt(this.getElement('paramJmin')?.value || '8'),
                Jmax: parseInt(this.getElement('paramJmax')?.value || '80'),
                S1: parseInt(this.getElement('paramS1')?.value || '50'),
                S2: parseInt(this.getElement('paramS2')?.value || '60'),
                H1: parseInt(this.getElement('paramH1')?.value || '1000'),
                H2: parseInt(this.getElement('paramH2')?.value || '2000'),
                H3: parseInt(this.getElement('paramH3')?.value || '3000'),
                H4: parseInt(this.getElement('paramH4')?.value || '4000'),
                I1: (this.getElement('paramI1')?.value || '').trim(),
                I2: (this.getElement('paramI2')?.value || '').trim(),
                I3: (this.getElement('paramI3')?.value || '').trim(),
                I4: (this.getElement('paramI4')?.value || '').trim(),
                I5: (this.getElement('paramI5')?.value || '').trim(),
            };

            const obfErrors = this.validateObfuscationParamsJS(formData.obfuscation_params, formData.mtu);
            if (obfErrors.length > 0) {
                this.showError('obfuscationError', obfErrors.join(' '));
                return;
            } else {
                this.hideError('obfuscationError');
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
                this.updatePublicIp(data.public_ip);
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

        const safe = (v) => this.escapeHtml(v);

        if (servers.length === 0) {
            serversList.innerHTML = `
                <div class="text-center py-8 text-gray-500">
                    No servers created yet. Create your first server above.
                </div>
            `;
            return;
        }

        serversList.innerHTML = servers.map(server => `
            <div class="bg-white rounded-lg shadow-md p-6">
                <div class="flex justify-between items-center mb-4">
                    <div>
                        <h3 class="text-lg font-semibold">${safe(server.name)}</h3>
                        <p class="text-sm text-gray-600">
                            ID: ${safe(server.id)} | Port: ${safe(server.port)} | Subnet: ${safe(server.subnet)}
                            ${server.obfuscation_enabled ? '| 🔒 Obfuscated' : ''}
                        </p>
                        <p class="text-sm text-gray-500">Public IP: ${safe(server.public_ip)}</p>
                    </div>
                    <div class="flex items-center space-x-2">
                        <span class="px-3 py-1 rounded-full text-sm ${
                            server.status === 'running' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                        }">${server.status}</span>
                        <button onclick="amneziaApp.deleteServer('${server.id}')" class="text-red-500 hover:text-red-700">
                            🗑️ Delete
                        </button>
                    </div>
                </div>
                <div class="space-x-2 mb-4">
                    <button onclick="amneziaApp.startServer('${server.id}')" class="bg-green-500 text-white px-3 py-1 rounded hover:bg-green-600">
                        Start
                    </button>
                    <button onclick="amneziaApp.stopServer('${server.id}')" class="bg-red-500 text-white px-3 py-1 rounded hover:bg-red-600">
                        Stop
                    </button>
                    <button onclick="amneziaApp.addClient('${server.id}')" class="bg-blue-500 text-white px-3 py-1 rounded hover:bg-blue-600">
                        Add Client
                    </button>
                    <button onclick="amneziaApp.showServerConfig('${server.id}')" class="bg-purple-500 text-white px-3 py-1 rounded hover:bg-purple-600">
                        Show Config
                    </button>
                </div>
                <div id="clients-${server.id}">
                    ${this.renderServerClients(server.id, server.clients || [])}
                </div>
            </div>
        `).join('');

        // Load clients for each server
        servers.forEach(server => {
            this.loadServerClients(server.id);
        });
    }

    renderServerClients(serverId, clients, traffic = {}) {
        if (clients.length === 0) {
            return '<p class="text-gray-500 text-sm">No clients yet.</p>';
        }

        const safe = (v) => this.escapeHtml(v);
        
        return `
            <h4 class="font-medium mb-2">Clients (${clients.length}):</h4>
            <div class="space-y-2">
                ${clients.map(client => {
                    const clientTraffic = traffic[client.id] || {received: '0 B', sent: '0 B'};
                    const isActive = this.isClientActiveFromTraffic(clientTraffic);
                    const endpoint = clientTraffic.endpoint;
                    const geo = clientTraffic.geo;
                    const geoCountryCode = clientTraffic.geo_country_code;
                    const flag = geoCountryCode ? this.countryCodeToFlagEmoji(geoCountryCode) : '';
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
                                    <span class="font-medium flex items-center gap-2">
                                        <span class="inline-block w-2 h-2 rounded-full ${isActive ? 'bg-green-500' : 'bg-red-500'}" title="${isActive ? 'active (≤ 5 minutes)' : 'inactive'}"></span>
                                        <span>${safe(client.name)} <span class="text-sm text-gray-600">(${safe(client.client_ip)})</span></span>
                                    </span>
                                    <span class="text-xs text-gray-600">${endpointLine}</span>
                                    ${handshakeLine}
                                </div>
                                <span class="text-xs text-gray-500 ml-6" style="margin-left: 0.5cm;">
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
                            </div>
                        </div>
                        <div class="flex space-x-2">
                            <button onclick="amneziaApp.showClientQRCode('${serverId}', '${client.id}')"
                                    class="bg-gradient-to-r from-purple-500 to-indigo-500 hover:from-purple-600 hover:to-indigo-600 text-white px-3 py-1.5 rounded-lg text-sm font-medium transition-all duration-200 shadow hover:shadow-md flex items-center"
                                    title="Show QR Code">
                                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v1m6 11h2m-6 0h-2v4m0-11v3m0 0h.01M12 12h4.01M16 20h4M4 12h4m12 0h.01M5 8h2a1 1 0 001-1V5a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1zm12 0h2a1 1 0 001-1V5a1 1 0 00-1-1h-2a1 1 0 00-1 1v2a1 1 0 001 1zM5 20h2a1 1 0 001-1v-2a1 1 0 00-1-1H5a1 1 0 00-1 1v2a1 1 0 001 1z"/>
                                </svg>
                                QR Code
                            </button>
                            <button onclick="amneziaApp.showClientIParamsModal('${serverId}', '${client.id}')"
                                    class="bg-gradient-to-r from-sky-500 to-blue-600 hover:from-sky-600 hover:to-blue-700 text-white px-3 py-1.5 rounded-lg text-sm font-medium transition-all duration-200 shadow hover:shadow-md flex items-center"
                                    title="Edit I1–I5 (client-only)">
                                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l9.586-9.586z"/>
                                </svg>
                                I1–I5
                            </button>
                            <button onclick="amneziaApp.downloadClientConfig('${serverId}', '${client.id}')"
                                    class="bg-gradient-to-r from-blue-500 to-cyan-500 hover:from-blue-600 hover:to-cyan-600 text-white px-3 py-1.5 rounded-lg text-sm font-medium transition-all duration-200 shadow hover:shadow-md flex items-center">
                                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                                </svg>
                                Download
                            </button>
                            <button onclick="amneziaApp.deleteClient('${serverId}', '${client.id}')"
                                    class="bg-gradient-to-r from-red-500 to-pink-500 hover:from-red-600 hover:to-pink-600 text-white px-3 py-1.5 rounded-lg text-sm font-medium transition-all duration-200 shadow hover:shadow-md flex items-center">
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

    startServer(serverId) {
        this.apiFetch(`/api/servers/${serverId}/start`, { method: 'POST' })
            .then(() => this.loadServers())
            .catch(error => {
                console.error('Error starting server:', error);
                alert('Error starting server: ' + error.message);
            });
    }

    stopServer(serverId) {
        this.apiFetch(`/api/servers/${serverId}/stop`, { method: 'POST' })
            .then(() => this.loadServers())
            .catch(error => {
                console.error('Error stopping server:', error);
                alert('Error stopping server: ' + error.message);
            });
    }

    addClient(serverId) {
        const clientName = prompt('Enter client name:');
        if (clientName) {
            this.apiFetch(`/api/servers/${serverId}/clients`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ name: clientName })
            })
            .then(() => this.loadServers())
            .catch(error => {
                console.error('Error adding client:', error);
                alert('Error adding client: ' + error.message);
            });
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

    displayServerConfigModal(serverInfo) {
        const safe = (v) => this.escapeHtml(v);

        const obfParams = serverInfo.obfuscation_params || {};
        const iKeys = ['I1', 'I2', 'I3', 'I4', 'I5'];
        const normalParams = Object.entries(obfParams).filter(([key]) => !iKeys.includes(key));
        const iLines = iKeys.map((key) => `${key} = ${obfParams[key] ?? ''}`).join('\n');

        const modalHtml = `
            <div id="configModal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
                <div class="relative top-20 mx-auto p-5 border w-11/12 md:w-3/4 lg:w-1/2 shadow-lg rounded-md bg-white">
                    <div class="mt-3">
                        <div class="flex justify-between items-center mb-4">
                            <h3 class="text-lg font-medium text-gray-900">Server Configuration: ${safe(serverInfo.name)}</h3>
                            <button onclick="amneziaApp.closeModal()" class="text-gray-400 hover:text-gray-600">
                                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                                </svg>
                            </button>
                        </div>

                        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
                            <div class="bg-gray-50 p-3 rounded">
                                <h4 class="font-semibold text-sm text-gray-700 mb-2">Basic Information</h4>
                                <div class="space-y-1 text-sm">
                                    <div><span class="font-medium">Interface:</span> ${serverInfo.interface}</div>
                                    <div><span class="font-medium">Port:</span> ${serverInfo.port}</div>
                                    <div><span class="font-medium">Subnet:</span> ${serverInfo.subnet}</div>
                                    <div><span class="font-medium">Server IP:</span> ${serverInfo.server_ip}</div>
                                    <div><span class="font-medium">Public IP:</span> ${serverInfo.public_ip}</div>
                                    <div><span class="font-medium">Status:</span>
                                        <span class="px-2 py-1 rounded-full text-xs ${
                                            serverInfo.status === 'running' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                                        }">${serverInfo.status}</span>
                                    </div>
                                </div>
                            </div>

                            <div class="bg-gray-50 p-3 rounded">
                                <h4 class="font-semibold text-sm text-gray-700 mb-2">Configuration</h4>
                                <div class="space-y-1 text-sm">
                                    <div><span class="font-medium">Protocol:</span> ${serverInfo.protocol}</div>
                                    <div><span class="font-medium">Obfuscation:</span> ${serverInfo.obfuscation_enabled ? 'Enabled' : 'Disabled'}</div>
                                    <div><span class="font-medium">Clients:</span> ${serverInfo.clients_count}</div>
                                    <div><span class="font-medium">DNS:</span> ${safe(serverInfo.dns.join(', '))}</div>
                                    <div><span class="font-medium">MTU:</span> ${serverInfo.mtu}</div>
                                    <div class="truncate"><span class="font-medium">Public Key:</span>
                                        <span class="font-mono text-xs">${safe(serverInfo.public_key)}</span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        ${serverInfo.obfuscation_enabled ? `
                        <div class="bg-blue-50 p-3 rounded mb-4">
                            <h4 class="font-semibold text-sm text-blue-700 mb-2">Obfuscation Parameters</h4>
                            <div class="grid grid-cols-3 md:grid-cols-6 gap-2 text-xs">
                                ${normalParams.map(([key, value]) => `
                                    <div class="text-center">
                                        <div class="font-medium">${safe(key)}</div>
                                        <div class="font-mono break-all whitespace-pre-wrap">${safe(value)}</div>
                                    </div>
                                `).join('')}
                            </div>

                            <div class="mt-3">
                                <div class="flex items-center justify-between mb-1">
                                    <h5 class="font-semibold text-xs text-blue-800">I Parameters (client-only)</h5>
                                    <button onclick="amneziaApp.saveServerIParams('${serverInfo.id}')"
                                            class="bg-blue-600 text-white px-3 py-1 rounded text-xs hover:bg-blue-700">
                                        Save I1–I5
                                    </button>
                                </div>

                                <div class="text-[11px] text-blue-800/80 mb-2">
                                    Defaults for NEW clients only (no server restart).
                                </div>

                                <div class="grid grid-cols-1 gap-2">
                                    ${iKeys.map((key) => `
                                        <label class="block text-xs">
                                            <div class="flex items-center justify-between mb-1">
                                                <span class="font-semibold text-blue-900">${safe(key)}</span>
                                            </div>
                                            <textarea id="serverIParam-${serverInfo.id}-${key}" rows="1"
                                                class="w-full px-2 py-1 border border-blue-200 rounded text-xs font-mono bg-white/70 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                                                placeholder="${safe(key)} =">${safe(obfParams[key] ?? '')}</textarea>
                                        </label>
                                    `).join('')}
                                </div>
                            </div>
                        </div>
                        ` : ''}

                        <div class="mb-4">
                            <h4 class="font-semibold text-sm text-gray-700 mb-2">Configuration Preview</h4>
                            <pre class="bg-gray-800 text-green-400 p-3 rounded text-xs overflow-x-auto max-h-40 overflow-y-auto">${safe(serverInfo.config_preview)}</pre>
                        </div>

                        <div class="flex justify-end space-x-3 pt-4 border-t">
                            <button onclick="amneziaApp.showRawServerConfig('${serverInfo.id}')"
                                    class="bg-blue-500 text-white px-4 py-2 rounded text-sm hover:bg-blue-600">
                                View Full Config
                            </button>
                            <button onclick="amneziaApp.downloadServerConfig('${serverInfo.id}')"
                                    class="bg-green-500 text-white px-4 py-2 rounded text-sm hover:bg-green-600">
                                Download Config
                            </button>
                            <button onclick="amneziaApp.closeModal()"
                                    class="bg-gray-500 text-white px-4 py-2 rounded text-sm hover:bg-gray-600">
                                Close
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        document.body.insertAdjacentHTML('beforeend', modalHtml);

        // Auto-resize I1–I5 textareas to fit content
        setTimeout(() => {
            for (const key of ['I1', 'I2', 'I3', 'I4', 'I5']) {
                const el = document.getElementById(`serverIParam-${serverInfo.id}-${key}`);
                this.enableTextareaAutosize(el, 260);
            }
        }, 0);
    }

    async saveServerIParams(serverId) {
        const iParams = {};
        for (const key of ['I1', 'I2', 'I3', 'I4', 'I5']) {
            const el = document.getElementById(`serverIParam-${serverId}-${key}`);
            iParams[key] = el ? el.value : '';
        }

        try {
            const response = await this.apiFetch(`/api/servers/${serverId}/i-params`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(iParams)
            });

            if (!response.ok) {
                const text = await response.text();
                throw new Error(text || 'Failed to update I parameters');
            }

            this.showTempMessage('I1–I5 updated (client-only).', 'success');
        } catch (error) {
            console.error('Error updating server I params:', error);
            this.showTempMessage('Failed to update I1–I5: ' + (error?.message || error), 'error');
        }
    }

    async showClientIParamsModal(serverId, clientId) {
        const cached = (this.serverClients.get(serverId) || []).find((c) => c.id === clientId);
        const safeName = this.escapeHtml(cached?.name || clientId);

        // Close any existing modal first
        this.closeModal();

        const modalHtml = `
            <div id="configModal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
                <div class="relative top-20 mx-auto p-5 border w-11/12 md:w-3/4 lg:w-1/2 shadow-lg rounded-md bg-white">
                    <div class="mt-3">
                        <div class="flex justify-between items-center mb-4">
                            <h3 class="text-lg font-medium text-gray-900">Client I1–I5: ${safeName}</h3>
                            <button onclick="amneziaApp.closeModal()" class="text-gray-400 hover:text-gray-600">
                                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                                </svg>
                            </button>
                        </div>

                        <div class="text-sm text-gray-600 mb-3">
                            Client-only parameters; different clients can have different values.
                        </div>

                        <div id="clientIParamsBody" class="space-y-3">
                            <div class="text-sm text-gray-500">Loading…</div>
                        </div>

                        <div class="flex justify-end space-x-3 pt-4 border-t mt-4">
                            <button onclick="amneziaApp.saveClientIParams('${serverId}', '${clientId}')"
                                    class="bg-blue-600 text-white px-4 py-2 rounded text-sm hover:bg-blue-700">
                                Save I1–I5
                            </button>
                            <button onclick="amneziaApp.closeModal()"
                                    class="bg-gray-500 text-white px-4 py-2 rounded text-sm hover:bg-gray-600">
                                Close
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        document.body.insertAdjacentHTML('beforeend', modalHtml);

        try {
            const res = await this.apiFetch(`/api/servers/${serverId}/clients`);
            if (!res.ok) throw new Error('Failed to load client list');
            const clients = await res.json();
            const client = (clients || []).find((c) => c.id === clientId);
            const obf = client?.obfuscation_params || {};

            const body = document.getElementById('clientIParamsBody');
            if (!body) return;
            const keys = ['I1', 'I2', 'I3', 'I4', 'I5'];
            body.innerHTML = keys.map((key) => {
                const value = obf[key] ?? '';
                return `
                    <label class="block text-sm">
                        <div class="font-semibold text-gray-800 mb-1">${this.escapeHtml(key)}</div>
                        <textarea id="clientIParam-${clientId}-${key}" rows="1"
                            class="w-full px-3 py-2 border border-gray-200 rounded text-xs font-mono bg-gray-50 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                            placeholder="${this.escapeHtml(key)} =">${this.escapeHtml(value)}</textarea>
                    </label>
                `;
            }).join('');

            // Auto-resize after insertion
            for (const key of keys) {
                const el = document.getElementById(`clientIParam-${clientId}-${key}`);
                this.enableTextareaAutosize(el, 260);
            }
        } catch (e) {
            const body = document.getElementById('clientIParamsBody');
            if (body) {
                body.innerHTML = `<div class="text-sm text-red-600">Failed to load current values: ${this.escapeHtml(e?.message || String(e))}</div>`;
            }
        }
    }

    async saveClientIParams(serverId, clientId) {
        const iParams = {};
        for (const key of ['I1', 'I2', 'I3', 'I4', 'I5']) {
            const el = document.getElementById(`clientIParam-${clientId}-${key}`);
            iParams[key] = el ? el.value : '';
        }

        try {
            const response = await this.apiFetch(`/api/servers/${serverId}/clients/${clientId}/i-params`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(iParams)
            });
            if (!response.ok) {
                const text = await response.text();
                throw new Error(text || 'Failed to update client I params');
            }
            this.showTempMessage('Client I1–I5 updated.', 'success');
        } catch (error) {
            console.error('Error updating client I params:', error);
            this.showTempMessage('Failed to update client I1–I5: ' + (error?.message || error), 'error');
        }
    }

    displayRawConfigModal(config) {
        const safe = (v) => this.escapeHtml(v);
        const modalHtml = `
            <div id="rawConfigModal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
                <div class="relative top-10 mx-auto p-5 border w-11/12 md:w-3/4 lg:w-2/3 shadow-lg rounded-md bg-white">
                    <div class="mt-3">
                        <div class="flex justify-between items-center mb-4">
                            <h3 class="text-lg font-medium text-gray-900">Raw Configuration: ${safe(config.server_name)}</h3>
                            <button onclick="amneziaApp.closeModal()" class="text-gray-400 hover:text-gray-600">
                                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                                </svg>
                            </button>
                        </div>

                        <div class="mb-4">
                            <div class="flex justify-between items-center mb-2">
                                <span class="text-sm text-gray-600">Config path: ${safe(config.config_path)}</span>
                                <button onclick="amneziaApp.copyToClipboard('${btoa(JSON.stringify(config))}')"
                                        class="bg-gray-500 text-white px-3 py-1 rounded text-xs hover:bg-gray-600">
                                    Copy JSON
                                </button>
                            </div>
                            <pre class="bg-gray-900 text-green-400 p-4 rounded text-sm overflow-x-auto max-h-96 overflow-y-auto">${safe(config.config_content)}</pre>
                        </div>

                        <div class="flex justify-end space-x-3 pt-4 border-t">
                            <button onclick="amneziaApp.downloadServerConfig('${config.server_id}')"
                                    class="bg-green-500 text-white px-4 py-2 rounded text-sm hover:bg-green-600">
                                Download Config
                            </button>
                            <button onclick="amneziaApp.closeModal()"
                                    class="bg-gray-500 text-white px-4 py-2 rounded text-sm hover:bg-gray-600">
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

    closeModal() {
        const existingModal = document.getElementById('configModal') || document.getElementById('rawConfigModal');
        if (existingModal) existingModal.remove();

        // Also close the create-server modal (this one is part of the DOM)
        this.closeCreateServerModal();
    }

    showClientQRCode(serverId, clientId) {
        const cached = (this.serverClients.get(serverId) || []).find((c) => c.id === clientId);
        const safeClientName = this.escapeHtml(cached?.name || clientId);
        // Create modal for QR code
        const modalHtml = `
            <div id="qrModal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50 flex items-center justify-center">
                <div class="relative p-8 border w-11/12 md:w-3/4 lg:w-2/3 xl:w-1/2 shadow-2xl rounded-2xl bg-white">
                    <div class="flex flex-col">
                        <div class="flex justify-between items-center w-full mb-6">
                            <h3 class="text-xl font-bold text-gray-900">QR Code for ${safeClientName}</h3>
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
                                <div class="bg-white p-6 rounded-xl border-2 border-gray-100 shadow-inner">
                                    <div id="qrcode" class="flex justify-center mb-4"></div>
                                    <p class="text-center text-sm text-gray-500">Scan with WireGuard app</p>
                                </div>
                                <!-- Download QR Code button outside the box -->
                                <div class="mt-4 text-center">
                                    <button onclick="amneziaApp.downloadQRCode()"
                                            class="inline-flex items-center bg-gradient-to-r from-blue-500 to-cyan-500 hover:from-blue-600 hover:to-cyan-600 text-white px-5 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 shadow hover:shadow-lg transform hover:-translate-y-0.5">
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
                                        <label class="block text-sm font-medium text-gray-700">Configuration Text</label>
                                        <div class="flex space-x-2">
                                            <button onclick="amneziaApp.toggleConfigView()"
                                                    class="text-blue-500 hover:text-blue-700 text-sm font-medium px-3 py-1.5 rounded-lg border border-blue-200 hover:bg-blue-50 transition-colors duration-200">
                                                Toggle View
                                            </button>
                                            <button onclick="amneziaApp.copyConfigText()"
                                                    class="bg-gradient-to-r from-blue-500 to-blue-600 hover:from-blue-600 hover:to-blue-700 text-white px-4 py-1.5 rounded-lg text-sm font-medium transition-colors duration-200 shadow hover:shadow-md">
                                                Copy Config
                                            </button>
                                        </div>
                                    </div>
                                    <textarea id="configText" rows="12"
                                        class="w-full px-4 py-3 border-2 border-gray-200 rounded-xl text-sm font-mono bg-gray-50 focus:ring-2 focus:ring-blue-500 focus:border-blue-500 transition-all duration-200"
                                        readonly
                                        placeholder="Loading configuration..."></textarea>
                                    <div class="flex justify-between items-center mt-3">
                                        <span id="configType" class="text-xs font-medium text-blue-500">Clean Config</span>
                                    </div>
                                </div>
                            </div>
                        </div>
                        
                        <div class="flex justify-end space-x-4 w-full pt-6 border-t border-gray-200">
                            <button onclick="amneziaApp.downloadClientConfig('${serverId}', '${clientId}')"
                                    class="bg-gradient-to-r from-green-500 to-green-600 hover:from-green-600 hover:to-green-700 text-white px-6 py-3 rounded-xl text-sm font-medium transition-all duration-200 shadow hover:shadow-lg transform hover:-translate-y-0.5">
                                <svg class="w-5 h-5 inline mr-2 -mt-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                                </svg>
                                Download Config File (.conf)
                            </button>
                            <button onclick="amneziaApp.closeQRModal()"
                                    class="bg-gradient-to-r from-gray-500 to-gray-600 hover:from-gray-600 hover:to-gray-700 text-white px-6 py-3 rounded-xl text-sm font-medium transition-all duration-200 shadow hover:shadow-lg">
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
            this.currentConfigType = 'clean';
            this.currentClientName = data.client_name;
            
            // Display clean config text
            const configTextArea = document.getElementById('configText');
            if (configTextArea) {
                configTextArea.value = this.currentCleanConfig;
                this.updateConfigTypeLabel();
            }
            
            // Generate QR code from clean config
            const qrContainer = document.getElementById('qrcode');
            if (qrContainer) {
                this.generateQrIntoContainer(qrContainer, this.currentCleanConfig);
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
        const configTextArea = document.getElementById('configText');
        if (configTextArea) {
            configTextArea.select();
            configTextArea.setSelectionRange(0, 99999); // For mobile devices
            
            try {
                navigator.clipboard.writeText(configTextArea.value).then(() => {
                    this.showTempMessage('Configuration copied to clipboard!', 'success');
                }).catch(err => {
                    // Fallback for older browsers
                    document.execCommand('copy');
                    this.showTempMessage('Configuration copied to clipboard!', 'success');
                });
            } catch (err) {
                document.execCommand('copy');
                this.showTempMessage('Configuration copied to clipboard!', 'success');
            }
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