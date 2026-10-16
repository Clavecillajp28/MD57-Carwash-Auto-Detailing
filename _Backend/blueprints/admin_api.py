import io
import math
import os
import re
import sqlite3
import tempfile
from shop_clock import datetime

from flask import Blueprint, jsonify, request, send_file, session

from analytics import queue_wait_minutes
from config import ACTIVITY_LOG_FILE, DB_FILE
from database import get_db_connection
from settings import all_settings, save_setting
from utils import get_active_staff_count, log_activity, owner_only, role_required
from vehicles import MAX_MODEL_LENGTH

admin_api_bp = Blueprint('admin_api', __name__)

# Matches lines written by utils.log_activity(): "[2026-09-11 14:32:00]
# User: manager - Action: Logged in as admin"
_LOG_LINE_RE = re.compile(r'^\[(?P<timestamp>[^\]]+)\] User: (?P<username>.*?) - Action: (?P<action>.*)$')


@admin_api_bp.route('/api/admin/create-user', methods=['POST'])
@admin_api_bp.route('/api/admin/invitations', methods=['POST'])
@role_required('admin')
@owner_only
def api_create_user():
    from employee_accounts import issue_invitation
    data = request.get_json(silent=True) or request.form
    if data.get('password') or data.get('new_password'):
        return jsonify(status='error', message='Employees set their own passwords through an invitation.'), 400
    try:
        issue_invitation(email=(data.get('email') or '').strip(),
                         fullname=(data.get('fullname') or '').strip(),
                         role=(data.get('role') or 'staff').strip().lower(),
                         created_by=session['user_id'])
    except ValueError as e:
        return jsonify(status='error', message=str(e)), 400
    except RuntimeError as e:
        return jsonify(status='error', message=str(e)), 503
    log_activity(session['user'], 'Submitted employee invitation to email provider')
    return jsonify(status='success', message='Invitation submitted to the email provider. Check delivery in Operations; the employee will choose their own password.')


@admin_api_bp.route('/api/admin/users', methods=['GET'])
@role_required('admin')
@owner_only
def api_list_users():
    conn = get_db_connection()
    try:
        rows = conn.execute("""SELECT user_id, username, COALESCE(fullname,username) AS name,
            role,is_owner,front_desk,disabled_at,notify_email FROM users
            WHERE role IN ('admin','staff') ORDER BY is_owner DESC,role,name""").fetchall()
        invitations = conn.execute("""SELECT invitation_id,email,fullname,role,created_at,expires_at,
            used_at,revoked_at FROM employee_invitations ORDER BY invitation_id DESC LIMIT 100""").fetchall()
    finally:
        conn.close()
    return jsonify(status='success', data=[{
        'user_id': r['user_id'], 'username': r['username'], 'name': r['name'],
        'role': 'owner' if r['is_owner'] else r['role'],
        'email': r['notify_email'] or (r['username'] if '@' in r['username'] else ''),
        'disabled': bool(r['disabled_at']), 'retired': bool(r['front_desk']),
        'is_me': r['user_id']==session['user_id'],
    } for r in rows], invitations=[dict(r) for r in invitations])


@admin_api_bp.route('/api/admin/set-account-disabled', methods=['POST'])
@role_required('admin')
@owner_only
def api_set_account_disabled():
    data = request.get_json(silent=True) or request.form
    try:
        user_id = int(data.get('user_id'))
    except (TypeError,ValueError):
        return jsonify(status='error',message='Which account?'),400
    disabled = str(data.get('disabled')).lower() in ('1','true','yes')
    if user_id == session['user_id']:
        return jsonify(status='error',message="You cannot disable your own account."),400
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        user = conn.execute('SELECT * FROM users WHERE user_id=?',(user_id,)).fetchone()
        if not user or user['role'] not in ('admin','staff'):
            return jsonify(status='error',message='No such employee account.'),404
        if user['front_desk'] and not disabled:
            return jsonify(status='error',message='This shared account is retired. Invite an individual employee.'),400
        if disabled and user['is_owner'] and not conn.execute("""SELECT 1 FROM users WHERE
            is_owner=1 AND role='admin' AND disabled_at IS NULL AND user_id<>?""",(user_id,)).fetchone():
            return jsonify(status='error',message='Keep at least one active owner.'),400
        conn.execute('UPDATE users SET disabled_at=? WHERE user_id=?',
                     (datetime.now().strftime('%Y-%m-%d %H:%M:%S') if disabled else None,user_id))
        conn.execute('UPDATE employee_invitations SET revoked_at=? WHERE user_id=? AND used_at IS NULL',
                     (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),user_id))
        conn.commit()
    finally:
        conn.close()
    if disabled and user['role']=='staff':
        from assignment import reallocate
        try:
            reallocate()
        except Exception:
            from flask import current_app
            current_app.logger.exception('Reallocation after disabling employee failed')
    log_activity(session['user'], f"{'Disabled' if disabled else 'Enabled'} account ID {user_id} ({user['fullname'] or user['username']})")
    return jsonify(status='success',message='Account access updated; existing sessions revoked.')


