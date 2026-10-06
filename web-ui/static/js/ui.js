// AmneziaWG Web UI - interface primitives: icons, toasts, dialogs, the drawer, menus
// and inline rename. They replace the browser's alert/confirm/prompt, which block the
// page, cannot be styled for dark mode and say "this page says" above the question.
//
// Stateless helpers on window.Ui; the page provides the roots they draw into
// (#toasts, #dialogRoot, #drawerRoot, #menu in index.html). Every value they
// interpolate is escaped here; `body`/`sub` arguments are trusted HTML the caller built.
(() => {
    const $ = (sel, root = document) => root.querySelector(sel);
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
    const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // Line icons (Feather style), drawn with currentColor.
    const ICONS = {
        plus: '<path d="M12 5v14M5 12h14"/>',
        x: '<path d="M18 6 6 18M6 6l12 12"/>',
        gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
        dots: '<circle cx="5" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.6" fill="currentColor" stroke="none"/>',
        refresh: '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
        qr: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM21 14v.01M14 21h.01M17 21h4M21 17v.01"/>',
        edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>',
        trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 11v6M14 11v6M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
        activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
        // The Connection analyzer (2.8): a radar, after Lucide's (ISC).
        radar: '<path d="M19.07 4.93A10 10 0 0 0 6.99 3.34"/><path d="M4 6h.01"/><path d="M2.29 9.62A10 10 0 1 0 21.31 8.35"/><path d="M16.24 7.76A6 6 0 1 0 8.23 16.67"/><path d="M12 18h.01"/><path d="M17.99 11.66A6 6 0 0 1 15.77 16.67"/><circle cx="12" cy="12" r="2"/><path d="m13.41 10.59 5.66-5.66"/>',
        logs: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
        code: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
        copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
        download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
        image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>',
        sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>',
        moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
        check: '<polyline points="20 6 9 17 4 12"/>',
        alert: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
        userPlus: '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/>',
        lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
        key: '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
        history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
        power: '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>',
        globe: '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    };
    const icon = (name, cls = 'w-4 h-4') =>
        `<svg class="${cls} flex-none" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;

    // --- toasts: short notes in a corner; errors stay longer ---------------------
    function toast(message, kind = 'success') {
        const root = $('#toasts');
        if (!root) return;
        const tone = {
            success: ['check', 'text-green-400 dark:text-green-600'],
            error: ['alert', 'text-red-400 dark:text-red-600'],
            info: ['alert', 'text-sky-300 dark:text-sky-600'],
        }[kind] || ['check', 'text-green-400 dark:text-green-600'];
        const el = document.createElement('div');
        el.className = 'toast pointer-events-auto flex items-start gap-2.5 w-full sm:w-auto sm:max-w-sm rounded-lg px-4 py-3 text-sm shadow-lg bg-gray-900 text-gray-50 dark:bg-[#f1f5f9] dark:text-gray-900';
        el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
        el.innerHTML = `<span class="mt-0.5 ${tone[1]}">${icon(tone[0])}</span><span class="flex-1">${esc(message)}</span>`
            + `<button type="button" class="-mr-1 opacity-60 hover:opacity-100" aria-label="Dismiss">${icon('x')}</button>`;
        el.querySelector('button').addEventListener('click', () => el.remove());
        root.appendChild(el);
        setTimeout(() => el.remove(), kind === 'error' ? 7000 : 4000);
    }

    // --- dialogs: views (QR, logs, config) and questions ---------------------------
    let dialogReturn = null;
    let dialogResolve = null;
    let dialogOnClose = null;

    function openDialog(html, { size = 'max-w-lg', onClose = null } = {}) {
        closeMenu();
        if (!$('#dialogRoot').hidden) closeDialog();
        const root = $('#dialogRoot');
        const box = $('#dialog');
        box.className = `${box.className.replace(/\bmax-w-\S+/g, '').trim()} ${size}`;
        box.innerHTML = html;
        dialogReturn = document.activeElement;
        dialogOnClose = onClose;
        root.hidden = false;
        requestAnimationFrame(() => root.classList.add('is-open'));
        (box.querySelector('[data-autofocus]') || box.querySelector('button, input, select, textarea'))?.focus();
        return box;
    }

    function closeDialog(result = false) {
        const root = $('#dialogRoot');
        if (!root || root.hidden) return;
        root.classList.remove('is-open');
        root.hidden = true;
        $('#dialog').innerHTML = '';
        const onClose = dialogOnClose;
        dialogOnClose = null;
        onClose?.();
        dialogReturn?.focus?.();
        if (dialogResolve) {
            const resolve = dialogResolve;
            dialogResolve = null;
            resolve(result);
        }
    }

    const dialogHeader = (title, sub = '') => `
        <div class="flex items-start justify-between gap-3 px-5 pt-4 pb-3">
            <div class="min-w-0">
                <h2 id="dialogTitle" class="text-lg font-semibold text-gray-900 dark:text-[#f1f5f9]">${title}</h2>
                ${sub ? `<p class="text-sm text-gray-700 dark:text-[#bac5d4]">${sub}</p>` : ''}
            </div>
            <button type="button" class="icon-btn -mr-2" data-close="dialog" aria-label="Close">${icon('x')}</button>
        </div>`;

    // An in-app confirm. `title` and `body` are trusted HTML (escape names first).
    // Resolves true only when the confirm button is pressed.
    function confirm({ title, body = '', confirmLabel = 'OK', cancelLabel = 'Cancel', danger = true }) {
        const badge = danger
            ? `<span class="flex-none w-10 h-10 rounded-full flex items-center justify-center bg-red-100 text-red-600 dark:bg-[#3b1219] dark:text-[#fca5a5]">${icon('trash', 'w-5 h-5')}</span>`
            : `<span class="flex-none w-10 h-10 rounded-full flex items-center justify-center bg-amber-100 text-amber-800 dark:bg-[#451a03] dark:text-[#fcd34d]">${icon('alert', 'w-5 h-5')}</span>`;
        const box = openDialog(`
            <div class="p-5 flex gap-4">
                ${badge}
                <div class="flex flex-col gap-1.5 min-w-0">
                    <h2 id="dialogTitle" class="text-base font-semibold text-gray-900 dark:text-[#f1f5f9]">${title}</h2>
                    <div class="text-sm text-gray-800 dark:text-[#d7dee9]">${body}</div>
                </div>
            </div>
            <div class="px-5 py-3 flex flex-wrap justify-end gap-2 border-t border-gray-300 dark:border-[#334155]">
                <button type="button" class="btn btn-secondary" data-close="dialog" data-autofocus>${esc(cancelLabel)}</button>
                <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-confirm>${esc(confirmLabel)}</button>
            </div>`, { size: 'max-w-md' });
        box.querySelector('[data-confirm]').addEventListener('click', () => closeDialog(true));
        return new Promise((resolve) => { dialogResolve = resolve; });
    }

    // Ask for one line of text. Resolves the trimmed text, or null when cancelled.
    function askText({ title, body = '', label, value = '', confirmLabel = 'OK', type = 'text' }) {
        const box = openDialog(`
            <form class="flex flex-col" novalidate>
                ${dialogHeader(title)}
                <div class="px-5 pb-5 flex flex-col gap-3">
                    ${body ? `<p class="text-sm text-gray-800 dark:text-[#d7dee9]">${body}</p>` : ''}
                    <div>
                        <label class="label" for="askTextInput">${esc(label)}</label>
                        <input id="askTextInput" type="${type}" class="field font-mono" value="${esc(value)}" autocomplete="off" data-autofocus>
                    </div>
                </div>
                <div class="px-5 py-3 flex flex-wrap justify-end gap-2 border-t border-gray-300 dark:border-[#334155]">
                    <button type="button" class="btn btn-secondary" data-close="dialog">Cancel</button>
                    <button type="submit" class="btn btn-primary">${esc(confirmLabel)}</button>
                </div>
            </form>`, { size: 'max-w-md' });
        const input = box.querySelector('input');
        let answer = null;
        box.querySelector('form').addEventListener('submit', (e) => {
            e.preventDefault();
            answer = input.value.trim() || null;
            closeDialog(true);
        });
        return new Promise((resolve) => { dialogResolve = (ok) => resolve(ok ? answer : null); });
    }

    // --- drawer: the forms (new server, settings, add/edit client) -----------------
    let drawerReturn = null;
    let drawerOnClose = null;

    // `onSubmit` runs for the form's submit (Enter, or the footer's type=submit button).
    function openDrawer({ title, sub = '', body, foot, onSubmit = null, onClose = null }) {
        closeMenu();
        if (!$('#drawerRoot').hidden) closeDrawer();
        $('#drawerTitle').innerHTML = title;
        $('#drawerSub').innerHTML = sub;
        $('#drawerBody').innerHTML = body;
        $('#drawerFoot').innerHTML = foot;
        $('#drawerForm').onsubmit = (e) => { e.preventDefault(); onSubmit?.(e); };
        drawerOnClose = onClose;
        drawerReturn = document.activeElement;
        const root = $('#drawerRoot');
        root.hidden = false;
        document.body.style.overflow = 'hidden';
        $('#toasts')?.classList.add('lift');
        requestAnimationFrame(() => root.classList.add('is-open'));
        setTimeout(() => $('#drawerBody input:not([type=checkbox]), #drawerBody select')?.focus(), reduceMotion() ? 0 : 60);
    }

    function closeDrawer() {
        const root = $('#drawerRoot');
        if (!root || root.hidden) return;
        root.classList.remove('is-open');
        root.hidden = true;
        document.body.style.overflow = '';
        $('#toasts')?.classList.remove('lift');
        $('#drawerForm').onsubmit = null;
        $('#drawerBody').innerHTML = '';
        const onClose = drawerOnClose;
        drawerOnClose = null;
        onClose?.();
        drawerReturn?.focus?.();
    }

    const isDrawerOpen = () => !$('#drawerRoot').hidden;

    // --- menu: the ⋯ buttons -------------------------------------------------------
    let menuAnchor = null;

    // items: [{ label, icon, danger?, run }] or '-' for a separator.
    function openMenu(anchor, items) {
        const reopen = menuAnchor !== anchor;
        closeMenu();
        if (!reopen) return; // a second click on the same button closes it
        const menu = $('#menu');
        menu.innerHTML = items.map((it, i) => (it === '-'
            ? '<div class="my-1 border-t border-gray-300 dark:border-[#334155]" role="separator"></div>'
            : `<button type="button" role="menuitem" data-i="${i}" class="w-full flex items-center gap-2.5 px-3 py-2 text-sm text-left ${it.danger
                ? 'text-red-600 hover:bg-red-50 dark:text-[#fca5a5] dark:hover:bg-[#3b1219]'
                : 'text-gray-800 hover:bg-gray-100 dark:text-[#e5e7eb] dark:hover:bg-[#273449]'} focus:outline-none focus-visible:bg-gray-100 dark:focus-visible:bg-[#273449]">${icon(it.icon)}${esc(it.label)}</button>`)).join('');
        menu.querySelectorAll('[data-i]').forEach((b) => b.addEventListener('click', () => {
            closeMenu();
            items[Number(b.dataset.i)].run();
        }));
        menu.hidden = false;
        const r = anchor.getBoundingClientRect();
        const w = menu.offsetWidth;
        const h = menu.offsetHeight;
        const left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
        const top = r.bottom + 4 + h > window.innerHeight - 8 ? r.top - h - 4 : r.bottom + 4;
        menu.style.left = `${left}px`;
        menu.style.top = `${Math.max(8, top)}px`;
        menuAnchor = anchor;
        anchor.setAttribute('aria-expanded', 'true');
        menu.querySelector('button')?.focus();
    }

    function closeMenu() {
        const menu = $('#menu');
        if (!menu || menu.hidden) {
            menuAnchor = null;
            return;
        }
        menu.hidden = true;
        menuAnchor?.setAttribute('aria-expanded', 'false');
        menuAnchor = null;
    }

    // --- inline rename (replaces prompt()) -----------------------------------------
    // Swaps `target` for a text field. Enter or leaving the field saves, Escape
    // cancels. onSave(newName) runs only for a real change and may return a promise;
    // if it rejects, the old name comes back and the error is shown.
    function startRename(target, { value, label, onSave, inputClass = 'text-base font-semibold' }) {
        if (!target || !target.isConnected) return;
        const input = document.createElement('input');
        input.type = 'text';
        input.value = value;
        input.className = `field h-8 w-48 sm:w-56 ${inputClass}`;
        input.setAttribute('aria-label', label || `New name for ${value}`);
        target.replaceWith(input);
        input.focus();
        input.select();
        let done = false;
        const finish = async (save) => {
            if (done) return;
            done = true;
            const name = input.value.trim();
            const changed = save && name && name !== value;
            target.textContent = changed ? name : value;
            if (input.isConnected) input.replaceWith(target);
            if (!changed) return;
            try {
                await onSave(name);
            } catch (error) {
                target.textContent = value;
                toast(error?.message || String(error), 'error');
            }
        };
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); finish(true); }
            if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
        });
        input.addEventListener('blur', () => finish(true));
    }

    // --- page-wide wiring ------------------------------------------------------------
    document.addEventListener('click', (e) => {
        const closer = e.target.closest('[data-close]');
        if (closer) {
            if (closer.dataset.close === 'drawer') closeDrawer(); else closeDialog(false);
            return;
        }
        if (!e.target.closest('#menu') && !e.target.closest('[aria-haspopup="menu"]')) closeMenu();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (!$('#menu').hidden) { const anchor = menuAnchor; closeMenu(); anchor?.focus(); return; }
        if (!$('#dialogRoot').hidden) { closeDialog(false); return; }
        if (!$('#drawerRoot').hidden) closeDrawer();
    });
    window.addEventListener('resize', closeMenu);
    window.addEventListener('scroll', closeMenu, true);

    window.Ui = {
        esc, icon, toast,
        openDialog, closeDialog, dialogHeader, confirm, askText,
        openDrawer, closeDrawer, isDrawerOpen,
        openMenu, closeMenu, startRename,
    };
})();
