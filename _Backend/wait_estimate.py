"""How much work is already ahead of a new car in its bay - the main input
of the wait-time model (ai_engine.py).

A bay washes one car at a time. Ahead of a car joining now is:
- the car being washed: its standard minutes minus the time it has run
  (never below zero); a car already marked done is out of the way;
- the cars waiting before it in that bay: walk-ins, and bookings due
  within the next 30 minutes - each at its standard minutes.
Standard minutes = the service's set duration plus the vehicle size's extra.
"""
from datetime import datetime, timedelta

from utils import parse_booking_datetime

DUE_SOON = timedelta(minutes=30)


def work_ahead_minutes(cursor, bay, now=None, exclude_reservation_id=None):
    now = now or datetime.now()
    rows = cursor.execute(
        """
        SELECT r.reservation_id, r.queue_status, r.started_at, r.booking_date, r.booking_time,
               s.base_duration_minutes + COALESCE(vt.multiplier_minutes, 0) AS standard
        FROM reservations r
        JOIN services s ON s.service_id = r.service_id
        LEFT JOIN vehicle_types vt ON vt.type_name = r.vehicle_type
        WHERE r.assigned_bay = ? AND r.queue_status IN ('waiting', 'in_progress')
        """,
        (bay,),
    ).fetchall()

    total = 0.0
    for row in rows:
        if row['reservation_id'] == exclude_reservation_id:
            continue
        standard = row['standard'] or 0
        if row['queue_status'] == 'in_progress':
            try:
                ran = (now - datetime.strptime(row['started_at'], '%Y-%m-%d %H:%M:%S')).total_seconds() / 60
            except (TypeError, ValueError):
                ran = 0
            total += max(0.0, standard - ran)
            continue
        booked = parse_booking_datetime(row['booking_date'], row['booking_time'])
        if booked is None or booked <= now + DUE_SOON:
            total += standard
    return round(total, 1)


def is_walk_in(item):
    return 'walk' in (item.get('booking_source') or '').lower()


def ahead_of_walk_ins(items, now=None):
    """{reservation_id: (cars_ahead, minutes_ahead)} for every waiting
    walk-in on the live queue board - the same rule as work_ahead_minutes,
    per car: the car being washed in its bay (its remaining standard
    minutes), bookings due within 30 minutes, and the walk-ins that joined
    before it. items: /api/get-queue rows (id, status, assigned_bay,
    booking_source, booking_date, booking_time, started_at, queue_number,
    duration_minutes = standard minutes)."""
    now = now or datetime.now()
    by_bay = {}
    for item in items:
        by_bay.setdefault(item['assigned_bay'], []).append(item)

    result = {}
    for bay_items in by_bay.values():
        washing, running, due_bookings, walk_ins = 0, 0.0, [], []
        for item in bay_items:
            standard = item['duration_minutes'] or 0
            if item['status'] == 'in_progress':
                try:
                    ran = (now - datetime.strptime(item['started_at'], '%Y-%m-%d %H:%M:%S')).total_seconds() / 60
                except (TypeError, ValueError):
                    ran = 0
                washing += 1
                running += max(0.0, standard - ran)
            elif item['status'] == 'waiting':
                if is_walk_in(item):
                    walk_ins.append(item)
                else:
                    booked = parse_booking_datetime(item['booking_date'], item['booking_time'])
                    if booked is None or booked <= now + DUE_SOON:
                        due_bookings.append(item)
        cars = washing + len(due_bookings)
        minutes = running + sum(b['duration_minutes'] or 0 for b in due_bookings)
        for walk_in in sorted(walk_ins, key=lambda w: (w['queue_number'] or 0, w['id'])):
            result[walk_in['id']] = (cars, round(minutes, 1))
            cars += 1
            minutes += walk_in['duration_minutes'] or 0
    return result
