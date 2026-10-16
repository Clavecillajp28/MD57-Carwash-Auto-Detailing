from datetime import timedelta
from shop_clock import datetime

from database import get_db_connection

# Applies independently by username and by IP - whichever limit a request
# hits first blocks it. Per-username stops one account being brute-forced
# from anywhere; per-IP stops one source spraying many different usernames.
# A sliding window (not a fixed "locked until" timestamp) means the lockout
# self-clears as old failures age out, with no separate unlock step needed.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_MINUTES = 15


def record_login_attempt(username, ip_address, success):
    # attempted_at is set explicitly here (Python's local datetime.now())
    # rather than relying on the column's DEFAULT CURRENT_TIMESTAMP, which
    # SQLite evaluates in UTC - is_locked_out()'s cutoff below is computed
    # with the same local datetime.now(), so both sides of that comparison
    # need to use the same clock or the sliding window silently never
    # matches whenever local time and UTC differ.
    attempted_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO login_attempts (username, ip_address, success, attempted_at) VALUES (?, ?, ?, ?)",
        (username, ip_address, 1 if success else 0, attempted_at),
    )
    conn.commit()
    conn.close()


def is_locked_out(username, ip_address):
    """Returns (locked: bool, retry_after_minutes: int | None). Callers
    should check this BEFORE validating the password - both to skip the
    (deliberately slow) password-hash comparison once locked out, and so
    the lockout behaves identically whether or not the username actually
    exists, which avoids leaking account existence through timing or
    response differences."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cutoff = (datetime.now() - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)).strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE LOWER(username) = LOWER(?) AND success = 0 AND attempted_at > ?",
        (username, cutoff),
    )
    username_failures = cursor.fetchone()[0]

    ip_failures = 0
    if ip_address:
        cursor.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE ip_address = ? AND success = 0 AND attempted_at > ?",
            (ip_address, cutoff),
        )
        ip_failures = cursor.fetchone()[0]

    conn.close()

    if username_failures >= MAX_FAILED_ATTEMPTS or ip_failures >= MAX_FAILED_ATTEMPTS:
        return True, LOCKOUT_WINDOW_MINUTES
    return False, None
