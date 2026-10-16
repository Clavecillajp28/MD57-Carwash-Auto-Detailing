import re
import sqlite3
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request, session

import slot_hold
from assignment import announce, choose_washer
from database import get_db_connection
from data_scope import SOURCES, parse_source, source_sql
from extensions import ai_predictor
from wait_estimate import ahead_of_walk_ins, work_ahead_minutes
from service_estimates import ServiceEstimates
import scheduling
from scheduling import next_bookable_date
from vehicles import canonical_vehicle_model, plate_key, vehicle_visits, visit_summary
from utils import (
    role_required, log_activity, get_weather_reading, parse_booking_datetime, get_active_staff_count,
    is_staff_on_duty, is_within_business_hours, mask_plate, is_own_booking, now_str,
    create_notification, notify_role, SERVICE_CATEGORY_LABELS,
    is_date_staffed, ACTIVE_BOOKING_STATUSES,
    BOOKING_TIME_FORMAT, last_customer_booking_date, booking_window_message,
)

queue_api_bp = Blueprint('queue_api', __name__)


_TARGET_COLUMNS = (
    "reservation_id, user_id, customer_email, customer_name, vehicle_plate_number, "
    "booking_date, booking_time, assigned_bay, queue_status"
)


def _action_target(cursor, statuses):
    """The ONE reservation a queue action applies to, in one of `statuses`.

    Every page sends reservation_id. An older caller sending only
    plate_number gets that plate's most recent matching booking - never
    every booking that happens to share the plate, which is how acting by
    plate used to cancel/start/complete two different reservations at once.

    Returns (row, error_response): error_response is set when neither was
    sent (400) or nothing matches (404); otherwise row is the reservation."""
    data = request.get_json(silent=True) or {}
    reservation_id = data.get('reservation_id')
    plate_number = (data.get('plate_number') or '').strip().upper()
    if not reservation_id and not plate_number:
        return None, (jsonify({'status': 'error', 'message': 'Which booking? reservation_id is required.'}), 400)

    in_clause = ','.join('?' * len(statuses))
    if reservation_id:
        cursor.execute(
            f"SELECT {_TARGET_COLUMNS} FROM reservations WHERE reservation_id = ? AND queue_status IN ({in_clause})",
            (reservation_id, *statuses),
        )
    else:
        cursor.execute(
            f"""SELECT {_TARGET_COLUMNS} FROM reservations
                WHERE vehicle_plate_number = ? AND queue_status IN ({in_clause})
                ORDER BY reservation_id DESC LIMIT 1""",
            (plate_number, *statuses),
        )
    row = cursor.fetchone()
    if row is None:
        return None, (jsonify({'status': 'error', 'message': 'No matching booking found - it may already have moved on.'}), 404)
    return row, None


def _next_queue_number(cursor):
    cursor.execute('SELECT IFNULL(MAX(queue_number), 0) + 1 FROM reservations')
    return cursor.fetchone()[0]

@queue_api_bp.route('/api/services', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_get_services():
    """The real, bookable service catalog with real per-vehicle-size
    pricing - backs both the booking form's service/price picker and the
    Pricing page's tables, so the two can never drift out of sync (unlike
    the old setup, where Pricing displayed ~22 services with sizes/prices
    that had no corresponding bookable service at all). Only active
    services are returned - the 3 retired legacy services (Body Wash/
    Express & Foam/Full Body Wash & Detailing) stay out of new bookings but
    are never deleted (see database.py's migration comment)."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT type_name, multiplier_minutes FROM vehicle_types ORDER BY type_id")
    vehicle_types = [dict(row) for row in cursor.fetchall()]

    cursor.execute(
        """
        SELECT service_id, service_name, service_category, default_bay, base_duration_minutes
        FROM services
        WHERE is_active = 1
        ORDER BY service_category, service_id
        """
    )
    service_rows = cursor.fetchall()

    cursor.execute("SELECT service_id, vehicle_type, price FROM service_prices")
    prices_by_service = {}
    for row in cursor.fetchall():
        prices_by_service.setdefault(row['service_id'], {})[row['vehicle_type']] = row['price']

    # Suggestions for the booking forms' car model field.
    cursor.execute("SELECT model_name FROM vehicle_models ORDER BY model_name")
    vehicle_models = [row['model_name'] for row in cursor.fetchall()]

    conn.close()

    categories = {key: {'label': label, 'services': []} for key, label in SERVICE_CATEGORY_LABELS}
    for row in service_rows:
        prices = prices_by_service.get(row['service_id'], {})
        # A service whose price doesn't vary by vehicle size (e.g. Back
        # 2 Zero) is flagged so the frontend can render it in the
        # "Fixed Price - All Vehicle Types" style instead of a per-size
        # table column, same visual distinction the Pricing page has
        # always drawn between the two kinds of service.
        fixed_price = len(set(prices.values())) <= 1 if prices else False
        entry = {
            'service_id': row['service_id'],
            'name': row['service_name'],
            'duration_minutes': row['base_duration_minutes'],
            'default_bay': row['default_bay'],
            'prices': prices,
            'fixed_price': fixed_price,
        }
        bucket = categories.get(row['service_category'])
        if bucket:
            bucket['services'].append(entry)

    return jsonify({'status': 'success', 'vehicle_types': vehicle_types, 'vehicle_models': vehicle_models,
                    'categories': categories}), 200


@queue_api_bp.route('/api/get-queue', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_get_queue():
    """The live queue board. Staff/admin see everything, unmasked - they
    need real names/plates/emails to do the job. A customer sees their own
    booking in full, but every OTHER customer's row is masked (name,
    email, plate) - this is a shared "where am I in line" view, not a
    directory of everyone else's booking.

    Rows retain their original queue numbers. Starting work also respects
    protected appointments (slot_hold.py) and actual washer availability.
    Read-only timing estimates reserve booked slots and fit walk-ins into
    the remaining bay/staff time; they never reorder or rewrite bookings."""
    is_staff_or_admin = session.get('role') in ('admin', 'staff')
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        SELECT r.reservation_id AS id,
               r.user_id,
               r.vehicle_plate_number AS plate_number,
               r.vehicle_type,
               r.vehicle_model,
               s.service_name AS service_type,
               r.customer_email AS email,
               COALESCE(r.customer_name, u.fullname, u.username, '') AS customer_name,
               r.queue_number,
               r.booking_source,
               r.assigned_bay,
               r.booking_date,
               r.booking_time,
               r.arrived_at,
               r.started_at,
               r.assigned_staff_id AS washer_id,
               (s.base_duration_minutes + COALESCE(vt.multiplier_minutes, 0)) AS duration_minutes,
               s.base_duration_minutes AS base_minutes,
               r.queue_status AS status,
               COALESCE(su.fullname, su.username) AS washer
        FROM reservations r
        JOIN services s ON r.service_id = s.service_id
        LEFT JOIN users u ON r.user_id = u.user_id
        LEFT JOIN vehicle_types vt ON r.vehicle_type = vt.type_name
        LEFT JOIN users su ON r.assigned_staff_id = su.user_id
        WHERE r.queue_status IN ('waiting', 'in_progress', 'pending_confirmation')
        ORDER BY r.queue_number ASC, r.reservation_id ASC
    """
    cursor.execute(query)
    rows = cursor.fetchall()
    # For staff: which waiting cars a booked slot is currently holding the
    # bay against (see slot_hold.py) - the same check api_start_service
    # enforces, so the Start button can say why up front.
    now = slot_hold.current_time()
    protected = slot_hold.protected_bookings_today(cursor, now) if is_staff_or_admin else []
    today = now.strftime('%Y-%m-%d')
    # Each waiting walk-in's wait-time estimate (the Random Forest model, on
    # what is ahead of that car in its bay right now) - minutes until it's
    # done. Bookings show their booked time instead.
    items = [dict(row) for row in rows]
    ahead = ahead_of_walk_ins(items, now)
    estimates = {}
    if ahead:
        ids = list(ahead)
        base_minutes = {it['id']: it['base_minutes'] for it in items}
        try:
            predicted = ai_predictor.predict_many(
                [(ahead[i][0], base_minutes[i], ahead[i][1]) for i in ids],
                get_active_staff_count(), get_weather_reading()['rain_mm'], now.hour)
            estimates = dict(zip(ids, predicted))
        except Exception as e:
            print(f"[WARNING] queue wait estimates failed: {e}")
    live_estimates = ServiceEstimates(cursor, ai_predictor, get_weather_reading()['rain_mm'], now) if items else None
    reservations = []
    for item in items:
        item['hold_reason'] = None
        # Today's queue vs bookings for a later day (listed apart on the board).
        item['is_today'] = item['status'] != 'waiting' or not item['booking_date'] or item['booking_date'] <= today
        item['cars_ahead'] = ahead[item['id']][0] if item['id'] in ahead else None
        item['est_minutes'] = estimates.get(item['id'])
        item['estimate'] = live_estimates.for_job(item['id'])
        item['is_mine'] = not is_staff_or_admin and is_own_booking(item['user_id'], item['email'])
        # A held bay only matters to a car that could start now - not to a
        # booking for a later day.
        if protected and item['status'] == 'waiting' and item['is_today']:
            candidate = {'reservation_id': item['id'], 'assigned_bay': item['assigned_bay'],
                         'duration_minutes': item['duration_minutes']}
            holder = slot_hold.holding_booking(candidate, protected, now)
            if holder:
                item['hold_reason'] = slot_hold.hold_reason(candidate, holder, now)
        if not is_staff_or_admin:
            if not item['is_mine']:  # customers see their own booking in full
                if item.get('email') and '@' in item['email']:
                    parts = item['email'].split('@')
                    item['email'] = f"{parts[0][:2]}***@{parts[1]}"
                item['customer_name'] = 'Customer'
                item['plate_number'] = mask_plate(item.get('plate_number'))
        del item['user_id'], item['base_minutes']
        reservations.append(item)
    conn.close()
    return jsonify({'status': 'success', 'data': reservations}), 200


