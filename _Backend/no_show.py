from datetime import timedelta
from shop_clock import datetime

from database import get_db_connection
from live_updates import publish
from utils import (
    log_activity, parse_booking_datetime, send_branded_email, create_notification, notify_role,
    start_periodic_worker, get_active_staff_count,
)
from scheduling import next_bookable_date

# Product decision (2026-09-11): scheduled bookings get a 30-minute grace
# period past their booked date/time. If staff haven't started the service
# by then, the slot is released (queue_status -> 'cancelled', cancel_reason
# -> 'no_show') and the customer is emailed that their reservation expired
# and they're welcome to rebook. Walk-ins have no booking_date/booking_time
# and are never touched - there's no scheduled time to measure lateness
# from for someone who's already physically waiting.
#
# Unless the shop was the one missing: if no staff were on duty, a booking
# still 'waiting' past its grace period isn't the customer's no-show. It's
# moved to the next day with available rostered staff (same time) and the
# customer is told - they can reschedule or cancel it from My Bookings if the new date
# doesn't suit them. If no later day has staff rostered yet, it's cancelled
# as 'staff_absent' with an apology instead of the no-show message.
GRACE_PERIOD_MINUTES = 30
SWEEP_INTERVAL_SECONDS = 60


def _send_no_show_email(email, customer_name, plate_number, booking_date, booking_time):
    try:
        send_branded_email(
            subject='MD57 Car Wash - Your Reserved Slot Was Released',
            recipient=email,
            heading='Your Reserved Slot Was Released',
            plain_text_body=(
                f"Hello {customer_name or 'Customer'}!\n\n"
                f"Your reservation for vehicle {plate_number} on {booking_date} at {booking_time} "
                f"has been released after a {GRACE_PERIOD_MINUTES}-minute grace period since we "
                f"didn't see you arrive.\n\n"
                f"No worries - you're welcome to book a new slot anytime through our booking page.\n\n"
                f"Sorry for any inconvenience!\n- MD57 Team"
            ),
        )
    except Exception as e:
        print(f"[WARNING] Failed to send no-show notification email to {email}: {e}")


def staff_were_absent(booking_date, now):
    """No staff to serve a booking on booking_date: for today, nobody is
    clocked in right now (the sweep runs as the grace period ends); for a
    past day (the sweep caught it late, e.g. the server was off), nobody
    clocked in at all that day."""
    if booking_date == now.strftime('%Y-%m-%d'):
        return get_active_staff_count() == 0
    conn = get_db_connection()
    row = conn.execute(
        "SELECT 1 FROM staff_attendance WHERE date_str = ? AND status IN ('Present', 'Late') LIMIT 1",
        (booking_date,),
    ).fetchone()
    conn.close()
    return row is None


def _handle_staff_absence(conn, row, now):
    cursor = conn.cursor()
    plate, old_date, time_str = row['vehicle_plate_number'], row['booking_date'], row['booking_time']
    new_date = next_bookable_date(now.strftime('%Y-%m-%d'), time_str, row['assigned_bay'], row['reservation_id'])
    if new_date:
        cursor.execute(
            """
            UPDATE reservations
            SET booking_date = ?, rescheduled_from = ?, reschedule_reason = 'staff_absent'
            WHERE reservation_id = ? AND queue_status = 'waiting'
            """,
            (new_date, f"{old_date} {time_str}", row['reservation_id']),
        )
    else:
        cursor.execute(
            """
            UPDATE reservations
            SET queue_status = 'cancelled', cancelled_at = ?, cancel_reason = 'staff_absent'
            WHERE reservation_id = ? AND queue_status = 'waiting'
            """,
            (now.strftime('%Y-%m-%d %H:%M:%S'), row['reservation_id']),
        )
    if cursor.rowcount == 0:
        return  # already handled by a concurrent change
    conn.commit()
    publish('data')

    if new_date:
        log_activity('System', f"Rescheduled booking for plate {plate} from {old_date} {time_str} to {new_date} - no staff on duty")
        title = 'Booking Rescheduled'
        customer_message = (
            f"Sorry - no staff were available for your {old_date} {time_str} booking ({plate}). "
            f"It has been moved to {new_date} at {time_str}. If that doesn't work for you, "
            f"you can pick another time or cancel it from My Bookings."
        )
        shop_message = f"{plate} ({old_date} {time_str}) was moved to {new_date} - no staff were on duty."
        category = 'booking_rescheduled'
    else:
        log_activity('System', f"Cancelled booking for plate {plate} ({old_date} {time_str}) - no staff on duty and none rostered ahead")
        title = 'Booking Cancelled'
        customer_message = (
            f"Sorry - no staff were available for your {old_date} {time_str} booking ({plate}), and no "
            f"upcoming day has staff scheduled yet, so it has been cancelled. Please book again soon."
        )
        shop_message = f"{plate} ({old_date} {time_str}) was cancelled - no staff were on duty and none are rostered ahead."
        category = 'booking_cancelled'

    if row['customer_email']:
        try:
            send_branded_email(
                subject=f'MD57 Car Wash - {title}',
                recipient=row['customer_email'],
                heading=title,
                plain_text_body=f"Hello {row['customer_name'] or 'Customer'}!\n\n{customer_message}\n\nWe're sorry for the inconvenience.\n- MD57 Team",
            )
        except Exception as e:
            print(f"[WARNING] Failed to send staff-absence email to {row['customer_email']}: {e}")
    if row['user_id']:
        create_notification(row['user_id'], title, customer_message, category, row['reservation_id'])
    for role in ('admin', 'staff'):
        notify_role(role, f'{title} (Staff Absent)', shop_message, category, row['reservation_id'])