@admin_api_bp.route('/api/admin/reset-user-password', methods=['POST'])
@role_required('admin')
@owner_only
def api_admin_reset_user_password():
    from employee_accounts import issue_invitation
    data = request.get_json(silent=True) or request.form
    if data.get('password') or data.get('new_password'):
        return jsonify(status='error',message='The employee must choose their own password using the recovery email.'),400
    conn = get_db_connection()
    try:
        user = conn.execute('SELECT * FROM users WHERE LOWER(username)=LOWER(?)',
                            ((data.get('username') or '').strip(),)).fetchone()
    finally:
        conn.close()
    if not user or user['role'] not in ('admin','staff'):
        return jsonify(status='error',message='No such employee account.'),404
    try:
        issue_invitation(email=user['notify_email'] or (user['username'] if '@' in user['username'] else ''),
                         fullname=user['fullname'] or user['username'],
                         role='owner' if user['is_owner'] else user['role'],
                         user_id=user['user_id'],created_by=session['user_id'])
    except ValueError as e:
        return jsonify(status='error',message=str(e)),400
    except RuntimeError as e:
        return jsonify(status='error',message=str(e)),503
    log_activity(session['user'],f"Sent password recovery for account ID {user['user_id']}")
    return jsonify(status='success',message='Recovery email submitted to the provider. Existing sessions have been revoked.')


@admin_api_bp.route('/api/admin/set-account-role', methods=['POST'])
@role_required('admin')
@owner_only
def api_set_account_role():
    data = request.get_json(silent=True) or request.form
    role = data.get('role')
    try:
        user_id = int(data.get('user_id'))
    except (TypeError,ValueError):
        return jsonify(status='error',message='Which account?'),400
    if role not in ('owner','admin','staff'):
        return jsonify(status='error',message='Choose Owner, Manager, or Staff/Washer.'),400
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        user = conn.execute('SELECT * FROM users WHERE user_id=?',(user_id,)).fetchone()
        if not user or user['role'] not in ('admin','staff') or user['front_desk']:
            return jsonify(status='error',message='No such active employee account.'),404
        if user['is_owner'] and role!='owner' and not conn.execute("""SELECT 1 FROM users
            WHERE is_owner=1 AND role='admin' AND disabled_at IS NULL AND user_id<>?""",(user_id,)).fetchone():
            return jsonify(status='error',message='Keep at least one active owner.'),400
        # Do not remove a washer role while their active work still needs that identity.
        if user['role']=='staff' and role!='staff' and conn.execute("""SELECT 1 FROM reservations
            WHERE assigned_staff_id=? AND queue_status IN ('in_progress','pending_confirmation')""",(user_id,)).fetchone():
            return jsonify(status='error',message='Finish or reassign this washer’s active jobs before changing their role.'),409
        conn.execute('UPDATE users SET role=?,is_owner=? WHERE user_id=?',
                     ('admin' if role=='owner' else role,int(role=='owner'),user_id))
        conn.execute('UPDATE employee_invitations SET revoked_at=? WHERE user_id=? AND used_at IS NULL',
                     (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),user_id))
        conn.commit()
    finally:
        conn.close()
    log_activity(session['user'],f"Changed account ID {user_id} role to {role}")
    return jsonify(status='success',message='Role updated; existing sessions revoked.')