@queue_api_bp.route('/api/get-cancelled', methods=['GET'])
@role_required('admin', 'staff')
def api_get_cancelled():
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        SELECT r.reservation_id AS id,
               r.vehicle_plate_number AS plate_number,
               r.vehicle_type,
               s.service_name AS service_type,
               r.customer_email AS email,
               COALESCE(r.customer_name, u.fullname, u.username, '') AS customer_name,
               r.booking_source,
               r.assigned_bay,
               r.booking_date,
               r.booking_time,
               r.cancelled_at,
               r.cancel_reason,
               r.queue_status AS status
        FROM reservations r
        JOIN services s ON r.service_id = s.service_id
        LEFT JOIN users u ON r.user_id = u.user_id
        WHERE r.queue_status = 'cancelled'
        ORDER BY r.reservation_id DESC
    """
    cursor.execute(query)
    cancelled_reservations = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'status': 'success', 'data': cancelled_reservations}), 200


@queue_api_bp.route('/api/get-archived-cancellations', methods=['GET'])
@role_required('admin')
def api_get_archived_cancellations():
    """Admin-only audit view of cancelled bookings that have aged out of the
    live `reservations` table (see cleanup.py's
    archive_expired_cancelled_bookings) - the record is never actually
    deleted, just moved here once it's old enough that it no longer needs
    to clutter the live Cancelled page."""
    limit = min(max(request.args.get('limit', 200, type=int), 1), 1000)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT a.reservation_id AS id,
               a.vehicle_plate_number AS plate_number,
               a.vehicle_type,
               COALESCE(s.service_name, 'Unknown Service') AS service_type,
               a.customer_email AS email,
               COALESCE(a.customer_name, '') AS customer_name,
               a.booking_source,
               a.assigned_bay,
               a.booking_date,
               a.booking_time,
               a.cancelled_at,
               a.cancel_reason,
               a.archived_at
        FROM reservations_archive a
        LEFT JOIN services s ON a.service_id = s.service_id
        ORDER BY a.archived_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'status': 'success', 'data': rows}), 200


@queue_api_bp.route('/api/get-ready', methods=['GET'])
@role_required('admin', 'staff')
def api_get_ready():
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        SELECT r.reservation_id AS id,
               r.vehicle_plate_number AS plate_number,
               r.vehicle_type,
               r.vehicle_model,
               s.service_name AS service_type,
               r.customer_email AS email,
               COALESCE(r.customer_name, u.fullname, u.username, '') AS customer_name,
               r.booking_source,
               r.assigned_bay,
               r.booking_date,
               r.booking_time,
               r.completed_at,
               r.queue_status AS status,
               r.ready_notified_at,
               COALESCE(su.fullname, su.username) AS washer
        FROM reservations r
        JOIN services s ON r.service_id = s.service_id
        LEFT JOIN users u ON r.user_id = u.user_id
        LEFT JOIN users su ON r.assigned_staff_id = su.user_id
        WHERE r.queue_status = 'ready'
        ORDER BY r.reservation_id DESC
    """
    cursor.execute(query)
    ready_reservations = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'status': 'success', 'data': ready_reservations}), 200


