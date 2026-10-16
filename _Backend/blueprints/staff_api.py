import csv as csv_module
import io
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, session, Response

from analytics import mcda_criteria
from assignment import rank_available_staff, reallocate
from service_estimates import ServiceEstimates
from scheduling import check_time, describe_problem
from database import get_db_connection
from extensions import ai_predictor
from utils import (
    role_required, log_activity, get_active_staff_count, get_latest_attendance, is_date_staffed,
    ACTIVE_BOOKING_STATUSES, is_staff_on_duty, now_str,
    get_latest_attendance_by_staff, is_on_duty_record, current_staff_name,
    send_branded_email,
    get_weather_snapshot, get_weather_reading,
    create_notification, notify_role,
    is_clock_in_open, CLOCK_IN_HOURS_TEXT,
    parse_booking_datetime, is_within_business_hours,
)

staff_api_bp = Blueprint('staff_api', __name__)


def _fetch_staff_accounts(cursor):
    cursor.execute(
        "SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' AND disabled_at IS NULL ORDER BY name"
    )
    return cursor.fetchall()


def clock_now():
    """The time attendance is judged against (the tests pin it)."""
    return datetime.now()


def _insert_attendance(staff_user_id, staff_name, date_str, status, time_in='--:--', time_out='--:--'):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO staff_attendance (staff_user_id, staff_name, date_str, status, time_in, time_out, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (staff_user_id, staff_name, date_str, status, time_in, time_out, now_str()),
    )
    conn.commit()
    conn.close()


def _is_date_str(value):
    """Whether value is a real 'YYYY-MM-DD' date - the format every
    date_str/shift_date/unavailable_date/booking_date comparison assumes."""
    try:
        datetime.strptime(value or '', '%Y-%m-%d')
        return True
    except (ValueError, TypeError):
        return False


def _staff_from_request(data):
    """(user_id, current display name) of the real staff account a request
    names - by staff_user_id (what the pages send) or by display name
    (older callers) - or (None, None) if it isn't one (a disabled account
    can't be clocked in or put on the roster)."""
    conn = get_db_connection()
    if data.get('staff_user_id'):
        row = conn.execute(
            "SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' AND disabled_at IS NULL AND user_id = ?",
            (data.get('staff_user_id'),),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT user_id, COALESCE(fullname, username) AS name FROM users WHERE role = 'staff' AND disabled_at IS NULL AND COALESCE(fullname, username) = ? ORDER BY user_id LIMIT 1",
            ((data.get('staff_name') or '').strip(),),
        ).fetchone()
    conn.close()
    return (row['user_id'], row['name']) if row else (None, None)


@staff_api_bp.route('/api/get-staff-list', methods=['GET'])
@role_required('admin', 'staff')
def api_get_staff_list():
    """Real seeded staff accounts (role='staff'), replacing the frontend's
    old hardcoded fictional roster."""
    conn = get_db_connection()
    rows = [dict(row) for row in _fetch_staff_accounts(conn.cursor())]
    conn.close()
    return jsonify({'status': 'success', 'data': rows}), 200



