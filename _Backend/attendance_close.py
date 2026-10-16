"""Closes out each day's attendance at closing time (9:00 PM).

Every minute, for today once the shop has closed and for any earlier day
left open (the server was off at closing):
- a staff member still clocked in is timed out - at closing, or when their
  last car that day was finished if that was later. While today's last cars
  are still waiting (a late walk-in or booking) or being washed, the washers who
  could take them stay clocked in;
- a clock-in request nobody confirmed or rejected expires. It is removed,
  not turned into attendance or an absence: nobody verified they were there.
staff_attendance is insert-only, so a time-out is a new row (see
utils.get_latest_attendance_by_staff).
"""
from datetime import datetime

from database import get_db_connection
from live_updates import publish
from utils import BUSINESS_CLOSE_HOUR, create_notification, log_activity, notify_role, now_str, start_periodic_worker

CHECK_INTERVAL_SECONDS = 60
CLOSING_TIME_TEXT = datetime(2000, 1, 1, BUSINESS_CLOSE_HOUR).strftime('%I:%M %p')   # '09:00 PM'


def close_out_attendance(now=None):
    """Returns (timed_out, expired) - how many of each this run."""
    now = now or datetime.now()
    today = now.strftime('%Y-%m-%d')
    closed_today = now.hour >= BUSINESS_CLOSE_HOUR
    conn = get_db_connection()
    cursor = conn.cursor()
    # The row that is "true right now" for each staff member and day.
    rows = cursor.execute(
        """
        SELECT a.attendance_id, a.staff_user_id, COALESCE(u.fullname, u.username, a.staff_name) AS staff_name,
               a.date_str, a.status, a.time_in, a.time_out
        FROM staff_attendance a
        LEFT JOIN users u ON u.user_id = a.staff_user_id
        WHERE a.date_str <= ?
          AND a.attendance_id = (SELECT MAX(b.attendance_id) FROM staff_attendance b
                                 WHERE b.staff_user_id = a.staff_user_id AND b.date_str = a.date_str)
          AND (a.status = 'Pending' OR (a.status IN ('Present', 'Late') AND a.time_out = '--:--'))
        """,
        (today,),
    ).fetchall()

    # Today's cars not started yet still need an on-duty washer.
    cars_waiting = cursor.execute(
        "SELECT 1 FROM reservations WHERE queue_status = 'waiting' AND (booking_date IS NULL OR booking_date = '' OR booking_date = ?) LIMIT 1",
        (today,),
    ).fetchone()

    timed_out, expired = [], []
    for row in rows:
        is_today = row['date_str'] == today
        if is_today and not closed_today:
            continue
        if row['status'] == 'Pending':
            cursor.execute("DELETE FROM staff_attendance WHERE attendance_id = ? AND status = 'Pending'", (row['attendance_id'],))
            expired.append(row)
            continue
        if is_today and (cars_waiting or cursor.execute(
                "SELECT 1 FROM reservations WHERE assigned_staff_id = ? AND queue_status = 'in_progress'", (row['staff_user_id'],)).fetchone()):
            continue  # still needed - timed out once the last cars are done
        last_done = cursor.execute(
            "SELECT MAX(completed_at) FROM reservations WHERE assigned_staff_id = ? AND substr(completed_at, 1, 10) = ?",
            (row['staff_user_id'], row['date_str']),
        ).fetchone()[0]
        time_out = CLOSING_TIME_TEXT
        if last_done and last_done[11:16] > f'{BUSINESS_CLOSE_HOUR:02d}:00':
            time_out = datetime.strptime(last_done, '%Y-%m-%d %H:%M:%S').strftime('%I:%M %p')
        cursor.execute(
            "INSERT INTO staff_attendance (staff_user_id, staff_name, date_str, status, time_in, time_out, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row['staff_user_id'], row['staff_name'], row['date_str'], row['status'], row['time_in'], time_out, now_str()),
        )
        timed_out.append((row, time_out))
    conn.commit()
    conn.close()

    for row, time_out in timed_out:
        log_activity('system', f"Automatic time-out at closing: {row['staff_name']} at {time_out} ({row['date_str']})")
        create_notification(row['staff_user_id'], 'Timed Out at Closing',
                            f"You were timed out automatically at {time_out.lstrip('0')} ({row['date_str']}).", 'attendance')
    for row in expired:
        log_activity('system', f"Clock-in request expired at closing: {row['staff_name']} ({row['time_in']}, {row['date_str']})")
        create_notification(row['staff_user_id'], 'Clock-In Request Expired',
                            f"Your clock-in request at {row['time_in'].lstrip('0')} ({row['date_str']}) wasn't confirmed before closing, so it expired.", 'attendance')
        notify_role('admin', 'Clock-In Request Expired',
                    f"{row['staff_name']}'s clock-in request at {row['time_in'].lstrip('0')} ({row['date_str']}) was never confirmed and expired at closing.",
                    'attendance')
    if timed_out or expired:
        publish('data')
    return len(timed_out), len(expired)


def start_attendance_close_worker(app):
    start_periodic_worker(app, close_out_attendance, CHECK_INTERVAL_SECONDS, 'attendance-close')
