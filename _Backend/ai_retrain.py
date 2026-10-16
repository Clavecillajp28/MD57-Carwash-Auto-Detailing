from database import get_db_connection
from extensions import ai_predictor
from utils import parse_duration_minutes, start_periodic_worker

# Retraining doesn't need to react quickly the way the no-show sweep does -
# real completed bookings accumulate slowly, so checking every 6 hours is
# plenty. The first cycle runs immediately at startup (see
# start_ai_retrain_scheduler) in case enough real data already exists.
RETRAIN_INTERVAL_SECONDS = 6 * 60 * 60


def _fetch_real_training_rows():
    """Pulls completed reservations that have a full operational snapshot
    (queue_length_snapshot/active_staff_snapshot, captured at booking time
    by api_submit_booking) and an honest actual-wait duration
    (completed_at - created_at, both explicit local timestamps). Rows
    booked before this feature shipped have NULL snapshots and are
    excluded - there's no way to reconstruct what the queue/staff looked
    like for them after the fact.

    Rainfall comes from the booking's stored precipitation_mm; bookings
    made before it was recorded (NULL) use 0.0 rather than an invented
    value.

    Walk-ins only: a scheduled booking made hours or days ahead isn't in
    line from the moment it's made, so its created_at -> completed_at is
    lead time, not a wait. Explicit synthetic history is excluded even
    when it contains full operational snapshots."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT r.reservation_id,r.queue_length_snapshot, r.active_staff_snapshot, r.precipitation_mm, r.work_ahead_minutes,
               r.created_at, r.completed_at, s.base_duration_minutes
        FROM reservations r
        JOIN services s ON r.service_id = s.service_id
        WHERE r.queue_status IN ('ready', 'completed')
          AND r.is_synthetic = 0
          AND COALESCE(r.booking_source, 'Walk-in Customer') = 'Walk-in Customer'
          AND r.queue_length_snapshot IS NOT NULL
          AND r.active_staff_snapshot IS NOT NULL
          AND r.completed_at IS NOT NULL AND r.completed_at != ''
          AND r.created_at IS NOT NULL
    """)
    rows = cursor.fetchall()
    conn.close()

    training_rows = []
    for row in rows:
        actual_wait = parse_duration_minutes(row['created_at'], row['completed_at'])
        if actual_wait is None:
            continue

        training_rows.append({
            'reservation_id':row['reservation_id'],'created_at':row['created_at'],'completed_at':row['completed_at'],
            'queue_length': row['queue_length_snapshot'],
            'active_staff': row['active_staff_snapshot'],
            'precipitation': row['precipitation_mm'] or 0.0,
            'service_base_time': row['base_duration_minutes'],
            'work_ahead_minutes': row['work_ahead_minutes'],
            'hour': int(row['created_at'][11:13]) if row['created_at'] and len(row['created_at']) >= 13 else None,
            'actual_wait_time': actual_wait,
        })
    return training_rows


def run_retrain_cycle():
    """One retraining attempt: pull real data, hand it to the predictor.
    Safe to call any time - retrain_with_real_data() no-ops (returns False)
    below the minimum real-sample threshold."""
    rows = _fetch_real_training_rows()
    from model_validation import evaluate
    evaluate(rows)
    return ai_predictor.retrain_with_real_data(rows)


def start_ai_retrain_scheduler(app):
    start_periodic_worker(app, run_retrain_cycle, RETRAIN_INTERVAL_SECONDS, 'ai-retrain-scheduler', shared=False)
