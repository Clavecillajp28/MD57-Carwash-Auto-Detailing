"""Encrypted, additive transfer of non-owner employee logins between installs."""
import base64
import hashlib
import json
from pathlib import Path
import sqlite3

from cryptography.fernet import Fernet, InvalidToken

from database import get_db_connection

MAX_BYTES = 1024 * 1024
FIELDS = ('username', 'fullname', 'password_hash', 'role', 'auth_provider',
          'notify_email', 'email_verified', 'privacy_consent_at', 'created_at')


def _cipher(secret):
    if not secret:
        raise ValueError('Configure the same MD57_EMPLOYEE_TRANSFER_KEY on both installations.')
    if isinstance(secret, str):
        secret = secret.encode()
    key = hashlib.sha256(b'md57-employee-transfer-v1:' + secret).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def export_employees(database_path, secret):
    # Read-only access: exporting never initializes or alters the local database.
    conn = sqlite3.connect(Path(database_path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(f'''SELECT {','.join(FIELDS)} FROM users
            WHERE role IN ('staff','admin') AND is_owner=0 AND disabled_at IS NULL
                AND COALESCE(front_desk,0)=0 ORDER BY user_id''')]
    finally:
        conn.close()
    data = json.dumps({'format': 'md57-employees-v1', 'employees': rows}).encode()
    return _cipher(secret).encrypt(data), len(rows)


def import_employees(token, secret):
    if not token or len(token) > MAX_BYTES:
        raise ValueError('Choose an employee transfer file smaller than 1 MB.')
    try:
        data = json.loads(_cipher(secret).decrypt(token))
    except (InvalidToken, ValueError, UnicodeDecodeError, TypeError):
        raise ValueError('Cannot read this transfer. Use the original file and matching transfer key. Legacy files use the original SECRET_KEY.') from None
    if not isinstance(data, dict) or data.get('format') != 'md57-employees-v1':
        raise ValueError('Unsupported employee transfer file.')
    rows = data.get('employees')
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError('A transfer can contain at most 500 employee accounts.')
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise ValueError('The transfer contains an invalid account.')
        if (not isinstance(row['username'], str) or not 1 <= len(row['username']) <= 254
                or row['role'] not in ('admin', 'staff')
                or not isinstance(row['password_hash'], str) or not 20 <= len(row['password_hash']) <= 512
                or row['auth_provider'] not in (None, '', 'google')
                or row['email_verified'] not in (0, 1)):
            raise ValueError('The transfer contains invalid employee credentials.')
        for key in ('fullname', 'notify_email', 'privacy_consent_at', 'created_at'):
            if row[key] is not None and (not isinstance(row[key], str) or len(row[key]) > 254):
                raise ValueError('The transfer contains invalid employee details.')
    conn = get_db_connection()
    added = skipped = 0
    try:
        conn.execute('BEGIN IMMEDIATE')
        for row in rows:
            # Never overwrite a deployed password, grant ownership, or reconnect
            # another installation's IDs to unrelated local history.
            exists = conn.execute('''SELECT 1 FROM users WHERE LOWER(username)=LOWER(?)
                OR (? IS NOT NULL AND LOWER(notify_email)=LOWER(?))
                OR (? IS NOT NULL AND LOWER(fullname)=LOWER(?) AND role IN ('staff','admin'))''',
                (row['username'], row['notify_email'], row['notify_email'], row['fullname'], row['fullname'])).fetchone()
            if exists:
                skipped += 1
                continue
            conn.execute(f'INSERT INTO users ({",".join(FIELDS)}) VALUES ({",".join("?" for _ in FIELDS)})',
                         [row[k] for k in FIELDS])
            added += 1
        conn.commit()
        return {'added': added, 'skipped': skipped}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    import argparse
    import os
    import secrets
    from config import DB_FILE, BASE_DIR
    parser = argparse.ArgumentParser(description='Export active employee logins for private Owner import.')
    parser.add_argument('--output', default=str(Path(BASE_DIR) / '.runtime' / 'employee-transfer.md57employees'))
    parser.add_argument('--legacy-session-key', action='store_true', help='Explicit compatibility export using the existing session secret.')
    args = parser.parse_args()
    transfer_key=os.environ.get('MD57_EMPLOYEE_TRANSFER_KEY')
    if not transfer_key and args.legacy_session_key:
        transfer_key=os.environ.get('SECRET_KEY')
    if not transfer_key:
        key_path=Path(BASE_DIR)/'.runtime'/'employee-transfer.key'
        key_path.parent.mkdir(parents=True,exist_ok=True)
        if not key_path.exists():
            descriptor=os.open(key_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                stream.write(secrets.token_urlsafe(48))
        transfer_key=key_path.read_text(encoding='utf-8').strip()
        print(f'Set MD57_EMPLOYEE_TRANSFER_KEY on the destination from private file {key_path}.')
    token, count = export_employees(DB_FILE, transfer_key)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(token)
    print(f'Exported {count} active employee accounts to {target}. Keep this file private.')
