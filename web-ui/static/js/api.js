// AmneziaWG Web UI - API helper
class ApiClient {
    // nginx's Basic Auth is the only credential, and the browser attaches it itself.
    buildHeaders(existingHeaders, method) {
        const headers = new Headers(existingHeaders || {});

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

    async fetch(input, init = {}) {
        const nextInit = { ...(init || {}) };
        nextInit.headers = this.buildHeaders(nextInit.headers, nextInit.method);
        return window.fetch(input, nextInit);
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