@staff_api_bp.route('/api/staff-real-metrics', methods=['GET'])
@role_required('admin', 'staff')
def api_staff_real_metrics():
    """Each washer's MCDA criteria (speed, punctuality, rating) and job
    counts - all time and today - for the Staff page."""
    conn = get_db_connection()
    cursor = conn.cursor()
    staff_rows = _fetch_staff_accounts(cursor)

    # Display the same system history as Analytics, including generated
    # records retained by the shop. Only the live table supplies active jobs;
    # an archived record can contribute a finished service, never a live task.
    today = datetime.now().strftime('%Y-%m-%d')
    cursor.execute(
        """
        WITH jobs AS (
            SELECT assigned_staff_id, queue_status, completed_at, vehicle_plate_number
            FROM reservations
            UNION ALL
            SELECT assigned_staff_id, queue_status, completed_at, vehicle_plate_number
            FROM reservations_archive WHERE queue_status IN ('ready', 'completed')
        )
        SELECT assigned_staff_id,
               SUM(CASE WHEN queue_status IN ('ready', 'completed') THEN 1 ELSE 0 END) AS completed_jobs,
               SUM(CASE WHEN queue_status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress_jobs,
               SUM(CASE WHEN queue_status = 'pending_confirmation' THEN 1 ELSE 0 END) AS pending_confirmation_jobs,
               SUM(CASE WHEN queue_status IN ('ready', 'completed') AND substr(completed_at, 1, 10) = ? THEN 1 ELSE 0 END) AS done_today,
               MAX(CASE WHEN queue_status = 'in_progress' THEN vehicle_plate_number END) AS washing,
               MAX(CASE WHEN queue_status = 'pending_confirmation' THEN vehicle_plate_number END) AS awaiting_confirmation
        FROM jobs
        GROUP BY assigned_staff_id
        """,
        (today,),
    )
    jobs_by_staff = {row['assigned_staff_id']: row for row in cursor.fetchall()}

    results = []
    for staff in staff_rows:
        speed, punctuality, rating, _ = mcda_criteria(cursor, staff['user_id'], source='all')
        jobs = jobs_by_staff.get(staff['user_id'])
        results.append({
            'user_id': staff['user_id'],
            'name': staff['name'],
            'speed': speed,
            'punctuality': punctuality,
            'rating': rating,
            'completed_jobs': jobs['completed_jobs'] if jobs else 0,
            'in_progress_jobs': jobs['in_progress_jobs'] if jobs else 0,
            'pending_confirmation_jobs': jobs['pending_confirmation_jobs'] if jobs else 0,
            'awaiting_confirmation': jobs['awaiting_confirmation'] if jobs else None,
            'done_today': jobs['done_today'] if jobs else 0,
            'washing': jobs['washing'] if jobs else None,
        })

    conn.close()
    total_completed = sum(row['completed_jobs'] for row in jobs_by_staff.values())
    return jsonify({
        'status': 'success',
        'data': results,
        'total_completed_jobs': total_completed,
        'completed_today': sum(row['done_today'] for row in jobs_by_staff.values()),
        'washing_now': sum(row['in_progress_jobs'] for row in jobs_by_staff.values()),
        'unattributed_completed_jobs': total_completed - sum(r['completed_jobs'] for r in results),
    }), 200


@staff_api_bp.route('/api/recommend-staff', methods=['GET'])
@role_required('admin', 'staff')
def api_recommend_staff():
    """Who the next car will go to, and everyone on duty (free or busy) -
    shown on the queue page before a car starts. The same ranking makes the
    automatic assignment when it starts (assignment.py), so what's shown is
    the assignment that will happen."""
    conn = get_db_connection()
    ranking = rank_available_staff(conn.cursor())
    conn.close()

    ranked = [{key: c[key] for key in ('user_id', 'name', 'speed', 'punctuality', 'rating', 'total_jobs', 'jobs_today',
                                       'has_history', 'saw_score')} for c in ranking['ranked']]
    on_duty = [{'user_id': s['user_id'], 'name': s['name'], 'busy': s['busy'], 'jobs_today': s['jobs_today']}
               for s in ranking['on_duty']]
    if not ranked:
        reason = 'no_staff_on_duty' if not on_duty else 'all_on_duty_staff_busy'
    else:
        reason = 'ranked_by_performance' if ranked[0]['has_history'] else 'no_track_record_yet_rotation_pick'
    return jsonify({
        'status': 'success',
        'recommended': ranked[0] if ranked else None,
        'ranked_available': ranked,
        'on_duty': on_duty,
        'reason': reason,
        'available_count': len(ranked),
        'busy_count': ranking['busy_count'],
        'off_duty_count': ranking['off_duty_count'],
    }), 200


@staff_api_bp.route('/api/staff-attendance', methods=['POST'])
@role_required('admin')
def api_staff_attendance():
    """Admin records attendance (time in / time out) for a staff member;
    staff ask through /api/staff-clock-in-request instead."""
    data = request.get_json() or {}
    date_str = data.get('date_str')
    status = data.get('status')
    time_in = data.get('time_in', '--:--')
    time_out = data.get('time_out', '--:--')

    # Must reference a real staff account, not an arbitrary string.
    staff_user_id, staff_name = _staff_from_request(data)
    if not staff_user_id:
        return jsonify({'status': 'error', 'message': 'Unknown staff member.'}), 400
    if not _is_date_str(date_str) or status not in ('Present', 'Late', 'Absent'):
        return jsonify({'status': 'error', 'message': 'A valid date and status (Present, Late or Absent) are required.'}), 400
    if time_out != '--:--' and not is_staff_on_duty(staff_user_id, date_str):
        return jsonify({'status': 'error', 'message': f'{staff_name} is not clocked in, so there is nothing to time out.'}), 400
    now = clock_now()
    today = now.strftime('%Y-%m-%d')
    if date_str > today:
        return jsonify({'status': 'error', 'message': "Attendance can't be recorded for a day that hasn't come yet."}), 400
    if time_out == '--:--' and status in ('Present', 'Late'):  # a time-in
        if is_staff_on_duty(staff_user_id, date_str):
            return jsonify({'status': 'error', 'message': f'{staff_name} is already clocked in.'}), 409
        if date_str == today and not is_clock_in_open(now):
            return jsonify({'status': 'error', 'message': f'Clock-in is open {CLOCK_IN_HOURS_TEXT}. The shop is closed right now.'}), 400

    _insert_attendance(staff_user_id, staff_name, date_str, status, time_in, time_out)

    # time_out stays '--:--' on a time-in action (see staff.html's
    # staffSelfAction) and is only ever set on a time-out action, so it
    # doubles as a reliable signal for which of the two just happened.
    if time_out == '--:--':
        log_activity(session.get('user', staff_name), f'Time-In: {staff_name} at {time_in}')
        notif_message = f'Attendance recorded: Time-In at {time_in} ({status}).'
    else:
        log_activity(session.get('user', staff_name), f'Time-Out: {staff_name} at {time_out}')
        notif_message = f'Attendance recorded: Time-Out at {time_out}.'

    create_notification(staff_user_id, 'Attendance Recorded', notif_message, 'attendance')
    notify_role('admin', 'Staff Attendance', f'{staff_name} - {notif_message}', 'attendance', exclude_user_id=session.get('user_id'))

    # Someone came on or went off duty - cars without a working washer are
    # given to whoever is now available (automated staff allocation).
    reallocate()
    return jsonify({'status': 'success', 'message': 'Attendance updated successfully!'}), 200




