// MD57 Car Wash - shared page behavior (toast, logout, CSRF, theme toggle).
// Loaded on every authenticated page via base.html. Depends on the
// <meta name="csrf-token"> tag and the #floating-toast markup that
// base.html also provides.

(function() {
    // Attach the CSRF token to every non-GET fetch automatically, so page
    // scripts never have to remember to do it themselves.
    const originalFetch = window.fetch;
    const bookingKeys = new Map();
    let csrfRefresh = null;
    window.fetch = function(url, options) {
        options = options || {};
        const method = (options.method || 'GET').toUpperCase();
        const sameOrigin = new URL(url instanceof Request ? url.url : url, location.href).origin === location.origin;
        if (sameOrigin && method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
            const token = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            if (token) {
                if (options.headers instanceof Headers) {
                    if (!options.headers.has('X-CSRFToken')) options.headers.append('X-CSRFToken', token);
                } else if (Array.isArray(options.headers)) {
                    options.headers.push(['X-CSRFToken', token]);
                } else {
                    options.headers = options.headers || {};
                    if (!options.headers['X-CSRFToken']) options.headers['X-CSRFToken'] = token;
                }
            }
        }
        // Retain a key after a lost/5xx response, including a page refresh.
        // A changed form or a confirmed response gets a fresh request key.
        const booking = method === 'POST' && String(url) === '/api/submit-booking';
        let bookingBody;
        if (booking && typeof options.body === 'string') {
            bookingBody = options.body;
            let pending;
            try { pending = JSON.parse(sessionStorage.getItem('md57_pending_booking') || 'null'); } catch (_) {}
            const key = pending?.body === bookingBody ? pending.key : (bookingKeys.get(bookingBody) || crypto.randomUUID());
            bookingKeys.set(bookingBody, key);
            try { sessionStorage.setItem('md57_pending_booking', JSON.stringify({body:bookingBody, key})); } catch (_) {}
            options.headers = new Headers(options.headers || {});
            options.headers.set('Idempotency-Key', key);
        }
        return originalFetch(url, options).then(async response=>{
            // A stale page or another tab can leave the signed token out of
            // sync with the session cookie. Retry only an explicit CSRF
            // rejection (the handler has not run), never an arbitrary error.
            if (sameOrigin && response.status === 400 && !(url instanceof Request)
                    && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
                const error = await response.clone().json().catch(() => null);
                if (error?.code === 'csrf_failed') {
                    if (!csrfRefresh) {
                        csrfRefresh = originalFetch('/api/csrf-token', {
                            credentials: 'same-origin', cache: 'no-store',
                        }).then(async result => {
                            if (!result.ok) throw new Error('Session expired');
                            const data = await result.json();
                            if (!data.csrf_token) throw new Error('Missing security token');
                            document.querySelector('meta[name="csrf-token"]')?.setAttribute('content', data.csrf_token);
                            return data.csrf_token;
                        }).finally(() => { csrfRefresh = null; });
                    }
                    const freshToken = await csrfRefresh.catch(() => null);
                    if (freshToken) {
                        const headers = new Headers(options.headers || {});
                        headers.set('X-CSRFToken', freshToken);
                        response = await originalFetch(url, {...options, headers});
                    }
                }
            }
            if (booking && response.status < 500) {
                bookingKeys.delete(bookingBody);
                try { sessionStorage.removeItem('md57_pending_booking'); } catch (_) {}
            }
            if(response.status===403&&String(url).startsWith('/api/')){
                const data=await response.clone().json().catch(()=>null);
                if(data?.code==='step_up_required') window.location.assign('/account-security');
            }
            return response;
        });
    };
})();

// toggleTheme / syncThemeToggleIcon live in theme.js (loaded in <head>).

// The current theme's value of a colour token, for anything drawn on a
// <canvas> (Chart.js), which can't read CSS variables itself:
//     themeColor('--chart-1')        -> '#d4b25e'
//     themeColor('--chart-1', 0.35)  -> 'rgba(212, 178, 94, 0.35)'
// Charts redraw on the 'md57-theme-changed' event, so they follow the theme.
function themeColor(token, alpha) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
    const hex = /^#([0-9a-f]{6})$/i.exec(value);
    if (alpha === undefined || !hex) return value;
    const n = parseInt(hex[1], 16);
    return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

