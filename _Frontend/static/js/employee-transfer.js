(() => {
    'use strict';
    const form = document.getElementById('employeeImport');
    if (!form) return;
    const message = document.getElementById('employeeImportResult');
    const input = document.getElementById('employeeTransferFile');
    input.addEventListener('change', () => {
        message.classList.remove('is-error', 'is-success');
        message.textContent = '';
    });
    form.addEventListener('submit', async event => {
        event.preventDefault();
        const button = form.querySelector('button');
        const file = input.files[0];
        if (!file) return;
        message.classList.remove('is-error', 'is-success');
        if (file.size > 1024 * 1024) {
            message.classList.add('is-error');
            message.textContent = 'Choose an employee transfer file smaller than 1 MB.';
            return;
        }
        button.disabled = true;
        input.disabled = true;
        form.setAttribute('aria-busy', 'true');
        message.textContent = 'Importing employees…';
        try {
            const body = new FormData();
            body.append('file', file);
            const response = await fetch('/api/operations/employees/import', {method: 'POST', body});
            const result = await response.json().catch(() => { throw new Error('The server could not complete the import. Please try again.'); });
            if (!response.ok) throw new Error(result.message || 'Import failed.');
            message.textContent = `${result.added} employee${result.added === 1 ? '' : 's'} imported; ${result.skipped} existing account${result.skipped === 1 ? '' : 's'} skipped. Employees can sign in with their previous credentials.`;
            message.classList.add('is-success');
        } catch (error) {
            message.classList.add('is-error');
            message.textContent = error.message;
        } finally {
            button.disabled = false;
            input.disabled = false;
            form.setAttribute('aria-busy', 'false');
        }
    });
})();