# A clock-in after 8:15 AM is recorded as Late.
LATE_CUTOFF_HOUR, LATE_CUTOFF_MINUTE = 8, 15


@staff_api_bp.route('/api/staff-clock-in-request', methods=['POST'])
@role_required('staff')
def api_staff_clock_in_request():
    """Staff can't clock themselves in directly anymore, but they can ask -
    this records the real moment they showed up as a 'Pending' row; admin
    then confirms (/api/confirm-clock-in) or rejects
    (/api/reject-clock-in) it. The requested time_in is what's kept as the
    real clock-in time once confirmed, not whenever admin got to it."""
    staff_user_id = session.get('user_id')
    staff_name = current_staff_name()
    row = get_latest_attendance(staff_user_id)

    if is_on_duty_record(row):
        return jsonify({'status': 'error', 'message': 'You are already clocked in today.'}), 409
    if row and row['status'] == 'Pending':
        return jsonify({'status': 'error', 'message': 'Your clock-in request is already waiting for admin to confirm it.'}), 409

    now = clock_now()
    if not is_clock_in_open(now):
        return jsonify({'status': 'error', 'message': f"You can request a clock-in from {CLOCK_IN_HOURS_TEXT}. The shop is closed right now."}), 400
    time_in = now.strftime('%I:%M %p')
    _insert_attendance(staff_user_id, staff_name, now.strftime('%Y-%m-%d'), 'Pending', time_in)

    log_activity(session.get('user', staff_name), f'Requested clock-in at {time_in}')
    notify_role('admin', 'Clock-In Request', f'{staff_name} requested clock-in at {time_in}.', 'attendance')
    return jsonify({'status': 'success', 'message': 'Clock-in request sent - waiting for admin to confirm.'}), 200


@staff_api_bp.route('/api/cancel-clock-in-request', methods=['POST'])
@role_required('staff')
def api_cancel_clock_in_request():
    """Staff take back their own request while it's still Pending (sent by
    mistake, or they're leaving). The Pending row is removed outright - a
    request nobody acted on isn't attendance, and isn't an absence either."""
    staff_user_id = session.get('user_id')
    staff_name = current_staff_name()
    row = get_latest_attendance(staff_user_id)
    if not row or row['status'] != 'Pending':
        return jsonify({'status': 'error', 'message': 'You have no clock-in request waiting.'}), 400

    conn = get_db_connection()
    conn.execute("DELETE FROM staff_attendance WHERE attendance_id = ? AND status = 'Pending'", (row['latest_id'],))
    conn.commit()
    conn.close()

    log_activity(session.get('user', staff_name), 'Cancelled their clock-in request')
    notify_role('admin', 'Clock-In Request Cancelled', f'{staff_name} cancelled their clock-in request.', 'attendance')
    return jsonify({'status': 'success', 'message': 'Clock-in request cancelled.'}), 200


@staff_api_bp.route('/api/clock-in-requests', methods=['GET'])
@role_required('admin')
def api_get_clock_in_requests():
    """Every staff member whose latest attendance row today is still
    'Pending' - the admin-facing approval queue."""
    pending = [
        {'staff_user_id': row['staff_user_id'], 'staff_name': row['staff_name'], 'time_in': row['time_in']}
        for row in get_latest_attendance_by_staff() if row['status'] == 'Pending'
    ]
    return jsonify({'status': 'success', 'data': pending}), 200


