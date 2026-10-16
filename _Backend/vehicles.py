"""Vehicle identity, car models and completed visits - the one definition
shared by booking, History and Manager Analytics (analytics.py).

A vehicle is its plate number, ignoring case, spaces and dashes ('abc 1234'
and 'ABC-1234' are the same car) - not an account: walk-ins have none, and
one account can own several cars.

A visit is one vehicle on one calendar day with at least one finished
service ('ready' or 'completed'). Two services on the same day are one
visit; cancelled and no-show bookings are none."""
import re
from datetime import datetime
from statistics import median

from database import get_db_connection

MAX_MODEL_LENGTH = 60


def plate_key(plate):
    return re.sub(r'[^A-Z0-9]', '', (plate or '').upper())


def canonical_vehicle_model(cursor, typed):
    """What to store for a typed car model: the listed model it names
    ('vios' or 'toyota vios' -> 'Toyota Vios'), otherwise the text as typed
    with its spacing tidied, or None if nothing was entered."""
    text = ' '.join((typed or '').split())[:MAX_MODEL_LENGTH]
    if not text:
        return None
    listed = [row['model_name'] for row in cursor.execute("SELECT model_name FROM vehicle_models")]
    lowered = text.lower()
    for name in listed:
        if name.lower() == lowered:
            return name
    # The model without its make - only when exactly one listed model fits.
    by_short_name = [name for name in listed if name.lower().split(' ', 1)[-1] == lowered]
    return by_short_name[0] if len(by_short_name) == 1 else text


def vehicle_visits():
    """{plate key: {'days': [date, ...] oldest first, 'model': the most
    recently recorded model for that plate, or None}} for every vehicle
    with at least one visit."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT vehicle_plate_number, substr(completed_at, 1, 10) AS day, vehicle_model
        FROM reservations
        WHERE queue_status IN ('ready', 'completed')
          AND completed_at IS NOT NULL AND completed_at != ''
        ORDER BY completed_at
        """
    ).fetchall()
    conn.close()

    vehicles = {}
    for row in rows:
        key = plate_key(row['vehicle_plate_number'])
        try:
            day = datetime.strptime(row['day'], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            continue
        if not key:
            continue
        vehicle = vehicles.setdefault(key, {'days': [], 'model': None})
        if day not in vehicle['days']:
            vehicle['days'].append(day)
        if row['vehicle_model']:
            vehicle['model'] = row['vehicle_model']
    for vehicle in vehicles.values():
        vehicle['days'].sort()
    return vehicles


def return_gaps(days):
    """Days between each visit and the next."""
    return [(later - earlier).days for earlier, later in zip(days, days[1:])]


def visit_summary(vehicle):
    """What History shows for one vehicle. usual_return_days is the median
    gap between its visits - None until it has come back at least once."""
    if not vehicle:
        return {'visit_count': 0, 'last_visit': None, 'usual_return_days': None}
    gaps = return_gaps(vehicle['days'])
    return {
        'visit_count': len(vehicle['days']),
        'last_visit': vehicle['days'][-1].isoformat(),
        'usual_return_days': round(median(gaps), 1) if gaps else None,
    }
