import secrets
import sqlite3
import time
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, current_app
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_db_connection
from extensions import oauth
from login_security import record_login_attempt, is_locked_out
from password_policy import password_error
from utils import log_activity, get_redirect_for_role, send_branded_email, now_str

auth_bp = Blueprint('auth', __name__)

# How long a password-reset OTP stays valid, and how soon another one can
# be issued for the same account - the cooldown stops a refreshed/resubmitted
# form (or someone spamming the form) from mail-bombing one inbox with a
# fresh code every time. Shorter than the old link-based lifetime (30 min)
# since a 6-digit code is meant to be typed in immediately after arriving,
# not saved for later, and a shorter window also narrows the brute-force
# surface described below.
RESET_TOKEN_LIFETIME_MINUTES = 10
RESET_REQUEST_COOLDOWN_MINUTES = 2

# A 6-digit code only has 1,000,000 possible values - far fewer than the
# old secrets.token_urlsafe(32) link, which was only ever guessable by
# brute force in a practical sense. Capping wrong guesses per issued code
# is what actually keeps this safe; without it, a script could just try
# all million combinations against a known username within the code's
# lifetime.
MAX_OTP_ATTEMPTS = 5


def _generate_otp_code():
    return ''.join(secrets.choice('0123456789') for _ in range(6))


def _send_password_reset_email(email, fullname, otp_code):
    try:
        send_branded_email(
            subject='MD57 Car Wash - Password Reset Code',
            recipient=email,
            heading='Password Reset Code',
            highlight=otp_code,
            plain_text_body=(
                f"Hello {fullname or 'there'}!\n\n"
                f"We received a request to reset your MD57 Car Wash account password.\n\n"
                f"Your password reset code is: {otp_code}\n\n"
                f"Enter this code on the password reset page to continue. It's valid for "
                f"{RESET_TOKEN_LIFETIME_MINUTES} minutes and can only be used once.\n\n"
                f"If you didn't request this, you can safely ignore this email - your password will not change.\n\n"
                f"- MD57 Team"
            ),
        )
    except Exception as e:
        print(f"[WARNING] Failed to send password reset email to {email}: {e}")


def _send_verification_email(email, fullname, code):
    """Returns whether the email went out - registration can't continue
    without it, unlike a reset code (which fails silently on purpose)."""
    try:
        send_branded_email(
            subject='MD57 Car Wash - Verify Your Email',
            recipient=email,
            heading='Verify Your Email',
            highlight=code,
            plain_text_body=(
                f"Hello {fullname or 'there'}!\n\n"
                f"Your MD57 Car Wash verification code is: {code}\n\n"
                f"Enter this code on the registration page to create your account. It's valid for "
                f"{RESET_TOKEN_LIFETIME_MINUTES} minutes.\n\n"
                f"If you didn't try to register, you can safely ignore this email - no account is created without the code.\n\n"
                f"- MD57 Team"
            ),
        )
        return True
    except Exception as e:
        print(f"[WARNING] Failed to send verification email to {email}: {e}")
        return False


def _is_local_path(target):
    """True only for a path on this site ('/queue') - '//host' and
    '/\\host' are both read by browsers as a different site."""
    return bool(target) and target.startswith('/') and not target.startswith(('//', '/\\'))


def _start_session(user,mfa_verified=False):
    # A callback may have updated credentials since its original SELECT.
    authenticated_hash = user['password_hash']
    authenticated_version = user['session_version']
    conn = get_db_connection()
    try:
        user = conn.execute('SELECT * FROM users WHERE user_id=?', (user['user_id'],)).fetchone()
    finally:
        conn.close()
    if (not user or user['disabled_at'] or user['password_hash'] != authenticated_hash
            or user['session_version'] != authenticated_version):
        session.clear()
        return False
    from blueprints.mfa_api import enabled, pending_login
    if user['is_owner'] and enabled(user['user_id']) and not mfa_verified:
        session.clear()
        session['mfa_pending']=pending_login(user,request.form.get('next') or request.args.get('next'))
        return True
    session.clear()
    session['user_id'] = user['user_id']
    session['user'] = user['username']
    session['role'] = user['role']
    session['is_owner'] = bool(user['is_owner'])
    session['session_version'] = user['session_version']
    session['authenticated_at'] = time.time()
    if mfa_verified:
        session['mfa_verified_at']=time.time()
    session['fullname'] = user['fullname'] if user['fullname'] else user['username']
    return True