// Mobile off-canvas sidebar drawer (see the max-width:768px rules in
// style.css) - on desktop .sidebar-toggle-btn is display:none, so these
// never fire there; only the mobile drawer reads .sidebar's 'open' class.
function toggleSidebar() {
    const sidebar = document.getElementById('primarySidebar');
    if (!sidebar) return;
    if (sidebar.classList.contains('open')) {
        closeSidebar();
    } else {
        openSidebar();
    }
}

function openSidebar() {
    const sidebar = document.getElementById('primarySidebar');
    const backdrop = document.getElementById('sidebarBackdrop');
    const btn = document.getElementById('sidebarToggleBtn');
    const icon = document.getElementById('sidebarToggleIcon');
    if (!sidebar) return;
    sidebar.classList.add('open');
    if (backdrop) backdrop.classList.add('show');
    if (btn) btn.setAttribute('aria-expanded', 'true');
    if (icon) icon.className = 'fa-solid fa-xmark';
    document.body.style.overflow = 'hidden';
}

function closeSidebar() {
    const sidebar = document.getElementById('primarySidebar');
    const backdrop = document.getElementById('sidebarBackdrop');
    const btn = document.getElementById('sidebarToggleBtn');
    const icon = document.getElementById('sidebarToggleIcon');
    if (!sidebar) return;
    sidebar.classList.remove('open');
    if (backdrop) backdrop.classList.remove('show');
    if (btn) btn.setAttribute('aria-expanded', 'false');
    if (icon) icon.className = 'fa-solid fa-bars';
    document.body.style.overflow = '';
}

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeSidebar();
});

// A viewport resize past the mobile breakpoint (e.g. rotating to
// landscape, or a resizable desktop test window) should drop the
// drawer-open state - otherwise .sidebar's transform/open styling could
// carry over stale once the media query stops applying it.
window.addEventListener('resize', () => {
    if (window.innerWidth > 768) closeSidebar();
});


// Pages that include a #logoutAnimationModal show it briefly before
// redirecting (its optional data-delay overrides the default); everywhere
// else signs out immediately.
async function handleLogout() {
    if (!(await confirmDialog("You will need to sign in again to continue.", { title: 'Sign out?', confirmText: 'Sign Out' }))) return;
    try { sessionStorage.removeItem('md57_pending_booking'); } catch (_) {}
    const farewell = document.getElementById('logoutAnimationModal');
    if (!farewell) {
        startPageProgress();
        window.location.href = '/logout';
        return;
    }
    farewell.style.display = 'flex';
    setTimeout(() => { window.location.href = '/logout'; }, Number(farewell.dataset.delay) || 2200);
}

// The system's own confirmation window, in place of the browser's grey
// confirm() box. Resolves true if confirmed, false if cancelled, closed
// with Escape, or dismissed by clicking outside it:
//     if (!(await confirmDialog('Cancel this booking?', { danger: true }))) return;
function confirmDialog(message, options = {}) {
    const { title = 'Are you sure?', confirmText = 'Confirm', cancelText = 'Cancel', danger = false } = options;
    let overlay = document.getElementById('confirmDialog');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'confirmDialog';
        overlay.className = 'modal-overlay confirm-dialog';
        overlay.innerHTML = `
            <div class="modal-content" role="alertdialog" aria-modal="true" aria-labelledby="confirmDialogTitle">
                <h3 id="confirmDialogTitle"></h3>
                <p id="confirmDialogMessage"></p>
                <div class="confirm-dialog-actions">
                    <button type="button" class="btn-secondary" id="confirmDialogCancel"></button>
                    <button type="button" id="confirmDialogOk"></button>
                </div>
            </div>`;
        document.body.appendChild(overlay);
    }
    const okBtn = document.getElementById('confirmDialogOk');
    const cancelBtn = document.getElementById('confirmDialogCancel');
    document.getElementById('confirmDialogTitle').innerText = title;
    document.getElementById('confirmDialogMessage').innerText = message;
    okBtn.innerText = confirmText;
    okBtn.className = danger ? 'btn-danger' : 'btn-primary';
    cancelBtn.innerText = cancelText;

    return new Promise(resolve => {
        const finish = result => {
            overlay.classList.remove('open');
            okBtn.removeEventListener('click', onOk);
            cancelBtn.removeEventListener('click', onCancel);
            overlay.removeEventListener('click', onBackdrop);
            document.removeEventListener('keydown', onKey);
            resolve(result);
        };
        const onOk = () => finish(true);
        const onCancel = () => finish(false);
        const onBackdrop = e => { if (e.target === overlay) finish(false); };
        const onKey = e => { if (e.key === 'Escape') finish(false); };
        okBtn.addEventListener('click', onOk);
        cancelBtn.addEventListener('click', onCancel);
        overlay.addEventListener('click', onBackdrop);
        document.addEventListener('keydown', onKey);
        overlay.classList.add('open');
        okBtn.focus();
    });
}

