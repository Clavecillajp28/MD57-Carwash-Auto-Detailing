"""Owner-issued, single-use employee invitations and password recovery."""
import hashlib
import re
import secrets
import sqlite3
from password_policy import password_error
from datetime import datetime, timedelta

from flask import current_app, url_for

from database import get_db_connection
from utils import send_branded_email, now_str


def token_hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def valid_email(email):
    return len(email) <= 254 and bool(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email))


def personal_name(name):
    return (2 <= len(name) <= 120 and sum(c.isalpha() for c in name) >= 2
            and not any(ord(c) < 32 for c in name)
            and name.casefold().replace(' ', '') not in
            {'owner', 'admin', 'manager', 'staff', 'washer', 'frontdesk', 'systemmanager'})


def issue_invitation(*, email, fullname, role, created_by, user_id=None, replace_id=None):
    """Commit the token hash before delivery; a failed send revokes that token.

    Raw tokens exist only in the email. Reset requests immediately revoke
    existing sessions, even if email delivery subsequently needs a retry.
    """
    email = email.strip().lower()
    if not valid_email(email):
        raise ValueError('A valid personal email address is required.')
    if role not in ('owner', 'admin', 'staff'):
        raise ValueError('Choose Owner, Manager, or Staff/Washer.')
    if user_id is None and not personal_name(fullname):
        raise ValueError('Enter the person’s full name, rather than a shared role name.')
    token = secrets.token_urlsafe(32)
    created = now_str()
    expires = (datetime.now() + timedelta(hours=48)).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        version = None
        if user_id is None:
            if conn.execute('SELECT 1 FROM users WHERE LOWER(username)=? OR LOWER(notify_email)=?', (email, email)).fetchone():
                raise ValueError('That email already belongs to an account. Use password recovery for an existing employee.')
            if conn.execute("""SELECT 1 FROM users WHERE role IN ('admin','staff')
                AND LOWER(COALESCE(fullname,username))=LOWER(?)""", (fullname,)).fetchone():
                raise ValueError('An employee with that full name already exists. Use their existing account.')
            pending = conn.execute("""SELECT invitation_id FROM employee_invitations
                WHERE LOWER(email)=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?""", (email, created)).fetchone()
            if pending and pending['invitation_id'] != replace_id:
                raise ValueError('An invitation is already pending for this email. Resend or revoke it first.')
        else:
            user = conn.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
            if not user or user['role'] not in ('admin', 'staff') or user['front_desk']:
                raise ValueError('Choose an existing employee account.')
            if user['disabled_at']:
                raise ValueError('Enable the account before sending password recovery.')
            if user['auth_provider'] == 'google':
                raise ValueError('This account uses Google Sign-In. Recover access through Google.')
            # Re-read target identity and role inside the write transaction.
            expected_email = (user['notify_email'] or (user['username'] if valid_email(user['username']) else '')).lower()
            if expected_email != email:
                raise ValueError('The account email changed. Reload the accounts list and try again.')
            fullname = user['fullname'] or user['username']
            role = 'owner' if user['is_owner'] else user['role']
            conn.execute('UPDATE users SET session_version=session_version+1 WHERE user_id=?', (user_id,))
            version = conn.execute('SELECT session_version FROM users WHERE user_id=?', (user_id,)).fetchone()[0]
            conn.execute('UPDATE password_resets SET used=1 WHERE user_id=?', (user_id,))
            conn.execute("UPDATE employee_invitations SET revoked_at=? WHERE user_id=? AND used_at IS NULL AND revoked_at IS NULL", (created, user_id))
        if replace_id is not None:
            conn.execute('UPDATE employee_invitations SET revoked_at=? WHERE invitation_id=? AND used_at IS NULL', (created, replace_id))
        cur = conn.execute("""INSERT INTO employee_invitations
            (email,fullname,role,user_id,created_by,token_hash,session_version,created_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?)""", (email, fullname, role, user_id, created_by, token_hash(token), version, created, expires))
        invitation_id = cur.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    base = current_app.config.get('PUBLIC_BASE_URL', '').rstrip('/')
    path = url_for('auth.accept_invitation', token=token)
    link = base + path if base else url_for('auth.accept_invitation', token=token, _external=True)
    try:
        send_branded_email('MD57 employee account access', email, 'Set your own password',
                           f'Hello {fullname},\n\nThe owner has invited you to access MD57 as '
                           f'{dict(owner="Owner", admin="Manager", staff="Staff/Washer")[role]}. '
                           f'Choose your own password using this link:\n{link}\n\n'
                           'This link expires in 48 hours and can be used once. '
                           'Keep your password private and use only your own account.')
    except Exception:
        conn = get_db_connection()
        try:
            conn.execute('UPDATE employee_invitations SET revoked_at=? WHERE invitation_id=?', (now_str(), invitation_id))
            conn.commit()
        finally:
            conn.close()
        raise RuntimeError('Email delivery failed. No usable link was left active. Check mail settings and resend.') from None
    return invitation_id


