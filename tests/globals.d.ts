// The page's shared globals, for tsc (tsconfig.json). The scripts are classic, not
// modules: index.html loads them in order and each hands its part to the next through
// `window` (and top-level names). Types come from the code itself (`typeof`), so
// nothing here restates a signature.

// forms.js and modals.js copy their methods onto AmneziaApp.prototype; each of those
// methods says `@this {AmneziaApp}`. One way only: the reverse would be a cycle.
interface AmneziaApp extends FormUi, ModalUi {}

// Readonly<> on the plain objects: in a .js file TypeScript treats an object built from a
// literal as open and lets any property through, so `window.Ui.tost` would pass.
interface Window {
    AppConfig: Readonly<typeof AppConfig>;
    Protocols: Readonly<typeof Protocols>;
    Ui: Readonly<typeof Ui>;
    ApiClient: typeof ApiClient;
    Charts: Readonly<typeof Charts>;
    ServerUi: typeof ServerUi;
    FormUi: typeof FormUi;
    ModalUi: typeof ModalUi;
}

// A chart series (charts.js): Mbit/s per point, null where there is no data.
type Series = Array<number | null>;

// static/vendor/qrcode-1.0.0.min.js (davidshimjs/qrcodejs), the part the page uses.
declare class QRCode {
    constructor(
        element: HTMLElement,
        options: { text: string; width?: number; height?: number; colorDark?: string; colorLight?: string; correctLevel?: number },
    );
    static CorrectLevel: { L: number; M: number; Q: number; H: number };
}