let toastTimer = null;

function showToast(title, message, type = 'success') {
    const toast = document.getElementById('floating-toast');
    const toastTitle = document.getElementById('toast-title');
    const toastMsg = document.getElementById('toast-message');
    const toastIcon = document.getElementById('toast-icon');
    if (!toast || !toastTitle || !toastMsg || !toastIcon) return;

    toast.className = '';
    toast.classList.add(type);
    toastTitle.innerText = title;
    toastMsg.innerText = message;
    toastIcon.innerHTML = type === 'success'
        ? '<i class="fa-solid fa-circle-check" style="color: var(--accent-green);"></i>'
        : '<i class="fa-solid fa-circle-exclamation" style="color: var(--accent-red);"></i>';
    toast.classList.add('show');

    // One timer: an earlier message's timer must not hide this one early.
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.classList.remove('show'); }, 3500);
}

// Escapes a value for safe insertion as HTML text/attribute content.
function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

const md57Actions = new Map();
function registerActions(actions) {
    for (const [name, handler] of Object.entries(actions)) md57Actions.set(name, handler);
}
function actionAttributes(name, args = []) {
    return `data-md57-action="${escapeHtml(name)}" data-md57-args="${escapeHtml(JSON.stringify(args))}"`;
}
function dispatchAction(event) {
    if (event.type === 'keydown' && !['Enter', ' '].includes(event.key)) return;
    const element = event.target.closest('[data-md57-action]');
    if (!element || (event.type === 'keydown' && element.tagName === 'BUTTON')) return;
    const handler = md57Actions.get(element.dataset.md57Action);
    if (!handler) return;
    event.preventDefault();
    const args = JSON.parse(element.dataset.md57Args || '[]');
    handler(...args);
}
document.addEventListener('click', dispatchAction);
document.addEventListener('keydown', dispatchAction);

// --- IN-APP NOTIFICATION CENTER (bell icon in base.html's header) ---
// Independent of email/Gmail preferences by design (see the backend's
// utils.create_notification). New notifications arrive as live updates
// (see startLiveUpdates below); the slow poll only covers a dropped
// stream. Only runs at all on pages where the bell exists (base.html only
// renders it for a logged-in session), so anonymous pages like /login do
// nothing here.
let notifPollTimer = null;

function initNotificationCenter() {
    const bell = document.getElementById('notifBellBtn');
    if (!bell) return; // not logged in - nothing to poll
    refreshNotifications();
    if (notifPollTimer) clearInterval(notifPollTimer);
    notifPollTimer = setInterval(refreshNotifications, 2 * LIVE_FALLBACK_POLL_MS);

    document.addEventListener('click', (e) => {
        const wrap = document.querySelector('.notif-bell-wrap');
        const panel = document.getElementById('notifPanel');
        if (!wrap || !panel || !panel.classList.contains('show')) return;
        if (!wrap.contains(e.target)) closeNotifPanel();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeNotifPanel();
    });
}

