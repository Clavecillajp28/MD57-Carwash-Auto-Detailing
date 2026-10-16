import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from functools import wraps

from flask import session, request, jsonify, redirect, url_for, has_request_context, current_app
from flask_mail import Message
from markupsafe import escape

from config import ACTIVITY_LOG_FILE, LOGO_PATH
from database import get_db_connection
from extensions import mail
from live_updates import publish


# --- BRANDED EMAIL ---
_LOGO_CID = 'md57_logo'


def send_branded_email(subject, recipient, heading, plain_text_body, highlight=None, **options):
    from mail_outbox import send
    return send(subject,recipient,heading,plain_text_body,highlight,**options)


def _deliver_branded_email(subject, recipient, heading, plain_text_body, highlight=None, message_id=None):
    """Every customer-facing email (ready notifications, no-show releases,
    reset codes): an HTML layout carrying the MD57 logo,
    with the plain-text version kept as the `body` fallback for clients
    that don't render HTML. The logo is attached inline via Content-ID
    (cid:) rather than linked to this app's own /static/ URL, so it
    displays correctly in the recipient's inbox even when this app isn't
    publicly reachable (e.g. running on localhost during development).
    `highlight`, if given, renders as a large bold letter-spaced box (e.g.
    an OTP code) between the heading and the body paragraphs."""
    paragraphs = [p.strip() for p in plain_text_body.split('\n\n') if p.strip()]
    html_paragraphs = ''.join(
        f'<p style="margin:0 0 14px 0; color:#334155; font-size:14px; line-height:1.6; white-space:pre-line;">{escape(p)}</p>'
        for p in paragraphs
    )
    highlight_html = ''
    if highlight:
        highlight_html = (
            '<div style="margin:0 0 18px 0; padding:16px; background:#f8fafc; border:1px dashed #cbd5e1; '
            'border-radius:8px; text-align:center;">'
            f'<span style="font-size:28px; font-weight:700; letter-spacing:8px; color:#9c7a2e; '
            f'font-family:\'Courier New\',monospace;">{escape(highlight)}</span>'
            '</div>'
        )
    html_body = f"""
    <div style="max-width:480px; margin:0 auto; font-family:'Segoe UI',Arial,sans-serif; background:#f1f3f6; padding:24px;">
        <div style="background:#0d1b30; border-radius:12px 12px 0 0; padding:22px; text-align:center;">
            <img src="cid:{_LOGO_CID}" alt="MD57 Car Wash" width="64" height="64" style="width:64px; height:64px; border-radius:10px; object-fit:contain; background:#ffffff; padding:4px; display:inline-block;">
            <h1 style="margin:10px 0 0 0; color:#f3f0e8; font-size:16px; font-weight:700;">MD57 Car Wash &amp; Auto Detailing</h1>
        </div>
        <div style="background:#ffffff; border:1px solid #e2e8f0; border-top:none; border-radius:0 0 12px 12px; padding:24px;">
            <h2 style="margin:0 0 14px 0; color:#0d1b30; font-size:18px; font-weight:700;">{escape(heading)}</h2>
            {highlight_html}
            {html_paragraphs}
        </div>
        <p style="text-align:center; color:#94a3b8; font-size:11px; margin-top:16px;">MD57 Car Wash &amp; Auto Detailing &bull; This is an automated message.</p>
    </div>
    """

    transport = current_app.config.get('MAIL_TRANSPORT', 'smtp')
    if transport == 'brevo':
        from email_transport import send_brevo
        # Keep API email independent of attachments and public asset URLs.
        # The text brand heading above remains when the local CID logo is removed.
        import re
        html_body = re.sub(r'<img\b[^>]*>', '', html_body)
        return send_brevo(subject, recipient, plain_text_body, html_body, message_id)
    if transport != 'smtp':
        raise RuntimeError('MAIL_TRANSPORT must be smtp or brevo.')
    msg = Message(subject=subject, recipients=[recipient], body=plain_text_body, html=html_body)
    if message_id:
        msg.msgId=message_id
    try:
        with open(LOGO_PATH, 'rb') as f:
            msg.attach('md57logo.jpg', 'image/jpeg', f.read(), headers={'Content-ID': f'<{_LOGO_CID}>'})
    except OSError as e:
        print(f"[WARNING] Could not attach logo to email: {e}")
    mail.send(msg)


# Display order/labels for the 3 published pricing categories - shared by
# /api/services (booking form + Pricing modal) and the analytics service-mix
# chart, so a category never renders in a different order or under a
# different label in one place than another.
SERVICE_CATEGORY_LABELS = [
    ('general', 'Car Wash & General'),
    ('detailing', 'Detailing'),
    ('motorcycle', 'Motorcycle'),
]