@queue_api_bp.route('/api/get-history', methods=['GET'])
@role_required('admin', 'staff')
def api_get_history():
    """Every reservation that reached a final state - completed (archived
    from Ready) or cancelled - lands here automatically, so History is one
    chronological record of everything that's finished, not just completed
    jobs. Cancelled bookings stay actionable in /api/get-cancelled too
    (admin can still restore them from there); this is the read-only
    record of both outcomes together, newest first by whichever timestamp
    actually closed the booking out."""
    try:
        source = parse_source(request.args.get('source'))
    except ValueError as error:
        return jsonify(status='error', message=str(error)), 400
    if session.get('role') == 'staff' and source != 'real':
        return jsonify(status='error', message='Presentation history is available to managers only.'), 403
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        SELECT r.reservation_id AS id,
               r.vehicle_plate_number AS plate_number,
               r.vehicle_type,
               r.vehicle_model,
               s.service_name AS service_type,
               COALESCE(r.price, s.price) AS amount,
               r.customer_email AS email,
               COALESCE(r.customer_name, u.fullname, u.username, '') AS customer_name,
               r.booking_source,
               r.assigned_bay,
               r.booking_date,
               r.booking_time,
               r.completed_at,
               r.cancelled_at,
               r.ready_notified_at,
               r.picked_up_at,
               r.picked_up_by,
               COALESCE(NULLIF(TRIM(r.picked_up_by_name),''), pu.fullname, pu.username) AS picked_up_by_name,
               r.cancel_reason,
               r.queue_status AS status,
               r.is_synthetic,
               COALESCE(su.fullname, su.username) AS washer
        FROM reservations r
        JOIN services s ON r.service_id = s.service_id
        LEFT JOIN users u ON r.user_id = u.user_id
        LEFT JOIN users su ON r.assigned_staff_id = su.user_id
        LEFT JOIN users pu ON r.picked_up_by = pu.user_id
        WHERE r.queue_status IN ('completed', 'cancelled')
    """
    query += ' AND ' + source_sql(source, 'r')
    params = []
    if session.get('role') == 'staff':
        # A staff session only sees jobs *they* actually handled
        # (assigned_staff_id = their own user_id, set once in
        # api_start_service the moment they claim a job) - a coworker's
        # service history isn't theirs to see. A cancelled booking
        # nobody ever started has no assigned_staff_id and so belongs
        # to no individual staff member's history, only to admin's full
        # view. Admin keeps seeing every record, same as before.
        query += " AND r.assigned_staff_id = ?"
        params.append(session.get('user_id'))
    archive_query = query.replace('FROM reservations r', 'FROM reservations_archive r')
    archive_query += ' AND NOT EXISTS (SELECT 1 FROM reservations live WHERE live.reservation_id=r.reservation_id)'
    cursor.execute('SELECT * FROM (' + query + ' UNION ALL ' + archive_query +
                   ') ORDER BY COALESCE(picked_up_at, completed_at, cancelled_at) DESC, id DESC', params * 2)
    history_records = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # visit_count / last_visit / usual_return_days for each record's
    # vehicle, counted across every completed visit on file (see
    # vehicles.py) - not just the rows this session is shown, so a staff
    # member's own-jobs-only history still reports a car's real totals.
    vehicles = vehicle_visits(source=source)
    for record in history_records:
        record.update(visit_summary(vehicles.get(plate_key(record['plate_number']))))
    return jsonify({'status': 'success', 'data': history_records,
                    'source': {'key': source, 'label': SOURCES[source]}}), 200


@queue_api_bp.route('/api/available-times', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_available_times():
    """Which start times a service fits on ?date= - for ?service_id= and
    ?vehicle_type= (a new booking) or ?reservation_id= (moving one). Each
    time says why it isn't available ('busy' until when, 'booked', 'past',
    'closing'); next_available is the first one that is. No customer details."""
    date_str = request.args.get('date', '')
    if not parse_booking_datetime(date_str, '08:00 AM'):
        return jsonify({'status': 'error', 'message': 'Pick a valid date.'}), 400
    conn = get_db_connection()
    cursor = conn.cursor()
    exclude = None
    if request.args.get('reservation_id'):
        row = cursor.execute("SELECT reservation_id, user_id, customer_email, assigned_bay FROM reservations WHERE reservation_id = ?",
                             (request.args.get('reservation_id'),)).fetchone()
        if not row or (session.get('role') == 'customer' and not is_own_booking(row['user_id'], row['customer_email'])):
            conn.close()
            return jsonify({'status': 'error', 'message': 'Booking not found.'}), 404
        exclude, bay = row['reservation_id'], row['assigned_bay']
        minutes = scheduling.booking_minutes(cursor, exclude)
    else:
        service = cursor.execute("SELECT default_bay FROM services WHERE service_id = ?", (request.args.get('service_id'),)).fetchone()
        if not service:
            conn.close()
            return jsonify({'status': 'error', 'message': 'Pick a service first.'}), 400
        bay = service['default_bay']
        minutes = scheduling.service_minutes(cursor, request.args.get('service_id'), request.args.get('vehicle_type'))
    times = scheduling.day_times(cursor, date_str, minutes, bay, exclude)
    busy = scheduling.bay_busy_until(cursor, bay, exclude_reservation_id=exclude) if date_str == datetime.now().strftime('%Y-%m-%d') else None
    conn.close()
    return jsonify({
        'status': 'success', 'bay': bay, 'minutes': minutes, 'times': times,
        'next_available': next((t['time'] for t in times if t['available']), None),
        'busy_until': busy.strftime(BOOKING_TIME_FORMAT) if busy else None,
    }), 200


@queue_api_bp.route('/api/bay-availability', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_bay_availability():
    """The homepage calendar: the days that already have bookings (from
    today on), and with ?date=, how many of the bays are free at each start
    time for a typical 30-minute service. No customer details."""
    conn = get_db_connection()
    cursor = conn.cursor()
    booked_dates = [r[0] for r in cursor.execute(
        f"""SELECT DISTINCT booking_date FROM reservations WHERE booking_date >= ? AND booking_time IS NOT NULL
            AND queue_status IN ({','.join('?' * len(ACTIVE_BOOKING_STATUSES))}) ORDER BY booking_date""",
        (datetime.now().strftime('%Y-%m-%d'), *ACTIVE_BOOKING_STATUSES),
    )]
    result = {'status': 'success', 'booked_dates': booked_dates}
    date_str = request.args.get('date')
    if date_str and parse_booking_datetime(date_str, '08:00 AM'):
        bays = [r[0] for r in cursor.execute("SELECT DISTINCT default_bay FROM services WHERE is_active = 1 ORDER BY default_bay")]
        per_bay = {bay: scheduling.day_times(cursor, date_str, 30, bay) for bay in bays}
        result['bays'] = len(bays)
        result['times'] = [{'time': entry['time'],
                            'free_bays': sum(1 for bay in bays if per_bay[bay][i]['available']),
                            'past': entry.get('reason') == 'past'}
                           for i, entry in enumerate(per_bay[bays[0]])] if bays else []
    conn.close()
    return jsonify(result), 200


@queue_api_bp.route('/api/my-bookings', methods=['GET'])
@role_required('customer')
def api_my_bookings():
    # The WHERE below is utils.is_own_booking's ownership rule, as SQL.
    user_id = session.get('user_id')
    user_email = session.get('user')
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        SELECT r.reservation_id AS id,
               r.vehicle_plate_number AS plate_number,
               r.vehicle_type,
               r.vehicle_model,
               s.service_name AS service_type,
               r.service_id,
               r.queue_number,
               r.booking_date,
               r.booking_time,
               r.arrived_at,
               r.assigned_bay,
               r.queue_status AS status,
               r.cancelled_at,
               r.cancel_reason,
               r.completed_at,
               r.rescheduled_from,
               r.picked_up_at,
               r.reschedule_reason,
               COALESCE(r.price, s.price) AS price,
               CASE WHEN f.feedback_id IS NOT NULL THEN 1 ELSE 0 END AS has_feedback
        FROM reservations r
        JOIN services s ON r.service_id = s.service_id
        LEFT JOIN feedbacks f ON f.reservation_id = r.reservation_id
        WHERE r.user_id = ? OR (r.user_id IS NULL AND LOWER(r.customer_email) = LOWER(?))
        ORDER BY r.reservation_id DESC
    """
    cursor.execute(query, (user_id, user_email))
    bookings = [dict(row) for row in cursor.fetchall()]
    active = [b for b in bookings if b['status'] in ACTIVE_BOOKING_STATUSES]
    if active:
        estimates = ServiceEstimates(cursor, ai_predictor, get_weather_reading()['rain_mm'], slot_hold.current_time())
        for booking in active:
            booking['estimate'] = estimates.for_job(booking['id'])
    conn.close()
    return jsonify({'status': 'success', 'data': bookings}), 200


