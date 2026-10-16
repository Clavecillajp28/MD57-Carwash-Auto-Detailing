from datetime import datetime, timedelta

from database import get_db_connection
from utils import send_branded_email, start_periodic_worker, get_latest_attendance, notify_role

# Shift reminders aren't as time-sensitive as the no-show sweep (60s) or as
# expensive to compute as AI retraining (6h) - hourly is frequent enough to
# catch a shift added earlier the same day without hammering the gateway.
REMINDER_CHECK_INTERVAL_SECONDS = 60 * 60

# Rostered staff who haven't clocked in by 30 minutes after opening (8:00
# AM) trigger one admin alert each, checked every 5 minutes - early enough
# to call them or cover the shift before bookings are affected.
MISSING_STAFF_CUTOFF = (8, 30)
ROSTER_WATCH_INTERVAL_SECONDS = 5 * 60


def _send_shift_reminder_email(email, staff_name, shift_date, shift_note):
    """Returns whether the email went out."""
    try:
        note_line = f"\n\nNote from admin: {shift_note}" if shift_note else ""
        send_branded_email(
            subject='MD57 Car Wash - Shift Reminder for Tomorrow',
            recipient=email,
            heading='Reminder: You\'re Scheduled Tomorrow',
            plain_text_body=(
                f"Hello {staff_name}!\n\n"
                f"Just a reminder that you're scheduled to work on {shift_date}."
                f"{note_line}\n\n"
                f"See you then!\n- MD57 Team"
            ),
        )
        return True
    except Exception as e:
        print(f"[WARNING] Failed to send shift reminder email to {email}: {e}")
        return False


def process_shift_reminders():
    """Reminds staff about a shift scheduled for tomorrow, once, by email to
    whichever opted-in address (reminder_email) they've set on their own
    account - see the "Notification Settings" section in staff.html. A
    staff member with none set is skipped every cycle (reminded_at stays
    NULL, not marked as handled) so that opting in later, before the shift
    date passes, still gets them reminded on the next hourly check."""
    conn = get_db_connection()
    cursor = conn.cursor()
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

    cursor.execute(
        """
        SELECT s.shift_id, COALESCE(u.fullname, u.username, s.staff_name) AS staff_name,
               s.shift_date, s.shift_note, u.reminder_email AS notify_email
        FROM staff_shifts s
        LEFT JOIN users u ON u.user_id = s.staff_user_id
        WHERE s.shift_date = ? AND s.reminded_at IS NULL
        """,
        (tomorrow_str,),
    )
    candidates = cursor.fetchall()

    for row in candidates:
        if not row['notify_email']:
            continue  # nothing to notify with yet - leave reminded_at NULL, retry next cycle
        if not _send_shift_reminder_email(row['notify_email'], row['staff_name'], row['shift_date'], row['shift_note']):
            continue  # mail failed - leave reminded_at NULL so the next cycle retries

        cursor.execute(
            "UPDATE staff_shifts SET reminded_at = ? WHERE shift_id = ?",
            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), row['shift_id']),
        )
        conn.commit()

    conn.close()


def _not_clocked_in(staff_user_id, day):
    # No attendance row at all, or a rejected request. Pending means they're
    # here and waiting on admin; Present/Late means they came in.
    latest = get_latest_attendance(staff_user_id, day)
    return latest is None or latest['status'] == 'Absent'


def process_missing_rostered_staff(now=None):
    """Alerts admin (once per shift) about each staff member on today's
    roster - and not marked unavailable - who hasn't clocked in by
    MISSING_STAFF_CUTOFF. Someone with a pending clock-in request isn't
    flagged: that request already notified admin."""
    now = now or datetime.now()
    if (now.hour, now.minute) < MISSING_STAFF_CUTOFF:
        return
    today = now.strftime('%Y-%m-%d')

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT s.shift_id, s.staff_user_id, COALESCE(u.fullname, u.username, s.staff_name) AS staff_name
        FROM staff_shifts s
        LEFT JOIN users u ON u.user_id = s.staff_user_id
        WHERE s.shift_date = ? AND s.late_alerted_at IS NULL AND s.staff_user_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM staff_unavailability n
              WHERE n.staff_user_id = s.staff_user_id AND n.unavailable_date = s.shift_date
          )
        """,
        (today,),
    )
    missing = [row for row in cursor.fetchall() if _not_clocked_in(row['staff_user_id'], today)]
    if missing:
        bookings_today = cursor.execute(
            "SELECT COUNT(*) FROM reservations WHERE booking_date = ? AND queue_status = 'waiting'", (today,)
        ).fetchone()[0]
        for row in missing:
            notify_role(
                'admin', 'Rostered Staff Not Clocked In',
                f"{row['staff_name']} is on today's roster but hasn't clocked in yet "
                f"({bookings_today} booking(s) scheduled today). Call them or arrange cover.",
                'attendance',
            )
            cursor.execute("UPDATE staff_shifts SET late_alerted_at = ? WHERE shift_id = ?",
                           (now.strftime('%Y-%m-%d %H:%M:%S'), row['shift_id']))
        conn.commit()
    conn.close()


def start_roster_watch(app):
    start_periodic_worker(app, process_missing_rostered_staff, ROSTER_WATCH_INTERVAL_SECONDS, 'roster-watch')


def start_shift_reminder_scheduler(app):
    start_periodic_worker(app, process_shift_reminders, REMINDER_CHECK_INTERVAL_SECONDS, 'shift-reminder-scheduler')