def accept_token(token, password):
    """Consume and create/update in one transaction, preventing token reuse."""
    if password_error(password):
        raise ValueError(password_error(password))
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        raise ValueError('This link is invalid, expired, or already used.')
    from werkzeug.security import generate_password_hash
    password_hash = generate_password_hash(password)
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        invitation = conn.execute("""SELECT * FROM employee_invitations
            WHERE token_hash=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?""", (token_hash(token), now_str())).fetchone()
        if not invitation:
            raise ValueError('This link is invalid, expired, or already used.')
        if invitation['created_by'] is None or not conn.execute("""SELECT 1 FROM users
            WHERE user_id=? AND role='admin' AND is_owner=1 AND disabled_at IS NULL""", (invitation['created_by'],)).fetchone():
            raise ValueError('The inviting owner no longer has access. Ask the owner for a new invitation.')
        user_id = invitation['user_id']
        if user_id is None:
            if conn.execute('SELECT 1 FROM users WHERE LOWER(username)=? OR LOWER(notify_email)=?', (invitation['email'], invitation['email'])).fetchone():
                raise ValueError('An account now uses this email. Ask the owner for a new invitation.')
            if conn.execute("SELECT 1 FROM users WHERE role IN ('admin','staff') AND LOWER(COALESCE(fullname,username))=LOWER(?)", (invitation['fullname'],)).fetchone():
                raise ValueError('An account now uses this name. Ask the owner to review it.')
            cur = conn.execute("""INSERT INTO users
                (username,password_hash,role,fullname,is_owner,notify_email,email_verified)
                VALUES (?,?,?,?,?,?,1)""", (invitation['email'], password_hash,
                    'admin' if invitation['role']=='owner' else invitation['role'],
                    invitation['fullname'], int(invitation['role']=='owner'), invitation['email']))
            user_id = cur.lastrowid
        else:
            user = conn.execute('SELECT * FROM users WHERE user_id=?', (user_id,)).fetchone()
            if (not user or user['disabled_at'] or user['front_desk'] or user['role'] not in ('admin','staff')
                    or user['session_version'] != invitation['session_version']):
                raise ValueError('Account access changed. Ask the owner for a new invitation.')
            conn.execute('UPDATE users SET password_hash=?, email_verified=1 WHERE user_id=?', (password_hash,user_id))
            conn.execute('UPDATE password_resets SET used=1 WHERE user_id=?', (user_id,))
        conn.execute('UPDATE employee_invitations SET used_at=? WHERE invitation_id=?', (now_str(),invitation['invitation_id']))
        conn.execute("""UPDATE employee_invitations SET revoked_at=?
            WHERE LOWER(email)=? AND invitation_id<>? AND used_at IS NULL AND revoked_at IS NULL""", (now_str(),invitation['email'],invitation['invitation_id']))
        conn.commit()
        return user_id
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError('The account already exists. Ask the owner to review it.') from None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