function toggleNotifPanel() {
    const panel = document.getElementById('notifPanel');
    if (!panel) return;
    if (panel.classList.contains('show')) {
        closeNotifPanel();
    } else {
        panel.classList.add('show');
        document.getElementById('notifBellBtn')?.setAttribute('aria-expanded', 'true');
        refreshNotifications();
    }
}

function closeNotifPanel() {
    const panel = document.getElementById('notifPanel');
    if (!panel) return;
    panel.classList.remove('show');
    document.getElementById('notifBellBtn')?.setAttribute('aria-expanded', 'false');
}

function refreshNotifications() {
    fetch('/api/notifications?limit=30')
        .then(res => res.ok ? res.json() : null)
        .then(data => {
            if (!data || data.status !== 'success') return;
            renderNotifBadge(data.unread_count || 0);
            renderNotifList(data.data || []);
        })
        .catch(() => {});
}

function renderNotifBadge(unreadCount) {
    const badge = document.getElementById('notifBadge');
    if (!badge) return;
    if (unreadCount > 0) {
        badge.hidden = false;
        badge.innerText = unreadCount > 99 ? '99+' : String(unreadCount);
    } else {
        badge.hidden = true;
    }
}

function renderNotifList(items) {
    const list = document.getElementById('notifPanelList');
    if (!list) return;
    if (!items.length) {
        list.innerHTML = '<div class="notif-empty"><i class="fa-regular fa-bell-slash"></i> No notifications yet.</div>';
        return;
    }
    // Each notification is a link to the page it's about (n.link, decided
    // by the server for this account's role - see notifications_api.py).
    list.innerHTML = items.map(n => `
        <a class="notif-item ${n.is_read ? '' : 'unread'}" href="${escapeHtml(n.link || '/')}" onclick="handleNotifClick(event, ${Number(n.notification_id)}, this)">
            <div class="notif-item-title">${escapeHtml(n.title)}</div>
            <div class="notif-item-message">${escapeHtml(n.message)}</div>
            <div class="notif-item-time">${escapeHtml(formatRelativeTime(n.created_at))}</div>
        </a>
    `).join('');
}

// Marks the notification read, then goes where it points - in that order,
// so the badge is already right when the next page loads.
function handleNotifClick(event, notificationId, el) {
    event.preventDefault();
    const go = () => { startPageProgress(); window.location.href = el.getAttribute('href'); };
    if (!el.classList.contains('unread')) {
        go();
        return;
    }
    el.classList.remove('unread');
    fetch('/api/notifications/mark-read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ notification_id: notificationId }),
        keepalive: true
    })
    .catch(() => {})
    .finally(go);
}

// A notification link names one booking as '?booking=<id>'. Returns that
// id (or null) and takes it out of the address bar, so refreshing the page
// doesn't bring the same booking up again. Each page calls this once and
// acts on it after its first data load.
function takeFocusBookingId() {
    const params = new URLSearchParams(window.location.search);
    const id = Number(params.get('booking'));
    if (!id) return null;
    params.delete('booking');
    const query = params.toString();
    history.replaceState(null, '', window.location.pathname + (query ? '?' + query : ''));
    return id;
}

function markAllNotificationsRead() {
    fetch('/api/notifications/mark-all-read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
    })
        .then(() => refreshNotifications())
        .catch(() => {});
}

// Coarse relative-time label ("Just now" / "5m ago" / "3h ago" / "2d ago")
// for a 'YYYY-MM-DD HH:MM:SS' timestamp - notifications don't need
// second-level precision, just a quick sense of how recent they are.
function formatRelativeTime(sqlTimestamp) {
    if (!sqlTimestamp) return '';
    const then = parseServerDateTime(sqlTimestamp);
    if (!then) return sqlTimestamp;
    const diffMs = Date.now() - then;
    const mins = Math.floor(diffMs / 60000);
    if (mins < 1) return 'Just now';
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
}

