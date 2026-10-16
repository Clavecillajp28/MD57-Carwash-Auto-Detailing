"""Additive schema for recovery, delivery, account protection and quality records."""


def install(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS audit_records (
            audit_id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL,
            actor_id INTEGER, actor_name TEXT, entity TEXT NOT NULL, entity_id TEXT,
            action TEXT NOT NULL, reason TEXT, before_json TEXT, after_json TEXT);
        CREATE INDEX IF NOT EXISTS idx_audit_search ON audit_records(occurred_at,entity,entity_id);
        CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_records
            BEGIN SELECT RAISE(ABORT,'Audit records are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_records
            BEGIN SELECT RAISE(ABORT,'Audit records are append-only'); END;
        CREATE TABLE IF NOT EXISTS mail_outbox (
            mail_id INTEGER PRIMARY KEY, dedupe_key TEXT UNIQUE NOT NULL,
            recipient TEXT NOT NULL, subject TEXT NOT NULL, payload TEXT,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, sent_at TEXT, next_attempt REAL NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0, claim_token TEXT, last_error TEXT,
            expires_at REAL NOT NULL, retryable INTEGER NOT NULL DEFAULT 1);
        CREATE INDEX IF NOT EXISTS idx_outbox_due ON mail_outbox(status,next_attempt);
        CREATE TABLE IF NOT EXISTS owner_mfa (
            user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            secret TEXT, pending_secret TEXT, pending_until REAL, enabled INTEGER NOT NULL DEFAULT 0,
            last_counter INTEGER NOT NULL DEFAULT -1);
        CREATE TABLE IF NOT EXISTS owner_email_challenges (
            challenge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            purpose TEXT NOT NULL, email TEXT NOT NULL, code_hash TEXT NOT NULL,
            session_version INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0, used_at REAL);
        CREATE INDEX IF NOT EXISTS idx_owner_email_issued ON owner_email_challenges(user_id,created_at);
        CREATE TABLE IF NOT EXISTS model_evaluations (
            evaluation_id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
            evaluated_at TEXT NOT NULL, train_samples INTEGER NOT NULL, test_samples INTEGER NOT NULL,
            train_until TEXT NOT NULL, test_from TEXT NOT NULL, test_until TEXT NOT NULL,
            mae REAL NOT NULL, rmse REAL NOT NULL, bias REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS mfa_recovery_codes (
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            code_hash TEXT NOT NULL, used_at TEXT, PRIMARY KEY(user_id,code_hash));
        CREATE TABLE IF NOT EXISTS service_checks (
            reservation_id INTEGER PRIMARY KEY REFERENCES reservations(reservation_id) ON DELETE CASCADE,
            steps_json TEXT NOT NULL DEFAULT '{}', concern TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
            concern_resolved INTEGER NOT NULL DEFAULT 0, updated_by INTEGER,
            updated_by_name TEXT, updated_at TEXT NOT NULL, manager_reviewed_by INTEGER,
            manager_reviewed_at TEXT);
    ''')
    mfa_columns = {r[1] for r in conn.execute('PRAGMA table_info(owner_mfa)')}
    for name, definition in (('method', "TEXT NOT NULL DEFAULT 'totp'"), ('email', 'TEXT')):
        if name not in mfa_columns:
            conn.execute(f'ALTER TABLE owner_mfa ADD COLUMN {name} {definition}')
    for table in ('reservations','reservations_archive'):
        if 'checklist_required' not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN checklist_required INTEGER NOT NULL DEFAULT 0')
    # Only human-readable operational values are audited: credentials, MFA
    # secrets, OTPs, invitation tokens and uploaded ID images are excluded.
    tables = {
        'users': ('user_id', ['username','fullname','role','is_owner','notify_email','disabled_at']),
        'reservations': ('reservation_id', ['queue_status','assigned_staff_id','assigned_bay','booking_date','booking_time','picked_up_at','is_synthetic']),
        'services': ('service_id', ['service_name','base_duration_minutes','default_bay','is_active','price']),
        'service_prices': ('service_id', ['vehicle_type','price']),
        'staff_attendance': ('attendance_id',['staff_user_id','staff_name','date_str','status','time_in','time_out']),
        'app_settings': ('setting_key', ['setting_value']),
        'service_checks': ('reservation_id', ['steps_json','concern','concern_resolved','manager_reviewed_by']),
    }
    for table, (key, wanted) in tables.items():
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        if key not in columns:
            continue
        fields = [c for c in wanted if c in columns]
        if not fields:
            continue
        def snapshot(prefix):
            return 'json_object(' + ','.join(f"'{c}',{prefix}.{c}" for c in fields) + ')'
        changes = ' OR '.join(f'OLD.{c} IS NOT NEW.{c}' for c in fields)
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS audit_{table}_update AFTER UPDATE ON {table}
            WHEN {changes} BEGIN
            INSERT INTO audit_records(occurred_at,actor_id,actor_name,entity,entity_id,action,reason,before_json,after_json)
            VALUES(datetime('now','localtime'),audit_actor_id(),audit_actor_name(),'{table}',NEW.{key},
                'update',audit_reason(),{snapshot('OLD')},{snapshot('NEW')}); END''')
        for event,prefix in (('INSERT','NEW'),('DELETE','OLD')):
            before=snapshot('OLD') if event=='DELETE' else 'NULL'
            after=snapshot('NEW') if event=='INSERT' else 'NULL'
            conn.execute(f'''CREATE TRIGGER IF NOT EXISTS audit_{table}_{event.lower()} AFTER {event} ON {table}
                BEGIN INSERT INTO audit_records(occurred_at,actor_id,actor_name,entity,entity_id,action,reason,before_json,after_json)
                VALUES(datetime('now','localtime'),audit_actor_id(),audit_actor_name(),'{table}',{prefix}.{key},
                    '{event.lower()}',audit_reason(),{before},{after}); END''')
