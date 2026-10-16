"""Self-service profiles. Account identity and permission changes stay separate."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import unicodedata

from flask import Blueprint, current_app, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_db_connection
from employee_accounts import personal_name, valid_email
from login_security import is_locked_out, record_login_attempt
from password_policy import password_error
from utils import log_activity, now_str, role_required, send_branded_email

profile_api_bp = Blueprint('profile_api', __name__)
EMAIL_CODE_MINUTES = 10
EMAIL_RESEND_SECONDS = 120
MAX_CODE_ATTEMPTS = 5
GOOGLE_REAUTH_SECONDS = 300


class ProfileError(Exception):
    def __init__(self, message, status=400, code='invalid_request'):
        self.message, self.status, self.code = message, status, code


@profile_api_bp.errorhandler(ProfileError)
def profile_error(error):
    if error.code == 'access_changed':
        session.clear()
    return jsonify(status='error', message=error.message, code=error.code), error.status


def _data(allowed):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise ProfileError('Only the fields shown in this profile form can be changed.')
    return data


def _text(value, label, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        raise ProfileError(f'Enter a valid {label}.')
    return value.strip()


def _user(conn):
    user = conn.execute('SELECT * FROM users WHERE user_id=?', (session.get('user_id'),)).fetchone()
    if (not user or user['disabled_at'] or user['session_version'] != session.get('session_version', 0)):
        raise ProfileError('Your access changed. Please sign in again.', 401, 'access_changed')
    return user


@contextmanager
def _transaction(expected=None):
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        user = _user(conn)
        if expected is not None and (user['session_version'] != expected['session_version']
                                      or user['password_hash'] != expected['password_hash']):
            raise ProfileError('Your access changed. Please sign in again.', 401, 'access_changed')
        yield conn, user
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _read_user():
    conn = get_db_connection()
    try:
        return _user(conn)
    finally:
        conn.close()


def _account_email(user):
    if user['role'] == 'customer' or user['auth_provider'] == 'google':
        return user['username'] if valid_email(user['username']) else ''
    return user['notify_email'] or (user['username'] if valid_email(user['username']) else '')


def _reauthenticate(user, password):
    if user['auth_provider'] == 'google':
        age = time.time() - session.get('authenticated_at', 0)
        if not 0 <= age <= GOOGLE_REAUTH_SECONDS:
            raise ProfileError('Sign in again with Google before continuing.', 403, 'reauth_required')
        return
    locked, _ = is_locked_out(user['username'], request.remote_addr)
    if locked:
        raise ProfileError('Too many incorrect password attempts. Try again in 15 minutes.', 429, 'rate_limited')
    valid = isinstance(password, str) and 0 < len(password) <= 128 and check_password_hash(user['password_hash'], password)
    record_login_attempt(user['username'], request.remote_addr, valid)
    if not valid:
        raise ProfileError('Your current password is incorrect.', 400, 'incorrect_password')


def _invalidate_recovery(conn, user_id):
    conn.execute('UPDATE password_resets SET used=1 WHERE user_id=?', (user_id,))
    conn.execute('DELETE FROM profile_email_changes WHERE user_id=?', (user_id,))
    conn.execute('UPDATE employee_invitations SET revoked_at=? WHERE user_id=? AND used_at IS NULL AND revoked_at IS NULL', (now_str(), user_id))


def _notice(email, heading, text):
    if email and valid_email(email):
        try:
            send_branded_email(subject='MD57 account security', recipient=email, heading=heading,
                               plain_text_body=text)
        except Exception:
            # A notification outage must not undo an already committed security action.
            current_app.logger.warning('Profile security notification could not be delivered')


def _profile(conn, user):
    pending = conn.execute('SELECT email,expires_at,attempts FROM profile_email_changes WHERE user_id=? AND expires_at>? AND session_version=?',
                           (user['user_id'], now_str(), user['session_version'])).fetchone()
    return {
        'user_id': user['user_id'], 'username': user['username'], 'fullname': user['fullname'] or user['username'],
        'role': 'owner' if user['role'] == 'admin' and user['is_owner'] else 'manager' if user['role']=='admin' else user['role'],
        'email': _account_email(user), 'email_verified': bool(user['email_verified']),
        'sign_in_method': 'google' if user['auth_provider']=='google' else 'password',
        'can_change_email': user['role']=='customer' and user['auth_provider']!='google',
        'reminder_email': user['reminder_email'] if user['role']=='staff' else None,
        'created_at': user['created_at'], 'updated_at': user['profile_updated_at'],
        'pending_email': dict(pending) if pending else None,
    }


@profile_api_bp.route('/api/profile', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def get_profile():
    conn = get_db_connection()
    try:
        return jsonify(status='success', profile=_profile(conn, _user(conn)))
    finally:
        conn.close()


@profile_api_bp.route('/api/profile', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def update_profile():
    data = _data({'fullname', 'reminder_email'})
    if not data:
        raise ProfileError('Choose a profile detail to update.')
    with _transaction() as (conn, user):
        name = user['fullname'] or user['username']
        if 'fullname' in data:
            name = _text(data['fullname'], 'full name', 120)
            if (not 2 <= len(name) <= 120 or not any(c.isalpha() for c in name)
                    or any(unicodedata.category(c).startswith('C') for c in name) or '<' in name or '>' in name):
                raise ProfileError('Enter your full name using 2 to 120 characters.')
            if user['role'] in ('admin', 'staff') and name != user['fullname']:
                if not personal_name(name):
                    raise ProfileError('Use your personal name, rather than a shared role name.')
                if conn.execute("""SELECT 1 FROM users WHERE user_id<>? AND role IN ('admin','staff')
                    AND LOWER(COALESCE(fullname,username))=LOWER(?)""", (user['user_id'], name)).fetchone():
                    raise ProfileError('Another employee already uses that name. Ask the owner to review the accounts.', 409)
        reminder = user['reminder_email']
        if 'reminder_email' in data:
            if user['role'] != 'staff':
                raise ProfileError('Shift reminders are available to staff accounts only.', 403)
            reminder = _text(data['reminder_email'], 'reminder email', 254).lower() or None
            if reminder and not valid_email(reminder):
                raise ProfileError('Enter a valid reminder email, or leave it blank to opt out.')
        if user['role']=='staff' and name != user['fullname']:
            # Future schedule keys still include the display name. Keep those
            # mutable records aligned so a rename cannot create duplicate shifts.
            today = datetime.now().strftime('%Y-%m-%d')
            try:
                for table, date_column in (('staff_shifts','shift_date'), ('staff_unavailability','unavailable_date')):
                    conn.execute(f'UPDATE {table} SET staff_name=? WHERE staff_user_id=? AND {date_column}>=?',
                                 (name, user['user_id'], today))
            except sqlite3.IntegrityError:
                raise ProfileError('That name conflicts with a future roster record. Ask the manager to review it.', 409) from None
        conn.execute('UPDATE users SET fullname=?,reminder_email=?,profile_updated_at=? WHERE user_id=?',
                     (name, reminder, now_str(), user['user_id']))
        profile = _profile(conn, _user(conn))
    session['fullname'] = name
    log_activity(session['user'], 'Updated own profile details')
    return jsonify(status='success', message='Your profile has been saved.', profile=profile)


@profile_api_bp.route('/api/profile/password', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def change_password():
    data = _data({'current_password', 'password', 'confirm_password'})
    user = _read_user()
    if user['auth_provider']=='google':
        raise ProfileError('Your password is managed by Google.', 403)
    _reauthenticate(user, data.get('current_password'))
    password = data.get('password')
    if password_error(password):
        raise ProfileError(password_error(password))
    if password != data.get('confirm_password'):
        raise ProfileError('The new passwords do not match.')
    if check_password_hash(user['password_hash'], password):
        raise ProfileError('Choose a different password from your current one.')
    new_hash = generate_password_hash(password)
    with _transaction(user) as (conn, current):
        conn.execute('UPDATE users SET password_hash=?,profile_updated_at=? WHERE user_id=?', (new_hash, now_str(), current['user_id']))
        _invalidate_recovery(conn, current['user_id'])
    log_activity(session['user'], 'Changed own password; all sessions revoked')
    session.clear()
    _notice(_account_email(user), 'Your password was changed', 'Your MD57 password was changed and all sessions were signed out. If this was not you, use password recovery or contact the owner immediately.')
    return jsonify(status='success', message='Password updated. Sign in again on each device.', signed_out=True)


@profile_api_bp.route('/api/profile/sign-out-all', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def sign_out_all():
    data = _data({'current_password'})
    user = _read_user()
    _reauthenticate(user, data.get('current_password'))
    with _transaction(user) as (conn, current):
        conn.execute('UPDATE users SET session_version=session_version+1 WHERE user_id=?', (current['user_id'],))
        _invalidate_recovery(conn, current['user_id'])
    log_activity(session['user'], 'Signed out all own sessions')
    session.clear()
    return jsonify(status='success', message='All devices have been signed out.', signed_out=True)


def _customer_email_user(conn):
    user = _user(conn)
    if user['role'] != 'customer' or user['auth_provider']=='google':
        raise ProfileError('This sign-in email is managed by the owner or by Google.', 403)
    return user


def _email_available(conn, email, user_id):
    if conn.execute('SELECT 1 FROM users WHERE user_id<>? AND (LOWER(username)=? OR LOWER(notify_email)=?)',
                    (user_id, email, email)).fetchone():
        raise ProfileError('That email cannot be used for this account.', 409)
    if conn.execute('''SELECT 1 FROM employee_invitations WHERE LOWER(email)=?
        AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?''', (email, now_str())).fetchone():
        raise ProfileError('That email cannot be used for this account.', 409)


def _code_hash(user_id, email, nonce_hash, code):
    message = f'{user_id}:{email}:{nonce_hash}:{code}'.encode('utf-8')
    secret = current_app.secret_key
    return hmac.new(secret.encode('utf-8') if isinstance(secret, str) else secret, message, hashlib.sha256).hexdigest()


@profile_api_bp.route('/api/profile/email/request', methods=['POST'])
@role_required('customer')
def request_email_change():
    data = _data({'email', 'current_password'})
    user = _read_user()
    if user['auth_provider']=='google':
        raise ProfileError('Your sign-in email is managed by Google.', 403)
    _reauthenticate(user, data.get('current_password'))
    email = _text(data.get('email'), 'email address', 254).lower()
    if not valid_email(email) or email == user['username'].lower():
        raise ProfileError('Enter a valid new email address.')
    nonce = secrets.token_urlsafe(32)
    nonce_hash = hashlib.sha256(nonce.encode()).hexdigest()
    code = ''.join(secrets.choice('0123456789') for _ in range(6))
    now = datetime.now()
    expires = (now + timedelta(minutes=EMAIL_CODE_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
    with _transaction(user) as (conn, current):
        _customer_email_user(conn)
        _email_available(conn, email, current['user_id'])
        pending = conn.execute('SELECT created_at FROM profile_email_changes WHERE user_id=?', (current['user_id'],)).fetchone()
        cutoff = (now-timedelta(seconds=EMAIL_RESEND_SECONDS)).strftime('%Y-%m-%d %H:%M:%S')
        if pending and pending['created_at'] > cutoff:
            raise ProfileError('A code was recently sent. Wait two minutes before requesting another.', 429, 'rate_limited')
        conn.execute('''INSERT OR REPLACE INTO profile_email_changes
            (user_id,email,code_hash,nonce_hash,session_version,created_at,expires_at)
            VALUES (?,?,?,?,?,?,?)''', (current['user_id'], email, _code_hash(current['user_id'], email, nonce_hash, code),
                nonce_hash, current['session_version'], now_str(), expires))
    try:
        send_branded_email(subject='MD57 - Verify your new email', recipient=email, heading='Verify your new email',
            highlight=code, plain_text_body=f'Your verification code is {code}. It expires in 10 minutes. Enter it on your MD57 profile in the same browser. Your email stays unchanged until you confirm this code.')
    except Exception:
        with _transaction(user) as (conn, current):
            conn.execute('DELETE FROM profile_email_changes WHERE user_id=? AND nonce_hash=?', (current['user_id'], nonce_hash))
        raise ProfileError('The verification email could not be sent. Your email has not changed. Try again later.', 503)
    session['profile_email_nonce'] = nonce
    log_activity(session['user'], 'Requested own email change verification')
    return jsonify(status='success', message='Verification code sent. Your current email has not changed.',
                   pending_email={'email': email, 'expires_at': expires, 'attempts': 0})


@profile_api_bp.route('/api/profile/email/verify', methods=['POST'])
@role_required('customer')
def verify_email_change():
    data = _data({'code'})
    code = _text(data.get('code'), 'verification code', 32)
    error = None
    with _transaction() as (conn, user):
        _customer_email_user(conn)
        pending = conn.execute('SELECT * FROM profile_email_changes WHERE user_id=?', (user['user_id'],)).fetchone()
        nonce = session.get('profile_email_nonce', '')
        if (not pending or pending['expires_at'] <= now_str() or pending['session_version'] != user['session_version']
                or not nonce or not hmac.compare_digest(pending['nonce_hash'], hashlib.sha256(nonce.encode()).hexdigest())):
            raise ProfileError('This code is invalid or expired. Request a new one from this browser.')
        if pending['attempts'] >= MAX_CODE_ATTEMPTS:
            raise ProfileError('Too many incorrect codes. Request a new code.', 429, 'rate_limited')
        if not re.fullmatch(r'[0-9]{6}', code) or not hmac.compare_digest(pending['code_hash'], _code_hash(user['user_id'], pending['email'], pending['nonce_hash'], code)):
            conn.execute('UPDATE profile_email_changes SET attempts=attempts+1 WHERE user_id=?', (user['user_id'],))
            error = 'The verification code is incorrect.'
        else:
            _email_available(conn, pending['email'], user['user_id'])
            # Keep ownership attached to the account, including legacy walk-ins,
            # before its previous address can ever be registered by another person.
            if user['username']:
                for table in ('reservations', 'reservations_archive'):
                    conn.execute(f'''UPDATE {table} SET user_id=? WHERE user_id IS NULL
                        AND LOWER(customer_email)=LOWER(?)''', (user['user_id'], user['username']))
            conn.execute('''UPDATE users SET username=?,notify_email=?,email_verified=1,
                session_version=session_version+1,profile_updated_at=? WHERE user_id=?''',
                (pending['email'], pending['email'], now_str(), user['user_id']))
            conn.execute('DELETE FROM pending_registrations WHERE LOWER(email)=?', (pending['email'],))
            _invalidate_recovery(conn, user['user_id'])
    if error:
        raise ProfileError(error)
    log_activity(session['user'], 'Verified own new email; all sessions revoked')
    session.clear()
    _notice(_account_email(user), 'Your sign-in email was changed', 'The sign-in email on your MD57 account was changed after verification. All sessions were signed out. If this was not you, contact the shop immediately.')
    return jsonify(status='success', message='Email verified and updated. Sign in with your new email.', signed_out=True)


@profile_api_bp.route('/api/profile/email/cancel', methods=['POST'])
@role_required('customer')
def cancel_email_change():
    _data(set())
    with _transaction() as (conn, user):
        _customer_email_user(conn)
        # Preserve the cooldown record, but make the pending challenge unusable.
        conn.execute("UPDATE profile_email_changes SET expires_at=?,attempts=? WHERE user_id=?",
                     (now_str(), MAX_CODE_ATTEMPTS, user['user_id']))
    session.pop('profile_email_nonce', None)
    return jsonify(status='success', message='Email change cancelled. Your current email is unchanged.')