function parseServerDateTime(sqlTimestamp) {
    // 'YYYY-MM-DD HH:MM:SS' -> epoch ms, treated as local time (matches how
    // this app writes every timestamp - see database.py's created_at
    // comments), or null if missing/malformed. A plain
    // `new Date('YYYY-MM-DD HH:MM:SS')` is not reliably parsed across
    // browsers, so this is built explicitly.
    const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(sqlTimestamp || '');
    if (!m) return null;
    const [, y, mo, d, h, mi, s] = m.map(Number);
    return new Date(y, mo - 1, d, h, mi, s).getTime();
}

function serviceEstimateLines(estimate) {
    if (!estimate) return ['Estimate unavailable. Please refresh.'];
    const wait = ['in_progress', 'overdue'].includes(estimate.status) ? 'Service has started'
        : estimate.status === 'awaiting_confirmation' && estimate.estimated_start_at ? 'Wash finished'
        : estimate.queue_wait_minutes == null ? 'Awaiting availability'
        : estimate.queue_wait_minutes === 0 ? '0 min (no queue delay expected)'
        : `about ${formatMinutes(estimate.queue_wait_minutes)}`;
    const lines = [
        `Wait before service: ${wait}`,
        `Service duration: about ${formatMinutes(estimate.service_minutes)}`,
        `Estimated finish: ${estimate.estimated_finish_at ? formatTimestamp(estimate.estimated_finish_at) : estimate.message}`,
    ];
    if (estimate.estimated_finish_at && ['scheduled', 'overdue'].includes(estimate.status)) lines.push(estimate.message);
    return lines;
}

function serviceEstimateSummary(estimate) {
    return serviceEstimateLines(estimate).join(' Â· ');
}

function renderServiceEstimate(container, estimate) {
    container.replaceChildren();
    serviceEstimateLines(estimate).forEach(line => {
        const row = document.createElement('div');
        row.textContent = line;
        container.appendChild(row);
    });
    if (estimate && estimate.estimated_finish_at && !['scheduled', 'overdue'].includes(estimate.status)) {
        const note = document.createElement('small');
        note.textContent = estimate.message;
        container.appendChild(note);
    }
}

document.addEventListener('DOMContentLoaded', initNotificationCenter);

// --- KPI STRIP (Home, admin/staff) ---
// Fills `${prefix}-queue-count`, `-completed-today`, `-avg-wait-today` and
// `-active-staff` from /api/admin/metrics. Resolves true once rendered,
// false if the server answered with an error; rejects on a network error.
function loadKpiStrip(prefix = 'home') {
    return fetch('/api/admin/metrics')
        .then(res => res.json())
        .then(data => {
            if (data.status !== 'success') return false;
            // setCount (motion.js): the numbers count up, and only when they change.
            setCount(document.getElementById(`${prefix}-queue-count`), data.queue_count);
            const upcoming = document.getElementById(`${prefix}-upcoming-count`);
            if (upcoming) {
                upcoming.textContent = data.upcoming_count
                    ? `+ ${data.upcoming_count} booking${data.upcoming_count === 1 ? '' : 's'} for later days` : 'No bookings for later days';
            }
            setCount(document.getElementById(`${prefix}-completed-today`), data.completed_today);
            const avgWait = document.getElementById(`${prefix}-avg-wait-today`);
            if (data.avg_wait_today_minutes !== null && data.avg_wait_today_minutes !== undefined) {
                setCount(avgWait, data.avg_wait_today_minutes, n => `${n} <small>min</small>`);
            } else {
                avgWait.innerHTML = '<small>No completions yet today</small>';
            }
            setCount(document.getElementById(`${prefix}-active-staff`), data.active_staff);
            return true;
        });
}

// --- BOOKING TIMES ---
// Offered by the server per service and bay (/api/available-times,
// scheduling.py) - every 30 minutes from 8:00 AM, finishing by 9:00 PM.

// --- SERVICE / PRICING CATALOG ---
// /api/services is the one real source of truth for what services exist,
// what each costs per vehicle size, and which bay it routes to - shared by
// the Pricing modal below, the booking form (booking.html), and the
// walk-in modal (queue_dashboard.html), so all three can never drift out
// of sync with each other or with the shop's real published price list the
// way the old hardcoded "3 services"/"4 vehicle types" copies eventually did.
function formatPeso(amount) {
    if (amount === null || amount === undefined || amount === '') return 'N/A';
    return '\u20b1' + Number(amount).toLocaleString('en-PH', { maximumFractionDigits: 0 });
}

