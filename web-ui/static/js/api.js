// AmneziaWG Web UI - API helper
class ApiClient {
    getToken() {
        try {
            return String(localStorage.getItem('amnezia_api_token') || '').trim();
        } catch (_) {
            return '';
        }
    }

    setToken(token) {
        try {
            const value = String(token || '').trim();
            if (value) localStorage.setItem('amnezia_api_token', value);
            else localStorage.removeItem('amnezia_api_token');
        } catch (_) {
            // ignore
        }
    }

    buildHeaders(existingHeaders, method) {
        const headers = new Headers(existingHeaders || {});
        const token = this.getToken();
        if (token) headers.set('X-API-Token', token);

        // Mutating requests must declare JSON. The backend rejects anything else,
        // which is what stops a cross-site <form> from driving the API: a form can
        // only send urlencoded/text-plain/multipart, and setting this content-type
        // cross-origin forces a CORS preflight that nginx will not allow.
        const verb = String(method || 'GET').toUpperCase();
        if (verb !== 'GET' && verb !== 'HEAD' && !headers.has('Content-Type')) {
            headers.set('Content-Type', 'application/json');
        }

        return headers;
    }

    // True when the server is asking specifically for an API token, rather than for
    // the reverse proxy's Basic Auth credentials. Nginx answers unauthenticated
    // requests with its own 401 (WWW-Authenticate: Basic) long before Flask runs, so
    // prompting for a token on every 401 asks for the wrong credential.
    async isMissingApiToken(response) {
        const authScheme = String(response.headers.get('WWW-Authenticate') || '');
        if (/^\s*basic/i.test(authScheme)) return false;

        try {
            const data = await response.clone().json();
            return /token/i.test(String(data?.error || ''));
        } catch (_) {
            return false;
        }
    }

    async fetch(input, init = {}) {
        const method = (init || {}).method;
        const nextInit = { ...(init || {}) };
        nextInit.headers = this.buildHeaders(nextInit.headers, method);

        let response = await window.fetch(input, nextInit);

        if (response.status === 401 && await this.isMissingApiToken(response)) {
            const entered = prompt('API token required. Paste API_TOKEN value:', this.getToken());
            if (entered && String(entered).trim()) {
                this.setToken(String(entered).trim());
                const retryInit = { ...(init || {}) };
                retryInit.headers = this.buildHeaders(retryInit.headers, method);
                response = await window.fetch(input, retryInit);
            }
        }

        return response;
    }

    filenameFromContentDisposition(contentDisposition, fallback) {
        const value = String(contentDisposition || '');
        const match = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(value);
        const raw = (match && (match[1] || match[2])) ? (match[1] || match[2]) : '';
        try {
            const decoded = raw ? decodeURIComponent(raw) : '';
            return decoded || fallback;
        } catch (_) {
            return raw || fallback;
        }
    }

    async downloadBlob(url, fallbackFilename) {
        const response = await this.fetch(url);
        if (!response.ok) {
            let message = `HTTP ${response.status}`;
            try {
                const error = await response.json();
                message = error?.error || message;
            } catch (_) {
                // ignore
            }
            throw new Error(message);
        }

        const blob = await response.blob();
        const filename = this.filenameFromContentDisposition(response.headers.get('Content-Disposition'), fallbackFilename);

        const blobUrl = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = blobUrl;
        anchor.download = filename || 'download';
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    }
}

window.ApiClient = ApiClient;
