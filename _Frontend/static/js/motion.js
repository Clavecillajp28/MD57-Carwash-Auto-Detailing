// MD57 motion - loaded on every page (the app shell before common.js, and
// the sign-in pages on its own). No dependencies. The matching styles are
// the MOTION section of core.css.
//
//   page progress bar   a thin gold line while the next page loads
//   skeletons           shimmer placeholders while a list loads
//   setCount            numbers that count up to their value
//   renderAnimatedList  list items that slide in and out as the list changes
//   moveFrom            an item travelling from one list to another
//   MD57_EASE           the --ease curve, for motion drawn on a <canvas>
//
// Everything here stands down when the device asks for reduced motion.

const MD57_REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// ---------------------------------------------------------------- page progress
// Started by anything that leaves the page: a same-site link, a normal form
// submit, or a script calling startPageProgress() before it sets
// location.href. No 'beforeunload' listener - those keep a page out of the
// browser's back/forward cache.
function startPageProgress() {
    let bar = document.getElementById('pageProgress');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'pageProgress';
        bar.className = 'page-progress';
        document.body.appendChild(bar);
    }
    bar.classList.remove('run');
    void bar.offsetWidth;  // restart the animation
    bar.classList.add('run');
    // A click that turned out to be a file download never leaves the page.
    setTimeout(() => bar.classList.remove('run'), 5000);
}

document.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('a[href]');
    if (!link || link.target === '_blank' || link.hasAttribute('download')) return;
    const href = link.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('javascript:') || link.origin !== window.location.origin) return;
    startPageProgress();
});
document.addEventListener('submit', event => {
    // Checked a tick later: a form handled by fetch() calls preventDefault().
    setTimeout(() => { if (!event.defaultPrevented) startPageProgress(); }, 0);
});
window.addEventListener('pageshow', () => document.getElementById('pageProgress')?.classList.remove('run'));

// ---------------------------------------------------------------- skeletons
// skeletonHtml('cards:3')    three card-shaped blocks (a list of queue cards)
// skeletonHtml('lines:4')    four lines of text
// skeletonHtml('rows:4x3')   four table rows of three cells (inside a <tbody>)
function skeletonHtml(spec) {
    const [kind, size] = spec.split(':');
    const widths = [72, 54, 88, 63, 80, 47];
    if (kind === 'rows') {
        const [rows, cells] = size.split('x').map(Number);
        return Array.from({ length: rows }, (_, r) =>
            '<tr class="skeleton-row">' + Array.from({ length: cells }, (_, c) =>
                `<td><span class="skeleton skeleton-line" style="width: ${widths[(r + c) % widths.length]}%"></span></td>`
            ).join('') + '</tr>'
        ).join('');
    }
    const count = Number(size);
    if (kind === 'cards') {
        return Array.from({ length: count }, () => '<div class="skeleton skeleton-card"></div>').join('');
    }
    return Array.from({ length: count }, (_, i) =>
        `<span class="skeleton skeleton-line" style="width: ${widths[i % widths.length]}%"></span>`
    ).join('');
}

// Any element marked data-skeleton="cards:3" starts out showing that
// placeholder; the page's own render replaces it when the data arrives. If
// it never does (the request failed), say so rather than shimmer forever.
document.querySelectorAll('[data-skeleton]').forEach(el => { el.innerHTML = skeletonHtml(el.dataset.skeleton); });
setTimeout(() => {
    // data-skeleton-lazy: filled on demand (a pop-up nobody has opened yet), so not a failure.
    document.querySelectorAll('[data-skeleton]:not([data-skeleton-lazy])').forEach(el => {
        if (!el.querySelector(':scope > .skeleton, :scope > .skeleton-row')) return;
        const message = "Couldn't load this. Refresh the page to try again.";
        el.innerHTML = el.tagName === 'TBODY'
            ? `<tr><td colspan="12" class="skeleton-failed">${message}</td></tr>`
            : `<p class="skeleton-failed">${message}</p>`;
    });
}, 12000);

// ---------------------------------------------------------------- count-up
// setCount(el, 12)                          0 -> 12 the first time, then old -> new
// setCount(el, 34, n => `${n} <small>min</small>`)   with its own formatting
// A value that hasn't changed isn't re-animated, so refreshes stay quiet.
function setCount(el, value, render = n => String(n)) {
    if (!el) return;
    const target = Number(value);
    if (!Number.isFinite(target)) { el.innerHTML = render(value); return; }
    const from = Number.isFinite(el._count) ? el._count : 0;
    el._count = target;
    if (MD57_REDUCED_MOTION || from === target) { el.innerHTML = render(target); return; }
    const start = performance.now();
    const whole = Number.isInteger(target) && Number.isInteger(from);
    (function tick(now) {
        if (el._count !== target) return;  // a newer value took over
        const t = Math.min(1, (now - start) / 600);
        const eased = from + (target - from) * (1 - Math.pow(1 - t, 3));
        el.innerHTML = render(t === 1 ? target : whole ? Math.round(eased) : Math.round(eased * 10) / 10);
        if (t < 1) requestAnimationFrame(tick);
    })(start);
}

