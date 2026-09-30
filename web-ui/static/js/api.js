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
        let response;
        try {
            response = await window.fetch(input, nextInit);
        } catch (error) {
            // Safari reports a 401 on fetch as "access control checks", a TypeError,
            // exactly like a network failure; a static file (no auth) tells them apart.
            if (await this.serverAnswers()) this.signInAgain();
            throw error;
        }
        if (response.status === 401) this.signInAgain();
        return response;
    }

    async serverAnswers() {
        try {
            return (await window.fetch('/static/favicon.ico', { cache: 'no-store' })).ok;
        } catch (_) {
            return false;
        }
    }

    // The browser's saved sign-in no longer works (the password was changed, perhaps in
    // another browser). fetch() never shows a sign-in prompt, and Safari not even an
    // error, so the page would just stop updating: reload it, since loading the page
    // itself is what makes every browser ask.
    signInAgain() {
        if (this.signingIn) return;
        this.signingIn = true;
        window.Ui?.toast?.('The panel\'s password has changed. Sign in again.', 'info');
        setTimeout(() => window.location.reload(), 1500);
    }

    // After the panel's credential changed, give it to the browser: a request made with
    // explicit credentials that succeeds replaces the ones the browser caches for this
    // site. Without it the browser keeps sending the old password, every request is a
    // 401, and a reload lands on nginx's error page. fetch() cannot carry them (Chrome
    // refuses credentials in its URL); XMLHttpRequest.open takes them as arguments.
    // Resolves true when the new credential works.
    async rememberCredentials(user, password) {
        const accepted = await new Promise((resolve) => {
            const xhr = new XMLHttpRequest();
            xhr.open('GET', '/api/settings', true, user, password);
            xhr.onloadend = () => resolve(xhr.status === 200);
            xhr.send();
        });
        if (!accepted) return false;
        // Did the browser keep them? A browser may accept them for that one request
        // and still send the old ones afterwards; only a plain request tells.
        try {
            return (await window.fetch('/api/settings')).ok;
        } catch (_) {
            return false;
        }
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
