"""When a booked car can use its bay - the booking times offered and accepted.

A bay washes one car at a time. A booking holds its bay from its start time
for the service's length (the service's set minutes plus the vehicle size's
extra) plus a clean-up gap. A time is offered only when:
- it is still ahead (today) and the service ends by closing time;
- it doesn't overlap another booking in that bay;
- for today, the bay is actually free by then - the car being washed is
  done (its estimated finish) and the walk-ins already waiting have had
  their turn ("Bay busy until 2:30 PM").
Times run every SLOT_MINUTES from opening. The server checks again when a
booking is made or moved, so two customers can't take the same time.
"""
from datetime import datetime, timedelta

from database import get_db_connection
from utils import ACTIVE_BOOKING_STATUSES, BOOKING_TIME_FORMAT, parse_booking_datetime

SLOT_MINUTES = 30
GAP_MINUTES = 10
OPEN_MINUTE = 8 * 60          # 8:00 AM
CLOSE_MINUTE = 21 * 60        # 9:00 PM - a booked service must be done by then
OVERDUE_ALLOWANCE = 5         # a car past its estimate is assumed to need ~5 more minutes
_GAP = timedelta(minutes=GAP_MINUTES)


def slot_times():
    """'08:00 AM', '08:30 AM', ... '08:30 PM' - every start time of the day."""
    day = datetime(2000, 1, 1)
    return [(day + timedelta(minutes=m)).strftime(BOOKING_TIME_FORMAT)
            for m in range(OPEN_MINUTE, CLOSE_MINUTE, SLOT_MINUTES)]


def service_minutes(cursor, service_id, vehicle_type):
    row = cursor.execute(
        """SELECT s.base_duration_minutes + COALESCE(vt.multiplier_minutes, 0)
           FROM services s LEFT JOIN vehicle_types vt ON vt.type_name = ?
           WHERE s.service_id = ?""",
        (vehicle_type, service_id),
    ).fetchone()
    return row[0] if row and row[0] else None


def booking_minutes(cursor, reservation_id):
    row = cursor.execute("SELECT service_id, vehicle_type FROM reservations WHERE reservation_id = ?", (reservation_id,)).fetchone()
    return service_minutes(cursor, row['service_id'], row['vehicle_type']) if row else None


def _booked_intervals(cursor, date_str, bay, exclude_reservation_id=None):
    """[(start, end incl. clean-up gap, plate)] for the bay's active bookings that day."""
    rows = cursor.execute(
        f"""
        SELECT r.reservation_id, r.vehicle_plate_number, r.booking_date, r.booking_time,
               s.base_duration_minutes + COALESCE(vt.multiplier_minutes, 0) AS minutes
        FROM reservations r
        JOIN services s ON s.service_id = r.service_id
        LEFT JOIN vehicle_types vt ON vt.type_name = r.vehicle_type
        WHERE r.booking_date = ? AND r.assigned_bay = ? AND r.reservation_id != ?
          AND r.queue_status IN ({','.join('?' * len(ACTIVE_BOOKING_STATUSES))})
        """,
        (date_str, bay, exclude_reservation_id or -1, *ACTIVE_BOOKING_STATUSES),
    ).fetchall()
    intervals = []
    for row in rows:
        start = parse_booking_datetime(row['booking_date'], row['booking_time'])
        if start:
            intervals.append((start, start + timedelta(minutes=row['minutes'] or 0) + _GAP, row['vehicle_plate_number']))
    return intervals


def bay_busy_until(cursor, bay, now=None, exclude_reservation_id=None):
    """When the bay will be free right now, going by what's in it today: the
    car being washed (its estimated finish) and the walk-ins already waiting
    for it. None when it's free now."""
    now = now or datetime.now()
    rows = cursor.execute(
        """
        SELECT r.reservation_id, r.queue_status, r.started_at, r.booking_date,
               s.base_duration_minutes + COALESCE(vt.multiplier_minutes, 0) AS minutes
        FROM reservations r
        JOIN services s ON s.service_id = r.service_id
        LEFT JOIN vehicle_types vt ON vt.type_name = r.vehicle_type
        WHERE r.assigned_bay = ? AND r.reservation_id != ?
          AND (r.queue_status = 'in_progress'
               OR (r.queue_status = 'waiting' AND (r.booking_date IS NULL OR r.booking_date = '')))
        ORDER BY r.queue_status = 'waiting', r.reservation_id
        """,
        (bay, exclude_reservation_id or -1),
    ).fetchall()
    free_at = now
    for row in rows:
        if row['queue_status'] == 'in_progress':
            try:
                started = datetime.strptime(row['started_at'], '%Y-%m-%d %H:%M:%S')
            except (TypeError, ValueError):
                started = now
            finish = started + timedelta(minutes=row['minutes'] or 0)
            free_at = max(free_at, max(finish, now + timedelta(minutes=OVERDUE_ALLOWANCE)) + _GAP)
        else:
            free_at = free_at + timedelta(minutes=row['minutes'] or 0) + _GAP
    return free_at if free_at > now else None


