// Pop-up windows (.modal-overlay, shown with .open) - shared by the app
// (base.html) and the sign-in pages. A click on the dark backdrop closes it.
function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.add('open');
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.classList.remove('open');
}

window.addEventListener('click', event => {
    if (event.target.classList.contains('modal-overlay')) event.target.classList.remove('open');
});