async function fetchServiceCatalog() {
    const res = await fetch('/api/services');
    const data = await res.json();
    if (data.status !== 'success') throw new Error(data.message || 'Failed to load services.');
    return data;
}

function populateVehicleTypeSelect(selectId, catalog) {
    const select = document.getElementById(selectId);
    if (!select) return;
    select.innerHTML = catalog.vehicle_types.map(vt =>
        `<option value="${escapeHtml(vt.type_name)}">${escapeHtml(vt.type_name)}</option>`
    ).join('');
}

// Fills a car model field's <datalist> with the models /api/services
// suggests. The field stays free text - a model that isn't listed is kept
// as typed.
function populateVehicleModelList(listId, catalog) {
    const list = document.getElementById(listId);
    if (!list) return;
    list.innerHTML = (catalog.vehicle_models || []).map(model =>
        `<option value="${escapeHtml(model)}"></option>`
    ).join('');
}

// 'Toyota Vios (Medium)' - or just the size for a booking made before the
// model was recorded.
function describeVehicle(item) {
    const size = item.vehicle_type || 'Vehicle';
    return item.vehicle_model ? `${item.vehicle_model} (${size})` : size;
}

// A return interval in days as text: 3 -> '3 days', 21 -> '3 weeks'.
function formatReturnInterval(days) {
    if (days === null || days === undefined) return '';
    if (days < 14) {
        const n = Math.round(days * 10) / 10;
        return `${n} day${n === 1 ? '' : 's'}`;
    }
    return `${Math.round(days / 7 * 10) / 10} weeks`;
}

// Only ever lists services that actually have a price for the given
// vehicle_type - a motorcycle-only service never appears once a car size
// is selected, and vice versa, so the dropdown can't offer a combination
// /api/submit-booking would reject.
function populateServiceSelectForVehicleType(selectId, catalog, vehicleType) {
    const select = document.getElementById(selectId);
    if (!select || !catalog || !vehicleType) return;
    let html = '';
    Object.values(catalog.categories).forEach(cat => {
        const available = cat.services.filter(svc => vehicleType in svc.prices);
        if (available.length === 0) return;
        const options = available.map(svc =>
            `<option value="${svc.service_id}">${escapeHtml(svc.name)} (${escapeHtml(svc.default_bay)})</option>`
        ).join('');
        html += `<optgroup label="${escapeHtml(cat.label)}">${options}</optgroup>`;
    });
    select.innerHTML = html || '<option value="">No services available for this vehicle size</option>';
}

function findServiceInCatalog(catalog, serviceId) {
    if (!catalog || !serviceId) return null;
    for (const cat of Object.values(catalog.categories)) {
        const found = cat.services.find(s => String(s.service_id) === String(serviceId));
        if (found) return found;
    }
    return null;
}

// --- PRICING MODAL (base.html - available from every page's sidebar) ---
// Rendered dynamically from the same /api/services catalog above (fetched
// once, the first time the modal is opened) instead of hardcoded prices in
// the template - the whole point of storing pricing in the database is
// that an admin can update it from the pricing management page without
// touching any template.
let _pricingModalCatalog = null;

// Opens the price list, loading it the first time.
async function openPricingModal() {
    openModal('pricingModal');
    if (_pricingModalCatalog) return; // already loaded this session
    const body = document.getElementById('pricingModalBody');
    if (!body) return;
    try {
        _pricingModalCatalog = await fetchServiceCatalog();
        renderPricingModalCategory('general');
    } catch (err) {
        body.innerHTML = '<p style="text-align:center; color: var(--text-muted); padding: 20px 0;">Could not load pricing right now. Please try again later.</p>';
    }
}