def check_time(cursor, date_str, time_str, minutes, bay, exclude_reservation_id=None, now=None):
    """None if a booking of `minutes` can start at date_str/time_str in bay,
    otherwise why not: {'reason': 'invalid'|'past'|'closing'|'busy'|'booked', 'until': datetime?}."""
    now = now or datetime.now()
    start = parse_booking_datetime(date_str, time_str)
    if start is None or not minutes:
        return {'reason': 'invalid'}
    minute_of_day = start.hour * 60 + start.minute
    if minute_of_day < OPEN_MINUTE:
        return {'reason': 'closing'}
    end = start + timedelta(minutes=minutes)
    if start <= now:
        return {'reason': 'past'}
    if end > start.replace(hour=CLOSE_MINUTE // 60, minute=CLOSE_MINUTE % 60, second=0):
        return {'reason': 'closing'}
    if start.date() == now.date():
        busy = bay_busy_until(cursor, bay, now, exclude_reservation_id)
        if busy and start < busy:
            return {'reason': 'busy', 'until': busy}
    for other_start, other_end, _ in _booked_intervals(cursor, date_str, bay, exclude_reservation_id):
        if start < other_end and other_start < end + _GAP:
            return {'reason': 'booked', 'until': other_end}
    return None


def day_times(cursor, date_str, minutes, bay, exclude_reservation_id=None, now=None):
    """Every start time of the day with whether this service fits there."""
    now = now or datetime.now()
    times = []
    for time_str in slot_times():
        problem = check_time(cursor, date_str, time_str, minutes, bay, exclude_reservation_id, now)
        entry = {'time': time_str, 'available': problem is None}
        if problem:
            entry['reason'] = problem['reason']
            if problem.get('until'):
                entry['until'] = problem['until'].strftime(BOOKING_TIME_FORMAT)
        times.append(entry)
    return times


def next_available_time(cursor, date_str, minutes, bay, exclude_reservation_id=None, after=None, now=None):
    """The first start time that day (from `after`, a time string) the service fits, or None."""
    after_dt = parse_booking_datetime(date_str, after) if after else None
    for entry in day_times(cursor, date_str, minutes, bay, exclude_reservation_id, now):
        if entry['available'] and (after_dt is None or parse_booking_datetime(date_str, entry['time']) >= after_dt):
            return entry['time']
    return None


def describe_problem(problem, bay):
    """A customer-facing sentence for check_time's answer."""
    reason = problem['reason']
    until = problem.get('until')
    if reason == 'busy':
        return f"{bay} is busy until about {until.strftime('%I:%M %p').lstrip('0')}."
    if reason == 'booked':
        return f"{bay} already has a booking then (until about {until.strftime('%I:%M %p').lstrip('0')})."
    if reason == 'closing':
        return 'This service would run past closing time (9:00 PM).'
    if reason == 'past':
        return 'That time has already passed.'
    return 'Please pick a valid date and time.'


def next_bookable_date(after_date_str, time_str, bay, exclude_reservation_id=None, minutes=None):
    """The earliest date after after_date_str with an available rostered
    staff member (see is_date_staffed) on which the booking fits in `bay` at
    `time_str`, or None if the roster has no such day yet. minutes defaults
    to the length of the booking being moved."""
    conn = get_db_connection()
    cursor = conn.cursor()
    if minutes is None and exclude_reservation_id:
        minutes = booking_minutes(cursor, exclude_reservation_id)
    dates = [r[0] for r in cursor.execute(
        """
        SELECT DISTINCT s.shift_date FROM staff_shifts s
        WHERE s.shift_date > ? AND s.staff_user_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM staff_unavailability u
              WHERE u.staff_user_id = s.staff_user_id AND u.unavailable_date = s.shift_date
          )
        ORDER BY s.shift_date
        LIMIT 60
        """,
        (after_date_str,),
    )]
    found = next((day for day in dates
                  if check_time(cursor, day, time_str, minutes or 30, bay, exclude_reservation_id) is None), None)
    conn.close()
    return found