// ---------------------------------------------------------------- easing for drawn motion
// core.css's --ease (cubic-bezier(0.2, 0.8, 0.2, 1)) as a function of time
// 0..1, for motion painted frame by frame (a chart on a <canvas>).
function cubicBezier(x1, y1, x2, y2) {
    const at = (a1, a2, t) => (((1 - 3 * a2 + 3 * a1) * t + (3 * a2 - 6 * a1)) * t + 3 * a1) * t;
    return x => {
        if (x <= 0 || x >= 1) return Math.min(1, Math.max(0, x));
        let lo = 0, hi = 1, t = x;
        for (let i = 0; i < 20; i++) {   // the t where the curve's x is `x`
            t = (lo + hi) / 2;
            if (at(x1, x2, t) < x) lo = t; else hi = t;
        }
        return at(y1, y2, t);
    };
}
const MD57_EASE = cubicBezier(0.2, 0.8, 0.2, 1);

// ---------------------------------------------------------------- moving between lists
// rectsByKey(scope)   where each visible [data-key] element in scope is now
// moveFrom(el, rect)  plays el from that earlier position to where it is now -
//                     an item that moved to another list travels there
function rectsByKey(scope) {
    const rects = new Map();
    scope.querySelectorAll('[data-key]:not(.list-leave)').forEach(el => {
        const rect = el.getBoundingClientRect();
        if (rect.width) rects.set(el.dataset.key, { rect, el });
    });
    return rects;
}

function moveFrom(el, rect) {
    if (MD57_REDUCED_MOTION) return;
    const now = el.getBoundingClientRect();
    const dx = rect.left - now.left, dy = rect.top - now.top;
    if (!dx && !dy) return;
    el.classList.remove('list-enter');
    // --base and --ease (core.css)
    el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }],
               { duration: 250, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
}

// ---------------------------------------------------------------- animated lists
// Replaces container's children with `entries` ([{ key, html }], one root
// element per html). The first render fades the items in one after another;
// after that only what changed moves: new keys slide in, and keys that are
// gone slide out before being removed. An item whose html hasn't changed
// keeps its element, so a refresh never cuts off what it's animating. An
// empty list shows emptyHtml.
function renderAnimatedList(container, entries, emptyHtml) {
    if (!container) return;
    const previous = container._listKeys;  // undefined until the first render
    const old = new Map();
    container.querySelectorAll(':scope > [data-key]:not(.list-leave)').forEach((el, index) => old.set(el.dataset.key, { el, index }));

    const template = document.createElement('template');
    const fresh = entries.map(({ key, html }, index) => {
        const kept = old.get(String(key));
        if (kept && kept.el._html === html) return kept.el;
        template.innerHTML = html.trim();
        const el = template.content.firstElementChild;
        el._html = html;
        el.dataset.key = String(key);
        if (!MD57_REDUCED_MOTION && (!previous || !previous.has(String(key)))) {
            el.classList.add('list-enter');
            el.style.setProperty('--i', previous ? 0 : Math.min(index, 8));
            // Done once - a kept element that later changes place mustn't replay it.
            el.addEventListener('animationend', () => el.classList.remove('list-enter'), { once: true });
        }
        return el;
    });

    // Into order, moving only what has to move (a moved element restarts its CSS animations).
    const wanted = new Set(fresh);
    [...container.childNodes].forEach(node => { if (!wanted.has(node)) node.remove(); });
    fresh.forEach((el, i) => { if (container.children[i] !== el) container.insertBefore(el, container.children[i] || null); });
    if (!fresh.length && emptyHtml) container.innerHTML = emptyHtml;

    if (previous && !MD57_REDUCED_MOTION) {
        const kept = new Set(entries.map(entry => String(entry.key)));
        old.forEach(({ el, index }, key) => {
            if (kept.has(key)) return;
            el.classList.remove('list-enter');
            el.classList.add('list-leave');
            container.insertBefore(el, container.children[index] || null);
            setTimeout(() => el.remove(), 260);
        });
    }
    container._listKeys = new Set(entries.map(entry => String(entry.key)));
}