@staff_api_bp.route('/api/confirm-clock-in', methods=['POST'])
@role_required('admin')
def api_confirm_clock_in():
    data = request.get_json() or {}
    staff_user_id, staff_name = _staff_from_request(data)

    row = get_latest_attendance(staff_user_id) if staff_user_id else None
    if not row or row['status'] != 'Pending':
        return jsonify({'status': 'error', 'message': 'No pending clock-in request for that staff member.'}), 400

    time_in = row['time_in']
    try:
        requested_dt = datetime.strptime(time_in, '%I:%M %p')
        is_late = (requested_dt.hour, requested_dt.minute) > (LATE_CUTOFF_HOUR, LATE_CUTOFF_MINUTE)
    except (ValueError, TypeError):
        is_late = False
    new_status = 'Late' if is_late else 'Present'

    _insert_attendance(staff_user_id, staff_name, datetime.now().strftime('%Y-%m-%d'), new_status, time_in)

    log_activity(session.get('user', 'Admin'), f'Confirmed clock-in for {staff_name} at {time_in} ({new_status})')
    create_notification(
        staff_user_id, 'Clock-In Confirmed',
        f'Your clock-in at {time_in} was confirmed ({new_status}).', 'attendance',
    )
    reallocate()  # a car started while nobody was clocked in goes to them now
    return jsonify({'status': 'success', 'message': f'{staff_name} is now clocked in ({new_status}).'}), 200


@staff_api_bp.route('/api/reject-clock-in', methods=['POST'])
@role_required('admin')
def api_reject_clock_in():
    data = request.get_json() or {}
    staff_user_id, staff_name = _staff_from_request(data)

    row = get_latest_attendance(staff_user_id) if staff_user_id else None
    if not row or row['status'] != 'Pending':
        return jsonify({'status': 'error', 'message': 'No pending clock-in request for that staff member.'}), 400

    _insert_attendance(staff_user_id, staff_name, datetime.now().strftime('%Y-%m-%d'), 'Absent')

    log_activity(session.get('user', 'Admin'), f'Rejected clock-in request for {staff_name}')
    create_notification(
        staff_user_id, 'Clock-In Request Rejected',
        'Your clock-in request was not confirmed by admin. Please check with them.', 'attendance',
    )
    return jsonify({'status': 'success', 'message': f'Clock-in request for {staff_name} rejected.'}), 200


