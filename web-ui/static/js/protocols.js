// AmneziaWG Web UI - the page's configuration from the backend, and the protocol helpers.
//
// index.html carries #appConfig, rendered from the Python tuples by page_config()
// (routes/system.py): the protocol table, the parameter key lists and the new-server
// defaults. Nothing here is a copy, so adding a protocol generation is a backend-only
// change: the <option> lists, the field gating and every capability check read it.
const AppConfig = JSON.parse(document.getElementById('appConfig')?.textContent || '{}');
const PROTOCOLS = AppConfig.protocols?.supported || [];
const DEFAULT_PROTOCOL = AppConfig.protocols?.default || PROTOCOLS[0]?.id || '';

const Protocols = {
    DEFAULT: DEFAULT_PROTOCOL,

    all() {
        return PROTOCOLS.map((p) => p.id);
    },

    // Unknown/missing values fall back to the default, matching
    // AmneziaManager.normalize_protocol().
    normalize(value) {
        const raw = String(value || '').trim();
        return PROTOCOLS.some((p) => p.id === raw) ? raw : DEFAULT_PROTOCOL;
    },

    supports(protocol, capability) {
        const entry = PROTOCOLS.find((p) => p.id === this.normalize(protocol));
        return !!(entry && entry[capability]);
    },

    supportsS34(protocol) {
        return this.supports(protocol, 'supportsS34');
    },

    supportsHeaderRanges(protocol) {
        return this.supports(protocol, 'supportsHeaderRanges');
    },

    supportsAwg3(protocol) {
        return this.supports(protocol, 'supportsAwg3');
    },

    supportsAwg31(protocol) {
        return this.supports(protocol, 'supportsAwg31');
    },

    // <option> markup for a <select>; used by both the create form and the server
    // settings drawer so the two lists cannot drift apart.
    optionsHtml(selected) {
        const current = this.normalize(selected);
        return PROTOCOLS
            .map((p) => `<option value="${p.id}"${p.id === current ? ' selected' : ''}>${p.id}</option>`)
            .join('');
    },
};

window.AppConfig = AppConfig;
window.Protocols = Protocols;
