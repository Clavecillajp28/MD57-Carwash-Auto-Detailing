import os
from datetime import datetime, timedelta

from database import get_db_connection
from live_updates import publish
from utils import log_activity, now_str, start_periodic_worker

# How long a cancelled booking stays visible in the live /cancelled list and
# in the `reservations` table before being moved into reservations_archive.
# This is the actual fix for "cancelled bookings pile up forever and clutter
# the database" - they were already excluded from every active-booking/
# queue/slot-conflict query the moment they're marked 'cancelled' (see
# api_get_queue/api_booked_slots/api_submit_booking's slot-conflict check),
# so the remaining problem was purely that they never left the live table.
# Configurable via .env for a real deployment; 14 days is a reasonable
# default for local/demo use.
CANCELLED_RETENTION_DAYS = int(os.environ.get('CANCELLED_BOOKING_RETENTION_DAYS', 14))
SWEEP_INTERVAL_SECONDS = 60 * 60  # hourly - not time-sensitive like the 60s no-show sweep

# Every reservations column that has a same-named counterpart in
# reservations_archive (see database.py) - kept as an explicit list rather
# than SELECT * so a future column added to one table without the other
# fails loudly (KeyError) instead of silently inserting misaligned data.
_ARCHIVE_COLUMNS = [
    'reservation_id', 'user_id', 'customer_name', 'vehicle_plate_number', 'vehicle_type',
    'service_id', 'customer_email', 'queue_number', 'queue_status', 'booking_source',
    'assigned_bay', 'booking_date', 'booking_time', 'cancelled_at', 'cancel_reason',
    'completed_at', 'created_at', 'weather_condition', 'queue_length_snapshot',
    'active_staff_snapshot', 'assigned_staff_id', 'predicted_wait_minutes', 'started_at', 'price',
    'rescheduled_from', 'reschedule_reason', 'precipitation_mm', 'vehicle_model', 'arrived_at',
    'work_ahead_minutes', 'late_notice_minutes', 'is_synthetic',
    'ready_notified_at', 'picked_up_at', 'picked_up_by', 'picked_up_by_name',
]


def archive_expired_cancelled_bookings():
    """Moves cancelled bookings older than CANCELLED_RETENTION_DAYS out of
    the live `reservations` table and into `reservations_archive` - the
    record is preserved for audit/history, just off every table an active
    booking/queue/slot/history query reads from, and off the /cancelled
    page's live list. Once archived (and deleted from `reservations`), a
    booking can no longer be found by /api/restore-booking - it cannot
    accidentally re-enter the queue.

    Only ever touches a row that is *still* queue_status='cancelled' at the
    moment of archiving: if an admin restores it (back to 'waiting')
    anytime before this runs, the DELETE's own WHERE clause simply matches
    zero rows and nothing is archived for it - the same
    already-handled-by-a-concurrent-change guard no_show.py's sweep uses."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=CANCELLED_RETENTION_DAYS)).strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute(
        """
        SELECT * FROM reservations
        WHERE queue_status = 'cancelled'
          AND cancelled_at IS NOT NULL AND cancelled_at != ''
          AND cancelled_at < ?
        """,
        (cutoff,),
    )
    candidates = cursor.fetchall()

    archived_count = 0
    # archived_at is passed in local time (see utils.now_str) rather than
    # left to the column's UTC default.
    placeholders = ', '.join('?' for _ in _ARCHIVE_COLUMNS)
    insert_sql = (f"INSERT OR IGNORE INTO reservations_archive ({', '.join(_ARCHIVE_COLUMNS)}, archived_at) "
                  f"VALUES ({placeholders}, ?)")

    for row in candidates:
        cursor.execute(
            "DELETE FROM reservations WHERE reservation_id = ? AND queue_status = 'cancelled'",
            (row['reservation_id'],),
        )
        if cursor.rowcount == 0:
            continue  # restored concurrently between the SELECT above and here - leave it alone
        cursor.execute(insert_sql, [row[col] for col in _ARCHIVE_COLUMNS] + [now_str()])
        conn.commit()
        archived_count += 1

    conn.close()
    if archived_count:
        publish('data')
        log_activity(
            'System',
            f'Archived {archived_count} expired cancelled booking(s) '
            f'(older than {CANCELLED_RETENTION_DAYS} days) to reservations_archive',
        )


def start_cleanup_sweeper(app):
    start_periodic_worker(app, archive_expired_cancelled_bookings, SWEEP_INTERVAL_SECONDS, 'cancelled-booking-archiver')