@staff_api_bp.route('/api/staff-status', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_staff_status():
    """Backs the booking page's staff-availability banner. `active_staff`
    is how many are clocked in right now; `staff_available` answers for
    ?date=YYYY-MM-DD (default today) with the same rule
    /api/submit-booking enforces for customers (utils.is_date_staffed), so
    the banner and the booking outcome always agree."""
    date_str = request.args.get('date') or datetime.now().strftime('%Y-%m-%d')
    return jsonify({
        'status': 'success',
        'date': date_str,
        'active_staff': get_active_staff_count(),
        'staff_available': is_date_staffed(date_str),
    }), 200


@staff_api_bp.route('/api/export-attendance-csv', methods=['GET'])
@role_required('admin')
def api_export_attendance_csv():
    """Admin-only CSV export of attendance records, generated straight from
    staff_attendance - every column is a real column already stored by the
    system (staff_name, date_str, status, time_in, time_out, created_at),
    nothing invented. Optional start_date/end_date (YYYY-MM-DD, inclusive)
    narrow the export; omitted, every record on file is included. A UTF-8
    byte-order mark is prefixed so Excel (which otherwise guesses the
    system codepage) opens it correctly instead of mangling it."""
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    conn = get_db_connection()
    cursor = conn.cursor()
    query = ("SELECT COALESCE(u.fullname, u.username, a.staff_name) AS staff_name, a.date_str, a.status, "
             "a.time_in, a.time_out, a.created_at FROM staff_attendance a LEFT JOIN users u ON u.user_id = a.staff_user_id")
    clauses = []
    params = []
    if start_date:
        clauses.append("a.date_str >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("a.date_str <= ?")
        params.append(end_date)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY a.date_str DESC, a.attendance_id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    output.write('﻿')  # UTF-8 BOM - keeps Excel/Google Sheets from mis-decoding the file
    writer = csv_module.writer(output)
    writer.writerow(['Staff Name', 'Date', 'Status', 'Time In', 'Time Out', 'Recorded At'])
    for row in rows:
        writer.writerow([
            row['staff_name'], row['date_str'], row['status'],
            row['time_in'], row['time_out'], row['created_at'],
        ])

    log_activity(session.get('user', 'Admin'), 'Exported attendance logs to CSV')
    filename = f"MD57_Attendance_Logs_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        output.getvalue().encode('utf-8'),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@staff_api_bp.route('/api/get-staff-attendance', methods=['GET'])
@role_required('admin', 'staff')
def api_get_staff_attendance():
    """Returns the latest attendance record per staff member for a given date
    (defaults to today)."""
    date_str = request.args.get('date_str', datetime.now().strftime('%Y-%m-%d'))
    rows = [dict(row) for row in get_latest_attendance_by_staff(date_str)]
    return jsonify({'status': 'success', 'date_str': date_str, 'data': rows}), 200


# Forward-looking schedule, distinct from staff_attendance (which only
# ever records what already happened). Default window matches the
# "shift roster" framing - a week out, not the whole history.
ROSTER_DEFAULT_DAYS_AHEAD = 6


@staff_api_bp.route('/api/staff-shifts', methods=['GET'])
@role_required('admin', 'staff')
def api_get_staff_shifts():
    today = datetime.now().strftime('%Y-%m-%d')
    start_date = request.args.get('start_date', today)
    end_date = request.args.get(
        'end_date',
        (datetime.now() + timedelta(days=ROSTER_DEFAULT_DAYS_AHEAD)).strftime('%Y-%m-%d'),
    )
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT s.shift_id, s.staff_user_id, COALESCE(u.fullname, u.username, s.staff_name) AS staff_name,
               s.shift_date, s.shift_note, s.created_by
        FROM staff_shifts s
        LEFT JOIN users u ON u.user_id = s.staff_user_id
        WHERE s.shift_date BETWEEN ? AND ?
        ORDER BY s.shift_date ASC, staff_name ASC
        """,
        (start_date, end_date),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'status': 'success', 'data': rows}), 200


@staff_api_bp.route('/api/staff-shifts', methods=['POST'])
@role_required('admin')
def api_add_staff_shift():
    """Admin-only, same "must reference a real staff account" validation
    api_staff_attendance already uses. One row per staff/day - scheduling
    the same person for a date they're already scheduled just updates the
    note instead of erroring, so re-submitting the roster form is harmless.
    repeat_weeks (0-12) also schedules the same weekday for that many
    following weeks, so a regular schedule isn't re-entered day by day."""
    data = request.get_json() or {}
    shift_date = (data.get('shift_date') or '').strip()
    shift_note = (data.get('shift_note') or '').strip() or None
    try:
        repeat_weeks = min(max(int(data.get('repeat_weeks') or 0), 0), 12)
        first_day = datetime.strptime(shift_date, '%Y-%m-%d') if shift_date else None
    except ValueError:
        return jsonify({'status': 'error', 'message': 'Invalid date or repeat count.'}), 400

    if not shift_date or not (data.get('staff_user_id') or data.get('staff_name')):
        return jsonify({'status': 'error', 'message': 'Staff member and date are required.'}), 400

    staff_user_id, staff_name = _staff_from_request(data)
    if not staff_user_id:
        return jsonify({'status': 'error', 'message': 'Unknown staff member.'}), 400
    if shift_date < datetime.now().strftime('%Y-%m-%d'):
        return jsonify({'status': 'error', 'message': "That day has already passed - a shift can't be scheduled for it."}), 400

    dates = [(first_day + timedelta(weeks=week)).strftime('%Y-%m-%d') for week in range(repeat_weeks + 1)]
    conn = get_db_connection()
    conn.executemany(
        """
        INSERT INTO staff_shifts (staff_user_id, staff_name, shift_date, shift_note, created_by)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(staff_name, shift_date) DO UPDATE SET shift_note = excluded.shift_note, created_by = excluded.created_by
        """,
        [(staff_user_id, staff_name, day, shift_note, session.get('user', 'Admin')) for day in dates],
    )
    conn.commit()
    conn.close()

    when = shift_date if not repeat_weeks else f'{shift_date} and the same weekday for {repeat_weeks} more week(s)'
    log_activity(session.get('user', 'Admin'), f'Scheduled {staff_name} for shift on {when}')
    return jsonify({'status': 'success', 'message': f'{staff_name} scheduled for {when}.', 'dates': dates}), 200


@staff_api_bp.route('/api/unstaffed-booking-days', methods=['GET'])
@role_required('admin')
def api_unstaffed_booking_days():
    """Upcoming days that already have bookings but no available staff on
    the roster (or, for today, nobody clocked in either) - what admin should
    schedule someone for before the no-show sweep has to move them."""
    conn = get_db_connection()
    rows = conn.execute(
        f"""
        SELECT booking_date, COUNT(*) AS bookings FROM reservations
        WHERE booking_date >= ? AND queue_status IN ({','.join('?' * len(ACTIVE_BOOKING_STATUSES))})
        GROUP BY booking_date ORDER BY booking_date
        """,
        (datetime.now().strftime('%Y-%m-%d'), *ACTIVE_BOOKING_STATUSES),
    ).fetchall()
    conn.close()
    days = [{'date': r['booking_date'], 'bookings': r['bookings']} for r in rows if not is_date_staffed(r['booking_date'])]
    return jsonify({'status': 'success', 'data': days}), 200


@staff_api_bp.route('/api/delete-staff-shift', methods=['POST'])
@role_required('admin')
def api_delete_staff_shift():
    data = request.get_json() or {}
    shift_date = (data.get('shift_date') or '').strip()

    if not shift_date or not (data.get('staff_user_id') or data.get('staff_name')):
        return jsonify({'status': 'error', 'message': 'Staff member and date are required.'}), 400

    staff_user_id, staff_name = _staff_from_request(data)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM staff_shifts WHERE staff_user_id = ? AND shift_date = ?",
        (staff_user_id, shift_date),
    )
    removed = cursor.rowcount
    conn.commit()
    conn.close()

    if removed == 0:
        return jsonify({'status': 'error', 'message': 'No matching shift found.'}), 404

    log_activity(session.get('user', 'Admin'), f'Removed {staff_name} from the roster for {shift_date}')
    return jsonify({'status': 'success', 'message': f'{staff_name} removed from {shift_date}.'}), 200


@staff_api_bp.route('/api/staff-unavailability', methods=['GET'])
@role_required('admin', 'staff')
def api_get_staff_unavailability():
    """Admin sees every staff member's marked-unavailable dates in the
    window (needs the full picture to schedule around it). A staff session
    sees only their own - a coworker's reason for being unavailable isn't
    anyone else's business, only admin's."""
    today = datetime.now().strftime('%Y-%m-%d')
    start_date = request.args.get('start_date', today)
    is_admin = session.get('role') == 'admin'
    # Admin: the roster window. Staff: every upcoming day of their own -
    # a day marked further ahead than the roster shows must still be listed,
    # or they could never remove it.
    end_date = request.args.get(
        'end_date',
        (datetime.now() + timedelta(days=ROSTER_DEFAULT_DAYS_AHEAD)).strftime('%Y-%m-%d') if is_admin else '9999-12-31',
    )
    conn = get_db_connection()
    cursor = conn.cursor()
    if is_admin:
        cursor.execute(
            """
            SELECT n.unavailability_id, n.staff_user_id, COALESCE(u.fullname, u.username, n.staff_name) AS staff_name,
                   n.unavailable_date, n.reason
            FROM staff_unavailability n
            LEFT JOIN users u ON u.user_id = n.staff_user_id
            WHERE n.unavailable_date BETWEEN ? AND ?
            ORDER BY n.unavailable_date ASC, staff_name ASC
            """,
            (start_date, end_date),
        )
    else:
        cursor.execute(
            """
            SELECT unavailability_id, staff_user_id, staff_name, unavailable_date, reason
            FROM staff_unavailability
            WHERE unavailable_date BETWEEN ? AND ? AND staff_user_id = ?
            ORDER BY unavailable_date ASC
            """,
            (start_date, end_date, session.get('user_id')),
        )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'status': 'success', 'data': rows}), 200


