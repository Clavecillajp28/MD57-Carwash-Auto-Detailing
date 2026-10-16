(() => {
    'use strict';
    const form = document.getElementById('employeeImport');
    if (!form) return;
    form.addEventListener('submit', async event => {
        event.preventDefault();
        const button = form.querySelector('button');
        const message = document.getElementById('employeeImportResult');
        const file = document.getElementById('employeeTransferFile').files[0];
        if (!file) return;
        if (file.size > 1024 * 1024) {
            message.textContent = 'Choose an employee transfer file smaller than 1 MB.';
            return;
        }
        button.disabled = true;
        message.textContent = 'Importing employees…';
        try {
            const body = new FormData();
            body.append('file', file);
            const response = await fetch('/api/operations/employees/import', {method: 'POST', body});
            const result = await response.json();
            if (!response.ok) throw new Error(result.message || 'Import failed.');
            message.textContent = `${result.added} employees imported; ${result.skipped} existing accounts skipped. Employees can sign in with their previous credentials.`;
        } catch (error) {
            message.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });
})();