@queue_api_bp.route('/api/reschedule-booking', methods=['POST'])
@role_required('customer')
def api_reschedule_booking():
    """A customer moves their own still-waiting scheduled booking to another
    date/time (My Bookings). Same rules as a new booking: a future time
    within business hours, a day with staff to serve them (see
    is_date_staffed) and the bay free for the whole service (scheduling.py). The booking
    keeps its place in the queue and its price."""
    data = request.get_json(silent=True) or {}
    new_date = (data.get('booking_date') or '').strip()
    new_time = (data.get('booking_time') or '').strip()

    scheduled_dt = parse_booking_datetime(new_date, new_time)
    if scheduled_dt is None:
        return jsonify({'status': 'error', 'message': 'Please pick a valid date and time.'}), 400
    if scheduled_dt < datetime.now():
        return jsonify({'status': 'error', 'message': 'That time has already passed.'}), 400
    if not is_within_business_hours(scheduled_dt):
        return jsonify({'status': 'error', 'message': 'MD57 is open 8:00 AM - 9:00 PM. Please choose a time within business hours.'}), 400
    new_time = scheduled_dt.strftime(BOOKING_TIME_FORMAT)
    new_date = scheduled_dt.strftime('%Y-%m-%d')
    last_date = last_customer_booking_date()
    if last_date and new_date > last_date:
        return jsonify({'status': 'error', 'message': booking_window_message()}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    target, error = _action_target(cursor, ('waiting',))
    if error:
        conn.close()
        return error
    if not is_own_booking(target['user_id'], target['customer_email']):
        conn.close()
        return jsonify({'status': 'error', 'message': 'You can only reschedule your own bookings.'}), 403
    if not target['booking_date']:
        conn.close()
        return jsonify({'status': 'error', 'message': "A walk-in booking has no scheduled time to move."}), 400

    if not is_date_staffed(new_date):
        conn.close()
        suggestion = next_bookable_date(new_date, new_time, target['assigned_bay'], target['reservation_id'])
        if suggestion and last_date and suggestion > last_date:
            suggestion = None
        message = f"No staff are scheduled on {new_date}."
        message += f" The next day with staff and room at {new_time} is {suggestion}." if suggestion else " Please try another day."
        return jsonify({'status': 'error', 'message': message, 'suggested_date': suggestion}), 400
    minutes = scheduling.booking_minutes(cursor, target['reservation_id'])
    problem = scheduling.check_time(cursor, new_date, new_time, minutes, target['assigned_bay'], target['reservation_id'])
    if problem:
        suggestion = scheduling.next_available_time(cursor, new_date, minutes, target['assigned_bay'], target['reservation_id'], after=new_time)
        conn.close()
        message = scheduling.describe_problem(problem, target['assigned_bay'])
        message += f" The next free time that day is {suggestion}." if suggestion else " There's no free time left that day."
        return jsonify({'status': 'error', 'message': message, 'next_available': suggestion}), 400

    old_slot = f"{target['booking_date']} {target['booking_time']}"
    cursor.execute(
        """
        UPDATE reservations
        SET booking_date = ?, booking_time = ?, rescheduled_from = ?, reschedule_reason = 'customer',
            arrived_at = NULL
        WHERE reservation_id = ? AND queue_status = 'waiting'
        """,
        (new_date, new_time, old_slot, target['reservation_id']),
    )
    conn.commit()
    conn.close()

    plate_number = target['vehicle_plate_number']
    log_activity(session.get('user', 'Customer'), f'Rescheduled booking for plate {plate_number} from {old_slot} to {new_date} {new_time}')
    for role in ('admin', 'staff'):
        notify_role(role, 'Booking Rescheduled', f'{plate_number} moved from {old_slot} to {new_date} {new_time} by the customer.',
                    'booking_rescheduled', target['reservation_id'])
    return jsonify({'status': 'success', 'message': f'Your booking is now on {new_date} at {new_time}.'}), 200


@queue_api_bp.route('/api/cancel-booking', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def api_cancel_booking():
    user_role = session.get('role')
    user_id = session.get('user_id')

    conn = get_db_connection()
    cursor = conn.cursor()
    target, error = _action_target(cursor, ('waiting', 'in_progress'))
    if error:
        conn.close()
        return error

    # A customer can only cancel their own booking.
    if user_role == 'customer' and not is_own_booking(target['user_id'], target['customer_email']):
        conn.close()
        return jsonify({'status': 'error', 'message': 'Unauthorized: You can only cancel your own bookings.'}), 403

    plate_number = target['vehicle_plate_number']
    cancel_reason = 'customer' if user_role == 'customer' else 'staff'
    cursor.execute(
        """
        UPDATE reservations
        SET queue_status = 'cancelled', cancelled_at = ?, cancel_reason = ?
        WHERE reservation_id = ? AND queue_status IN ('waiting', 'in_progress')
        """,
        (now_str(), cancel_reason, target['reservation_id']),
    )
    conn.commit()
    conn.close()

    log_activity(session.get('user', 'System'), f'Cancelled booking for plate: {plate_number}')

    # In-app notifications - fire-and-forget, never allowed to fail the
    # cancellation itself.
    reservation_id = target['reservation_id']
    owner_user_id = target['user_id']
    if owner_user_id and owner_user_id != user_id:
        create_notification(
            owner_user_id, 'Booking Cancelled',
            f'Your booking for vehicle {plate_number} has been cancelled.',
            'booking_cancelled', reservation_id,
        )
    notify_role(
        'admin', 'Booking Cancelled',
        f'Booking for {plate_number} was cancelled ({cancel_reason}).',
        'booking_cancelled', reservation_id, exclude_user_id=user_id,
    )
    if user_role != 'staff':
        notify_role(
            'staff', 'Booking Cancelled',
            f'Booking for {plate_number} was cancelled and is no longer in the queue.',
            'booking_cancelled', reservation_id, exclude_user_id=user_id,
        )

    return jsonify({'status': 'success', 'message': 'Booking cancelled successfully!'}), 200


@queue_api_bp.route('/api/restore-booking', methods=['POST'])
@role_required('admin')
def api_restore_booking():
    conn = get_db_connection()
    cursor = conn.cursor()
    target, error = _action_target(cursor, ('cancelled',))
    if error:
        conn.close()
        return error

    # A booking whose scheduled time has already passed rejoins the live
    # queue as of now, with its old slot kept in rescheduled_from. Left on
    # its past date/time, the no-show sweep would cancel it again within a
    # minute (and email the customer a second "slot released" notice).
    scheduled_dt = parse_booking_datetime(target['booking_date'], target['booking_time'])
    slot_passed = scheduled_dt is not None and scheduled_dt < datetime.now()

    next_queue_number = _next_queue_number(cursor)
    cursor.execute("""
        UPDATE reservations
        SET queue_status = 'waiting', cancelled_at = NULL, cancel_reason = NULL, queue_number = ?
        WHERE reservation_id = ? AND queue_status = 'cancelled'
    """, (next_queue_number, target['reservation_id']))
    if slot_passed:
        cursor.execute("""
            UPDATE reservations
            SET booking_date = NULL, booking_time = NULL, rescheduled_from = ?, reschedule_reason = 'restored'
            WHERE reservation_id = ?
        """, (f"{target['booking_date']} {target['booking_time']}", target['reservation_id']))
    conn.commit()
    conn.close()

    plate_number = target['vehicle_plate_number']
    log_activity(session.get('user', 'Staff'), f'Restored booking for plate: {plate_number} to queue #{next_queue_number}')
    if target['user_id']:
        create_notification(
            target['user_id'], 'Booking Restored',
            f'Your booking for vehicle {plate_number} is back in the queue - queue #{next_queue_number}.',
            'booking_confirmed', target['reservation_id'],
        )
    return jsonify({'status': 'success', 'message': f'Booking for {plate_number} restored to active queue!'}), 200


@queue_api_bp.route('/api/start-service', methods=['POST'])
@role_required('admin', 'staff')
def api_start_service():
    # A staff account could otherwise start (and get attributed on) a job
    # without ever actually being clocked in by admin.
    if session.get('role') == 'staff' and not is_staff_on_duty(session.get('user_id')):
        return jsonify({
            'status': 'error',
            'message': 'You are not clocked in today. Ask admin to record your attendance before starting a service.'
        }), 403

    conn = None
    try:
        # BEGIN IMMEDIATE closes the same race window api_submit_booking's
        # own transaction closes: two near-simultaneous "Start" clicks for
        # two different waiting bookings in the same bay could otherwise
        # both pass the bay-occupancy SELECT below before either UPDATE
        # commits, starting two cars in one bay at once.
        conn = get_db_connection()
        conn.isolation_level = None
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        target, error = _action_target(cursor, ('waiting',))
        if error:
            cursor.execute("ROLLBACK")
            conn.close()
            return error
        plate_number = target['vehicle_plate_number']

        # A scheduled booking (walk-ins have no booking_date) can only be
        # started on the day it's actually scheduled for.
        if target['booking_date'] and target['booking_date'] != datetime.now().strftime('%Y-%m-%d'):
            cursor.execute("ROLLBACK")
            conn.close()
            return jsonify({
                'status': 'error',
                'message': f"This booking is scheduled for {target['booking_date']}, not today. It can only be started on its scheduled day."
            }), 400

        # BAY OCCUPANCY - only one vehicle actually being worked (or done
        # and awaiting admin's sign-off) per bay at a time. This is where
        # "wait your turn for the bay" is enforced - a car whose bay is busy
        # simply stays 'waiting'. 'ready' doesn't count as occupying - a
        # finished car is treated as already moved out of the bay for pickup.
        cursor.execute(
            """
            SELECT vehicle_plate_number FROM reservations
            WHERE assigned_bay = ? AND queue_status IN ('in_progress', 'pending_confirmation')
              AND reservation_id != ?
            LIMIT 1
            """,
            (target['assigned_bay'], target['reservation_id']),
        )
        occupying = cursor.fetchone()
        if occupying:
            cursor.execute("ROLLBACK")
            conn.close()
            return jsonify({
                'status': 'error',
                'message': f"{target['assigned_bay']} is currently occupied by {occupying['vehicle_plate_number']}. "
                           f"{plate_number} will stay in the queue until that bay is free."
            }), 409

        # BOOKED-SLOT HOLD - a customer who booked a time from their own
        # account finds the bay free at that time: nothing else starts in
        # it unless it will be done by then (see slot_hold.py). Admin can
        # override, e.g. when the customer has called to say they're late.
        cursor.execute(
            """
            SELECT s.base_duration_minutes + COALESCE(vt.multiplier_minutes, 0)
            FROM reservations r
            JOIN services s ON r.service_id = s.service_id
            LEFT JOIN vehicle_types vt ON r.vehicle_type = vt.type_name
            WHERE r.reservation_id = ?
            """,
            (target['reservation_id'],),
        )
        duration_row = cursor.fetchone()
        candidate = {'reservation_id': target['reservation_id'], 'assigned_bay': target['assigned_bay'],
                     'duration_minutes': duration_row[0] if duration_row else 0}
        now = slot_hold.current_time()
        holder = slot_hold.holding_booking(candidate, slot_hold.protected_bookings_today(cursor, now), now)
        data = request.get_json(silent=True) or {}
        overridden = bool(holder) and session.get('role') == 'admin' and bool(data.get('override_hold'))
        if holder and not overridden:
            cursor.execute("ROLLBACK")
            conn.close()
            return jsonify({
                'status': 'error',
                'message': f"{slot_hold.hold_reason(candidate, holder, now)} {plate_number} stays in the queue until then.",
                'held_for': holder['vehicle_plate_number'],
                'can_override': session.get('role') == 'admin',
            }), 409

        # Who washes it is decided by the system (assignment.py - automated
        # staff allocation): the top-ranked free washer, the same one the
        # queue page shows before Start. The WHERE clause means a re-click
        # can't reassign it.
        washer = choose_washer(cursor)
        if washer is None:
            conn.rollback()
            conn.close()
            return jsonify({'status': 'error', 'message': 'No free washer is clocked in. This vehicle stays waiting until a washer is available.'}), 409
        cursor.execute("""
            UPDATE reservations
            SET queue_status = 'in_progress', assigned_staff_id = ?, started_at = ?, checklist_required=1
            WHERE reservation_id = ? AND queue_status = 'waiting'
        """, (washer['user_id'], now_str(), target['reservation_id']))
        details = cursor.execute(
            "SELECT s.service_name FROM reservations r JOIN services s ON s.service_id = r.service_id WHERE r.reservation_id = ?",
            (target['reservation_id'],),
        ).fetchone()

        conn.commit()
        conn.close()

        log_activity(session.get('user', 'Staff'), f"Started service for plate: {plate_number} (assigned to {washer['name']})")
        if overridden:
            log_activity(session.get('user', 'Admin'),
                         f"Overrode the bay hold for {holder['vehicle_plate_number']}'s {holder['booking_time']} booking "
                         f"to start {plate_number}")
        announce(target, washer, details['service_name'] if details else 'service')
        return jsonify({
            'status': 'success',
            'message': f"{plate_number} started - assigned to {washer['name']}.",
            'assigned_to': {'user_id': washer['user_id'], 'name': washer['name']},
        }), 200
    except Exception:
        if conn is not None:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            conn.close()
        raise  # app.py's 500 handler answers (as JSON for /api/) and Flask logs the traceback


@queue_api_bp.route('/api/mark-arrived', methods=['POST'])
@role_required('admin', 'staff')
def api_mark_arrived():
    """Staff check a scheduled customer in when they turn up (the
    "Customer Has Arrived" button). From then on their bay stays held for them past
    the booked time (see slot_hold.py), and the no-show sweep leaves the
    booking alone - they're here, just waiting for the bay. Sending
    arrived: false undoes a mistaken check-in."""
    arrived = (request.get_json(silent=True) or {}).get('arrived', True)
    conn = get_db_connection()
    cursor = conn.cursor()
    target, error = _action_target(cursor, ('waiting',))
    if error:
        conn.close()
        return error
    if not target['booking_date']:
        conn.close()
        return jsonify({'status': 'error', 'message': 'A walk-in is already here - there is nothing to check in.'}), 400
    if target['booking_date'] != datetime.now().strftime('%Y-%m-%d'):
        conn.close()
        return jsonify({
            'status': 'error',
            'message': f"This booking is for {target['booking_date']} - it can only be checked in on that day."
        }), 400

    cursor.execute(
        "UPDATE reservations SET arrived_at = ? WHERE reservation_id = ? AND queue_status = 'waiting'",
        (now_str() if arrived else None, target['reservation_id']),
    )
    conn.commit()
    conn.close()

    plate_number = target['vehicle_plate_number']
    if not arrived:
        log_activity(session.get('user', 'Staff'), f'Undid check-in for plate: {plate_number}')
        return jsonify({'status': 'success', 'message': f'Check-in undone for {plate_number}.'}), 200

    log_activity(session.get('user', 'Staff'), f'Checked in plate: {plate_number} ({target["booking_time"]} booking)')
    if target['user_id']:
        create_notification(
            target['user_id'], 'Checked In',
            f"You're checked in for your {target['booking_time']} booking ({plate_number}).",
            'queue_update', target['reservation_id'],
        )
    return jsonify({'status': 'success', 'message': f'{plate_number} checked in.'}), 200


@queue_api_bp.route('/api/complete-service', methods=['POST'])
@role_required('admin', 'staff')
def api_complete_service():
    """Staff marking a job done doesn't go straight to Ready - it lands in
    'pending_confirmation' until admin signs off (see /api/confirm-complete
    and /api/reject-complete), so a customer can't be notified their car is
    ready on staff's say-so alone. Admin completing a job directly (e.g.
    covering a shift) still goes straight to 'ready'."""
    conn = get_db_connection()
    cursor = conn.cursor()
    target, error = _action_target(cursor, ('in_progress',))
    if error:
        conn.close()
        return error

    plate_number = target['vehicle_plate_number']
    new_status = 'ready' if session.get('role') == 'admin' else 'pending_confirmation'
    from blueprints.quality_api import readiness
    conn.execute('BEGIN IMMEDIATE')
    target=conn.execute('SELECT * FROM reservations WHERE reservation_id=?',(target['reservation_id'],)).fetchone()
    error=readiness(conn,target,manager=session.get('role')=='admin')
    if error:
        conn.rollback(); conn.close(); return error
    cursor.execute(
        """
        UPDATE reservations
        SET queue_status = ?, completed_at = ?
        WHERE reservation_id = ? AND queue_status = 'in_progress' AND started_at IS NOT NULL
        """,
        (new_status, now_str(), target['reservation_id']),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        return jsonify({'status': 'error', 'message': 'Start this service before marking it complete, or refresh its current status.'}), 409
    conn.commit()
    conn.close()

    if new_status == 'ready':
        log_activity(session.get('user', 'Staff'), f'Marked service as completed for plate: {plate_number}')
        if target['user_id']:
            create_notification(
                target['user_id'], 'Appointment Ready',
                f'Your vehicle {plate_number} is ready for pickup!',
                'ready', target['reservation_id'],
            )
        return jsonify({'status': 'success', 'message': 'Service marked as completed!'}), 200

    log_activity(session.get('user', 'Staff'), f'Marked service done, awaiting admin confirmation, for plate: {plate_number}')
    notify_role(
        'admin', 'Booking Awaiting Confirmation',
        f'{plate_number} was marked done by staff - confirm to notify the customer.',
        'queue_update', target['reservation_id'],
    )
    return jsonify({'status': 'success', 'message': 'Marked done - waiting for admin to confirm before the customer is notified.'}), 200


@queue_api_bp.route('/api/confirm-complete', methods=['POST'])
@role_required('admin')
def api_confirm_complete():
    """Admin signs off on a job staff marked done, moving it into the real
    Ready-for-pickup list. completed_at is left as-is - it already holds
    the real moment staff finished the wash, which is what Speed metrics
    and MAE tracking should measure."""
    conn = get_db_connection()
    cursor = conn.cursor()
    target, error = _action_target(cursor, ('pending_confirmation',))
    if error:
        conn.close()
        return error
    from blueprints.quality_api import readiness
    conn.execute('BEGIN IMMEDIATE')
    target=conn.execute('SELECT * FROM reservations WHERE reservation_id=?',(target['reservation_id'],)).fetchone()
    error=readiness(conn,target,manager=True)
    if error:
        conn.rollback(); conn.close(); return error
    cursor.execute(
        "UPDATE reservations SET queue_status = 'ready' WHERE reservation_id = ? AND queue_status = 'pending_confirmation'",
        (target['reservation_id'],),
    )
    if cursor.rowcount!=1:
        conn.rollback(); conn.close()
        return jsonify(status='error',message='This service changed. Refresh the queue.'),409
    conn.commit()
    conn.close()

    plate_number = target['vehicle_plate_number']
    log_activity(session.get('user', 'Admin'), f'Confirmed service completion for plate: {plate_number}')
    if target['user_id']:
        create_notification(
            target['user_id'], 'Appointment Ready',
            f'Your vehicle {plate_number} is ready for pickup!',
            'ready', target['reservation_id'],
        )
    return jsonify({'status': 'success', 'message': f'Confirmed - {plate_number} is now ready for pickup.'}), 200


@queue_api_bp.route('/api/reject-complete', methods=['POST'])
@role_required('admin')
def api_reject_complete():
    """Admin sends a job staff marked done back to in_progress - e.g. it
    wasn't actually finished. Clears completed_at; started_at and
    assigned_staff_id are untouched so the same staff member picks up
    where they left off."""
    conn = get_db_connection()
    cursor = conn.cursor()
    target, error = _action_target(cursor, ('pending_confirmation',))
    if error:
        conn.close()
        return error
    cursor.execute(
        """
        UPDATE reservations
        SET queue_status = 'in_progress', completed_at = NULL
        WHERE reservation_id = ? AND queue_status = 'pending_confirmation'
        """,
        (target['reservation_id'],),
    )
    conn.commit()
    conn.close()

    plate_number = target['vehicle_plate_number']
    log_activity(session.get('user', 'Admin'), f'Sent service back to in-progress for plate: {plate_number}')
    return jsonify({'status': 'success', 'message': f'{plate_number} sent back to in-progress.'}), 200


@queue_api_bp.route('/api/confirm-pickup', methods=['POST'])
@queue_api_bp.route('/api/archive-ready', methods=['POST'])
@role_required('admin')
def api_archive_ready():
    """Confirm physical collection. archive-ready is a compatibility alias.

    The conditional update makes collection a single, auditable transition.
    completed_at remains the wash finish used by analytics and retraining.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    target, error = _action_target(cursor, ('ready',))
    if error:
        conn.close()
        return error
    picked_up_at = now_str()
    picked_up_by = session.get('user_id')
    picked_up_by_name = session.get('fullname') or session.get('user') or 'Admin'
    cursor.execute("""UPDATE reservations SET queue_status='completed', picked_up_at=?,
        picked_up_by=?,picked_up_by_name=? WHERE reservation_id=? AND queue_status='ready'
        AND picked_up_at IS NULL""",
        (picked_up_at, picked_up_by, picked_up_by_name, target['reservation_id']))
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        return jsonify({'status': 'error', 'message': 'This vehicle has already moved on. Refresh the Ready list.'}), 409
    conn.commit()
    conn.close()

    plate_number = target['vehicle_plate_number']
    log_activity(session.get('user', 'Admin'), f'Confirmed pickup for booking MD57-{target["reservation_id"]}: {plate_number} at {picked_up_at}')
    if target['user_id']:
        create_notification(
            target['user_id'], 'Vehicle Collected',
            f'Your vehicle {plate_number} has been collected. Thanks for choosing MD57!',
            'service_completed', target['reservation_id'],
        )
    return jsonify({'status': 'success', 'message': f'Pickup confirmed for {plate_number}.',
                    'picked_up_at': picked_up_at, 'picked_up_by': picked_up_by,
                    'picked_up_by_name': picked_up_by_name}), 200


@queue_api_bp.route('/api/submit-booking', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def api_submit_booking():
    conn = None
    try:
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form

        customer_name = data.get('customer_name') or data.get('name')
        plate_number = data.get('plate_number')
        vehicle_type = data.get('vehicle_type', 'Medium')
        typed_vehicle_model = data.get('vehicle_model')
        # A real catalog service (see /api/services) - its name, bay and
        # price are all read from the database below, never trusted from
        # the client.
        requested_service_id = data.get('service_id')
        customer_email = data.get('email') or data.get('customer_email')
        booking_source = data.get('booking_source', 'Online Booking')
        booking_date = data.get('booking_date')
        booking_time = data.get('booking_time')

        if not customer_name:
            customer_name = session.get('fullname') or session.get('user')

        # 'Online Booking' means one thing only: a customer booked it from
        # their own account - it's what earns the booked-slot hold (see
        # slot_hold.py), so it comes from the session's role, never from
        # the request.
        if session.get('role') == 'customer':
            booking_source = slot_hold.PROTECTED_BOOKING_SOURCE
        elif booking_source == slot_hold.PROTECTED_BOOKING_SOURCE:
            booking_source = 'Admin Booking' if session.get('role') == 'admin' else 'Staff Booking'

        if not plate_number:
            return jsonify({'status': 'error', 'message': 'Plate number is required!'}), 400

        plate_number = plate_number.strip().upper()

        # Deliberately loose - real plate formats vary a lot (old 3+3,
        # new 3+4, motorcycle, conduction/temporary, personalized) and a
        # strict format regex risks rejecting a real customer's plate.
        # This only catches the "---"/single-character garbage kind of
        # input, not anything shaped like an actual plate.
        if len(re.findall(r'[A-Z0-9]', plate_number)) < 2:
            return jsonify({'status': 'error', 'message': 'Plate number looks invalid - it needs at least 2 letters/digits.'}), 400

        if not requested_service_id:
            return jsonify({'status': 'error', 'message': 'Please pick a service from the current list.'}), 400

        # The booking calendar UI hides past dates/times, but that's a
        # client-side convenience only - a direct API call could otherwise
        # book any date in the past. Walk-ins (no booking_date/booking_time)
        # skip this check entirely, same as they skip the slot-conflict
        # check below.
        if bool(booking_date) != bool(booking_time):
            return jsonify({'status': 'error', 'message': 'A scheduled booking needs both a date and a time.'}), 400

        if booking_date and booking_time:
            scheduled_dt = parse_booking_datetime(booking_date, booking_time)
            if scheduled_dt is None:
                return jsonify({'status': 'error', 'message': 'Invalid booking date/time format.'}), 400
            if scheduled_dt < datetime.now():
                return jsonify({'status': 'error', 'message': 'Booking date/time cannot be in the past.'}), 400
            if not is_within_business_hours(scheduled_dt):
                return jsonify({'status': 'error', 'message': 'MD57 is open 8:00 AM - 9:00 PM. Please choose a time within business hours.'}), 400
            # Stored in one format, so a slot's bookings are all counted
            # together (see slot_has_room) however the time was sent.
            booking_time = scheduled_dt.strftime(BOOKING_TIME_FORMAT)
            booking_date = scheduled_dt.strftime('%Y-%m-%d')
            last_date = last_customer_booking_date()
            if session.get('role') == 'customer' and last_date and booking_date > last_date:
                return jsonify({'status': 'error', 'message': booking_window_message()}), 400
        else:
            # Walk-ins join the live queue immediately, so "now" itself has
            # to be within business hours - there's no scheduled time to
            # check instead.
            if not is_within_business_hours(datetime.now()):
                return jsonify({'status': 'error', 'message': 'MD57 is open 8:00 AM - 9:00 PM. Walk-ins can only be added during business hours.'}), 400

        conn = get_db_connection()
        # Manual transaction control: BEGIN IMMEDIATE grabs the write lock
        # up front, so a second concurrent submit-booking request blocks
        # here instead of interleaving with this one between reading the
        # next queue_number and the INSERT below - otherwise two
        # simultaneous requests could both land on the same queue_number.
        conn.isolation_level = None
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")

        # vehicle_type must be one of the configured vehicle_types rows -
        # anything else would get no size minutes and understate the time.
        cursor.execute("SELECT type_name FROM vehicle_types WHERE LOWER(type_name) = LOWER(?)", (vehicle_type,))
        vt_row = cursor.fetchone()
        if not vt_row:
            cursor.execute("SELECT type_name FROM vehicle_types ORDER BY type_name")
            valid_types = [r['type_name'] for r in cursor.fetchall()]
            conn.execute("ROLLBACK")
            conn.close()
            return jsonify({
                'status': 'error',
                'message': f"Invalid vehicle type '{vehicle_type}'. Must be one of: {', '.join(valid_types)}."
            }), 400
        vehicle_type = vt_row['type_name']

        # NOTE ON BAY/TIME "CONFLICTS": this system is a queue (strict
        # first-come-first-served by queue_number - see api_get_queue's own
        # docstring), not an appointment calendar with exclusive slots. Two
        # customers wanting the same service around the same time is normal
        # real-world demand, not an error - the second one simply queues
        # behind the first for that bay and is served once it's free (see
        # api_start_service's bay-occupancy check, which is where
        # "only one car per bay at a time" is actually enforced). This used
        # to flatly reject a second booking that matched an existing one's
        # exact assigned_bay/booking_date/booking_time with a 400 error -
        # confusing for a customer who just wanted to book the same service
        # a few minutes apart, and wrong for the underlying queue model:
        # booking_date/booking_time are each customer's own expected
        # arrival estimate, not a reservation of exclusive bay time.

        # Resolve the service and its price. Must be an active service with
        # a real service_prices row for the requested vehicle_type - a
        # missing row means an invalid combination (e.g. a motorcycle-only
        # service booked with a car size), rejected outright rather than
        # silently booked at no price.
        cursor.execute(
            "SELECT service_id, service_name, default_bay, base_duration_minutes FROM services WHERE service_id = ? AND is_active = 1",
            (requested_service_id,),
        )
        serv_row = cursor.fetchone()
        if not serv_row:
            conn.execute("ROLLBACK")
            conn.close()
            return jsonify({'status': 'error', 'message': 'That service is not available. Please pick a service from the current list.'}), 400
        service_id = serv_row['service_id']
        service_name = serv_row['service_name']
        # The service's own real bay assignment wins over whatever the
        # client sent - read from the record, not trusted from the request.
        assigned_bay = serv_row['default_bay']

        cursor.execute(
            "SELECT price FROM service_prices WHERE service_id = ? AND vehicle_type = ?",
            (service_id, vehicle_type),
        )
        price_row = cursor.fetchone()
        if not price_row:
            conn.execute("ROLLBACK")
            conn.close()
            return jsonify({
                'status': 'error',
                'message': f"'{service_name}' is not available for vehicle size '{vehicle_type}'. Please choose a matching vehicle size."
            }), 400
        booking_price = price_row['price']

        # A customer can only book a day someone can actually serve them -
        # staff on the shift roster (and not marked unavailable), or, for
        # today, already clocked in (see is_date_staffed). Today with no
        # staff is refused outright. A future day with no staff gets the
        # next staffed day with room in this bay at the same time proposed
        # instead: nothing is saved, and the booking page asks the customer
        # to accept the new date (it just resubmits with it) or cancel.
        # Staff and admin booking on a customer's behalf aren't restricted.
        rejection = None
        if session.get('role') == 'customer':
            today_str = datetime.now().strftime('%Y-%m-%d')
            target_date = booking_date or today_str
            if not is_date_staffed(target_date):
                if target_date == today_str:
                    rejection = (jsonify({
                        'status': 'error',
                        'message': "No staff are on duty or scheduled today, so today's slots can't be booked. Please choose another date.",
                    }), 400)
                else:
                    proposed_date = next_bookable_date(target_date, booking_time, assigned_bay,
                                                       minutes=scheduling.service_minutes(cursor, service_id, vehicle_type))
                    last_date = last_customer_booking_date()
                    if proposed_date and last_date and proposed_date > last_date:
                        proposed_date = None  # the next staffed day is further out than customers may book
                    if proposed_date is None:
                        rejection = (jsonify({
                            'status': 'error',
                            'message': f"No staff are scheduled on {target_date} or any later day yet. Please try again once the shop has published its schedule.",
                        }), 400)
                    else:
                        rejection = (jsonify({
                            'status': 'reschedule_required',
                            'message': f"No staff are scheduled on {target_date}. Your booking can be moved to {proposed_date} at {booking_time} instead.",
                            'requested_date': target_date,
                            'proposed_date': proposed_date,
                        }), 200)  # not an error - an answer the customer confirms

        # The bay has to be free for the whole service at that time - no
        # other booking overlapping it, and for today, nothing still being
        # washed or waiting ahead (scheduling.py). Walk-ins are exempt -
        # they're already here. Checked inside this BEGIN IMMEDIATE
        # transaction, so two simultaneous requests can't take one time.
        if rejection is None and booking_date and booking_time:
            minutes = scheduling.service_minutes(cursor, service_id, vehicle_type)
            problem = scheduling.check_time(cursor, booking_date, booking_time, minutes, assigned_bay)
            if problem:
                suggestion = scheduling.next_available_time(cursor, booking_date, minutes, assigned_bay, after=booking_time)
                message = scheduling.describe_problem(problem, assigned_bay)
                message += f" The next free time that day is {suggestion}." if suggestion else " There's no free time left that day."
                rejection = (jsonify({'status': 'error', 'message': message, 'next_available': suggestion}), 400)

        if rejection:
            conn.execute("ROLLBACK")
            conn.close()
            return rejection

        next_queue_number = _next_queue_number(cursor)

        # Whose booking this is: the customer's own account when they book
        # for themselves. Staff/admin entering one aren't its owner - it's
        # linked to the customer account with the email they typed, if
        # there is one, so that customer sees it in My Bookings and gets
        # its notifications (and staff don't get "your vehicle is ready").
        if session.get('role') == 'customer':
            user_id = session.get('user_id')
        else:
            cursor.execute(
                "SELECT user_id FROM users WHERE role = 'customer' AND LOWER(username) = LOWER(?)",
                ((customer_email or '').strip(),),
            )
            owner_row = cursor.fetchone()
            user_id = owner_row['user_id'] if owner_row else None
        weather = get_weather_reading()
        vehicle_model = canonical_vehicle_model(cursor, typed_vehicle_model)

        # Operational context at the moment of booking, captured for
        # ai_retrain.py to later train the wait-time model on real data
        # instead of only the synthetic bootstrap in ai_engine.py. Counted
        # before this row is inserted, so it reflects "how many were ahead
        # of you" rather than including this booking itself.
        cursor.execute(
            """
            SELECT COUNT(*) FROM reservations
            WHERE service_id = ?
              AND queue_status IN ('waiting', 'in_progress')
            """,
            (service_id,),
        )
        queue_length_snapshot = cursor.fetchone()[0]

        active_staff_snapshot = get_active_staff_count()

        # The model's own prediction for this exact booking, using the same
        # snapshot inputs stored above - lets ai_retrain.py later compute a
        # real MAE/RMSE against what actually happened (completed_at -
        # created_at). Rainfall and the
        # service's real duration are the same inputs retraining learns
        # from (both stored on the reservation).
        # The work already ahead in this bay - the model's strongest input.
        work_ahead = work_ahead_minutes(cursor, assigned_bay)
        try:
            predicted_wait_minutes = ai_predictor.predict_ewt(
                queue_length_snapshot, active_staff_snapshot, weather['rain_mm'], serv_row['base_duration_minutes'],
                work_ahead_minutes=work_ahead,
            )
        except Exception as e:
            print(f"[WARNING] predict_ewt failed during booking submission: {e}")
            predicted_wait_minutes = None

        # Set explicitly in Python (local time) rather than relying on the
        # column's DEFAULT CURRENT_TIMESTAMP, which SQLite evaluates in
        # UTC - completed_at is already set this way (see api_complete_service),
        # so leaving created_at on the SQL-level default would make every
        # actual-wait-time diff (completed_at - created_at) silently wrong
        # by the local/UTC offset. Same class of bug as the login-lockout
        # fix; this is what get_analytics_metrics and ai_retrain.py both
        # depend on for an honest wait-time figure.
        created_at = now_str()

        try:
            cursor.execute(
                """
                INSERT INTO reservations (user_id, customer_name, vehicle_plate_number, vehicle_type, vehicle_model, service_id, customer_email, queue_number, queue_status, booking_source, assigned_bay, booking_date, booking_time, weather_condition, precipitation_mm, queue_length_snapshot, active_staff_snapshot, predicted_wait_minutes, work_ahead_minutes, created_at, price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (user_id, customer_name, plate_number, vehicle_type, vehicle_model, service_id, customer_email, next_queue_number, booking_source, assigned_bay, booking_date, booking_time, weather['condition'], weather['rain_mm'], queue_length_snapshot, active_staff_snapshot, predicted_wait_minutes, work_ahead, created_at, booking_price),
            )
        except sqlite3.IntegrityError as e:
            # No slot-exclusivity index exists anymore (see database.py -
            # bay/time is informational, not exclusive, per the note above),
            # so this now only fires for a genuinely unexpected constraint
            # violation - surfaced plainly rather than assumed to be the
            # old slot-conflict case.
            cursor.execute("ROLLBACK")
            conn.close()
            return jsonify({'status': 'error', 'message': f'Could not save booking: {e}'}), 400

        booking_id = cursor.lastrowid
        conn.commit()
        try:
            estimate = ServiceEstimates(cursor, ai_predictor, weather['rain_mm']).for_job(booking_id)
        except Exception:
            # A display failure cannot turn a successfully saved booking into
            # an apparent rejection (which could cause a duplicate retry).
            current_app.logger.exception('Live timing unavailable for saved booking %s', booking_id)
            estimate = None
        conn.close()

        log_activity(session.get('user', 'Guest/Customer'), f'Created booking for plate: {plate_number} Source: {booking_source}')

        # In-app notifications - independent of email/Gmail preferences.
        # The booking's owner (user_id above - None for a walk-in with no
        # customer account behind it) is told it's confirmed; staff/admin
        # are told about every new booking regardless of who placed it,
        # minus whoever just placed it themselves.
        actor_id = session.get('user_id')
        if user_id:
            create_notification(
                user_id, 'Booking Confirmed',
                f'Your booking for vehicle {plate_number} ({service_name}) is confirmed - queue #{next_queue_number}.',
                'booking_confirmed', booking_id,
            )
        new_booking_message = f'New {booking_source.lower()} - {plate_number} ({service_name}, {assigned_bay}).'
        for role in ('staff', 'admin'):
            notify_role(role, 'New Booking', new_booking_message, 'new_booking', booking_id, exclude_user_id=actor_id)

        return jsonify({
            'status': 'success',
            'message': 'Booking saved successfully and added to the queue.',
            'booking_id': f"MD57-{booking_id}",
            'booking_source': booking_source,
            'queue_number': next_queue_number,
            'plate_number': plate_number,
            'vehicle_model': vehicle_model,
            'assigned_bay': assigned_bay,
            'price': booking_price,
            'predicted_wait_minutes': predicted_wait_minutes,
            'estimate': estimate,
        }), 200

    except Exception:
        # Every specific failure path above explicitly rolls back and
        # closes `conn` once the BEGIN IMMEDIATE transaction has started -
        # this catch-all is the one path that didn't, leaving a
        # write-lock-holding connection to whatever CPython's GC timing
        # happens to do if an exception fires between BEGIN IMMEDIATE and
        # commit (e.g. the AI predictor or a snapshot lookup raising).
        # `conn` is None if the exception happened before the transaction
        # even started, so there's nothing to release in that case.
        if conn is not None:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            conn.close()
        raise
