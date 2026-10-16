"""Import-safe background startup with cross-process leases and run health."""
from datetime import datetime
import os
import logging
import threading
import time
import uuid

from database import get_db_connection

_registry_lock = threading.Lock()
_process_instance = uuid.uuid4().hex[:10]
LEASE_SECONDS = 300


def _clock():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def run_job(name, job, interval_seconds):
    """Run when due with a renewable lease; recover a crashed owner after 5 min."""
    owner, now = uuid.uuid4().hex, time.time()
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT OR IGNORE INTO background_jobs(name) VALUES (?)', (name,))
        claimed = conn.execute('''UPDATE background_jobs SET owner=?,lease_until=?,last_started=?
            WHERE name=? AND lease_until<=? AND next_run_at<=?''',
            (owner, now + LEASE_SECONDS, _clock(), name, now, now)).rowcount
        conn.commit()
    finally:
        conn.close()
    if not claimed:
        return False
    stopped = threading.Event()

    def renew():
        while not stopped.wait(LEASE_SECONDS / 3):
            lease_conn = get_db_connection()
            try:
                lease_conn.execute('UPDATE background_jobs SET lease_until=? WHERE name=? AND owner=?',
                                   (time.time() + LEASE_SECONDS, name, owner))
                lease_conn.commit()
            except Exception:
                logging.getLogger(__name__).exception('Could not renew job lease: %s', name)
            finally:
                lease_conn.close()

    heartbeat = threading.Thread(target=renew, daemon=True, name=f'{name}-lease')
    heartbeat.start()
    error = None
    try:
        job()
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'[:1000]
        raise
    finally:
        stopped.set()
        heartbeat.join(timeout=1)
        conn = get_db_connection()
        try:
            conn.execute('''UPDATE background_jobs SET owner=NULL,lease_until=0,next_run_at=?,
                last_success=CASE WHEN ? IS NULL THEN ? ELSE last_success END,last_error=?
                WHERE name=? AND owner=?''',
                (time.time() + interval_seconds, error, _clock(), error, name, owner))
            conn.commit()
        finally:
            conn.close()
    return True


def start_periodic_worker(app, job, interval_seconds, name, shared=True):
    """One thread per app/job; shared jobs also coordinate across processes."""
    with _registry_lock:
        registry = app.extensions.setdefault('background_workers', {})
        if name in registry and registry[name].is_alive():
            return registry[name]
        # The predictor lives in memory: each process needs its own retraining.
        lease_key = name if shared else f'{name}:{os.getpid()}:{_process_instance}'

        def loop():
            while True:
                try:
                    with app.app_context():
                        run_job(lease_key, job, interval_seconds)
                except Exception:
                    app.logger.exception('Background job failed: %s', name)
                time.sleep(interval_seconds)

        worker = threading.Thread(target=loop, daemon=True, name=name)
        registry[name] = worker
        worker.start()
        return worker


def start_background_jobs(app):
    if not app.config.get('BACKGROUND_JOBS_ENABLED', True) or app.testing:
        return
    from no_show import start_no_show_sweeper
    from ai_retrain import start_ai_retrain_scheduler
    from shift_reminder import start_shift_reminder_scheduler, start_roster_watch
    from cleanup import start_cleanup_sweeper
    from late_notice import start_late_notice_worker
    from attendance_close import start_attendance_close_worker
    from backups import scheduled_backup
    from mail_outbox import process_outbox
    start_periodic_worker(app,scheduled_backup,3600,'automatic-backup')
    start_periodic_worker(app,process_outbox,60,'mail-outbox')
    for start in (start_no_show_sweeper, start_late_notice_worker, start_attendance_close_worker,
                  start_ai_retrain_scheduler, start_shift_reminder_scheduler, start_roster_watch,
                  start_cleanup_sweeper):
        start(app)