@staff_api_bp.route('/api/staff-unavailability', methods=['POST'])
@role_required('staff')
def api_add_staff_unavailability():
    """Staff-only, and always for the session's own identity - the whole
    point is giving staff a real, first-party way to say "I can't work
    this day" that admin sees before building the roster, not a channel
    for anyone to speak for someone else. Mirrors the self-only shape of
    /api/staff-clock-in-request."""
    data = request.get_json() or {}
    unavailable_date = (data.get('unavailable_date') or '').strip()
    reason = (data.get('reason') or '').strip() or None
    if not _is_date_str(unavailable_date):
        return jsonify({'status': 'error', 'message': 'A valid date is required.'}), 400
    today = datetime.now().strftime('%Y-%m-%d')
    if unavailable_date < today:
        return jsonify({'status': 'error', 'message': 'That day has already passed.'}), 400
    if unavailable_date == today and is_staff_on_duty(session.get('user_id')):
        return jsonify({'status': 'error', 'message': "You're clocked in today - ask the manager to time you out instead."}), 400

    staff_name = current_staff_name()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO staff_unavailability (staff_user_id, staff_name, unavailable_date, reason)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(staff_name, unavailable_date) DO UPDATE SET reason = excluded.reason
        """,
        (session.get('user_id'), staff_name, unavailable_date, reason),
    )
    rostered = cursor.execute(
        "SELECT 1 FROM staff_shifts WHERE staff_user_id = ? AND shift_date = ?", (session.get('user_id'), unavailable_date)
    ).fetchone()
    conn.commit()
    conn.close()

    log_activity(session.get('user', staff_name), f'Marked unavailable on {unavailable_date}')
    message = f'Marked unavailable on {unavailable_date}.'
    if rostered:
        # They were counted on for that day - the roster needs someone else.
        notify_role('admin', 'Rostered Staff Unavailable',
                    f"{staff_name} is on the roster for {unavailable_date} but marked that day unavailable"
                    f"{' (' + reason + ')' if reason else ''}.", 'attendance')
        message += " You're on the roster that day, so admin has been told."
    return jsonify({'status': 'success', 'message': message}), 200


@staff_api_bp.route('/api/delete-staff-unavailability', methods=['POST'])
@role_required('staff')
def api_delete_staff_unavailability():
    data = request.get_json() or {}
    unavailable_date = (data.get('unavailable_date') or '').strip()
    if not unavailable_date:
        return jsonify({'status': 'error', 'message': 'A date is required.'}), 400

    staff_name = current_staff_name()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM staff_unavailability WHERE staff_user_id = ? AND unavailable_date = ?",
        (session.get('user_id'), unavailable_date),
    )
    removed = cursor.rowcount
    conn.commit()
    conn.close()

    if removed == 0:
        return jsonify({'status': 'error', 'message': 'No matching entry found.'}), 404

    log_activity(session.get('user', staff_name), f'Cleared unavailability on {unavailable_date}')
    return jsonify({'status': 'success', 'message': f'Cleared for {unavailable_date}.'}), 200


@staff_api_bp.route('/api/my-notify-contact', methods=['GET'])
@role_required('staff')
def api_get_my_notify_contact():
    """Returns only the session's own opt-in contact info - kept off
    /api/get-staff-list entirely, since that endpoint is visible to every
    staff/admin session and this isn't something a coworker needs to see
    just to view the roster."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT reminder_email AS notify_email FROM users WHERE user_id = ?",
        (session.get('user_id'),),
    )
    row = cursor.fetchone()
    conn.close()
    return jsonify({
        'status': 'success',
        'notify_email': row['notify_email'] if row else None,
    }), 200


