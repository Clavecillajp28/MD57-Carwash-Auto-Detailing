/* Own-account profile forms. No credentials are kept in browser storage. */
(() => {
    'use strict';
    const el = id => document.getElementById(id);
    const roles = {owner:'Owner',manager:'Manager',staff:'Staff / Washer',customer:'Customer'};
    let profile;
    function status(id, message='', error=false) {
        const node=el(id);
        node.textContent=message;
        node.classList.toggle('is-error',error);
        node.classList.toggle('is-success',Boolean(message)&&!error);
        node.setAttribute('role',error?'alert':'status');
    }
    async function api(path, body) {
        const response=await fetch(path, body===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        const data=await response.json();
        if (response.status===401) { window.location.assign('/login?next=/profile'); throw new Error('Please sign in again.'); }
        if (!response.ok) {
            if(data.code==='reauth_required') el('profileReauthenticate').hidden=false;
            throw new Error(data.message || 'The update could not be saved. Try again.');
        }
        return data;
    }
    const dateText=value => value ? new Date(value.replace(' ','T')).toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'}) : 'Not recorded';
    function pendingEmail(pending) {
        el('profileEmailVerifyForm').hidden=!pending;
        if(pending) el('profilePendingEmail').textContent=`Enter the code sent to ${pending.email} in this browser. Expires ${pending.expires_at}. You can request another after two minutes.`;
        el('profileEmailCode').value='';
    }
    function render(data) {
        profile=data;
        el('profileName').textContent=profile.fullname;
        el('profileRole').textContent=roles[profile.role] || 'Account';
        el('profileAvatar').textContent=profile.fullname.trim().split(/\s+/u).slice(0,2).map(part=>Array.from(part)[0]).join('').toUpperCase();
        el('profileFullname').value=profile.fullname;
        el('profileUsername').value=profile.username;
        el('profileMethod').textContent=profile.sign_in_method==='google'?'Google Sign-In':'Email or username and password';
        el('profileCreated').textContent=dateText(profile.created_at);
        el('profileUpdated').textContent=dateText(profile.updated_at);
        el('profileNameHint').textContent=profile.role==='customer'?'Used for your profile and new bookings. Past booking details are kept.':'This name identifies you on your work and future actions. Existing attendance and pickup history is preserved.';
        el('profileEmail').textContent=profile.email || 'No account email on file';
        el('profileEmailVerified').textContent=profile.email&&profile.email_verified?'Email verified':'Verification not recorded';
        el('profileEmailForm').hidden=!profile.can_change_email;
        el('profileEmailHint').textContent=profile.sign_in_method==='google'?'Your sign-in email is managed by Google.':profile.role==='customer'?'Verify a new address before changing the email you use to sign in.':profile.role==='owner'?'You can update your recovery email in Accounts. Your sign-in identifier stays the same.':'Your login and recovery email are managed by the owner. Ask the owner if they need to change.';
        el('profileOwnerAccounts').hidden=profile.role!=='owner'||profile.sign_in_method==='google';
        el('profileReminderSection').hidden=profile.role!=='staff';
        el('profileReminderEmail').value=profile.reminder_email || '';
        const google=profile.sign_in_method==='google';
        el('profilePasswordForm').hidden=google;
        el('profilePasswordHint').textContent=google?'Your password is managed by Google. Change it through your Google account.':el('profileNewPassword').title+' Changing it signs out all devices.';
        el('profileSessionPasswordRow').hidden=google;
        el('profileSessionPassword').required=!google;
        pendingEmail(profile.pending_email);
        document.querySelector('.sidebar-profile-name').textContent=profile.fullname;
        document.querySelector('.sidebar-avatar').textContent=el('profileAvatar').textContent;
        CURRENT_USER.fullname=profile.fullname;
    }
    async function load() {
        el('profileLoading').hidden=false;
        el('profileLoadError').hidden=true;
        try { render((await api('/api/profile')).profile); el('profileContent').hidden=false; }
        catch(error) { el('profileLoadError').hidden=false; el('profileLoadError').querySelector('p').textContent=error.message; }
        finally { el('profileLoading').hidden=true; }
    }
    function submit(formId, statusId, task) {
        el(formId).addEventListener('submit',async event=>{
            event.preventDefault();
            const form=event.currentTarget, buttons=[...form.querySelectorAll('button')];
            buttons.forEach(button=>button.disabled=true); status(statusId);
            try { await task(form); }
            catch(error) { status(statusId,error.message || 'Could not reach the server. Try again.',true); }
            finally { buttons.forEach(button=>button.disabled=false); }
        });
    }
    submit('profileDetailsForm','profileDetailsStatus',async()=>{
        const data=await api('/api/profile',{fullname:el('profileFullname').value.trim()});
        render(data.profile); status('profileDetailsStatus',data.message);
    });
    submit('profileReminderForm','profileReminderStatus',async()=>{
        const data=await api('/api/profile',{reminder_email:el('profileReminderEmail').value.trim()});
        render(data.profile); status('profileReminderStatus',data.message);
    });
    submit('profilePasswordForm','profilePasswordStatus',async form=>{
        const data=await api('/api/profile/password',{current_password:el('profileCurrentPassword').value,password:el('profileNewPassword').value,confirm_password:el('profileConfirmPassword').value});
        form.reset(); status('profilePasswordStatus',data.message); window.location.assign('/login?reset=1');
    });
    submit('profileEmailForm','profileEmailStatus',async form=>{
        const data=await api('/api/profile/email/request',{email:el('profileNewEmail').value.trim(),current_password:el('profileEmailPassword').value});
        form.reset(); status('profileEmailStatus',data.message); pendingEmail(data.pending_email); el('profileEmailCode').focus();
    });
    submit('profileEmailVerifyForm','profileEmailVerifyStatus',async()=>{
        const data=await api('/api/profile/email/verify',{code:el('profileEmailCode').value.trim()});
        status('profileEmailVerifyStatus',data.message); window.location.assign('/login?email_updated=1');
    });
    el('profileCancelEmail').addEventListener('click',async event=>{
        event.currentTarget.disabled=true;
        try { const data=await api('/api/profile/email/cancel',{}); pendingEmail(null); status('profileEmailStatus',data.message); }
        catch(error) { status('profileEmailVerifyStatus',error.message,true); }
        finally { el('profileCancelEmail').disabled=false; }
    });
    submit('profileSessionsForm','profileSessionsStatus',async form=>{
        if(!(await confirmDialog('Every device, including this one, will need to sign in again.',{title:'Sign out all devices?',confirmText:'Sign out all'}))) return;
        const data=await api('/api/profile/sign-out-all',{current_password:el('profileSessionPassword').value});
        form.reset(); status('profileSessionsStatus',data.message); window.location.assign('/login');
    });
    el('profileShowPassword').addEventListener('change',event=>{
        ['profileCurrentPassword','profileNewPassword','profileConfirmPassword'].forEach(id=>el(id).type=event.currentTarget.checked?'text':'password');
    });
    el('profileRetry').addEventListener('click',load);
    load();
})();
