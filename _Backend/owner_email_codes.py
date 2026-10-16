"""Purpose-bound, one-use Owner email codes. Mail delivery never holds a DB lock."""
import hashlib
import hmac
import re
import secrets
import time

from flask import current_app

from database import get_db_connection

LIFETIME_SECONDS = 300
RESEND_SECONDS = 60
MAX_ATTEMPTS = 5
MAX_SENDS_PER_HOUR = 10
PURPOSES = {'enable', 'login', 'verify', 'disable', 'recovery'}


class EmailCodeError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def gmail(value):
    value = value.strip().lower() if isinstance(value, str) else ''
    if len(value)>254 or not re.fullmatch(r'[a-z0-9][a-z0-9.+_-]*@(?:gmail|googlemail)\.com', value):
        raise EmailCodeError('Enter the Owner\'s Gmail address.')
    return value


def address(user, state=None):
    return gmail(state['email'] if state and state['email'] and state['method'] == 'email'
                 else user['notify_email'] or user['username'])


def masked(email):
    local, domain = email.rsplit('@', 1)
    return local[:2] + '***@' + domain


def digest(challenge_id, code):
    return hmac.new(str(current_app.secret_key).encode(),
                    f'owner-email:{challenge_id}:{code}'.encode(), hashlib.sha256).hexdigest()


def issue(user, purpose, email):
    if purpose not in PURPOSES:
        raise EmailCodeError('Choose a supported verification action.')
    email = gmail(email)
    now = time.time()
    challenge_id = secrets.token_urlsafe(32)
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT * FROM users WHERE user_id=?', (user['user_id'],)).fetchone()
        if (not current or current['disabled_at'] or not current['is_owner']
                or current['session_version'] != user['session_version']):
            raise EmailCodeError('Account access changed. Sign in again.', 401)
        recent = conn.execute('SELECT created_at FROM owner_email_challenges WHERE user_id=? ORDER BY created_at DESC LIMIT 1',
                              (user['user_id'],)).fetchone()
        if recent and now - recent['created_at'] < RESEND_SECONDS:
            raise EmailCodeError('Wait one minute before requesting another code.', 429)
        count = conn.execute('SELECT COUNT(*) FROM owner_email_challenges WHERE user_id=? AND created_at>?',
                             (user['user_id'], now - 3600)).fetchone()[0]
        if count >= MAX_SENDS_PER_HOUR:
            raise EmailCodeError('Email request limit reached. Try again later or use a recovery code.', 429)
        code = str(secrets.randbelow(1_000_000)).zfill(6)
        conn.execute('UPDATE owner_email_challenges SET used_at=? WHERE user_id=? AND purpose=? AND used_at IS NULL',
                     (now, user['user_id'], purpose))
        conn.execute('''INSERT INTO owner_email_challenges
            (challenge_id,user_id,purpose,email,code_hash,session_version,created_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?)''',
                     (challenge_id, user['user_id'], purpose, email, digest(challenge_id, code),
                      user['session_version'], now, now + LIFETIME_SECONDS))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    try:
        from utils import send_branded_email
        label = {'enable': 'Enable Owner email verification', 'login': 'Owner sign-in',
                 'verify': 'Verify sensitive Owner actions', 'disable': 'Disable Owner email verification',
                 'recovery': 'Replace Owner recovery codes'}[purpose]
        send_branded_email(
            subject='MD57 - Owner Security Code', recipient=email, heading=label, highlight=code,
            plain_text_body=f'Hello {user["fullname"] or "Owner"},\n\nYour MD57 security code is {code}.\n'
                            f'Purpose: {label}.\nIt expires in five minutes and can only be used once.\n'
                            'Do not share this code. If you did not request it, do not approve the action.',
            dedupe_key='owner-email:' + challenge_id, retryable=False)
    except Exception:
        conn = get_db_connection()
        try:
            conn.execute('UPDATE owner_email_challenges SET used_at=? WHERE challenge_id=?', (time.time(), challenge_id))
            conn.commit()
        finally:
            conn.close()
        raise EmailCodeError('The code could not be sent. Try again after one minute, or use a recovery code.', 503) from None
    conn = get_db_connection()
    try:
        conn.execute('UPDATE owner_email_challenges SET delivered=1 WHERE challenge_id=? AND used_at IS NULL', (challenge_id,))
        conn.commit()
    finally:
        conn.close()
    return challenge_id


def verify(conn, user, purpose, challenge_id, code, expected_email=None):
    """Caller commits attempts even on rejection; BEGIN IMMEDIATE serializes consumption."""
    row = conn.execute('SELECT * FROM owner_email_challenges WHERE challenge_id=? AND user_id=? AND purpose=?',
                       (challenge_id, user['user_id'], purpose)).fetchone()
    if (not row or row['used_at'] is not None or not row['delivered'] or row['expires_at'] <= time.time()
            or row['attempts'] >= MAX_ATTEMPTS or row['session_version'] != user['session_version']
            or (expected_email is not None and row['email'] != expected_email)):
        return False
    if not isinstance(code, str) or not hmac.compare_digest(row['code_hash'], digest(challenge_id, code)):
        conn.execute('UPDATE owner_email_challenges SET attempts=attempts+1 WHERE challenge_id=?', (challenge_id,))
        return False
    conn.execute('UPDATE owner_email_challenges SET used_at=? WHERE challenge_id=?', (time.time(), challenge_id))
    return True