@auth_bp.route('/employee-invitation/<token>', methods=['GET', 'POST'])
def accept_invitation(token):
    from employee_accounts import accept_token, token_hash
    conn = get_db_connection()
    try:
        invitation = conn.execute("""SELECT fullname,email,role FROM employee_invitations
            WHERE token_hash=? AND used_at IS NULL AND revoked_at IS NULL AND expires_at>?""",
            (token_hash(token), now_str())).fetchone()
    finally:
        conn.close()
    error = None
    status = 200 if invitation else 400
    if request.method == 'POST' and invitation:
        password = request.form.get('password') or ''
        if password_error(password):
            error = password_error(password)
        elif password != request.form.get('confirm_password'):
            error = 'Passwords do not match.'
        else:
            try:
                user_id = accept_token(token, password)
            except ValueError as e:
                error = str(e)
            else:
                session.clear()
                log_activity(f'Account ID {user_id}', 'Employee set their own password using owner invitation')
                return redirect(url_for('auth.login_page', reset=1))
        status = 400
    response = current_app.make_response((render_template('employee_invitation.html', invitation=invitation, error=error), status))
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@auth_bp.route('/auth/google')
def google_login():
    next_url = request.args.get('next')
    if _is_local_path(next_url):
        session['oauth_next'] = next_url
    else:
        session.pop('oauth_next', None)
    public_base = current_app.config.get('PUBLIC_BASE_URL', '').rstrip('/')
    redirect_uri = (
        public_base + url_for('auth.google_auth_callback')
        if public_base else url_for('auth.google_auth_callback', _external=True)
    )
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route('/auth/google/callback')
def google_auth_callback():
    try:
        token = oauth.google.authorize_access_token()
        user_info = token.get('userinfo')
        if not user_info:
            resp = oauth.google.get('userinfo')
            user_info = resp.json()

        email = user_info.get('email')
        fullname = user_info.get('name', email)
        if not email or user_info.get('email_verified') is False:
            return redirect(url_for('auth.login_page', error='Google sign-in failed. Please try again.'))

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM users WHERE LOWER(username) = LOWER(?)', (email,))
        user = cursor.fetchone()

        # A customer account registered with this address before
        # registration verified emails: nobody ever proved they own it, so
        # its password may belong to someone else. Google just proved who
        # does - the account becomes theirs alone and that password stops
        # working (it's Google sign-in only from here, like any other
        # Google account).
        if user and user['role'] == 'customer' and user['auth_provider'] != 'google' and not user['email_verified']:
            cursor.execute(
                "UPDATE users SET password_hash = ?, auth_provider = 'google', email_verified = 1 WHERE user_id = ?",
                (generate_password_hash(secrets.token_hex(32)), user['user_id']),
            )
            cursor.execute("UPDATE password_resets SET used = 1 WHERE user_id = ?", (user['user_id'],))
            conn.commit()
            log_activity(email, 'Unverified password account claimed through Google sign-in - password login disabled')

        if not user:
            # A random password that's immediately discarded after hashing -
            # nobody, including this code, ever knows it - and
            # auth_provider='google' makes /login refuse password sign-in for
            # these accounts outright.
            unusable_password = generate_password_hash(secrets.token_hex(32))
            cursor.execute(
                'INSERT INTO users (username, password_hash, role, fullname, auth_provider, email_verified, privacy_consent_at) VALUES (?, ?, ?, ?, ?, 1, ?)',
                (email, unusable_password, 'customer', fullname, 'google', now_str())
            )
            cursor.execute("DELETE FROM pending_registrations WHERE LOWER(email) = LOWER(?)", (email,))
            conn.commit()
            cursor.execute('SELECT * FROM users WHERE LOWER(username) = LOWER(?)', (email,))
            user = cursor.fetchone()

        user = cursor.execute('SELECT * FROM users WHERE user_id=?', (user['user_id'],)).fetchone()
        conn.close()

        if user['disabled_at']:
            log_activity(user['username'], 'Login blocked: account is disabled')
            return redirect(url_for('auth.login_page', error='This account has been disabled. Please contact the owner.'))
        next_url = session.pop('oauth_next', None)
        if not _start_session(user):
            return redirect(url_for('auth.login_page', error='Account access changed. Please sign in again.'))
        if session.get('mfa_pending'):
            session['mfa_pending']={**session['mfa_pending'],'next':next_url}
            return redirect('/login/verify')
        log_activity(user['username'], f"Successfully logged in via Google as {user['role']}")
        return redirect(next_url if _is_local_path(next_url) else get_redirect_for_role(user['role']))

    except Exception as e:
        print(f"Google Auth Error: {e}")
        return redirect(url_for('auth.login_page', error='Google sign-in failed. Please try again.'))


