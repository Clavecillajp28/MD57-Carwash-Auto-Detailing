// Generic multi-step form navigator - powers the stepper markup (see
// core.css's .stepper/.stepper-step/.form-step rules) on booking.html,
// create_user.html and register.html - its own file so the sign-in pages,
// which don't load common.js, share it too.
//
// Expects, inside `formEl`: one `.form-step[data-step="N"]` per step (step
// 1 visible, the rest starting `style="display:none"` in the markup), a
// matching `.stepper-step[data-step="N"]` per step in the indicator, and
// `.stepper-back-btn` / `.stepper-next-btn` / `.stepper-submit-btn`
// buttons. `validateStepFn(stepNumber)` is caller-supplied (each form's
// required fields differ) - return false and show your own toast to
// block advancing past an incomplete step; omit it to never block.
// `onStepShown(stepNumber)` fires after every render (e.g. to refresh a
// review-step summary right before it's shown).
function initStepper(formEl, totalSteps, validateStepFn, onStepShown) {
    let current = 1;
    // The step indicator may sit just outside the form (register.html).
    const indicatorScope = formEl.querySelector('.stepper-step') ? formEl : formEl.parentElement;

    function render() {
        formEl.querySelectorAll('.form-step').forEach(el => {
            el.style.display = (parseInt(el.dataset.step, 10) === current) ? '' : 'none';
        });
        indicatorScope.querySelectorAll('.stepper-step').forEach(el => {
            const n = parseInt(el.dataset.step, 10);
            el.classList.toggle('active', n === current);
            el.classList.toggle('completed', n < current);
        });
        const backBtn = formEl.querySelector('.stepper-back-btn');
        const nextBtn = formEl.querySelector('.stepper-next-btn');
        const submitBtn = formEl.querySelector('.stepper-submit-btn');
        if (backBtn) backBtn.style.display = current === 1 ? 'none' : '';
        if (nextBtn) nextBtn.style.display = current === totalSteps ? 'none' : '';
        if (submitBtn) submitBtn.style.display = current === totalSteps ? '' : 'none';
        if (typeof onStepShown === 'function') onStepShown(current);
        formEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function next() {
        if (typeof validateStepFn === 'function' && !validateStepFn(current)) return;
        if (current < totalSteps) { current++; render(); }
    }
    function back() {
        if (current > 1) { current--; render(); }
    }

    const backBtn = formEl.querySelector('.stepper-back-btn');
    const nextBtn = formEl.querySelector('.stepper-next-btn');
    if (backBtn) backBtn.addEventListener('click', back);
    if (nextBtn) nextBtn.addEventListener('click', next);

    render();
    return { next, back, current: () => current };
}
