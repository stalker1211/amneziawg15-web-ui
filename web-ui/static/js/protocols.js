// AmneziaWG Web UI - the protocol table.
//
// SINGLE SOURCE OF TRUTH for the frontend. Adding a protocol generation should mean
// editing PROTOCOLS below and nothing else: the <option> lists, the field gating and
// every capability check read from here.
//
// This mirrors AmneziaManager in services/amnezia_manager.py, which is authoritative
// (the server re-validates everything). Keep the two in step:
//
//   supportsS34         -> protocol_supports_s34()
//   supportsHeaderRanges-> protocol_supports_header_ranges()
//   supportsAwg3        -> protocol_supports_awg3()
//   supportsAwg31       -> protocol_supports_awg31()
const PROTOCOLS = [
    {
        id: 'AWG 1.5',
        supportsS34: false,           // S3/S4 padding
        supportsHeaderRanges: false,  // H1-H4 as "1200-1400"
        supportsAwg3: false,          // header protection, content padding, timings
        supportsAwg31: false,         // random trailers, disable cookies
    },
    {
        id: 'AWG 2.0',
        supportsS34: true,
        supportsHeaderRanges: true,
        supportsAwg3: false,
        supportsAwg31: false,
    },
    {
        id: 'AWG 3.0',
        supportsS34: true,
        supportsHeaderRanges: true,
        supportsAwg3: true,
        supportsAwg31: false,
    },
    {
        id: 'AWG 3.1',
        supportsS34: true,
        supportsHeaderRanges: true,
        supportsAwg3: true,
        supportsAwg31: true,
    },
];

const DEFAULT_PROTOCOL = 'AWG 1.5';

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
    // config modal so the two lists cannot drift apart.
    optionsHtml(selected) {
        const current = this.normalize(selected);
        return PROTOCOLS
            .map((p) => `<option value="${p.id}"${p.id === current ? ' selected' : ''}>${p.id}</option>`)
            .join('');
    },
};

window.Protocols = Protocols;
