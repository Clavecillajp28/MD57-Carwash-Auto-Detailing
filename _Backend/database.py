import os
import sqlite3
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash
from password_policy import password_error

from config import DB_FILE


# Shared by the initial CREATE and the one-time ON DELETE SET NULL rebuild
# in init_db(), so the two can't drift apart.
_NOTIFICATIONS_TABLE_COLUMNS = """
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    related_reservation_id INTEGER,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (related_reservation_id) REFERENCES reservations(reservation_id) ON DELETE SET NULL
"""


def _column_names(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return [col[1] for col in cursor.fetchall()]


def get_db_connection():
    # timeout=30 (SQLite's own internal retry window, not a Python-level
    # wait) makes a writer retry for up to 30s instead of raising "database
    # is locked" on the first collision - matters most for this project's
    # db file living inside a OneDrive-synced folder, where the sync
    # client can transiently hold its own lock on the file.
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from flask import has_request_context, session, request
    conn.create_function('audit_actor_id', 0, lambda: session.get('user_id') if has_request_context() else None)
    conn.create_function('audit_actor_name', 0, lambda: (session.get('fullname') or session.get('user') or 'System') if has_request_context() else 'System')
    def audit_reason():
        if not has_request_context(): return None
        data=request.get_json(silent=True) if request.is_json else request.form
        reason=data.get('reason') if hasattr(data,'get') else None
        return reason[:500].replace('\r',' ').replace('\n',' ') if isinstance(reason,str) else None
    conn.create_function('audit_reason',0,audit_reason)
    return conn


def init_db():
    print(f"Initializing Database at: {DB_FILE}")
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'staff', 'customer')),
            fullname TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    user_columns = _column_names(cursor, 'users')
    if "fullname" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN fullname TEXT;")
    if "created_at" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP;")
    # Marks accounts created via "Sign in with Google" (auth.py's
    # google_auth_callback) so password-based /login can refuse them
    # outright, rather than relying solely on the random per-account
    # password those accounts get instead of a real one.
    if "auth_provider" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN auth_provider TEXT;")
    # Owner-managed employee recovery contact. Reminders use their own
    # separate preference below and cannot redirect password recovery.
    if "notify_email" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN notify_email TEXT;")
    # Reminder preferences must never change an account's recovery address.
    if 'reminder_email' not in user_columns:
        cursor.execute('ALTER TABLE users ADD COLUMN reminder_email TEXT')
        cursor.execute("UPDATE users SET reminder_email=notify_email WHERE role='staff'")
    if 'profile_updated_at' not in user_columns:
        cursor.execute('ALTER TABLE users ADD COLUMN profile_updated_at TEXT')

    # 1 once the account's email address is known to belong to whoever
    # holds it: it was created through the emailed registration code
    # (auth.py's register flow) or by Google sign-in. Accounts from before
    # that existed, and ones an admin creates, stay 0.
    if "email_verified" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0;")
        cursor.execute("UPDATE users SET email_verified = 1 WHERE auth_provider = 'google'")

    # When the customer agreed to the Privacy Policy (Data Privacy Act,
    # RA 10173): ticked on the register form, or by continuing with Google.
    # NULL for accounts an admin creates and ones from before this existed.
    if "privacy_consent_at" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN privacy_consent_at TIMESTAMP;")

    # Retained solely to retire legacy shared accounts without losing IDs
    # referenced by history. It is not a supported account role.
    if "front_desk" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN front_desk INTEGER NOT NULL DEFAULT 0;")

    # Set when the owner disables an account (someone who left): it can't
    # sign in, and a disabled washer is never assigned a car. Its records stay.
    if "disabled_at" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN disabled_at TIMESTAMP;")

    # Owner is an explicit privilege on an admin account; managers no longer
    # inherit ownership merely because they are not a front-desk account.
    for column, ddl in (('is_owner', 'INTEGER NOT NULL DEFAULT 0'),
                        ('session_version', 'INTEGER NOT NULL DEFAULT 0')):
        if column not in user_columns:
            cursor.execute(f'ALTER TABLE users ADD COLUMN {column} {ddl}')
    cursor.execute("""CREATE TABLE IF NOT EXISTS employee_invitations (
        invitation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL, fullname TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('owner','admin','staff')),
        user_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
        created_by INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
        token_hash TEXT NOT NULL UNIQUE, session_version INTEGER,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        used_at TEXT, revoked_at TEXT)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS profile_email_changes (
        user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
        email TEXT NOT NULL, code_hash TEXT NOT NULL, nonce_hash TEXT NOT NULL,
        session_version INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL)""")
    # Covers every password/access update, including self-service resets and
    # future code paths. Re-enabling an account never resurrects old cookies.
    cursor.execute("""CREATE TRIGGER IF NOT EXISTS users_revoke_sessions
        AFTER UPDATE OF password_hash, role, is_owner, disabled_at ON users
        WHEN OLD.password_hash IS NOT NEW.password_hash
          OR OLD.role IS NOT NEW.role OR OLD.is_owner IS NOT NEW.is_owner
          OR OLD.disabled_at IS NOT NEW.disabled_at
        BEGIN
            UPDATE users SET session_version = session_version + 1
            WHERE user_id = NEW.user_id;
        END""")

    # A registration that hasn't proven its email yet. Nothing here is an
    # account: the users row is only created once the emailed code is
    # entered - from the same browser that asked for it (session_token).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_registrations (
            pending_id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            fullname TEXT,
            password_hash TEXT NOT NULL,
            code TEXT NOT NULL,
            session_token TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            sent_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS services (
            service_id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_name TEXT NOT NULL,
            service_type TEXT NOT NULL,
            base_duration_minutes INTEGER NOT NULL,
            price REAL NOT NULL
        )
    """)

    service_columns = _column_names(cursor, 'services')
    # is_active lets the real 2026-09 service catalog expansion (see
    # SERVICE_CATALOG below) retire the 3 original generic services
    # (Body Wash/Express & Foam/Full Body Wash & Detailing) from new
    # bookings without deleting them - existing reservations/feedback still
    # reference their service_id by foreign key, and History/CSV/PDF exports
    # need that row to keep resolving correctly for old records.
    if "is_active" not in service_columns:
        cursor.execute("ALTER TABLE services ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;")
    # Which of the 3 physical bays this service is actually performed in -
    # independent of service_category below (that's about how a customer
    # *finds* a service in the Pricing page/booking dropdown; this is about
    # which real bay the queue routes it to). Defaults every pre-existing
    # row to Bay #1 only so nothing is ever NULL; the actual legacy-service
    # values are set explicitly below.
    if "default_bay" not in service_columns:
        cursor.execute("ALTER TABLE services ADD COLUMN default_bay TEXT NOT NULL DEFAULT 'Bay #1';")
    # 'general' | 'detailing' | 'motorcycle' - purely a display/grouping
    # label for the Pricing page and the booking form's service dropdown
    # (matches the shop's own published price list categories). Distinct
    # from the legacy service_type column, which nothing reads anymore (the
    # wait-time model uses base_duration_minutes directly).
    if "service_category" not in service_columns:
        cursor.execute("ALTER TABLE services ADD COLUMN service_category TEXT NOT NULL DEFAULT 'general';")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vehicle_types (
            type_id INTEGER PRIMARY KEY AUTOINCREMENT,
            type_name TEXT NOT NULL UNIQUE,
            multiplier_minutes INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Per-service, per-vehicle-size price - the real fix for "Pricing shows
    # prices for services a customer can't actually book" (the 3 original
    # services only ever had one flat price each, with no size tiers at
    # all). A service with the same price regardless of vehicle size (e.g.
    # Back 2 Zero) simply gets an identical row for every vehicle_type
    # rather than a special "any size" sentinel, so price lookup is always
    # the same one query regardless of which kind of service it is.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_prices (
            service_id INTEGER NOT NULL,
            vehicle_type TEXT NOT NULL,
            price REAL NOT NULL,
            PRIMARY KEY (service_id, vehicle_type),
            FOREIGN KEY (service_id) REFERENCES services(service_id),
            FOREIGN KEY (vehicle_type) REFERENCES vehicle_types(type_name)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            customer_name TEXT,
            vehicle_plate_number TEXT NOT NULL,
            vehicle_type TEXT NOT NULL,
            service_id INTEGER,
            customer_email TEXT,
            queue_number INTEGER,
            queue_status TEXT DEFAULT 'waiting',
            booking_source TEXT DEFAULT 'Walk-in Customer',
            assigned_bay TEXT DEFAULT 'Bay #1',
            booking_date TEXT,
            booking_time TEXT,
            cancelled_at TEXT,
            cancel_reason TEXT,
            completed_at TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (service_id) REFERENCES services(service_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    res_columns = _column_names(cursor, 'reservations')
    # Synthetic history may populate the real UI, but must never become
    # evidence for retraining on real customer observations.
    if "is_synthetic" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN is_synthetic INTEGER NOT NULL DEFAULT 0;")
    if "user_id" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN user_id INTEGER REFERENCES users(user_id);")
    if "customer_name" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN customer_name TEXT;")
    if "customer_email" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN customer_email TEXT;")
    if "queue_number" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN queue_number INTEGER;")
    if "cancelled_at" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN cancelled_at TEXT;")
    if "cancel_reason" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN cancel_reason TEXT;")
    if "completed_at" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN completed_at TEXT;")
    if "booking_source" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN booking_source TEXT DEFAULT 'Walk-in Customer';")
    if "assigned_bay" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN assigned_bay TEXT DEFAULT 'Bay #1';")
    if "booking_date" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN booking_date TEXT;")
    if "booking_time" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN booking_time TEXT;")
    # ID upload/verification was never actually reachable through the
    # booking UI (no template ever rendered a file input for it) and
    # nothing ever reviewed or acted on a collected image - pure
    # collect-and-forget PII storage with zero function. Dropping these
    # columns also destroys any images/status already stored on existing
    # databases, which is the actual point: stopping new collection alone
    # wouldn't remove PII that's already sitting there.
    if "id_verified_status" in res_columns:
        cursor.execute("ALTER TABLE reservations DROP COLUMN id_verified_status;")
    if "valid_id_image" in res_columns:
        cursor.execute("ALTER TABLE reservations DROP COLUMN valid_id_image;")
    if "created_at" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN created_at TIMESTAMP;")
    if "weather_condition" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN weather_condition TEXT;")
    # Operational context captured at the moment each booking is placed -
    # how many bookings were already ahead of it for the same service, and
    # how many staff were actively clocked in. Nothing historical can be
    # reconstructed for older rows (this context was never recorded before),
    # but going forward it lets ai_retrain.py train the wait-time model on
    # real completed bookings instead of only the synthetic bootstrap data
    # in ai_engine.py.
    if "queue_length_snapshot" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN queue_length_snapshot INTEGER;")
    if "active_staff_snapshot" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN active_staff_snapshot INTEGER;")
    # Who actually started this job - set once, when a staff-role session
    # transitions a booking from waiting to in_progress (see
    # api_start_service). NULL if an admin started it (admins aren't
    # washers) or for any job started before this column existed.
    if "assigned_staff_id" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN assigned_staff_id INTEGER REFERENCES users(user_id);")
    # The wait-time model's own prediction at booking time (same inputs as
    # queue_length_snapshot/active_staff_snapshot), captured so real
    # accuracy (MAE/RMSE against the actual completed_at - created_at
    # duration) can be computed later - see ai_retrain.py's
    # analytics.py's prediction accuracy.
    if "predicted_wait_minutes" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN predicted_wait_minutes REAL;")
    # Set when a staff member actually starts the job (api_start_service),
    # distinct from created_at (when the booking was placed - includes
    # queue wait time) and completed_at (now a real manual "Mark as
    # Complete" click, not a countdown-timer guess). started_at ->
    # completed_at is the actual working duration, which is what a
    # per-staff "Speed" metric needs - using created_at instead would
    # unfairly penalize a washer for how many cars were ahead of them.
    if "started_at" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN started_at TIMESTAMP;")

    # The real price for this exact booking, resolved from service_prices at
    # submit-booking time and stored here (not recomputed later) so a price
    # change an admin makes afterward never silently changes what an
    # existing customer already booked at. NULL for every booking placed
    # before this column existed or under the 3 legacy pre-catalog services
    # (which have no service_prices rows) - callers fall back to
    # services.price for those, not a fabricated number.
    if "price" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN price REAL;")

    # Set when a booking's date/time is moved after it was made:
    # rescheduled_from is the old 'YYYY-MM-DD HH:MM AM' slot, and
    # reschedule_reason says why - 'staff_absent' (no_show.py moved it
    # because no staff were on duty) or 'customer' (they moved it from My
    # Bookings). My Bookings shows the customer what happened.
    # Rainfall (mm in the last hour) when the booking was made - a real
    # input for the wait-time model, alongside the Sunny/Cloudy/Rainy bucket.
    if "precipitation_mm" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN precipitation_mm REAL;")
    # The car's model ("unit" - e.g. Toyota Vios), for records and the
    # Manager Analytics model chart only: price still comes from
    # vehicle_type (the size). NULL on bookings made before it was asked.
    if "vehicle_model" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN vehicle_model TEXT;")
    # When staff checked a scheduled customer in on arrival (the
    # "Customer Has Arrived" button). Keeps their bay held past the booked time (see
    # slot_hold.py) and keeps the no-show sweep from cancelling someone
    # who is here but still waiting for the bay.
    if "arrived_at" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN arrived_at TIMESTAMP;")
    # Minutes of work already ahead in this booking's bay when it was made
    # (the car being washed, plus the cars waiting before it) - the wait-time
    # model's strongest input (see wait_estimate.py, ai_engine.py).
    if "work_ahead_minutes" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN work_ahead_minutes REAL;")
    # How late (minutes) the customer was last told their booked start is
    # running - late_notice.py only tells them again if it slips further.
    if "late_notice_minutes" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN late_notice_minutes INTEGER;")
    if "rescheduled_from" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN rescheduled_from TEXT;")
    if "reschedule_reason" not in res_columns:
        cursor.execute("ALTER TABLE reservations ADD COLUMN reschedule_reason TEXT;")

    # idx_active_slot_unique used to enforce "one active booking per
    # bay/date/time" at the database level, rejecting a second booking that
    # matched an existing one's exact bay+date+time with a 400 error. That
    # was wrong for this system's actual model: MD57 runs a real
    # first-come-first-served queue (see api_get_queue's docstring), not an
    # appointment calendar with exclusive slots - two customers wanting the
    # same service around the same time is normal demand, and the second
    # one should simply queue behind the first, not be turned away. Bay
    # capacity ("only one car actually being worked per bay at a time") is
    # now enforced at the point that actually matters instead -
    # api_start_service refuses to start a second job in a bay that already
    # has one in_progress/pending_confirmation, rather than blocking the
    # booking itself. DROP INDEX IF EXISTS is a no-op on a fresh database
    # that never created this index in the first place, and removes it from
    # any existing database that did.
    cursor.execute("DROP INDEX IF EXISTS idx_active_slot_unique")

    # database/md57_schema.sql documented these four indexes since early in
    # the project, but that file is reference-only - it's never executed,
    # so none of these actually existed until now. Every hot-path query in
    # queue_api.py filters by queue_status or vehicle_plate_number; those
    # were full table scans. idx_users_username is deliberately not
    # recreated here - users.username is already UNIQUE, and SQLite
    # creates an implicit index for that automatically, so a same-named
    # explicit one would just be a redundant duplicate.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations (queue_status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reservations_plate ON reservations (vehicle_plate_number)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reservations_date ON reservations (booking_date)")

    # Backs the login brute-force lockout in login_security.py: one row per
    # login attempt (success or failure), so a sliding-window COUNT query
    # can check recent failures per username and per IP without needing any
    # separate "locked_until" state to maintain.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            ip_address TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_username ON login_attempts (username, attempted_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts (ip_address, attempted_at)")

    # Backs the self-service "Forgot password" flow (auth.py). One row per
    # issued reset link; `used` also gets set on every other outstanding
    # token for an account the moment one of them is successfully redeemed,
    # so an older, still-unopened link can't remain valid after a reset.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            reset_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets (token)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets (user_id, used, created_at)")

    reset_columns = _column_names(cursor, 'password_resets')
    # `token` now holds a 6-digit numeric OTP (auth.py's _generate_otp_code)
    # instead of a long random link token - a code this short is only safe
    # to brute-force-resist with a per-code attempt cap, tracked here.
    if "attempts" not in reset_columns:
        cursor.execute("ALTER TABLE password_resets ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS staff_attendance (
            attendance_id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_name TEXT NOT NULL,
            date_str TEXT NOT NULL,
            status TEXT DEFAULT 'Absent',
            time_in TEXT DEFAULT '--:--',
            time_out TEXT DEFAULT '--:--',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # staff_bonuses (commission tracking, removed): dropped when it holds no
    # rows; a database that has some keeps it untouched.
    if cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'staff_bonuses'").fetchone() \
            and not cursor.execute("SELECT 1 FROM staff_bonuses LIMIT 1").fetchone():
        cursor.execute("DROP TABLE staff_bonuses")

    # Forward-looking schedule, separate from staff_attendance (which only
    # ever records what actually happened, never what's planned). One row
    # per staff member per day they're expected in - no formal shift
    # start/end time modeled, since the real attendance system doesn't
    # track that either (staff just clock in whenever); shift_note is a
    # free-text slot for "Morning", "Covering for Alan", etc.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS staff_shifts (
            shift_id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_name TEXT NOT NULL,
            shift_date TEXT NOT NULL,
            shift_note TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_shifts_unique ON staff_shifts (staff_name, shift_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_staff_shifts_date ON staff_shifts (shift_date)")

    shift_columns = _column_names(cursor, 'staff_shifts')
    # NULL = not yet reminded (or nothing to remind with - no opted-in
    # contact on file). Set once shift_reminder.py actually sends one, so
    # a staff member with a phone/email on file never gets texted twice
    # for the same shift.
    if "reminded_at" not in shift_columns:
        cursor.execute("ALTER TABLE staff_shifts ADD COLUMN reminded_at TIMESTAMP;")
    # Set once shift_reminder.py has alerted admin that this rostered staff
    # member hadn't clocked in by the cutoff - one alert per shift.
    if "late_alerted_at" not in shift_columns:
        cursor.execute("ALTER TABLE staff_shifts ADD COLUMN late_alerted_at TIMESTAMP;")

    # A staff-initiated "I can't work this day" flag - separate from
    # staff_shifts (which is admin's plan) for the same reason
    # staff_shifts is separate from staff_attendance: "what staff say they
    # can't do" and "what admin has scheduled" are different facts, and
    # conflating them would mean either fabricating one from the other or
    # overloading one table with two meanings. Admin sees this before
    # scheduling instead of finding out after the fact.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS staff_unavailability (
            unavailability_id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_name TEXT NOT NULL,
            unavailable_date TEXT NOT NULL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_unavailability_unique ON staff_unavailability (staff_name, unavailable_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_staff_unavailability_date ON staff_unavailability (unavailable_date)")

    # Attendance, roster and unavailability identify a staff member by
    # account (staff_user_id), not by display name - two accounts can't be
    # conflated, and a record keeps following its account. staff_name stays
    # as a readable snapshot. Existing rows are backfilled from the name,
    # and a trigger fills staff_user_id for any insert that only gives a
    # name (older callers, direct DB edits), so nothing falls through.
    for table, date_column in (('staff_attendance', 'date_str'), ('staff_shifts', 'shift_date'),
                               ('staff_unavailability', 'unavailable_date')):
        if "staff_user_id" not in _column_names(cursor, table):
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN staff_user_id INTEGER REFERENCES users(user_id);")
        resolve_by_name = (
            "(SELECT user_id FROM users WHERE role = 'staff' "
            "AND COALESCE(fullname, username) = {name} ORDER BY user_id LIMIT 1)"
        )
        cursor.execute(
            f"UPDATE {table} SET staff_user_id = {resolve_by_name.format(name=table + '.staff_name')} "
            f"WHERE staff_user_id IS NULL"
        )
        cursor.execute(f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_staff_user_id
            AFTER INSERT ON {table} WHEN NEW.staff_user_id IS NULL
            BEGIN
                UPDATE {table} SET staff_user_id = {resolve_by_name.format(name='NEW.staff_name')}
                WHERE rowid = NEW.rowid;
            END
        """)
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_staff_user ON {table} (staff_user_id, {date_column})")

    # A booking's user_id is the customer account it belongs to. Bookings
    # staff/admin entered used to store the staff/admin's own account
    # there - re-link those to the customer account with the booking's
    # email, or to nobody (see api_submit_booking). No rows match once
    # this has run.
    cursor.execute("""
        UPDATE reservations
        SET user_id = (SELECT c.user_id FROM users c
                       WHERE c.role = 'customer' AND LOWER(c.username) = LOWER(reservations.customer_email))
        WHERE user_id IN (SELECT user_id FROM users WHERE role != 'customer')
    """)

    # workforce_analytics was never written to or read from anywhere in the
    # app - drop it if an earlier run created it.
    cursor.execute("DROP TABLE IF EXISTS workforce_analytics")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedbacks (
            feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
            reservation_id INTEGER,
            user_id INTEGER,
            customer_name TEXT NOT NULL,
            plate_number TEXT NOT NULL,
            service_type TEXT NOT NULL,
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (reservation_id) REFERENCES reservations(reservation_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    feedback_columns = _column_names(cursor, 'feedbacks')
    if "reservation_id" not in feedback_columns:
        cursor.execute("ALTER TABLE feedbacks ADD COLUMN reservation_id INTEGER REFERENCES reservations(reservation_id);")
    if "user_id" not in feedback_columns:
        cursor.execute("ALTER TABLE feedbacks ADD COLUMN user_id INTEGER REFERENCES users(user_id);")

    # One review per reservation - stops a customer submitting unlimited
    # feedback for the same completed visit. NULL reservation_id is exempt
    # (older rows from before this link existed, and the original seed/demo
    # data below), since SQL never treats NULLs as equal to each other.
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_reservation_unique
        ON feedbacks (reservation_id)
        WHERE reservation_id IS NOT NULL
    """)

    # In-app notification center (separate from, and independent of, email/
    # Gmail notifications - a user who opted into email reminders still gets
    # one of these too). Always scoped to exactly one account via user_id -
    # there is no "broadcast" row; a message meant for every admin/staff
    # account is fanned out as one row per account (see utils.notify_role),
    # so unread counts and read/unread state are always per-account, never
    # shared. category is a free-text label ('booking_confirmed',
    # 'booking_cancelled', 'new_booking', 'attendance', 'ready', etc.) the
    # frontend uses to pick an icon - not constrained by a CHECK, since new
    # categories are just new strings, not a schema change.
    # related_reservation_id uses ON DELETE SET NULL deliberately - a
    # notification's title/message is self-contained text (e.g. "Your
    # booking for vehicle ABC-1234 has been cancelled"), so it stays
    # perfectly readable even after the reservation it links to is gone.
    # Without ON DELETE SET NULL, deleting a reservation that any
    # notification points to (e.g. cleanup.py archiving an old cancelled
    # booking out of `reservations` - which happens after almost every
    # cancellation, since api_cancel_booking creates exactly this kind of
    # notification) would fail with a foreign-key-constraint error and
    # silently block that booking from ever being archived.
    cursor.execute(f"CREATE TABLE IF NOT EXISTS notifications ({_NOTIFICATIONS_TABLE_COLUMNS})")

    # One-time repair for any environment (including this project's own dev
    # database, mid-development) where the table above was already created
    # before ON DELETE SET NULL was added - CREATE TABLE IF NOT EXISTS won't
    # retroactively change an existing table's constraints, and SQLite has
    # no ALTER TABLE for that, so the only way to fix an already-created
    # table is to rebuild it. Safe to run every startup: a no-op the moment
    # the table already has the clause.
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='notifications'")
    existing_notifications_sql = cursor.fetchone()
    if existing_notifications_sql and 'ON DELETE SET NULL' not in (existing_notifications_sql[0] or ''):
        cursor.execute("ALTER TABLE notifications RENAME TO notifications_old_fk_migration")
        cursor.execute(f"CREATE TABLE notifications ({_NOTIFICATIONS_TABLE_COLUMNS})")
        cursor.execute("""
            INSERT INTO notifications (notification_id, user_id, title, message, category, related_reservation_id, is_read, created_at)
            SELECT notification_id, user_id, title, message, category, related_reservation_id, is_read, created_at
            FROM notifications_old_fk_migration
        """)
        cursor.execute("DROP TABLE notifications_old_fk_migration")

    # Covers both "my recent notifications" (user_id, notification_id DESC)
    # and "my unread count" (user_id, is_read) with one index.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications (user_id, is_read, notification_id)")

    # Audit/history home for cancelled bookings once they age out of the
    # live `reservations` table (see cleanup.py's archive_expired_cancelled_bookings) -
    # a full column-for-column copy, not a summary, so nothing about an
    # old cancelled booking is actually lost, just moved off the table
    # every active-booking/queue/slot-conflict/history query reads from and
    # off the live Cancelled page. Deliberately has no foreign keys back to
    # `reservations`/`users`/`services` - the whole point is that this row
    # keeps meaning the same thing even after the reservation_id it came
    # from is deleted from `reservations`, or a referenced service/user is
    # later removed.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reservations_archive (
            reservation_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            customer_name TEXT,
            vehicle_plate_number TEXT,
            vehicle_type TEXT,
            service_id INTEGER,
            customer_email TEXT,
            queue_number INTEGER,
            queue_status TEXT,
            booking_source TEXT,
            assigned_bay TEXT,
            booking_date TEXT,
            booking_time TEXT,
            cancelled_at TEXT,
            cancel_reason TEXT,
            completed_at TEXT,
            created_at TIMESTAMP,
            weather_condition TEXT,
            queue_length_snapshot INTEGER,
            active_staff_snapshot INTEGER,
            assigned_staff_id INTEGER,
            predicted_wait_minutes REAL,
            started_at TIMESTAMP,
            price REAL,
            rescheduled_from TEXT,
            reschedule_reason TEXT,
            precipitation_mm REAL,
            vehicle_model TEXT,
            arrived_at TIMESTAMP,
            work_ahead_minutes REAL,
            late_notice_minutes INTEGER,
            archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    archive_columns = _column_names(cursor, 'reservations_archive')
    for column, ddl in (('price', 'REAL'), ('rescheduled_from', 'TEXT'), ('reschedule_reason', 'TEXT'), ('precipitation_mm', 'REAL'),
                        ('vehicle_model', 'TEXT'), ('arrived_at', 'TIMESTAMP'), ('work_ahead_minutes', 'REAL'),
                        ('late_notice_minutes', 'INTEGER')):
        if column not in archive_columns:
            cursor.execute(f"ALTER TABLE reservations_archive ADD COLUMN {column} {ddl};")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reservations_archive_plate ON reservations_archive (vehicle_plate_number)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reservations_archive_archived_at ON reservations_archive (archived_at)")

    if "is_synthetic" not in _column_names(cursor, 'reservations_archive'):
        cursor.execute("ALTER TABLE reservations_archive ADD COLUMN is_synthetic INTEGER NOT NULL DEFAULT 0;")

    # Bootstrap ownership separately from the lower-privilege Manager role.
    seed_legacy_accounts = os.environ.get('MD57_SEED_LEGACY_ACCOUNTS', 'false').lower() == 'true'
    # NOTE: default passwords below are widely known (they're in this repo's
    # history/docs) - set DEFAULT_ADMIN_PASSWORD / DEFAULT_CUSTOMER_PASSWORD /
    # DEFAULT_STAFF_PASSWORD in .env for any deployment beyond local dev.
    default_admin_password = os.environ.get('DEFAULT_ADMIN_PASSWORD', 'md57admin2026')
    default_customer_password = os.environ.get('DEFAULT_CUSTOMER_PASSWORD', 'customer123')
    default_staff_password = os.environ.get('DEFAULT_STAFF_PASSWORD', 'md57staff2026')
    if seed_legacy_accounts and (not os.environ.get('DEFAULT_ADMIN_PASSWORD') or not os.environ.get('DEFAULT_STAFF_PASSWORD')):
        print("[WARNING] Legacy test fixtures use built-in default seed passwords. "
              "Set DEFAULT_ADMIN_PASSWORD / DEFAULT_STAFF_PASSWORD / DEFAULT_CUSTOMER_PASSWORD "
              "in .env before deploying anywhere beyond local dev.")

    cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'")
    if cursor.fetchone()[0] == 0:
        if not seed_legacy_accounts and (not os.environ.get('DEFAULT_ADMIN_PASSWORD') or password_error(default_admin_password)):
            conn.close()
            raise RuntimeError('Set DEFAULT_ADMIN_PASSWORD to a private password to initialize the Owner account. '
                               + (password_error(default_admin_password) or 'Use a configured private password.'))
        admin_hash = generate_password_hash(default_admin_password)
        # The legacy identity is reserved for explicitly enabled test fixtures.
        bootstrap_username = 'manager' if seed_legacy_accounts else 'owner'
        bootstrap_name = 'Manager' if seed_legacy_accounts else 'Owner'
        cursor.execute(
            'INSERT INTO users (username, password_hash, role, fullname, is_owner) VALUES (?, ?, ?, ?, 1)',
            (bootstrap_username, admin_hash, 'admin', bootstrap_name),
        )

    # Shared customer credentials belong only to explicitly enabled test fixtures.
    cursor.execute("SELECT COUNT(*) FROM users WHERE username = 'customer_user'")
    if cursor.fetchone()[0] == 0 and seed_legacy_accounts:
        cust_hash = generate_password_hash(default_customer_password)
        cursor.execute(
            'INSERT INTO users (username, password_hash, role, fullname) VALUES (?, ?, ?, ?)',
            ('customer_user', cust_hash, 'customer', 'Default Customer'),
        )

    # Existing deployments retain their people. Do not recreate employees
    # with a common password every time an account is renamed or removed.
    seed_staff = seed_legacy_accounts
    # Seed Default Staff Users only for explicitly requested local fixtures.
    staff_names = ["TJ Barcebal", "Richmond Abarico", "Jonel Levy", "Alan Dizon", "Kiph Paganpan"]
    staff_hash = generate_password_hash(default_staff_password) if seed_staff else None
    for s_name in staff_names if seed_staff else []:
        # Also skipped when that person's account exists under another
        # username (e.g. renamed to a login like 'tj.barcebal').
        cursor.execute("SELECT COUNT(*) FROM users WHERE username = ? OR (role = 'staff' AND fullname = ?)", (s_name, s_name))
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                'INSERT INTO users (username, password_hash, role, fullname) VALUES (?, ?, ?, ?)',
                (s_name, staff_hash, 'staff', s_name),
            )

    # Seed Vehicle Types - replaced 2026-09 with the shop's real published
    # size tiers (Small/Medium/Large/XLarge for cars, two motorcycle sizes)
    # to match the real per-size price list in SERVICE_CATALOG below,
    # instead of the old ad-hoc Motor/Car/Pick up/Van categories that were
    # never actually priced by size at all. vehicle_type on a reservation is
    # a plain snapshot string, not a foreign key, so this never touches or
    # invalidates any existing booking's stored vehicle_type - only which
    # options a *new* booking can choose from.
    old_vehicle_types = ('Motor', 'Car', 'Pick up', 'Van')
    cursor.execute(
        f"DELETE FROM vehicle_types WHERE type_name IN ({','.join('?' * len(old_vehicle_types))})",
        old_vehicle_types,
    )
    # Duration multipliers below are this system's own estimates (the shop
    # only ever specified prices per size, not wash duration per size) -
    # scaled the same way the old Motor(0)/Car(5)/Pick up(15)/Van(25)
    # spread was, larger vehicle -> more minutes.
    default_vehicles = [
        ('Small', 0),
        ('Medium', 5),
        ('Large', 15),
        ('XLarge', 25),
        ('Motor (S)', 0),
        ('Motor (L)', 3),
    ]
    for v_name, v_mult in default_vehicles:
        cursor.execute("""
            INSERT OR IGNORE INTO vehicle_types (type_name, multiplier_minutes)
            VALUES (?, ?)
        """, (v_name, v_mult))

    # The original 3 generic services - retired (is_active=0) from new
    # bookings now that the real per-service, per-size catalog below exists,
    # but never deleted: existing reservations/feedback rows reference
    # service_id 1/2/3 by foreign key, and History/CSV/PDF exports need
    # those rows to keep resolving to a real service name for old records.
    # api_submit_booking's legacy name-matching fallback (for any caller
    # that doesn't send a service_id) still finds these regardless of
    # is_active, so nothing that already depended on booking "Body Wash" by
    # name breaks.
    default_services = [
        ('Body Wash', 'body_wash', 20, 150.00, 'general', 'Bay #1'),
        ('Express & Foam', 'express_foam', 35, 250.00, 'general', 'Bay #2'),
        ('Full Body Wash & Detailing', 'detailing', 90, 1000.00, 'detailing', 'Bay #3'),
    ]
    for s_name, s_type, s_dur, s_price, s_cat, s_bay in default_services:
        cursor.execute("SELECT service_id FROM services WHERE service_name = ?", (s_name,))
        existing_row = cursor.fetchone()
        if existing_row is None:
            cursor.execute(
                "INSERT INTO services (service_name, service_type, base_duration_minutes, price, is_active, service_category, default_bay) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (s_name, s_type, s_dur, s_price, s_cat, s_bay)
            )

    # --- REAL SERVICE CATALOG (2026-09) ---
    # The shop's actual published price list - every service a customer can
    # see on the Pricing page can now also actually be booked with the
    # matching price, instead of the old 3-generic-service/no-size-tiers
    # setup where Pricing advertised ~22 named services (with per-size
    # prices) that had no corresponding bookable service at all.
    #
    # Prices below are exact, as published. base_duration_minutes and
    # default_bay are this system's own estimates (only prices were ever
    # specified) - duration follows the same rough scale as the original 3
    # services (20/35/90 min for a basic/mid/full-detail job), and bay
    # assignment keeps the existing 3 physical bays' meaning intact (Bay #1
    # Main / Bay #2 Express / Bay #3 Detailing - see queue_dashboard.html)
    # rather than inventing new bay labels: general wash/add-on jobs -> Bay
    # #1, quick jobs and all motorcycle work -> Bay #2, full detailing work
    # -> Bay #3.
    #
    # A service whose price is the same across every vehicle size (e.g.
    # Back 2 Zero) still gets one service_prices row per vehicle size below,
    # all with that same price - see service_prices' own table comment for
    # why (one uniform lookup regardless of whether a service is tiered).
    CAR_SIZES = ('Small', 'Medium', 'Large', 'XLarge')
    MOTOR_SIZES = ('Motor (S)', 'Motor (L)')
    SERVICE_CATALOG = [
        # (name, category, bay, duration_minutes, {size: price})
        ('Standard Carwash', 'general', 'Bay #1', 20, dict(zip(CAR_SIZES, (150, 180, 210, 260)))),
        ('Premium Carwash', 'general', 'Bay #1', 35, dict(zip(CAR_SIZES, (450, 500, 550, 600)))),
        ('Basic Wax', 'general', 'Bay #1', 30, dict(zip(CAR_SIZES, (400, 500, 600, 650)))),
        ('Spray Wax', 'general', 'Bay #2', 15, dict(zip(CAR_SIZES, (200, 250, 300, 350)))),
        ('Interior Dressing', 'general', 'Bay #2', 15, dict(zip(CAR_SIZES, (200, 250, 300, 350)))),
        ('Asphalt Removal', 'general', 'Bay #1', 30, dict(zip(CAR_SIZES, (250, 350, 350, 450)))),
        ('Acid Rain Removal', 'general', 'Bay #1', 45, dict(zip(CAR_SIZES, (650, 750, 800, 1000)))),
        ('Seat Cover Removal', 'general', 'Bay #1', 20, dict(zip(CAR_SIZES, (300, 400, 400, 500)))),
        ('Seat Cover Installation', 'general', 'Bay #1', 30, dict(zip(CAR_SIZES, (400, 500, 550, 600)))),
        ('Back 2 Zero', 'general', 'Bay #1', 40, dict(zip(CAR_SIZES, (400, 400, 400, 400)))),
        ('Head Lights Restoration', 'general', 'Bay #1', 30, dict(zip(CAR_SIZES, (500, 500, 500, 500)))),
        ('Engine Wash', 'general', 'Bay #1', 30, dict(zip(CAR_SIZES, (500, 500, 500, 500)))),
        ('Glass Polishing', 'detailing', 'Bay #3', 45, dict(zip(CAR_SIZES, (1000, 1500, 1800, 2000)))),
        ('Engine Detailing', 'detailing', 'Bay #3', 60, dict(zip(CAR_SIZES, (1500, 2000, 2500, 3000)))),
        ('Glass Detailing', 'detailing', 'Bay #3', 60, dict(zip(CAR_SIZES, (2500, 2800, 3000, 3500)))),
        ('Exterior Detailing', 'detailing', 'Bay #3', 90, dict(zip(CAR_SIZES, (3000, 3500, 4000, 4500)))),
        ('Interior Detailing', 'detailing', 'Bay #3', 90, dict(zip(CAR_SIZES, (3000, 3500, 4000, 4500)))),
        ('Interior Carpet Removal', 'detailing', 'Bay #3', 60, dict(zip(CAR_SIZES, (2500, 3000, 3500, 4000)))),
        ('Ceramic Coating', 'detailing', 'Bay #3', 180, dict(zip(CAR_SIZES, (7000, 9000, 10000, 12500)))),
        ('Motorcycle Carwash', 'motorcycle', 'Bay #2', 15, dict(zip(MOTOR_SIZES, (150, 180)))),
        ('Motorcycle Basic Wax', 'motorcycle', 'Bay #2', 20, dict(zip(MOTOR_SIZES, (150, 200)))),
        ('Motorcycle Exterior Detailing', 'motorcycle', 'Bay #2', 60, dict(zip(MOTOR_SIZES, (1350, 1650)))),
    ]
    for name, category, bay, duration, prices_by_size in SERVICE_CATALOG:
        cursor.execute("SELECT service_id FROM services WHERE service_name = ?", (name,))
        row = cursor.fetchone()
        if row is None:
            # A representative price (the first size tier) satisfies the
            # legacy NOT NULL services.price column - the real per-size
            # prices customers and the booking form actually use live in
            # service_prices below; services.price is never read for these
            # new catalog rows (see get-history/export_pdf's COALESCE(r.price, ...)).
            first_price = next(iter(prices_by_size.values()))
            cursor.execute(
                """
                INSERT INTO services (service_name, service_type, base_duration_minutes, price, is_active, service_category, default_bay)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (name, category, duration, first_price, category, bay),
            )
            service_id = cursor.lastrowid
        else:
            service_id = row['service_id']

        for size, price in prices_by_size.items():
            cursor.execute(
                """
                INSERT INTO service_prices (service_id, vehicle_type, price) VALUES (?, ?, ?)
                ON CONFLICT(service_id, vehicle_type) DO NOTHING
                """,
                (service_id, size, price),
            )

    # Car models the booking forms suggest. Typing one of these (with or
    # without the make - "vios") is stored under this exact name, so the
    # model chart doesn't split one car into several spellings; anything
    # else is kept as typed and charted under "Other" (see vehicles.py).
    # Shop settings an admin changes on screen (key -> text value) - see
    # settings.py for the keys and their defaults.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL,
            updated_at TEXT,
            updated_by TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vehicle_models (
            model_id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL UNIQUE
        )
    """)
    default_vehicle_models = [
        'Toyota Vios', 'Toyota Wigo', 'Toyota Innova', 'Toyota Fortuner', 'Toyota Hilux', 'Toyota Avanza',
        'Toyota Raize', 'Toyota Rush', 'Toyota Corolla Altis', 'Toyota Hiace',
        'Mitsubishi Mirage', 'Mitsubishi Mirage G4', 'Mitsubishi Xpander', 'Mitsubishi Montero Sport',
        'Mitsubishi Strada', 'Mitsubishi L300',
        'Honda City', 'Honda Civic', 'Honda BR-V', 'Honda CR-V', 'Honda Brio',
        'Nissan Navara', 'Nissan Almera', 'Nissan Terra', 'Nissan Urvan',
        'Ford Ranger', 'Ford Everest', 'Ford Territory',
        'Suzuki Ertiga', 'Suzuki Swift', 'Suzuki Dzire', 'Suzuki S-Presso', 'Suzuki Jimny',
        'Hyundai Accent', 'Hyundai Stargazer', 'Hyundai Tucson',
        'Kia Soluto', 'Kia Sonet', 'Isuzu D-Max', 'Isuzu mu-X',
        'Mazda 3', 'Mazda CX-5', 'Geely Coolray', 'MG ZS', 'MG 5', 'Chevrolet Trailblazer',
        'Honda Click', 'Honda Beat', 'Honda PCX', 'Honda ADV', 'Honda TMX',
        'Yamaha NMAX', 'Yamaha Aerox', 'Yamaha Mio', 'Yamaha Sniper',
        'Suzuki Raider', 'Kawasaki Barako',
    ]
    cursor.executemany("INSERT OR IGNORE INTO vehicle_models (model_name) VALUES (?)",
                       [(name,) for name in default_vehicle_models])

    # Seed Feedbacks if empty
    cursor.execute("SELECT COUNT(*) FROM feedbacks")
    if cursor.fetchone()[0] == 0:
        sample_feedbacks = [
            ("Juan Dela Cruz", "ABC-1234", "Body Wash", 5, "Super linis at mabilis ang serbisyo!", (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')),
            ("Maria Santos", "XYZ-9876", "Full Body Wash & Detailing", 4, "Maganda ang gawa, worth it po.", (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')),
            ("Carlo Reyes", "NDB-5421", "Express & Foam", 5, "Sobrang kintab ng sasakyan ko!", datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
            ("Elena Gomez", "KLP-1122", "Body Wash", 4, "Mababait ang mga washers, recommend ko ito.", (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S'))
        ]
        cursor.executemany('''
            INSERT INTO feedbacks (customer_name, plate_number, service_type, rating, comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', sample_feedbacks)

    # Service finish, notification and physical collection are separate events.
    # Leave historical rows NULL: old "completed" records do not prove pickup.
    for table in ('reservations', 'reservations_archive'):
        columns = _column_names(cursor, table)
        for column, ddl in (('ready_notified_at', 'TEXT'), ('picked_up_at', 'TEXT'),
                            ('picked_up_by', 'INTEGER'), ('picked_up_by_name', 'TEXT')):
            if column not in columns:
                if column == 'picked_up_by' and table == 'reservations':
                    ddl += ' REFERENCES users(user_id)'
                cursor.execute(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')

    # Provenance also covers attendance: fictional shifts must not influence
    # real staff punctuality or operational recommendations.
    if "is_synthetic" not in _column_names(cursor, 'staff_attendance'):
        cursor.execute("ALTER TABLE staff_attendance ADD COLUMN is_synthetic INTEGER NOT NULL DEFAULT 0")
    if cursor.execute("SELECT 1 FROM sqlite_master WHERE name='synthetic_seed_records'").fetchone():
        cursor.execute("""UPDATE staff_attendance SET is_synthetic=1
            WHERE attendance_id IN (SELECT row_id FROM synthetic_seed_records WHERE table_name='staff_attendance')
              AND is_synthetic=0""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS background_jobs (
        name TEXT PRIMARY KEY, owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
        next_run_at REAL NOT NULL DEFAULT 0, last_started TEXT,
        last_success TEXT, last_error TEXT)""")
    cursor.execute('CREATE TABLE IF NOT EXISTS account_migrations (name TEXT PRIMARY KEY)')
    if not cursor.execute("SELECT 1 FROM account_migrations WHERE name='employee_access_v1'").fetchone():
        # Preserve IDs and attribution. Prefer the existing Owner account;
        # on older installations the first non-front-desk admin is bootstrap owner.
        owner = cursor.execute("""SELECT user_id FROM users
            WHERE role='admin' AND front_desk=0 AND disabled_at IS NULL
              AND LOWER(REPLACE(username,' ',''))<>'frontdesk'
            ORDER BY CASE WHEN LOWER(username)='owner' THEN 0 ELSE 1 END, user_id LIMIT 1""").fetchone()
        if owner:
            cursor.execute('UPDATE users SET is_owner=1 WHERE user_id=?', (owner['user_id'],))
        cursor.execute("""UPDATE users SET disabled_at=COALESCE(disabled_at, ?), front_desk=1, is_owner=0
            WHERE front_desk=1 OR LOWER(REPLACE(username, ' ', ''))='frontdesk'""",
            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),))
        # The old implicit-owner model must not leave any employee cookie
        # active across the permission migration, including regular managers.
        cursor.execute("UPDATE users SET session_version=session_version+1 WHERE role IN ('admin','staff')")
        cursor.execute("INSERT INTO account_migrations VALUES ('employee_access_v1')")
    if not cursor.execute("SELECT 1 FROM account_migrations WHERE name='manager_hierarchy_v2'").fetchone():
        # Retain the login and ID. A Manager is below a separate active Owner.
        cursor.execute("""UPDATE users SET is_owner=0 WHERE LOWER(username)='manager'
            AND role='admin' AND is_owner=1 AND EXISTS (
                SELECT 1 FROM users other WHERE other.user_id<>users.user_id
                AND other.role='admin' AND other.is_owner=1 AND other.disabled_at IS NULL)""")
        cursor.execute("""UPDATE users SET fullname='Manager',session_version=session_version+1
            WHERE role='admin' AND LOWER(TRIM(fullname))='system manager'""")
        cursor.execute("INSERT INTO account_migrations VALUES ('manager_hierarchy_v2')")
    from operational_schema import install
    install(conn)
    conn.commit()
    conn.close()