# No car wash service plausibly takes longer than this - shared by every
# place that turns a created_at/completed_at (or started_at/completed_at)
# pair into a duration and needs to reject corrupt/implausible rows: the
# analytics wait-time chart, per-staff speed scoring, and the AI retraining
# pipeline's data fetch and accuracy calculation.
MAX_PLAUSIBLE_SERVICE_MINUTES = 480


def parse_duration_minutes(start_str, end_str):
    """Parses two 'YYYY-MM-DD HH:MM:SS' timestamps and returns the minutes
    between them, or None if either is missing/malformed or the result
    isn't plausible (negative, or over MAX_PLAUSIBLE_SERVICE_MINUTES) -
    callers treat None as "exclude this row" rather than special-casing
    each failure mode themselves."""
    if not start_str or not end_str:
        return None
    try:
        start_dt = datetime.strptime(start_str, '%Y-%m-%d %H:%M:%S')
        end_dt = datetime.strptime(end_str.strip(), '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, AttributeError):
        return None

    diff = (end_dt - start_dt).total_seconds() / 60
    if diff < 0 or diff > MAX_PLAUSIBLE_SERVICE_MINUTES:
        return None
    return diff


def mask_plate(plate):
    """Reduces a plate number to a masked tail (e.g. 'ABC-1234' -> '****234')
    for any viewer who isn't staff/admin/the booking's own customer - a
    shared 3-character-tail convention so the queue board and the public
    feedback wall mask PII identically instead of drifting independently."""
    plate = plate or ''
    return ('*' * max(0, len(plate) - 3)) + plate[-3:] if len(plate) > 3 else plate


def start_periodic_worker(app, job, interval_seconds, name, shared=True):
    from background_jobs import start_periodic_worker as start
    return start(app, job, interval_seconds, name, shared=shared)


def now_str():
    """The current local time as 'YYYY-MM-DD HH:MM:SS' - how this app stores
    every timestamp. Passed explicitly on insert instead of relying on a
    column's DEFAULT CURRENT_TIMESTAMP, which SQLite evaluates in UTC."""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# --- ACTIVITY LOGGING FUNCTION (Audit Trail) ---
def log_activity(username, action):
    actor_id=session.get('user_id') if has_request_context() else None
    actor_name=(session.get('fullname') or username) if has_request_context() else username
    if has_request_context() and session.get('user_id') and username == session.get('user'):
        name = session.get('fullname') or username
        action = f'{action} [by {name}; account #{session["user_id"]}]'
    username = str(username).replace('\r', ' ').replace('\n', ' ')
    action = str(action).replace('\r', ' ').replace('\n', ' ')
    timestamp = now_str()
    conn=get_db_connection()
    try:
        conn.execute('''INSERT INTO audit_records(occurred_at,actor_id,actor_name,entity,action)
            VALUES (?,?,?,'activity',?)''',(timestamp,actor_id,str(actor_name),action))
        conn.commit()
    finally:
        conn.close()
    log_entry = f'[{timestamp}] User: {username} - Action: {action}\n'
    try:
        with open(ACTIVITY_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_entry)
    except Exception as e:
        print(f'Error writing log: {e}')


# --- WEATHER LOOKUPS (server-side, so WEATHER_API_KEY never reaches a browser) ---
# Every public function below returns None on any failure (missing key,
# network issue, bad response) - a weather hiccup must never block a
# booking or break a dashboard.
def _openweather_get(endpoint, timeout):
    """GETs OpenWeatherMap's /data/2.5/<endpoint> for WEATHER_CITY, or
    returns None when no API key is configured."""
    api_key = os.environ.get('WEATHER_API_KEY')
    if not api_key:
        return None
    city = os.environ.get('WEATHER_CITY', 'Malabon')
    # ",PH" pins this to the Philippines - without a country code, the
    # city-name lookup can resolve to a same-named city elsewhere.
    query = urllib.parse.urlencode({'q': f'{city},PH', 'appid': api_key, 'units': 'metric'})
    url = f'https://api.openweathermap.org/data/2.5/{endpoint}?{query}'
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _bucket_weather(reading):
    """Buckets one OpenWeatherMap reading into 'Sunny', 'Cloudy', or 'Rainy'
    for analytics, or None if it carries no condition."""
    main = reading.get('weather', [{}])[0].get('main', '').lower()
    if main in ('rain', 'drizzle', 'thunderstorm'):
        return 'Rainy'
    if main == 'clear':
        return 'Sunny'
    return 'Cloudy' if main else None


# Every wait-time prediction and booking reads the current weather; one
# lookup is shared for this long instead of calling the API each time.
WEATHER_CACHE_SECONDS = 5 * 60
_weather_cache = {'at': 0.0, 'reading': None}


def get_weather_reading():
    """Current conditions as {'condition': 'Sunny'|'Cloudy'|'Rainy'|None,
    'rain_mm': rainfall in the last hour (0.0 if none or unknown)} - stored
    on each booking and fed to the wait-time model."""
    now = time.time()
    if _weather_cache['reading'] is not None and now - _weather_cache['at'] < WEATHER_CACHE_SECONDS:
        return _weather_cache['reading']
    reading = {'condition': None, 'rain_mm': 0.0}
    try:
        payload = _openweather_get('weather', timeout=3)
        if payload:
            reading = {
                'condition': _bucket_weather(payload),
                'rain_mm': float((payload.get('rain') or {}).get('1h', 0.0)),
            }
    except Exception as e:
        print(f'[WARNING] Weather lookup failed, using no weather data: {e}')
    _weather_cache.update(at=now, reading=reading)
    return reading


def get_weather_snapshot():
    """Full current conditions (temperature, description, wind, humidity,
    pressure, visibility) for manager_analytics.html's live weather widget."""
    try:
        payload = _openweather_get('weather', timeout=4)
        if not payload:
            return None

        main = payload.get('main', {})
        wind = payload.get('wind', {})
        weather_list = payload.get('weather', [{}])
        visibility_m = payload.get('visibility')

        return {
            'temp_c': round(main.get('temp')) if main.get('temp') is not None else None,
            'humidity': main.get('humidity'),
            'pressure': main.get('pressure'),
            'wind_speed': wind.get('speed'),
            'condition': weather_list[0].get('description') if weather_list else None,
            'visibility_km': round(visibility_m / 1000, 1) if visibility_m is not None else None,
            'rain_mm': (payload.get('rain') or {}).get('1h', 0.0),
        }
    except Exception as e:
        print(f'[WARNING] Weather snapshot lookup failed: {e}')
        return None


# --- IN-APP NOTIFICATION HELPERS ---
# Independent of email/Gmail delivery on purpose (send_branded_email above) -
# a user who opted into email reminders still gets one of these rows too,
# and a mail-server outage never prevents an in-app notification from being
# recorded. Every call here is fire-and-forget: it opens/closes its own
# short-lived connection and never raises, so a notification failure can
# never break the booking/attendance/queue action that triggered it.
def create_notification(user_id, title, message, category='general', related_reservation_id=None):
    """Inserts one in-app notification for exactly one account. user_id must
    be a real users.user_id - callers that only have a display name (e.g.
    staff_name) need to resolve it to a user_id first."""
    if not user_id:
        return
    try:
        conn = get_db_connection()
        # created_at is set here in local time - the column's DEFAULT
        # CURRENT_TIMESTAMP is UTC, and the bell's "5m ago" label reads
        # this as local time like every other timestamp in the app.
        conn.execute(
            """
            INSERT INTO notifications (user_id, title, message, category, related_reservation_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, title, message, category, related_reservation_id, now_str()),
        )
        conn.commit()
        conn.close()
        publish('notifications', [user_id])
    except Exception as e:
        print(f"[WARNING] Failed to create notification for user {user_id}: {e}")


def notify_role(role, title, message, category='general', related_reservation_id=None, exclude_user_id=None):
    """Fans a notification out to every account with the given role - the
    admin-wide/staff-wide broadcast case ('New Booking', 'Staff Attendance',
    etc.). exclude_user_id skips the acting user's own account (e.g. an
    admin who just performed the action doesn't need to be told about it)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE role = ? AND disabled_at IS NULL", (role,))
        user_ids = [row['user_id'] for row in cursor.fetchall() if row['user_id'] != exclude_user_id]
        conn.close()
    except Exception as e:
        print(f"[WARNING] Failed to look up {role} accounts for notification fan-out: {e}")
        return
    for uid in user_ids:
        create_notification(uid, title, message, category, related_reservation_id)


# --- ROBUST SERVER-SIDE RBAC DECORATOR ---
def role_required(*allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user' not in session:
                if request.path.startswith('/api/'):
                    return jsonify({'status': 'error', 'message': 'Authentication required. Please log in.'}), 401
                # A site-relative path, so /login can send the user back
                # here without ever redirecting to another site.
                return redirect(url_for('auth.login_page', next=request.full_path.rstrip('?')))

            # An account the owner has disabled loses its session on its
            # next request, not only at its next sign-in.
            if session.get('user_id') is not None and account_disabled(session['user_id']):
                session.clear()
                if request.path.startswith('/api/'):
                    return jsonify({'status': 'error', 'message': 'This account has been disabled.'}), 401
                return redirect(url_for('auth.login_page', error='This account has been disabled. Please contact the owner.'))

            user_role = session.get('role')
            if not user_role or user_role not in allowed_roles:
                if request.path.startswith('/api/'):
                    return jsonify({
                        'status': 'error',
                        'message': f'Access denied: {user_role} role is not permitted for this resource.'
                    }), 403
                return redirect(get_redirect_for_role(user_role))

            return f(*args, **kwargs)
        return decorated_function
    return decorator


def is_owner():
    """Ownership is granted explicitly, independently of manager access."""
    return session.get('role') == 'admin' and bool(session.get('is_owner'))


def owner_only(f):
    """The owner's controls: prices and services, admin accounts, disabling
    accounts, the backup. Use under @role_required('admin')."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_owner():
            if request.path.startswith('/api/'):
                return jsonify({'status': 'error', 'message': 'Only the owner can do this.'}), 403
            return redirect(url_for('pages.home'))
        if request.endpoint!='mfa.security_api' and (request.method not in ('GET','HEAD','OPTIONS') or request.path=='/api/admin/backup' or '/backups/' in request.path):
            from blueprints.mfa_api import require_recent
            challenge=require_recent()
            if challenge: return challenge
        return f(*args, **kwargs)
    return decorated_function


def account_disabled(user_id):
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT disabled_at FROM users WHERE user_id = ?", (user_id,)).fetchone()
        conn.close()
    except Exception:
        return True
    return not row or bool(row['disabled_at'])


def get_redirect_for_role(role):
    if role == 'admin':
        return url_for('pages.home')  # the overview; Analytics is one click away
    elif role == 'staff':
        return url_for('pages.queue_page')
    else:
        return url_for('pages.home')


def is_own_booking(user_id, customer_email):
    """Whether a reservation belongs to the logged-in session: booked from
    this account, or entered by staff under this account's email. The one
    ownership rule behind My Bookings, the queue's masking, and a
    customer's cancel/reschedule/feedback."""
    session_email = (session.get('user') or '').lower()
    return bool(
        (user_id is not None and user_id == session.get('user_id'))
        or (user_id is None and customer_email and customer_email.lower() == session_email)
    )


def current_staff_name():
    """The session's display name, in the same COALESCE(fullname, username)
    form staff_attendance/staff_shifts/staff_unavailability store."""
    return session.get('fullname') or session.get('user')


def get_latest_attendance_by_staff(date_str=None):
    """One row per staff account for the given date (defaults to today): the
    row that's "true right now". staff_attendance is insert-only (never
    updated in place), so the highest attendance_id wins - SQLite fills the
    bare columns from that MAX() row. staff_name is the account's current
    display name."""
    if date_str is None:
        date_str = datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT a.staff_user_id, COALESCE(u.fullname, u.username) AS staff_name,
               a.date_str, a.status, a.time_in, a.time_out, MAX(a.attendance_id) AS latest_id
        FROM staff_attendance a
        JOIN users u ON u.user_id = a.staff_user_id
        WHERE a.date_str = ? AND a.is_synthetic=0 AND u.disabled_at IS NULL
        GROUP BY a.staff_user_id
        """,
        (date_str,),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_latest_attendance(staff_user_id, date_str=None):
    """Same as get_latest_attendance_by_staff, narrowed to one staff account.
    Returns None if they have no row at all for the day."""
    if date_str is None:
        date_str = datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT status, time_in, time_out, MAX(attendance_id) AS latest_id
        FROM staff_attendance
        WHERE date_str = ? AND staff_user_id = ? AND is_synthetic=0
        GROUP BY staff_user_id
        """,
        (date_str, staff_user_id),
    )
    row = cursor.fetchone()
    conn.close()
    return row


def is_on_duty_record(row):
    """The one definition of "on duty": clocked in (Present/Late) and not
    yet clocked out. Mirrored client-side by staff.html's
    renderAvailableStaff()."""
    return bool(row) and row['status'] in ('Present', 'Late') and row['time_out'] == '--:--'


def get_on_duty_staff_ids(date_str=None):
    return {row['staff_user_id'] for row in get_latest_attendance_by_staff(date_str) if is_on_duty_record(row)}


def get_active_staff_count(date_str=None):
    return len(get_on_duty_staff_ids(date_str))


def is_date_staffed(date_str):
    """Whether a customer can be served on date_str: at least one staff
    member is on the admin's shift roster for that day without having
    marked themselves unavailable - or, for today only, someone is already
    clocked in (staff can be on duty without a roster entry)."""
    conn = get_db_connection()
    rostered = conn.execute(
        """
        SELECT 1 FROM staff_shifts s
        WHERE s.shift_date = ? AND s.staff_user_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM staff_unavailability u
              WHERE u.staff_user_id = s.staff_user_id AND u.unavailable_date = s.shift_date
          )
        LIMIT 1
        """,
        (date_str,),
    ).fetchone()
    conn.close()
    if rostered:
        return True
    return date_str == datetime.now().strftime('%Y-%m-%d') and get_active_staff_count() > 0


# Statuses in which a booking still holds its bay (see scheduling.py).
ACTIVE_BOOKING_STATUSES = ('waiting', 'in_progress', 'pending_confirmation')


def _read_booking_max_days_ahead():
    raw = os.environ.get('BOOKING_MAX_DAYS_AHEAD', '').strip()
    try:
        return max(int(raw), 0) if raw else None
    except ValueError:
        return None


# How many days ahead a customer may book online: 0 = today only, 1 = today
# and tomorrow, and so on. None (the setting left out - the default) = no
# limit. Staff and admin booking for a customer are never limited, and
# neither is the system moving a booking because no staff were on duty.
BOOKING_MAX_DAYS_AHEAD = _read_booking_max_days_ahead()


def last_customer_booking_date():
    """The latest 'YYYY-MM-DD' a customer may book or reschedule to, or
    None when there is no limit."""
    if BOOKING_MAX_DAYS_AHEAD is None:
        return None
    return (datetime.now() + timedelta(days=BOOKING_MAX_DAYS_AHEAD)).strftime('%Y-%m-%d')


def booking_window_message():
    if BOOKING_MAX_DAYS_AHEAD == 0:
        return 'Online booking is open for today only.'
    return f'Online booking is open up to {BOOKING_MAX_DAYS_AHEAD} day(s) ahead.'


def is_staff_on_duty(staff_user_id, date_str=None):
    """Whether this staff member is clocked in (a job can only be started
    by someone who is)."""
    return is_on_duty_record(get_latest_attendance(staff_user_id, date_str))


# MD57 operates 8:00 AM - 9:00 PM. Kept as shared constants (not scattered
# literals) since both the booking-time validation and the walk-in check
# below need to agree on exactly the same window.
BUSINESS_OPEN_HOUR = 8
BUSINESS_CLOSE_HOUR = 21  # 9:00 PM, inclusive - the last bookable hour


def is_within_business_hours(dt):
    """True if dt's time-of-day (its date is ignored) falls within MD57's
    8:00 AM - 9:00 PM operating window, inclusive of both ends - 9:00 PM
    itself is still a valid time, matching the last slot the booking UI
    offers."""
    minutes = dt.hour * 60 + dt.minute
    return (BUSINESS_OPEN_HOUR * 60) <= minutes <= (BUSINESS_CLOSE_HOUR * 60)


# Staff clock in from an hour before opening (to be ready at 8:00) until
# closing. Outside that, nobody is on shift to clock in.
CLOCK_IN_EARLY_MINUTES = 60
CLOCK_IN_HOURS_TEXT = '7:00 AM to 9:00 PM'


def is_clock_in_open(dt):
    minutes = dt.hour * 60 + dt.minute
    # up to, not including, closing: at 9:00 PM the day's attendance is closed out (attendance_close.py)
    return (BUSINESS_OPEN_HOUR * 60 - CLOCK_IN_EARLY_MINUTES) <= minutes < (BUSINESS_CLOSE_HOUR * 60)


# How booking_time is stored - 'HH:MM AM', the same text the booking
# times are offered in (scheduling.slot_times).
BOOKING_TIME_FORMAT = '%I:%M %p'


def parse_booking_datetime(booking_date, booking_time):
    """Parses a reservation's stored booking_date ('YYYY-MM-DD') and
    booking_time ('HH:MM AM/PM' from the booking UI, or 'HH:MM' from any
    24h-format source) into a single datetime. Returns None if either value
    is missing or doesn't match a known format, rather than raising -
    callers should treat that as "can't determine a scheduled time" and
    decide what that means for them."""
    if not booking_date or not booking_time:
        return None
    for fmt in ('%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(f'{booking_date} {booking_time}', fmt)
        except (ValueError, TypeError):
            continue
    return None