function renderPricingModalCategory(categoryKey) {
    const body = document.getElementById('pricingModalBody');
    if (!body || !_pricingModalCatalog) return;
    const cat = _pricingModalCatalog.categories[categoryKey];
    if (!cat) return;

    const sizes = _pricingModalCatalog.vehicle_types.map(vt => vt.type_name)
        .filter(size => cat.services.some(svc => size in svc.prices));

    const tieredServices = cat.services.filter(svc => !svc.fixed_price);
    const fixedServices = cat.services.filter(svc => svc.fixed_price);

    let html = '';
    if (tieredServices.length) {
        html += '<div class="pricing-table-wrap"><table class="pricing-table"><thead><tr><th>Service</th>' +
            sizes.map(s => `<th>${escapeHtml(s)}</th>`).join('') + '</tr></thead><tbody>' +
            tieredServices.map(svc => `<tr><td>${escapeHtml(svc.name)}</td>` +
                sizes.map(s => `<td>${s in svc.prices ? formatPeso(svc.prices[s]) : '&mdash;'}</td>`).join('') +
                '</tr>').join('') +
            '</tbody></table></div>';
    }
    if (fixedServices.length) {
        html += `<h4 class="pricing-fixed-title">Fixed Price &mdash; All Vehicle Types</h4><ul class="pricing-fixed-list">` +
            fixedServices.map(svc => `<li><span>${escapeHtml(svc.name)}</span><strong>${formatPeso(Object.values(svc.prices)[0])}</strong></li>`).join('') +
            `</ul>`;
    }
    if (!tieredServices.length && !fixedServices.length) {
        html = '<p style="text-align:center; color: var(--text-muted); padding: 20px 0;">No services in this category yet.</p>';
    }

    document.getElementById('pricingModalBody').innerHTML = html;
}

function switchPricingTab(tabKey, btnEl) {
    document.querySelectorAll('.pricing-tab-btn').forEach(b => b.classList.toggle('active', b === btnEl));
    renderPricingModalCategory(tabKey);
}

// See openPricingModal's comment on why this manipulates the class
// directly instead of calling the generic closeModal() - that name is
// shadowed with a different (style.display-only) implementation on
// several pages, which would leave this modal not actually hidden again.
// Maps a queue/history/ready item to which of the 3 physical bays it
// belongs to: its real assigned_bay, or - only for a row with no
// recognizable bay - a guess from the service name.
function getNormalizedBayKey(item) {
    const bayStr = (item.assigned_bay || "").toLowerCase();
    for (const bay of ['Bay #1', 'Bay #2', 'Bay #3']) {
        if (bayStr.includes(bay.toLowerCase())) return bay;
    }
    const serviceStr = (item.service_type || "").toLowerCase();
    if (serviceStr.includes('detailing') || serviceStr.includes('coating') || serviceStr.includes('full body')) return "Bay #3";
    if (serviceStr.includes('express') || serviceStr.includes('foam')) return "Bay #2";
    return "Bay #1";
}

// Local YYYY-MM-DD, matching booking_date/date_str's stored format - not
// toISOString(), which is UTC and lands on the WRONG calendar day for
// roughly a third of every day in this app's own timezone (PH, UTC+8):
// any local time between midnight and 8:00 AM still reads as "yesterday"
// in UTC. Every page uses this one, so the browser and the server always
// agree on what "today" is.
function getTodayDateStr() {
    return getDateStrOffset(0);
}