@admin_api_bp.route('/api/admin/set-account-contact', methods=['POST'])
@role_required('admin')
@owner_only
def api_set_account_contact():
    from employee_accounts import valid_email
    data = request.get_json(silent=True) or request.form
    email = (data.get('email') or '').strip().lower()
    try:
        user_id = int(data.get('user_id'))
    except (TypeError,ValueError):
        return jsonify(status='error',message='Which account?'),400
    if not valid_email(email):
        return jsonify(status='error',message='Enter a valid personal email address.'),400
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        user = conn.execute("SELECT * FROM users WHERE user_id=? AND role IN ('admin','staff')",(user_id,)).fetchone()
        if not user or user['front_desk']:
            return jsonify(status='error',message='No such employee account.'),404
        if conn.execute("""SELECT 1 FROM users WHERE user_id<>?
            AND (LOWER(username)=? OR LOWER(notify_email)=?)""",(user_id,email,email)).fetchone():
            return jsonify(status='error',message='That email is already used by another account.'),400
        conn.execute('UPDATE users SET notify_email=?,email_verified=0,session_version=session_version+1 WHERE user_id=?',(email,user_id))
        conn.execute('UPDATE password_resets SET used=1 WHERE user_id=?',(user_id,))
        conn.execute('UPDATE employee_invitations SET revoked_at=? WHERE user_id=? AND used_at IS NULL',
                     (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),user_id))
        conn.commit()
    finally:
        conn.close()
    log_activity(session['user'],f"Updated recovery email for account ID {user_id}")
    return jsonify(status='success',message='Email updated; existing sessions revoked. Send recovery to verify the address.')


@admin_api_bp.route('/api/admin/invitations/<int:invitation_id>/<action>',methods=['POST'])
@role_required('admin')
@owner_only
def api_manage_invitation(invitation_id,action):
    from employee_accounts import issue_invitation
    if action not in ('resend','revoke'):
        return jsonify(status='error',message='Unknown action.'),404
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT * FROM employee_invitations WHERE invitation_id=?',(invitation_id,)).fetchone()
        if not row or row['used_at']:
            return jsonify(status='error',message='No unused invitation found.'),404
        if action=='revoke':
            conn.execute('UPDATE employee_invitations SET revoked_at=? WHERE invitation_id=?',
                         (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),invitation_id))
            conn.commit()
    finally:
        conn.close()
    if action=='resend':
        try:
            issue_invitation(email=row['email'],fullname=row['fullname'],role=row['role'],
                             user_id=row['user_id'],created_by=session['user_id'],replace_id=invitation_id)
        except ValueError as e:
            return jsonify(status='error',message=str(e)),400
        except RuntimeError as e:
            return jsonify(status='error',message=str(e)),503
    log_activity(session['user'],f"{action.title()} invitation ID {invitation_id}")
    return jsonify(status='success',message='Invitation '+('resent.' if action=='resend' else 'revoked.'))


@admin_api_bp.route('/api/admin/metrics', methods=['GET'])
@role_required('admin', 'staff')
def api_admin_metrics():
    """Today's numbers at the top of Home for admin and staff (common.js's
    loadKpiStrip). Nothing here is more sensitive than what /api/get-queue
    already shows staff."""
    conn = get_db_connection()
    cursor = conn.cursor()

    today_str = datetime.now().strftime('%Y-%m-%d')

    # In the queue now: cars being worked on or awaiting sign-off, walk-ins,
    # and bookings for today (or overdue). A booking for a later day isn't
    # in today's queue - it's counted separately as upcoming.
    cursor.execute(
        """
        SELECT
            SUM(CASE WHEN queue_status IN ('in_progress', 'pending_confirmation')
                       OR (queue_status = 'waiting' AND (booking_date IS NULL OR booking_date = '' OR booking_date <= ?))
                     THEN 1 ELSE 0 END),
            SUM(CASE WHEN queue_status = 'waiting' AND booking_date > ? THEN 1 ELSE 0 END)
        FROM reservations
        """,
        (today_str, today_str),
    )
    queue_count, upcoming_count = (value or 0 for value in cursor.fetchone())

    # Real "today" numbers a shop owner actually checks first, computed
    # in Python-local time (not SQLite's UTC date('now')) for the same
    # reason created_at is set explicitly elsewhere in this app - see
    # api_submit_booking's created_at comment.
    cursor.execute(
        """
        SELECT COUNT(*) FROM reservations
        WHERE queue_status IN ('ready', 'completed')
          AND completed_at IS NOT NULL AND substr(completed_at, 1, 10) = ?
        """,
        (today_str,),
    )
    completed_today = cursor.fetchone()[0]

    # Queue wait for the cars started today - the same definition as
    # Analytics' "Avg queue wait" (analytics.queue_wait_minutes).
    cursor.execute(
        """
        SELECT booking_source, booking_date, booking_time, created_at, arrived_at, started_at
        FROM reservations
        WHERE started_at IS NOT NULL AND substr(started_at, 1, 10) = ? AND queue_status != 'cancelled'
        """,
        (today_str,),
    )
    waits = [w for w in (queue_wait_minutes(row) for row in cursor.fetchall()) if w is not None]
    avg_wait_today_minutes = round(sum(waits) / len(waits)) if waits else None

    active_staff = get_active_staff_count()

    conn.close()
    return jsonify({
        'status': 'success',
        'queue_count': queue_count,
        'upcoming_count': upcoming_count,
        'completed_today': completed_today,
        'avg_wait_today_minutes': avg_wait_today_minutes,
        'active_staff': active_staff,
    }), 200


