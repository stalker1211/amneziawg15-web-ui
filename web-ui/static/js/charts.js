// AmneziaWG Web UI - traffic charts: the mirrored chart (download above a line, upload
// below it, on one scale), the presence strip, and rate formatting.
//
// Pure, like ui.js: they take numbers and return SVG or text. Values are Mbit/s from
// the device's side (↓ download = what the server sent, ↑ upload = what it received;
// fromApi translates the API's daemon terms), times are epoch seconds, and null is
// "no data", drawn as a gap, never as a zero. DEVELOPMENT.md §10 #18 step 13.
(() => {
    // Every colour utility carries its dark: partner (tests/test_frontend.py). Download
    // and upload passed the dataviz validator on the panel's cards (worst colour-blind
    // ΔE 24.7).
    const C = {
        dLine: 'fill-none stroke-[#2a78d6] dark:stroke-[#3987e5]',
        dArea: 'fill-[#2a78d6]/10 dark:fill-[#3987e5]/15',
        uLine: 'fill-none stroke-[#eb6834] dark:stroke-[#d95926]',
        uArea: 'fill-[#eb6834]/10 dark:fill-[#d95926]/15',
        gLine: 'fill-none stroke-gray-300 dark:stroke-[#475569]',
        gArea: 'fill-gray-300/40 dark:fill-[#475569]/30',
        grid: 'stroke-gray-200 dark:stroke-[#2b3647]',
        base: 'stroke-gray-400 dark:stroke-[#526077]',
        dKey: 'bg-[#2a78d6] dark:bg-[#3987e5]',
        uKey: 'bg-[#eb6834] dark:bg-[#d95926]',
        online: 'fill-[#22c55e] dark:fill-[#16a34a]',
        suspended: 'fill-[#fbbf24] dark:fill-[#f59e0b]',
        hatch: 'stroke-[#b45309]/50 dark:stroke-[#78350f]/70',
        track: 'fill-gray-200 dark:fill-[#334155]',
        onlineKey: 'bg-[#22c55e] dark:bg-[#16a34a]',
        trackKey: 'bg-gray-200 dark:bg-[#334155]',
        text: 'text-gray-900 dark:text-[#f1f5f9]',
        text2: 'text-gray-700 dark:text-[#bac5d4]',
        muted: 'text-gray-600 dark:text-[#98a6ba]',
    };

    const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
    const maxOf = (arr) => arr.reduce((a, v) => (isNum(v) && v > a ? v : a), 0);

    // --- numbers ---------------------------------------------------------------------
    const rateNum = (v) => (v >= 99.5 ? Math.round(v).toString() : v >= 9.95 ? v.toFixed(0) : v.toFixed(1));
    // "4.4 Mbit/s", "320 kbit/s" below 1 Mbit/s, "0 bit/s".
    const rate = (v) => (v >= 0.995 ? `${rateNum(v)} Mbit/s` : v >= 0.0005 ? `${Math.round(v * 1000)} kbit/s` : '0 bit/s');
    const tickNum = (v) => (Math.abs(v - Math.round(v)) < 1e-9 ? String(Math.round(v)) : v.toFixed(1));
    const niceCeil = (v) => {
        if (!(v > 0)) return 1;
        const p = 10 ** Math.floor(Math.log10(v));
        const m = v / p;
        return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
    };
    const hhmm = (epochSeconds) => {
        const d = new Date(epochSeconds * 1000);
        return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    };

    // GET /api/servers/<sid>/traffic -> Mbit/s from the device's side. `step` is the
    // spacing of the points: a tick (7 s) for 1h, a minute otherwise.
    function fromApi(body) {
        const mbit = (arr) => (arr || []).map((v) => (isNum(v) ? v / 1e6 : null));
        const clients = {};
        const totals = {};
        Object.entries(body.clients || {}).forEach(([cid, c]) => {
            clients[cid] = { down: mbit(c.sent_bps), up: mbit(c.received_bps), state: [...(c.state || '')] };
        });
        Object.entries(body.totals || {}).forEach(([cid, c]) => {
            totals[cid] = { down: c.sent_bytes || 0, up: c.received_bytes || 0 };
        });
        return {
            range: body.range, t: body.t || [], now: body.now, since: body.since,
            step: body.range === '1h' ? 7 : 60, clients, totals,
        };
    }

    // The sum over several series, point by point; null where every one is null.
    function sumSeries(list, n) {
        const out = new Array(n).fill(null);
        list.forEach((arr) => arr.forEach((v, i) => { if (isNum(v)) out[i] = (out[i] ?? 0) + v; }));
        return out;
    }

    // `values` at times `t`, averaged into `n` equal buckets over [t0, t1]; null where a
    // bucket has no data. For the small charts, which show the last hour at a glance.
    function buckets(t, values, t0, t1, n) {
        const sum = new Array(n).fill(0);
        const count = new Array(n).fill(0);
        const span = Math.max(1e-9, t1 - t0);
        t.forEach((tt, i) => {
            if (!isNum(values[i]) || tt < t0 || tt > t1) return;
            const j = Math.min(n - 1, Math.floor(((tt - t0) / span) * n));
            sum[j] += values[i];
            count[j] += 1;
        });
        return sum.map((s, j) => (count[j] ? s / count[j] : null));
    }

    // Where a series has no point for a while (a stopped server), a null in between, so
    // the line breaks there.
    function withGaps(t, series, step) {
        const out = { t: [], series: series.map(() => []) };
        t.forEach((tt, i) => {
            if (i > 0 && tt - t[i - 1] > 3 * step) {
                out.t.push((tt + t[i - 1]) / 2);
                out.series.forEach((s) => s.push(null));
            }
            out.t.push(tt);
            series.forEach((s, k) => out.series[k].push(s[i]));
        });
        return out;
    }

    // Download above the line, upload below it, on one scale. In the small charts the
    // line sits at 68% of the height, so they hold still as data arrives; the dialog
    // gives the upload side only the room it needs.
    const SPLIT = 0.68;
    function geometry(h, dTop, uTop, pad = 2, split = SPLIT) {
        const base = Math.round(h * split) + 0.5;
        const k = Math.min((base - pad) / Math.max(dTop, 1e-6), (h - base - pad) / Math.max(uTop, 1e-6));
        return { base, k };
    }

    // A line and its area per run of numbers; a null ends a run. A lone point gets a
    // short flat stroke, so it shows.
    function paths(xs, values, g, sign) {
        let line = '';
        let area = '';
        let run = [];
        const f = (v) => v.toFixed(1);
        const flush = () => {
            if (run.length === 1) run = [[run[0][0] - 1, run[0][1]], [run[0][0] + 1, run[0][1]]];
            if (run.length) {
                const pts = run.map(([x, y]) => `${f(x)},${f(y)}`).join('L');
                line += `M${pts}`;
                area += `M${f(run[0][0])},${g.base}L${pts}L${f(run[run.length - 1][0])},${g.base}Z`;
            }
            run = [];
        };
        values.forEach((v, i) => {
            if (isNum(v)) run.push([xs[i], g.base + sign * v * g.k]);
            else flush();
        });
        flush();
        return { line, area };
    }

    // `xs` places the points (default: evenly across `w`). `emph` ({down, up}) draws one
    // client's share in colour over the total in grey.
    function mirrored({ w, h, down, up, xs = null, emph = null, dTop, uTop, grid = [], width = 1.5, split = SPLIT }) {
        const n = down.length;
        const at = xs || Array.from({ length: n }, (_, i) => (n === 1 ? w / 2 : (i / (n - 1)) * w));
        const g = geometry(h, dTop ?? Math.max(maxOf(down), 0.5), uTop ?? Math.max(maxOf(up), 0.5), 2, split);
        const line = (d, cls) => (d ? `<path d="${d}" class="${cls}" stroke-width="${width}" stroke-linejoin="round" stroke-linecap="round"/>` : '');
        const area = (d, cls) => (d ? `<path d="${d}" class="${cls}"/>` : '');
        const pd = paths(at, down, g, -1);
        const pu = paths(at, up, g, 1);
        let s = `<svg class="block" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">`;
        grid.forEach((y) => { s += `<line x1="0" x2="${w}" y1="${y}" y2="${y}" class="${C.grid}" stroke-width="1"/>`; });
        if (emph) {
            const qd = paths(at, emph.down, g, -1);
            const qu = paths(at, emph.up, g, 1);
            s += area(pd.area, C.gArea) + area(pu.area, C.gArea) + line(pd.line, C.gLine) + line(pu.line, C.gLine);
            s += area(qd.area, C.dArea) + area(qu.area, C.uArea) + line(qd.line, C.dLine) + line(qu.line, C.uLine);
        } else {
            s += area(pd.area, C.dArea) + area(pu.area, C.uArea) + line(pd.line, C.dLine) + line(pu.line, C.uLine);
        }
        s += `<line x1="0" x2="${w}" y1="${g.base}" y2="${g.base}" class="${C.base}" stroke-width="1"/></svg>`;
        return { svg: s, g, xs: at };
    }

    // Suspended is amber, as its dot and pill are, and hatched: green and amber are the
    // pair red-green colour blindness merges, so the texture keeps them apart.
    let hatchSeq = 0;
    const hatchDef = (id) => `<defs><pattern id="${id}" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">`
        + `<rect width="5" height="5" class="${C.suspended}"/><line x1="0" y1="0" x2="0" y2="5" class="${C.hatch}" stroke-width="2.5"/></pattern></defs>`;
    function hatchSwatch() {
        hatchSeq += 1;
        const id = `trafficHatch${hatchSeq}`;
        return `<svg class="block" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">${hatchDef(id)}<rect width="10" height="10" rx="2" fill="url(#${id})"/></svg>`;
    }

    // One client's states on the chart's time axis: online green, suspended hatched
    // amber, offline the bare track; no data, no track. A point covers half a step on
    // either side, and a run ends where the points stop for a while.
    function presenceSvg({ t, states, t0, t1, step, w, h = 12 }) {
        hatchSeq += 1;
        const id = `trafficHatch${hatchSeq}`;
        const x = (tt) => Math.min(w, Math.max(0, ((tt - t0) / Math.max(1e-9, t1 - t0)) * w));
        let s = `<svg class="block" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">${hatchDef(id)}`;
        let i = 0;
        while (i < t.length) {
            let j = i;
            while (j + 1 < t.length && states[j + 1] === states[i] && t[j + 1] - t[j] <= 3 * step) j += 1;
            const paint = { o: `class="${C.online}"`, s: `fill="url(#${id})"`, '-': `class="${C.track}"` }[states[i]];
            if (paint) {
                const x0 = x(t[i] - step / 2);
                const x1 = x(t[j] + step / 2);
                s += `<rect x="${x0.toFixed(1)}" y="0" width="${Math.max(1, x1 - x0).toFixed(1)}" height="${h}" rx="2" ${paint}/>`;
            }
            i = j + 1;
        }
        return `${s}</svg>`;
    }

    window.Charts = {
        C, isNum, maxOf, rate, rateNum, tickNum, niceCeil, hhmm,
        fromApi, sumSeries, buckets, withGaps, geometry, mirrored, hatchSwatch, presenceSvg,
    };
})();