@staff_api_bp.route('/api/update-notify-contact', methods=['POST'])
@role_required('staff')
def api_update_notify_contact():
    """Staff-only opt-in contact info for shift reminders (shift_reminder.py).
    Always the session's own account - there's no admin path to set this
    for someone else, since the whole point is that handing over an email
    is the staff member's own choice, not something imposed on them. Can
    be cleared by sending an empty string."""
    from employee_accounts import valid_email
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data)-{'notify_email'} or not isinstance(data.get('notify_email', ''), str):
        return jsonify(status='error', message='Only your reminder email can be updated here.'), 400
    email = data.get('notify_email', '').strip().lower() or None

    if email and not valid_email(email):
        return jsonify({'status': 'error', 'message': 'That email address doesn\'t look right.'}), 400

    user_id = session.get('user_id')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET reminder_email = ?, profile_updated_at = datetime('now','localtime') WHERE user_id = ?",
        (email, user_id),
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': 'Notification contact info updated.'}), 200


@staff_api_bp.route('/api/predict-ewt', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def api_predict_ewt():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(status='error', message='Please select a service and vehicle size.'), 400
    day, time = data.get('booking_date'), data.get('booking_time')
    if any(value is not None and not isinstance(value, str) for value in (day, time, data.get('vehicle_type'))):
        return jsonify(status='error', message='Please select a valid service, size and booking time.'), 400
    try:
        service_id = int(data.get('service_id'))
    except (TypeError, ValueError):
        return jsonify(status='error', message='Please select a valid service.'), 400
    booked = parse_booking_datetime(day, time)
    now = clock_now()
    if bool(day) != bool(time) or ((day or time) and (booked is None or booked < now or not is_within_business_hours(booked))):
        return jsonify(status='error', message='Please select a valid future booking time during opening hours.'), 400
    conn = get_db_connection()
    try:
        service = conn.execute('SELECT service_id,base_duration_minutes,default_bay FROM services WHERE service_id=? AND is_active=1',
                               (service_id,)).fetchone()
        size = conn.execute('SELECT type_name,multiplier_minutes FROM vehicle_types WHERE type_name=?',
                            (data.get('vehicle_type'),)).fetchone()
        price = conn.execute('SELECT 1 FROM service_prices WHERE service_id=? AND vehicle_type=?',
                             (service_id, data.get('vehicle_type'))).fetchone()
        if not service or not size or not price:
            return jsonify(status='error', message='Please select an available service and matching vehicle size.'), 400
        standard = service['base_duration_minutes'] + size['multiplier_minutes']
        if booked:
            problem = check_time(conn.cursor(), day, time, standard, service['default_bay'], now=now)
            if problem:
                return jsonify(status='error', message=describe_problem(problem, service['default_bay'])), 400
        estimate = ServiceEstimates(conn.cursor(), ai_predictor, get_weather_reading()['rain_mm'], now).preview(
            service, size['type_name'], standard, day, time)
        return jsonify(status='success', estimate=estimate, predicted_ewt_minutes=estimate['total_minutes'])
    finally:
        conn.close()


@staff_api_bp.route('/api/weather-current', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_weather_current():
    """Current conditions for Analytics' weather widget, fetched here so
    WEATHER_API_KEY never reaches a browser."""
    snapshot = get_weather_snapshot()
    if snapshot is None:
        return jsonify({'status': 'error', 'message': 'Weather data unavailable right now.'}), 200
    return jsonify({'status': 'success', **snapshot}), 200


@staff_api_bp.route('/api/send-email', methods=['POST'])
@role_required('admin')
def send_email_notification():
    """Notifies a customer their vehicle is ready, by email - looked up
    from the reservation itself rather than trusted from the client, same
    "read it from the record, not the request" pattern item 6's feedback
    fix established."""
    data = request.get_json() or {}

    reservation_id = data.get('reservation_id')
    plate_number = data.get('plate_number')
    custom_subject = data.get('subject')
    custom_message = data.get('message')

    if not reservation_id and not plate_number:
        return jsonify({'status': 'error', 'message': 'Which booking? reservation_id is required.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    if reservation_id:
        cursor.execute(
            """SELECT r.reservation_id,r.vehicle_plate_number,r.customer_name,r.customer_email,
                      r.queue_status,r.completed_at,s.service_name FROM reservations r
               LEFT JOIN services s ON s.service_id=r.service_id WHERE r.reservation_id = ?""",
            (reservation_id,),
        )
    else:
        cursor.execute(
            """
            SELECT r.reservation_id,r.vehicle_plate_number,r.customer_name,r.customer_email,
                   r.queue_status,r.completed_at,s.service_name FROM reservations r
            LEFT JOIN services s ON s.service_id=r.service_id
            WHERE r.vehicle_plate_number = ?
            ORDER BY (r.queue_status = 'ready') DESC, r.reservation_id DESC
            LIMIT 1
            """,
            (plate_number,),
        )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({'status': 'error', 'message': 'No matching reservation found.'}), 404
    if row['queue_status'] != 'ready':
        return jsonify({'status': 'error', 'message': 'Ready notifications can only be sent for a vehicle awaiting pickup.'}), 409
    plate_number = row['vehicle_plate_number']
    service_type = row['service_name'] or 'Car Wash Service'

    customer_name = row['customer_name'] or 'Customer'
    recipient_email = row['customer_email']

    if not recipient_email:
        return jsonify({'status': 'error', 'message': 'Could not notify the customer (no email on file).'}), 400

    try:
        subject_to_use = custom_subject if custom_subject else 'MD57 Car Wash - Your Vehicle is Ready!'
        body_to_use = custom_message if custom_message else f"""Hello {customer_name}!

We are pleased to inform you that your vehicle with plate number {plate_number} (Service: {service_type}) is now complete and ready for pickup at MD57 Car Wash & Auto Detailing.

Thank you very much for your trust!
- MD57 Team"""
        send_branded_email(
            subject=subject_to_use,
            recipient=recipient_email,
            heading='Your Vehicle is Ready!',
            plain_text_body=body_to_use,
            dedupe_key=f'ready:{row["reservation_id"]}:{row["completed_at"]}',
        )
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Could not notify the customer (email failed: {e}).'}), 400

    notified_at = now_str()
    conn = get_db_connection()
    try:
        conn.execute('UPDATE reservations SET ready_notified_at=? WHERE reservation_id=?',
                     (notified_at, row['reservation_id']))
        conn.commit()
    finally:
        conn.close()
    log_activity(session.get('user', 'System'), f'Sent ready email for booking MD57-{row["reservation_id"]}: {plate_number}')
    return jsonify({'status': 'success', 'message': 'Ready notification sent. Confirm pickup when the vehicle is collected.',
                    'email_sent': True, 'ready_notified_at': notified_at}), 200
