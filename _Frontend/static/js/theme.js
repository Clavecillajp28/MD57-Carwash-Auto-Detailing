// MD57 theme - loaded in <head> on every page (the app and the sign-in
// pages), before anything paints, so a page never flashes the wrong theme.
// Dark is the default; light only when it was chosen (localStorage
// 'md57_theme'). Also marks the first page of a browser session, which
// shows the loader (see the MOTION section of core.css).
(function () {
    try {
        if (localStorage.getItem('md57_theme') !== 'light') document.documentElement.setAttribute('data-theme', 'dark');
    } catch (e) { document.documentElement.setAttribute('data-theme', 'dark'); }
    try {
        if (!sessionStorage.getItem('md57_loaded')) {
            sessionStorage.setItem('md57_loaded', '1');
            document.documentElement.classList.add('first-load');
        }
    } catch (e) {}
})();

function toggleTheme() {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    if (next === 'dark') document.documentElement.setAttribute('data-theme', 'dark');
    else document.documentElement.removeAttribute('data-theme');
    try { localStorage.setItem('md57_theme', next); } catch (e) {}
    syncThemeToggleIcon();
    // Chart.js draws onto <canvas>, which can't follow CSS variables - pages
    // with charts redraw on this.
    document.dispatchEvent(new CustomEvent('md57-theme-changed', { detail: { theme: next } }));
}

function syncThemeToggleIcon() {
    const icon = document.getElementById('themeToggleIcon');
    if (!icon) return;
    icon.className = document.documentElement.getAttribute('data-theme') === 'dark' ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
}

document.addEventListener('DOMContentLoaded', syncThemeToggleIcon);
