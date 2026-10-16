"""Automated staff allocation: who washes each car, decided by the system.

Available means clocked in today (the same on-duty definition as the Staff
page) and not already working on another car. Available staff are ranked by
the MCDA/SAW score (Speed 40%, Punctuality 40%, Rating 20% - see
analytics.mcda_criteria); ties go to whoever has had fewer jobs today, then
fewer jobs overall, so a new hire with no record yet still gets turns.

- When a car starts, it is assigned to the top-ranked available washer
  (choose_washer). If no washer is free and clocked in, the car stays waiting.
- Whenever attendance changes (a clock-in confirmed, a time-in or time-out
  recorded), reallocate() gives every car in a bay without a working
  washer - never assigned, or its washer has clocked out - to the best
  available one.
Nobody assigns staff by hand.
"""
from datetime import datetime

from analytics import mcda_criteria
from database import get_db_connection
from extensions import mcda_engine
from utils import create_notification, get_on_duty_staff_ids, log_activity, notify_role


def _staff_accounts(cursor):
    return cursor.execute(
        "SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' AND disabled_at IS NULL ORDER BY name"
    ).fetchall()


def _busy_staff_ids(cursor):
    rows = cursor.execute(
        "SELECT DISTINCT assigned_staff_id FROM reservations WHERE queue_status IN ('in_progress', 'pending_confirmation') AND assigned_staff_id IS NOT NULL"
    ).fetchall()
    return {row[0] for row in rows}


def rank_available_staff(cursor):
    """{'ranked': [...best first, free only], 'on_duty': [...], 'busy_count', 'off_duty_count'}."""
    on_duty_ids = get_on_duty_staff_ids()
    busy_ids = _busy_staff_ids(cursor)
    today = datetime.now().strftime('%Y-%m-%d')

    on_duty, candidates, off_duty_count = [], [], 0
    for staff in _staff_accounts(cursor):
        if staff['user_id'] not in on_duty_ids:
            off_duty_count += 1
            continue
        jobs = cursor.execute(
            "SELECT COUNT(*), SUM(CASE WHEN substr(started_at, 1, 10) = ? THEN 1 ELSE 0 END) FROM reservations WHERE assigned_staff_id = ? AND is_synthetic=0",
            (today, staff['user_id']),
        ).fetchone()
        busy = staff['user_id'] in busy_ids
        entry = {'user_id': staff['user_id'], 'name': staff['name'], 'busy': busy,
                 'total_jobs': jobs[0] or 0, 'jobs_today': jobs[1] or 0}
        on_duty.append(entry)
        if busy:
            continue
        speed, punctuality, rating, _ = mcda_criteria(cursor, staff['user_id'])
        entry.update(speed=speed, punctuality=punctuality, rating=rating,
                     has_history=speed is not None or punctuality is not None or rating is not None)
        candidates.append(entry)

    scores = {r['name']: r['saw_score'] for r in mcda_engine.evaluate_staff([
        {'name': c['name'], 'service_speed': c['speed'], 'punctuality': c['punctuality'], 'feedback': c['rating']}
        for c in candidates
    ])}
    for c in candidates:
        c['saw_score'] = scores.get(c['name'], 0.0)
    candidates.sort(key=lambda c: (-c['saw_score'], c['jobs_today'], c['total_jobs'], c['name']))
    return {'ranked': candidates, 'on_duty': on_duty,
            'busy_count': sum(1 for s in on_duty if s['busy']), 'off_duty_count': off_duty_count}


def choose_washer(cursor):
    """A free, clocked-in washer, or None when the car must stay waiting."""
    ranking = rank_available_staff(cursor)
    if ranking['ranked']:
        return ranking['ranked'][0]
    return None


def _customer_label(car):
    return car['customer_name'] or car['customer_email'] or 'Walk-in customer'


def announce(car, washer, service_name, previous=None):
    """In-app notices for an allocation: the washer, every admin, the
    customer (whose car it is), and the washer it was moved from, if any."""
    plate, bay, customer = car['vehicle_plate_number'], car['assigned_bay'], _customer_label(car)
    rid = car['reservation_id']
    create_notification(washer['user_id'], 'New Assignment',
                        f"You're assigned to {customer} - {plate} ({service_name}, {bay}).", 'assignment', rid)
    if previous:
        create_notification(previous['user_id'], 'Assignment Changed',
                            f"{plate} ({customer}) was moved to {washer['name']} because you're no longer clocked in.",
                            'assignment', rid)
    notify_role('admin', 'Staff Reassigned' if previous else 'Staff Assigned',
                f"{washer['name']} is assigned to {customer} - {plate} ({service_name}, {bay})."
                + (f" Moved from {previous['name']}, who clocked out." if previous else ''), 'assignment', rid)
    if car['user_id']:
        create_notification(car['user_id'], 'Washer Changed' if previous else 'Service Started',
                            f"{washer['name']} is now working on your {plate} ({service_name}).", 'queue_update', rid)


def reallocate():
    """Gives each car in a bay that has no working washer - none assigned, or
    its washer is no longer clocked in - to the best available washer, oldest
    started first, while free staff remain. Returns how many were assigned."""
    conn = get_db_connection()
    conn.isolation_level = None
    cursor = conn.cursor()
    cursor.execute("BEGIN IMMEDIATE")
    on_duty_ids = get_on_duty_staff_ids()
    cars = cursor.execute(
        """
        SELECT r.reservation_id, r.user_id, r.customer_name, r.customer_email, r.vehicle_plate_number, r.assigned_bay,
               r.assigned_staff_id, COALESCE(u.fullname, u.username) AS washer_name, s.service_name
        FROM reservations r
        JOIN services s ON s.service_id = r.service_id
        LEFT JOIN users u ON u.user_id = r.assigned_staff_id
        WHERE r.queue_status = 'in_progress'
        ORDER BY r.started_at, r.reservation_id
        """
    ).fetchall()
    needing = [car for car in cars if car['assigned_staff_id'] is None or car['assigned_staff_id'] not in on_duty_ids]

    moves = []
    for car in needing:
        ranked = rank_available_staff(cursor)['ranked']
        if not ranked:
            break
        washer = ranked[0]
        cursor.execute("UPDATE reservations SET assigned_staff_id = ? WHERE reservation_id = ? AND queue_status = 'in_progress'",
                       (washer['user_id'], car['reservation_id']))
        previous = {'user_id': car['assigned_staff_id'], 'name': car['washer_name']} if car['assigned_staff_id'] else None
        moves.append((car, washer, previous))
    conn.commit()
    conn.close()

    for car, washer, previous in moves:
        log_activity('System', f"Allocated {car['vehicle_plate_number']} to {washer['name']}"
                               + (f" (moved from {previous['name']}, no longer clocked in)" if previous else ''))
        announce(car, washer, car['service_name'], previous)
    return len(moves)