def process_no_show_bookings():
    """Releases scheduled bookings that are still 'waiting' more than
    GRACE_PERIOD_MINUTES past their booked date/time. Each release frees up
    its bay/slot immediately (the row leaves the 'waiting' set queried by
    /api/get-queue and scheduling's time check), so whichever car is
    next in that bay's queue is simply the next 'waiting' row staff see -
    no separate "advance to next car" step is needed."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT reservation_id, vehicle_plate_number, customer_name, customer_email, user_id,
               booking_date, booking_time, assigned_bay, arrived_at
        FROM reservations
        WHERE queue_status = 'waiting'
          AND booking_date IS NOT NULL AND booking_date != ''
          AND booking_time IS NOT NULL AND booking_time != ''
    """)
    candidates = cursor.fetchall()

    now = datetime.now()
    grace_period = timedelta(minutes=GRACE_PERIOD_MINUTES)
    cancelled_at_str = now.strftime('%Y-%m-%d %H:%M:%S')

    for row in candidates:
        scheduled_dt = parse_booking_datetime(row['booking_date'], row['booking_time'])
        if scheduled_dt is None or now - scheduled_dt < grace_period:
            continue
        # Checked in by staff (api_mark_arrived): they're here, waiting on
        # the bay - not a no-show, however long that takes today.
        if row['arrived_at'] and row['booking_date'] == now.strftime('%Y-%m-%d'):
            continue

        if staff_were_absent(row['booking_date'], now):
            _handle_staff_absence(conn, row, now)
            continue

        cursor.execute("""
            UPDATE reservations
            SET queue_status = 'cancelled', cancelled_at = ?, cancel_reason = 'no_show'
            WHERE reservation_id = ? AND queue_status = 'waiting'
        """, (cancelled_at_str, row['reservation_id']))
        if cursor.rowcount == 0:
            continue  # already handled by a concurrent sweep
        conn.commit()
        publish('data')

        log_activity('System', f"Auto-released no-show booking for plate {row['vehicle_plate_number']} "
                                f"(scheduled {row['booking_date']} {row['booking_time']}, "
                                f"{GRACE_PERIOD_MINUTES}-min grace period expired)")

        if row['customer_email']:
            _send_no_show_email(row['customer_email'], row['customer_name'],
                                 row['vehicle_plate_number'], row['booking_date'], row['booking_time'])

        # In-app notifications - independent of the email above, so the
        # customer still sees this in-website even without a working mail
        # setup, and staff/admin see it without needing to check email at all.
        if row['user_id']:
            create_notification(
                row['user_id'], 'Booking Cancelled',
                f"Your reservation for {row['vehicle_plate_number']} was released after a "
                f"{GRACE_PERIOD_MINUTES}-minute grace period with no arrival. You're welcome to rebook.",
                'booking_cancelled', row['reservation_id'],
            )
        notify_role(
            'admin', 'Booking Auto-Cancelled (No-Show)',
            f"{row['vehicle_plate_number']} was auto-released - no arrival within {GRACE_PERIOD_MINUTES} min of its scheduled time.",
            'booking_cancelled', row['reservation_id'],
        )
        notify_role(
            'staff', 'Booking Auto-Cancelled (No-Show)',
            f"{row['vehicle_plate_number']} was auto-released and is no longer in the queue.",
            'booking_cancelled', row['reservation_id'],
        )

    conn.close()


def start_no_show_sweeper(app):
    start_periodic_worker(app, process_no_show_bookings, SWEEP_INTERVAL_SECONDS, 'no-show-sweeper')
