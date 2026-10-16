"""Tells a booked customer when their bay is running late.

Every minute, for each bay, today's booked cars that haven't started are
lined up behind the car being washed: each starts at its booked time or
when the bay is free, whichever is later. A customer whose expected start
is LATE_MINUTES or more past their booked time gets an in-app notice - and
another only if it slips a further LATE_MINUTES. Walk-ins don't push a
booked car back (the booked-slot hold, slot_hold.py), so they aren't counted.
"""
from datetime import datetime, timedelta

from database import get_db_connection
from scheduling import GAP_MINUTES, OVERDUE_ALLOWANCE
from utils import create_notification, parse_booking_datetime, start_periodic_worker

LATE_MINUTES = 15
CHECK_INTERVAL_SECONDS = 60
LOOK_AHEAD = timedelta(minutes=90)


def _clock(dt):
    return dt.strftime('%I:%M %p').lstrip('0')


def notify_late_bookings(now=None):
    now = now or datetime.now()
    today = now.strftime('%Y-%m-%d')
    gap = timedelta(minutes=GAP_MINUTES)
    conn = get_db_connection()
    cursor = conn.cursor()
    rows = cursor.execute(
        """
        SELECT r.reservation_id, r.user_id, r.vehicle_plate_number, r.assigned_bay, r.queue_status, r.booking_date,
               r.booking_time, r.started_at, r.late_notice_minutes,
               s.base_duration_minutes + COALESCE(vt.multiplier_minutes, 0) AS minutes
        FROM reservations r
        JOIN services s ON s.service_id = r.service_id
        LEFT JOIN vehicle_types vt ON vt.type_name = r.vehicle_type
        WHERE r.queue_status = 'in_progress' OR (r.queue_status = 'waiting' AND r.booking_date = ?)
        """,
        (today,),
    ).fetchall()

    notices = []
    for bay in sorted({r['assigned_bay'] for r in rows}):
        free_at = now
        for car in (r for r in rows if r['assigned_bay'] == bay and r['queue_status'] == 'in_progress'):
            try:
                finish = datetime.strptime(car['started_at'], '%Y-%m-%d %H:%M:%S') + timedelta(minutes=car['minutes'] or 0)
            except (TypeError, ValueError):
                finish = now
            free_at = max(free_at, max(finish, now + timedelta(minutes=OVERDUE_ALLOWANCE)) + gap)

        booked = sorted(((parse_booking_datetime(r['booking_date'], r['booking_time']), r)
                         for r in rows if r['assigned_bay'] == bay and r['queue_status'] == 'waiting'),
                        key=lambda pair: (pair[0] or now, pair[1]['reservation_id']))
        for booked_at, car in booked:
            if booked_at is None or booked_at > now + LOOK_AHEAD:
                continue
            start = max(booked_at, free_at)
            free_at = start + timedelta(minutes=car['minutes'] or 0) + gap
            delay = int((start - booked_at).total_seconds() // 60)
            already = car['late_notice_minutes'] or 0
            if car['user_id'] and delay >= LATE_MINUTES and delay >= already + LATE_MINUTES:
                notices.append((car, booked_at, start, delay))

    for car, booked_at, start, delay in notices:
        cursor.execute("UPDATE reservations SET late_notice_minutes = ? WHERE reservation_id = ?", (delay, car['reservation_id']))
    conn.commit()
    conn.close()

    for car, booked_at, start, delay in notices:
        create_notification(
            car['user_id'], 'Running Late',
            f"{car['assigned_bay']} is running about {delay} minutes late - your {_clock(booked_at)} service for "
            f"{car['vehicle_plate_number']} should start around {_clock(start)}. Sorry for the wait.",
            'queue_update', car['reservation_id'],
        )
    return len(notices)


def start_late_notice_worker(app):
    start_periodic_worker(app, notify_late_bookings, CHECK_INTERVAL_SECONDS, 'late-notice')
