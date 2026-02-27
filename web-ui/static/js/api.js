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

    buildHeaders(existingHeaders) {
        const headers = new Headers(existingHeaders || {});
        const token = this.getToken();
        if (token) headers.set('X-API-Token', token);
        return headers;
    }

    async fetch(input, init = {}) {
        const nextInit = { ...(init || {}) };
        nextInit.headers = this.buildHeaders(nextInit.headers);

        let response = await window.fetch(input, nextInit);

        if (response.status === 401) {
            const entered = prompt('API token required. Paste API_TOKEN value:', this.getToken());
            if (entered && String(entered).trim()) {
                this.setToken(String(entered).trim());
                const retryInit = { ...(init || {}) };
                retryInit.headers = this.buildHeaders(retryInit.headers);
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