// Same local-date logic, offset by `days` (e.g. getDateStrOffset(1) for
// "tomorrow") - the toISOString()-based `new Date(Date.now() + 86400000)`
// pattern this replaces had the identical UTC bug as getTodayDateStr did.
function getDateStrOffset(days) {
    let d = new Date();
    d.setDate(d.getDate() + days);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

// A minute count as people say it: "45 min", "1 h 20 min", "2 h".
function formatMinutes(minutes) {
    const m = Math.max(0, Math.round(Number(minutes) || 0));
    if (m < 60) return `${m} min`;
    const h = Math.floor(m / 60), rest = m % 60;
    return rest ? `${h} h ${rest} min` : `${h} h`;
}

// A booking's day ('YYYY-MM-DD') and time ('09:00 AM') as "Today Â· 9:00 AM",
// "Tomorrow Â· 11:30 AM" or "Thu, Oct 8 Â· 1:30 PM" (the year only when it
// isn't this year).
function formatBookingWhen(dateStr, timeStr) {
    const time = (timeStr || '').replace(/^0(\d)/, '$1');
    if (!dateStr) return time;
    let day;
    if (dateStr === getDateStrOffset(0)) day = 'Today';
    else if (dateStr === getDateStrOffset(1)) day = 'Tomorrow';
    else if (dateStr === getDateStrOffset(-1)) day = 'Yesterday';
    else {
        const [y, mo, d] = dateStr.split('-').map(Number);
        day = new Date(y, mo - 1, d).toLocaleDateString('en-US', {
            weekday: 'short', month: 'short', day: 'numeric',
            ...(y !== new Date().getFullYear() ? { year: 'numeric' } : {}),
        });
    }
    return time ? `${day} Â· ${time}` : day;
}

// A server timestamp ('YYYY-MM-DD HH:MM:SS') the same way: "Today Â· 2:22 PM".
function formatTimestamp(sqlTimestamp, fallback = 'N/A') {
    const ms = parseServerDateTime(sqlTimestamp);
    if (ms === null) return sqlTimestamp || fallback;
    const d = new Date(ms);
    const dateStr = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    return formatBookingWhen(dateStr, d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true }));
}

// 1 -> "1st", 2 -> "2nd", 11 -> "11th", 23 -> "23rd".
function ordinal(n) {
    const tens = n % 100;
    if (tens >= 11 && tens <= 13) return `${n}th`;
    return n + ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] || 'th');
}

// --- LIVE UPDATES (Server-Sent Events - see the backend's live_updates.py) ---
// The server pushes 'data' whenever something pages display changes (on
// any device, not just this browser) and 'notifications' when this
// account gets one. Pages register what to re-fetch with onLiveUpdate();
// its slow fallback poll covers any gap while the stream reconnects.
const LIVE_FALLBACK_POLL_MS = 60000;

function onLiveUpdate(refresh) {
    document.addEventListener('md57-data-changed', refresh);
    setInterval(refresh, LIVE_FALLBACK_POLL_MS);
}

function startLiveUpdates() {
    if (!document.getElementById('notifBellBtn') || !window.EventSource) return; // not logged in
    if (document.body.dataset.liveUpdates === 'off') return;
    const source = new EventSource('/api/live');
    source.addEventListener('polling-only',()=>source.close());
    source.addEventListener('access-revoked', () => {
        source.close();
        window.location.replace('/login');
    });
    let refreshTimer = null;
    source.addEventListener('data', () => {
        // One action often announces several changes - refresh once.
        clearTimeout(refreshTimer);
        refreshTimer = setTimeout(() => document.dispatchEvent(new CustomEvent('md57-data-changed')), 250);
    });
    source.addEventListener('notifications', refreshNotifications);
}

document.addEventListener('DOMContentLoaded', startLiveUpdates);

// Shared by every CSV export button on this site - the column choice
// stays page-specific; quoting and the download itself happen here.
// History/Cancelled date filter: is a 'YYYY-MM-DD HH:MM:SS' timestamp
// within the From/To date inputs (either may be empty)? And the matching
// file-name part for an export.
function inDateRange(timestamp, fromId, toId) {
    const from = document.getElementById(fromId).value, to = document.getElementById(toId).value;
    const day = (timestamp || '').slice(0, 10);
    if (!day) return !from && !to;
    return (!from || day >= from) && (!to || day <= to);
}
function dateRangeLabel(fromId, toId) {
    const from = document.getElementById(fromId).value, to = document.getElementById(toId).value;
    if (!from && !to) return `all_to_${getTodayDateStr()}`;
    return `${from || 'start'}_to_${to || getTodayDateStr()}`;
}

function downloadCSV(filename, header, rows) {
    const cell = value => {
        let text = value === null || value === undefined ? '' : String(value);
        if (typeof value === 'string' && /^[\s\u0000-\u001f]*[=+@-]/.test(text)) text = "'" + text;
        return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    };
    const csv = [header, ...rows].map(row => row.map(cell).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
}

