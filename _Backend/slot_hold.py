"""A booked time slot is the booker's privilege.

A customer who booked a time from their own account must find their bay
free at that time. So nothing else may be started in that bay unless it is
expected to finish by the booked time - a 15-minute job at 9:30 is fine
ahead of a 10:00 booking, a 90-minute one is not.

Once the booked time arrives the bay stays held for HOLD_AFTER_BOOKED_MINUTES,
or - if staff have checked the customer in (reservations.arrived_at) - until
that booking is started. A customer who turns up later, within the no-show
grace period, is checked in and becomes next in the bay; a wash already
under way is never interrupted.

Walk-ins and later bookings are still accepted and queued - this only
decides what may be *started* (see api_start_service). Bookings staff enter
on someone's behalf are scheduled but not protected.
"""
from datetime import datetime, timedelta

from utils import parse_booking_datetime

HOLD_AFTER_BOOKED_MINUTES = 10
# booking_source of a booking a customer made from their own account -
# api_submit_booking sets it from the session's role, never from the request.
PROTECTED_BOOKING_SOURCE = 'Online Booking'


def current_time():
    return datetime.now()


def protected_bookings_today(cursor, now):
    """Today's still-waiting protected bookings, earliest slot first."""
    cursor.execute(
        """
        SELECT reservation_id, vehicle_plate_number, booking_date, booking_time, queue_number,
               assigned_bay, arrived_at
        FROM reservations
        WHERE queue_status = 'waiting' AND booking_source = ?
          AND booking_date = ? AND booking_time IS NOT NULL AND booking_time != ''
        """,
        (PROTECTED_BOOKING_SOURCE, now.strftime('%Y-%m-%d')),
    )
    bookings = []
    for row in cursor.fetchall():
        due = parse_booking_datetime(row['booking_date'], row['booking_time'])
        if due is not None:
            bookings.append({**dict(row), 'due': due})
    bookings.sort(key=_slot_order)
    return bookings


def _slot_order(booking):
    return (booking['due'], booking['queue_number'] or 0)


def holding_booking(candidate, protected, now):
    """The protected booking that stops `candidate` (reservation_id,
    assigned_bay, duration_minutes) being started right now, or None if it
    may start. A protected booking is never held by one whose slot comes
    after its own."""
    would_finish = now + timedelta(minutes=candidate['duration_minutes'] or 0)
    own = next((b for b in protected if b['reservation_id'] == candidate['reservation_id']), None)
    for booking in protected:
        if booking is own or booking['assigned_bay'] != candidate['assigned_bay']:
            continue
        if own and _slot_order(own) < _slot_order(booking):
            continue
        if would_finish <= booking['due']:
            continue
        if booking['arrived_at'] or now <= booking['due'] + timedelta(minutes=HOLD_AFTER_BOOKED_MINUTES):
            return booking
    return None


def hold_reason(candidate, booking, now):
    """Why `candidate` can't start yet, for staff."""
    slot = f"{booking['vehicle_plate_number']}'s {booking['booking_time']} booking"
    if now < booking['due']:
        return (f"{booking['assigned_bay']} is held for {slot} - this job needs about "
                f"{candidate['duration_minutes'] or 0} min and would not finish in time.")
    if booking['arrived_at']:
        return f"{booking['assigned_bay']} is held for {slot} - that customer has arrived and is next."
    return f"{booking['assigned_bay']} is held for {slot}, which is due now."