@admin_api_bp.route('/api/admin/settings', methods=['GET'])
@role_required('admin')
def api_get_settings():
    return jsonify({'status': 'success', 'settings': all_settings()}), 200


@admin_api_bp.route('/api/admin/settings', methods=['POST'])
@role_required('admin')
@owner_only
def api_save_setting():
    data = request.get_json(silent=True) or {}
    key, value = data.get('key'), data.get('value')
    error = save_setting(key, value, session.get('user', 'Admin'))
    if error:
        return jsonify({'status': 'error', 'message': error}), 400
    log_activity(session.get('user', 'Admin'), f'Changed setting {key} to {value}')
    return jsonify({'status': 'success', 'message': 'Saved.', 'settings': all_settings()}), 200


@admin_api_bp.route('/api/admin/backup', methods=['GET'])
@role_required('admin')
@owner_only
def api_backup_database():
    """A complete, consistent copy of the database as a download - made with
    SQLite's own backup, so it's safe while the shop is using the system.
    To restore: stop the server, replace md57_carwash.db with the copy
    (renamed), start the server again."""
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, 'backup.db')
        source = sqlite3.connect(DB_FILE, timeout=30)
        target = sqlite3.connect(path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        with open(path, 'rb') as f:
            data = f.read()
    stamp = datetime.now().strftime('%Y-%m-%d_%H%M')
    log_activity(session.get('user', 'Admin'), f'Downloaded a database backup ({len(data) // 1024} KB)')
    return send_file(io.BytesIO(data), as_attachment=True, download_name=f'md57_backup_{stamp}.db',
                     mimetype='application/octet-stream')


@admin_api_bp.route('/api/admin/background-jobs', methods=['GET'])
@role_required('admin')
@owner_only
def api_background_job_health():
    from flask import current_app
    from background_jobs import LEASE_SECONDS
    import time
    conn = get_db_connection()
    try:
        rows = [dict(r) for r in conn.execute('SELECT name,lease_until,next_run_at,last_started,last_success,last_error FROM background_jobs ORDER BY name')]
    finally:
        conn.close()
    now = time.time()
    for row in rows:
        row['running'] = row['lease_until'] > now
    return jsonify(status='success', enabled=current_app.config['BACKGROUND_JOBS_ENABLED'],
                   lease_seconds=LEASE_SECONDS, jobs=rows)


@admin_api_bp.route('/api/get-audit-logs', methods=['GET'])
@role_required('admin')
def api_get_audit_logs():
    """Serves the real server-side activity log (utils.log_activity writes
    every sign-in, booking, cancellation, feedback, attendance and account
    action to this file). No delete/clear endpoint exists
    for this on purpose: an audit trail that the people being audited can
    erase isn't much of one."""
    limit = min(max(request.args.get('limit', 100, type=int), 1), 500)
    entries = []
    if os.path.exists(ACTIVITY_LOG_FILE):
        with open(ACTIVITY_LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        # Newest first, capped to `limit` - the file itself is never
        # rotated or truncated here, this just avoids shipping the
        # whole thing over the wire on every modal open.
        for line in reversed(lines[-limit:]):
            line = line.rstrip('\n')
            match = _LOG_LINE_RE.match(line)
            if match:
                entries.append(match.groupdict())
            elif line:
                entries.append({'timestamp': '', 'username': '', 'action': line})

    return jsonify({'status': 'success', 'data': entries}), 200


# --- SERVICE PRICING MANAGEMENT (admin-only) ---
# Backs a dedicated admin page for editing the real service catalog /api/services
# reads from - the whole point of storing pricing in the database instead of
# hardcoded frontend numbers is that an admin can update it here without
# touching any template or code.

_VALID_BAYS = ('Bay #1', 'Bay #2', 'Bay #3')
_VALID_CATEGORIES = ('general', 'detailing', 'motorcycle')


def _parse_price(value):
    """A price as a non-negative, finite float - or None if it isn't one.
    Shared by every route that writes service_prices."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price >= 0 else None


@admin_api_bp.route('/api/admin/services-all', methods=['GET'])
@role_required('admin')
@owner_only
def api_admin_get_all_services():
    """Every service including retired/inactive ones (unlike the
    customer/staff-facing /api/services, which only ever returns active
    ones) - an admin managing the catalog needs to see and potentially
    reactivate a retired service, not just the ones currently bookable."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT service_id, service_name, service_category, default_bay,
               base_duration_minutes, is_active
        FROM services
        ORDER BY is_active DESC, service_category, service_id
        """
    )
    service_rows = cursor.fetchall()

    cursor.execute("SELECT service_id, vehicle_type, price FROM service_prices")
    prices_by_service = {}
    for row in cursor.fetchall():
        prices_by_service.setdefault(row['service_id'], {})[row['vehicle_type']] = row['price']

    cursor.execute("SELECT type_name FROM vehicle_types ORDER BY type_id")
    vehicle_types = [row['type_name'] for row in cursor.fetchall()]
    conn.close()

    data = []
    for row in service_rows:
        entry = dict(row)
        entry['prices'] = prices_by_service.get(row['service_id'], {})
        data.append(entry)

    return jsonify({'status': 'success', 'data': data, 'vehicle_types': vehicle_types}), 200


@admin_api_bp.route('/api/admin/update-service-price', methods=['POST'])
@role_required('admin')
@owner_only
def api_admin_update_service_price():
    """Updates (or creates) one service's price for one vehicle size -
    editing a single cell in the pricing management table, not the whole
    service at once, so a typo in one size doesn't risk resubmitting every
    other size's price along with it."""
    data = request.get_json() or {}
    service_id = data.get('service_id')
    vehicle_type = (data.get('vehicle_type') or '').strip()
    price = data.get('price')

    if not service_id or not vehicle_type:
        return jsonify({'status': 'error', 'message': 'service_id and vehicle_type are required.'}), 400
    price = _parse_price(price)
    if price is None:
        return jsonify({'status': 'error', 'message': 'Price must be a number, zero or higher.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT service_name FROM services WHERE service_id = ?", (service_id,))
    service_row = cursor.fetchone()
    if not service_row:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Unknown service.'}), 404
    cursor.execute("SELECT 1 FROM vehicle_types WHERE type_name = ?", (vehicle_type,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'status': 'error', 'message': f"Unknown vehicle size '{vehicle_type}'."}), 400

    cursor.execute(
        """
        INSERT INTO service_prices (service_id, vehicle_type, price) VALUES (?, ?, ?)
        ON CONFLICT(service_id, vehicle_type) DO UPDATE SET price = excluded.price
        """,
        (service_id, vehicle_type, price),
    )
    conn.commit()
    conn.close()

    log_activity(session.get('user', 'Admin'), f"Updated price: {service_row['service_name']} ({vehicle_type}) -> PHP {price}")
    return jsonify({'status': 'success', 'message': 'Price updated.'}), 200


@admin_api_bp.route('/api/admin/toggle-service-active', methods=['POST'])
@role_required('admin')
@owner_only
def api_admin_toggle_service_active():
    """Retires or reactivates a service from new bookings - never deletes
    one, since past reservations/feedback may still reference it by
    foreign key (see database.py's migration comment on the 3 original
    services for exactly this scenario)."""
    data = request.get_json() or {}
    service_id = data.get('service_id')
    is_active = data.get('is_active')

    if not service_id or is_active is None:
        return jsonify({'status': 'error', 'message': 'service_id and is_active are required.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT service_name FROM services WHERE service_id = ?", (service_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Unknown service.'}), 404

    cursor.execute("UPDATE services SET is_active = ? WHERE service_id = ?", (1 if is_active else 0, service_id))
    conn.commit()
    conn.close()

    log_activity(session.get('user', 'Admin'), f"{'Activated' if is_active else 'Deactivated'} service: {row['service_name']}")
    return jsonify({'status': 'success', 'message': 'Service updated.'}), 200


@admin_api_bp.route('/api/admin/add-service', methods=['POST'])
@role_required('admin')
@owner_only
def api_admin_add_service():
    """Adds a brand-new service to the catalog with a starting price for
    every vehicle size - the same shape /api/services and the booking form
    already expect, so a newly-added service is immediately bookable with
    no other change needed anywhere."""
    data = request.get_json() or {}
    service_name = (data.get('service_name') or '').strip()
    service_category = (data.get('service_category') or '').strip()
    default_bay = (data.get('default_bay') or '').strip()
    base_duration_minutes = data.get('base_duration_minutes')
    prices = data.get('prices') or {}

    if not service_name:
        return jsonify({'status': 'error', 'message': 'Service name is required.'}), 400
    if service_category not in _VALID_CATEGORIES:
        return jsonify({'status': 'error', 'message': f"service_category must be one of: {', '.join(_VALID_CATEGORIES)}."}), 400
    if default_bay not in _VALID_BAYS:
        return jsonify({'status': 'error', 'message': f"default_bay must be one of: {', '.join(_VALID_BAYS)}."}), 400
    try:
        base_duration_minutes = int(base_duration_minutes)
        if base_duration_minutes <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'base_duration_minutes must be a positive whole number.'}), 400
    if not prices or not isinstance(prices, dict):
        return jsonify({'status': 'error', 'message': 'At least one vehicle-size price is required.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT type_name FROM vehicle_types")
    valid_sizes = {row['type_name'] for row in cursor.fetchall()}
    clean_prices = {}
    for size, price in prices.items():
        if size not in valid_sizes:
            conn.close()
            return jsonify({'status': 'error', 'message': f"Unknown vehicle size '{size}'."}), 400
        clean_prices[size] = _parse_price(price)
        if clean_prices[size] is None:
            conn.close()
            return jsonify({'status': 'error', 'message': f"Price for '{size}' must be a number, zero or higher."}), 400

    cursor.execute("SELECT 1 FROM services WHERE service_name = ?", (service_name,))
    if cursor.fetchone():
        conn.close()
        return jsonify({'status': 'error', 'message': 'A service with that name already exists.'}), 400

    first_price = next(iter(clean_prices.values()))
    cursor.execute(
        """
        INSERT INTO services (service_name, service_type, base_duration_minutes, price, is_active, service_category, default_bay)
        VALUES (?, ?, ?, ?, 1, ?, ?)
        """,
        (service_name, service_category, base_duration_minutes, first_price, service_category, default_bay),
    )
    service_id = cursor.lastrowid
    for size, price in clean_prices.items():
        cursor.execute(
            "INSERT INTO service_prices (service_id, vehicle_type, price) VALUES (?, ?, ?)",
            (service_id, size, price),
        )
    conn.commit()
    conn.close()

    log_activity(session.get('user', 'Admin'), f"Added new service: {service_name} ({service_category}, {default_bay})")
    return jsonify({'status': 'success', 'message': f'{service_name} added to the catalog.', 'service_id': service_id}), 200


# --- CAR MODEL LIST (admin-only) ---
# The models the booking forms suggest (see vehicles.py). A model typed
# that isn't listed is kept as typed and charts under "Other" - this is
# where admin tidies those up: list it as its own model, or merge it into
# the listed model it really is.

@admin_api_bp.route('/api/admin/vehicle-models', methods=['GET'])
@role_required('admin')
@owner_only
def api_admin_get_vehicle_models():
    """The listed models, and every model on a booking that isn't listed -
    each with how many bookings carry it."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT vehicle_model, COUNT(*) AS bookings FROM reservations WHERE vehicle_model IS NOT NULL GROUP BY vehicle_model"
    )
    bookings_by_model = {row['vehicle_model']: row['bookings'] for row in cursor.fetchall()}
    cursor.execute("SELECT model_id, model_name FROM vehicle_models ORDER BY model_name")
    listed = [dict(row) for row in cursor.fetchall()]
    conn.close()

    listed_names = {model['model_name'].lower() for model in listed}
    for model in listed:
        model['bookings'] = bookings_by_model.get(model['model_name'], 0)
    unlisted = sorted(
        ({'model_name': name, 'bookings': count} for name, count in bookings_by_model.items() if name.lower() not in listed_names),
        key=lambda model: (-model['bookings'], model['model_name']),
    )
    return jsonify({'status': 'success', 'listed': listed, 'unlisted': unlisted}), 200


@admin_api_bp.route('/api/admin/add-vehicle-model', methods=['POST'])
@role_required('admin')
@owner_only
def api_admin_add_vehicle_model():
    data = request.get_json() or {}
    model_name = ' '.join((data.get('model_name') or '').split())[:MAX_MODEL_LENGTH]
    if not model_name:
        return jsonify({'status': 'error', 'message': 'Model name is required.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM vehicle_models WHERE LOWER(model_name) = LOWER(?)", (model_name,))
    if cursor.fetchone():
        conn.close()
        return jsonify({'status': 'error', 'message': 'That model is already on the list.'}), 400
    cursor.execute("INSERT INTO vehicle_models (model_name) VALUES (?)", (model_name,))
    # Bookings that typed it in a different case now carry the listed spelling.
    cursor.execute("UPDATE reservations SET vehicle_model = ? WHERE LOWER(vehicle_model) = LOWER(?)", (model_name, model_name))
    conn.commit()
    conn.close()

    log_activity(session.get('user', 'Admin'), f'Added car model: {model_name}')
    return jsonify({'status': 'success', 'message': f'{model_name} added to the model list.'}), 200


@admin_api_bp.route('/api/admin/delete-vehicle-model', methods=['POST'])
@role_required('admin')
@owner_only
def api_admin_delete_vehicle_model():
    """Takes a model off the suggestion list. Bookings that already carry
    it keep it (and chart under "Other" from then on)."""
    data = request.get_json() or {}
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT model_name FROM vehicle_models WHERE model_id = ?", (data.get('model_id'),))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Unknown model.'}), 404
    cursor.execute("DELETE FROM vehicle_models WHERE model_id = ?", (data.get('model_id'),))
    conn.commit()
    conn.close()

    log_activity(session.get('user', 'Admin'), f"Removed car model: {row['model_name']}")
    return jsonify({'status': 'success', 'message': f"{row['model_name']} removed from the model list."}), 200


@admin_api_bp.route('/api/admin/merge-vehicle-model', methods=['POST'])
@role_required('admin')
@owner_only
def api_admin_merge_vehicle_model():
    """Re-labels every booking carrying an unlisted model (e.g. 'Vios 2020')
    as the listed model it really is."""
    data = request.get_json() or {}
    from_name = (data.get('from_name') or '').strip()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT model_name FROM vehicle_models WHERE model_id = ?", (data.get('into_model_id'),))
    target = cursor.fetchone()
    if not from_name or not target:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Pick the model to merge and the listed model to merge it into.'}), 400
    cursor.execute("UPDATE reservations SET vehicle_model = ? WHERE LOWER(vehicle_model) = LOWER(?)", (target['model_name'], from_name))
    moved = cursor.rowcount
    conn.commit()
    conn.close()
    if moved == 0:
        return jsonify({'status': 'error', 'message': f'No bookings are recorded as "{from_name}".'}), 404

    log_activity(session.get('user', 'Admin'), f"Merged car model '{from_name}' into {target['model_name']} ({moved} booking(s))")
    return jsonify({'status': 'success', 'message': f"{moved} booking(s) now recorded as {target['model_name']}."}), 200