@auth_bp.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if not username or not password:
            return render_template('login.html', error='Please fill in both username and password!')

        username = username.strip()
        ip_address = request.remote_addr

        locked, retry_minutes = is_locked_out(username, ip_address)
        if locked:
            log_activity(username, 'Login blocked: too many failed attempts')
            return render_template(
                'login.html',
                error=f'Too many failed login attempts. Please try again in {retry_minutes} minutes.'
            )

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT * FROM users WHERE LOWER(username) = LOWER(?)', (username,))
        user = cursor.fetchone()
        conn.close()

        if user and user['auth_provider'] == 'google':
            log_activity(username, 'Login blocked: account uses Google Sign-In, password login refused')
            return render_template(
                'login.html',
                error='This account uses Google Sign-In. Please use the "Sign in with Google" button instead.'
            )

        if user and user['disabled_at'] and check_password_hash(user['password_hash'], password):
            log_activity(username, 'Login blocked: account is disabled')
            return render_template('login.html', error='This account has been disabled. Please contact the owner.')

        if user and check_password_hash(user['password_hash'], password):
            record_login_attempt(username, ip_address, True)
            if not _start_session(user):
                return render_template('login.html', error='Account access changed. Please sign in again.')
            if session.get('mfa_pending'):
                return redirect('/login/verify')
            log_activity(user['username'], f"Successfully logged in as {user['role']}")

            # The page the user was sent to /login from (see
            # utils.role_required) - carried through the form by
            # login.html's hidden "next" field.
            next_url = request.values.get('next')
            if _is_local_path(next_url):
                return redirect(next_url)
            return redirect(get_redirect_for_role(user['role']))

        record_login_attempt(username, ip_address, False)
        log_activity(username, 'Failed login attempt')
        return render_template('login.html', error='Invalid username or password!')

    return render_template('login.html')


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password_page():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()

        if username:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE LOWER(username) = LOWER(?)', (username,))
            user = cursor.fetchone()

            if user and user['auth_provider'] == 'google':
                # Google-linked accounts have no real password to reset -
                # nothing to email a code for.
                log_activity(username, 'Password reset requested for a Google-linked account - no email sent')
            elif user:
                cutoff = (datetime.now() - timedelta(minutes=RESET_REQUEST_COOLDOWN_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
                cursor.execute(
                    "SELECT 1 FROM password_resets WHERE user_id = ? AND used = 0 AND created_at > ?",
                    (user['user_id'], cutoff),
                )
                if cursor.fetchone():
                    log_activity(username, 'Password reset requested - throttled, a recent code is still valid')
                else:
                    now = datetime.now()
                    created_at = now.strftime('%Y-%m-%d %H:%M:%S')
                    expires_at = (now + timedelta(minutes=RESET_TOKEN_LIFETIME_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
                    # created_at is set explicitly here (Python's local
                    # datetime.now()) rather than relying on the column's
                    # DEFAULT CURRENT_TIMESTAMP, which SQLite evaluates in
                    # UTC - the cooldown check above compares against a
                    # cutoff built from the same local datetime.now(), so
                    # both sides need the same clock or the throttle
                    # silently never matches (same class of bug as
                    # login_security.py's lockout).
                    #
                    # `token` is still UNIQUE (a leftover from the old
                    # long-random-link design, where global uniqueness was
                    # free). A 6-digit OTP only has 1,000,000 possible
                    # values and password_resets rows are never deleted -
                    # given enough resets requested over this app's
                    # lifetime, two different accounts will eventually draw
                    # the same code and collide on that constraint. A
                    # crash here (uncaught IntegrityError -> raw 500) would
                    # also break the anti-enumeration property this whole
                    # flow is built around (a crash is a distinguishable
                    # response). Retrying with a fresh code on collision
                    # costs nothing and is honest about what "unique" here
                    # actually needs to mean (unique among currently-live
                    # codes, not for all time).
                    otp_code = None
                    for _attempt in range(5):
                        candidate = _generate_otp_code()
                        try:
                            cursor.execute(
                                "INSERT INTO password_resets (user_id, token, created_at, expires_at, attempts) VALUES (?, ?, ?, ?, 0)",
                                (user['user_id'], candidate, created_at, expires_at),
                            )
                            otp_code = candidate
                            break
                        except sqlite3.IntegrityError:
                            continue

                    if otp_code is None:
                        log_activity(username, 'Password reset requested - failed to generate a free OTP code after retries')
                    else:
                        conn.commit()
                        _send_password_reset_email(user['notify_email'] or user['username'], user['fullname'], otp_code)
                        log_activity(username, 'Password reset requested - OTP emailed')

            conn.close()

        # Always render the same "enter your code" step regardless of
        # whether the account existed, was Google-linked, or the email
        # actually sent - anything that varied by those cases would let an
        # attacker enumerate real accounts by watching for a different
        # response. Verification of the code itself happens on submit.
        return render_template('reset_password.html', username=username)

    return render_template('forgot_password.html')


@auth_bp.route('/reset-password', methods=['GET', 'POST'])
def reset_password_page():
    if request.method == 'GET':
        # Nothing to verify without a code having been requested first -
        # there's no token in the URL anymore for a bookmarked/shared link
        # to carry.
        return redirect(url_for('auth.forgot_password_page'))

    username = (request.form.get('username') or '').strip()
    otp_code = (request.form.get('otp') or '').strip()
    new_password = request.form.get('password')
    confirm_password = request.form.get('confirm_password')

    generic_invalid = 'Invalid or expired code. Please check the code or request a new one.'

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE LOWER(username) = LOWER(?)', (username,))
    user = cursor.fetchone()

    if not user:
        conn.close()
        return render_template('reset_password.html', username=username, error=generic_invalid)

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute(
        """
        SELECT reset_id, token, attempts FROM password_resets
        WHERE user_id = ? AND used = 0 AND expires_at > ?
        ORDER BY reset_id DESC LIMIT 1
        """,
        (user['user_id'], now_str),
    )
    reset_row = cursor.fetchone()

    if not reset_row:
        conn.close()
        return render_template('reset_password.html', username=username, error=generic_invalid)

    if reset_row['attempts'] >= MAX_OTP_ATTEMPTS:
        cursor.execute("UPDATE password_resets SET used = 1 WHERE reset_id = ?", (reset_row['reset_id'],))
        conn.commit()
        conn.close()
        log_activity(username, 'Password reset code locked out after too many incorrect attempts')
        return render_template(
            'reset_password.html', username=username,
            error='Too many incorrect attempts. Please request a new code.',
        )

    if reset_row['token'] != otp_code:
        cursor.execute("UPDATE password_resets SET attempts = attempts + 1 WHERE reset_id = ?", (reset_row['reset_id'],))
        conn.commit()
        conn.close()
        return render_template('reset_password.html', username=username, error=generic_invalid)

    if password_error(new_password):
        conn.close()
        return render_template('reset_password.html', username=username, error=password_error(new_password))
    if new_password != confirm_password:
        conn.close()
        return render_template('reset_password.html', username=username, error='Passwords do not match.')

    new_hash = generate_password_hash(new_password)
    cursor.execute("UPDATE users SET password_hash = ? WHERE user_id = ?", (new_hash, user['user_id']))
    # Invalidates every outstanding code for this account, not just the one
    # just used - an older, still-unused code shouldn't remain valid after
    # a successful reset.
    cursor.execute("UPDATE password_resets SET used = 1 WHERE user_id = ?", (user['user_id'],))
    conn.commit()
    conn.close()

    log_activity(username, 'Password reset completed')
    return redirect(url_for('auth.login_page', reset=1))


def _register_error(message, status=400, verify_email=None):
    """verify_email: the address a code was sent to - re-shows the "enter
    your code" step instead of the blank registration form."""
    if request.is_json:
        return jsonify({'status': 'error', 'message': message}), status
    return render_template('register.html', error=message, verify_email=verify_email), status


def _pending_registration(cursor):
    """This browser's unexpired pending registration, or None."""
    token = session.get('pending_registration')
    if not token:
        return None
    cursor.execute(
        "SELECT * FROM pending_registrations WHERE session_token = ? AND expires_at > ?",
        (token, now_str()),
    )
    return cursor.fetchone()


_CODE_EXPIRED = 'That verification code has expired. Please register again.'


@auth_bp.route('/register', methods=['GET', 'POST'])
def register_page():
    """Registering doesn't create an account - it emails a 6-digit code to
    the address given, and the account is only created once that code is
    entered (/register/verify). Without this, anyone could register
    someone else's email with a password of their own choosing and later
    read that person's bookings. Same code lifetime, attempt cap and
    cooldown as a password reset code."""
    if request.method == 'GET':
        return render_template('register.html')

    data = (request.get_json(silent=True) or {}) if request.is_json else request.form
    email = (data.get('email') or '').strip()
    password = data.get('password')
    fullname = (data.get('fullname') or '').strip()

    if not email or not password:
        return _register_error('Email and password are required!')
    if '@' not in email:
        return _register_error('Please enter a valid email address.')
    if password_error(password):
        return _register_error(password_error(password))
    if not data.get('privacy_consent'):
        return _register_error('Please agree to the Privacy Policy to create an account.')

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM users WHERE LOWER(username) = LOWER(?)', (email,))
    if cursor.fetchone():
        conn.close()
        return _register_error('Email is already registered! Please sign in.')

    now = datetime.now()
    cursor.execute("DELETE FROM pending_registrations WHERE expires_at < ?", (now_str(),))
    cursor.execute("SELECT session_token, sent_at FROM pending_registrations WHERE LOWER(email) = LOWER(?)", (email,))
    pending = cursor.fetchone()
    cooldown_cutoff = (now - timedelta(minutes=RESET_REQUEST_COOLDOWN_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
    if pending and pending['sent_at'] > cooldown_cutoff:
        conn.commit()
        conn.close()
        message = 'A verification code was just sent to this address. Check your inbox, or try again in a couple of minutes.'
        # This browser asked for that code - put it back on the code step.
        mine = pending['session_token'] == session.get('pending_registration')
        return _register_error(message, verify_email=email if mine else None)

    # Every submission replaces any earlier one for the address and gets a
    # new code, tied to this browser: an earlier, different submission
    # (someone else's password) can never be completed with this code.
    code = _generate_otp_code()
    token = secrets.token_urlsafe(32)
    cursor.execute("DELETE FROM pending_registrations WHERE LOWER(email) = LOWER(?)", (email,))
    cursor.execute(
        """
        INSERT INTO pending_registrations (email, fullname, password_hash, code, session_token, sent_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (email, fullname, generate_password_hash(password), code, token, now_str(),
         (now + timedelta(minutes=RESET_TOKEN_LIFETIME_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')),
    )
    conn.commit()

    if not _send_verification_email(email, fullname, code):
        cursor.execute("DELETE FROM pending_registrations WHERE session_token = ?", (token,))
        conn.commit()
        conn.close()
        return _register_error('We could not send a verification email to that address. Please check it and try again.', 502)
    conn.close()

    session['pending_registration'] = token
    log_activity(email, 'Registration started - verification code emailed')
    if request.is_json:
        return jsonify({'status': 'verification_required', 'message': f'We emailed a verification code to {email}.'}), 200
    return render_template('register.html', verify_email=email)


@auth_bp.route('/register/verify', methods=['POST'])
def register_verify():
    data = (request.get_json(silent=True) or {}) if request.is_json else request.form
    code = (data.get('code') or '').strip()

    conn = get_db_connection()
    cursor = conn.cursor()
    pending = _pending_registration(cursor)
    if not pending:
        conn.close()
        return _register_error(_CODE_EXPIRED)

    if pending['attempts'] >= MAX_OTP_ATTEMPTS:
        cursor.execute("DELETE FROM pending_registrations WHERE pending_id = ?", (pending['pending_id'],))
        conn.commit()
        conn.close()
        session.pop('pending_registration', None)
        return _register_error('Too many incorrect attempts. Please register again.')

    if pending['code'] != code:
        cursor.execute("UPDATE pending_registrations SET attempts = attempts + 1 WHERE pending_id = ?", (pending['pending_id'],))
        conn.commit()
        conn.close()
        return _register_error('That code is not correct. Please check it and try again.', verify_email=pending['email'])

    try:
        cursor.execute(
            'INSERT INTO users (username, password_hash, role, fullname, email_verified, privacy_consent_at) VALUES (?, ?, ?, ?, 1, ?)',
            (pending['email'], pending['password_hash'], 'customer', pending['fullname'] or pending['email'], now_str()),
        )
    except sqlite3.IntegrityError:
        conn.close()
        return _register_error('Email is already registered! Please sign in.')
    cursor.execute("DELETE FROM pending_registrations WHERE pending_id = ?", (pending['pending_id'],))
    conn.commit()
    conn.close()
    session.pop('pending_registration', None)

    log_activity(pending['email'], 'Registered new customer account (email verified)')
    if request.is_json:
        return jsonify({'status': 'success', 'message': 'Account registered successfully!'}), 200
    return redirect(url_for('auth.login_page', registered=1))


@auth_bp.route('/register/resend', methods=['POST'])
def register_resend():
    conn = get_db_connection()
    cursor = conn.cursor()
    pending = _pending_registration(cursor)
    if not pending:
        conn.close()
        return _register_error(_CODE_EXPIRED)

    now = datetime.now()
    cooldown_cutoff = (now - timedelta(minutes=RESET_REQUEST_COOLDOWN_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')
    if pending['sent_at'] > cooldown_cutoff:
        conn.close()
        return _register_error('A code was sent a moment ago. Please wait a couple of minutes before asking for another.',
                               verify_email=pending['email'])

    code = _generate_otp_code()
    cursor.execute(
        "UPDATE pending_registrations SET code = ?, attempts = 0, sent_at = ?, expires_at = ? WHERE pending_id = ?",
        (code, now_str(), (now + timedelta(minutes=RESET_TOKEN_LIFETIME_MINUTES)).strftime('%Y-%m-%d %H:%M:%S'),
         pending['pending_id']),
    )
    conn.commit()
    conn.close()

    if not _send_verification_email(pending['email'], pending['fullname'], code):
        return _register_error('We could not send the email. Please try again shortly.', 502, verify_email=pending['email'])
    if request.is_json:
        return jsonify({'status': 'verification_required', 'message': 'A new code was sent.'}), 200
    return render_template('register.html', verify_email=pending['email'], notice='A new code is on its way.')


@auth_bp.route('/logout')
def logout():
    username = session.get('user', 'Unknown')
    log_activity(username, 'Logged out from system')
    session.clear()
    return redirect(url_for('auth.login_page'))
