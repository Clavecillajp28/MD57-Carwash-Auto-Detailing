from flask import Blueprint, jsonify, request, session

from database import get_db_connection
from utils import role_required, log_activity, mask_plate, is_own_booking, now_str

feedback_api_bp = Blueprint('feedback_api', __name__)


@feedback_api_bp.route('/api/submit-feedback', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def api_submit_feedback():
    data = request.get_json() or {}
    reservation_id = data.get('reservation_id')
    comment = (data.get('comment') or '').strip()
    try:
        rating = int(data.get('rating', 5))
    except (TypeError, ValueError):
        rating = 0  # rejected by the range check below

    if not reservation_id:
        return jsonify({'status': 'error', 'message': 'A completed reservation is required to leave feedback.'}), 400

    if rating < 1 or rating > 5:
        return jsonify({'status': 'error', 'message': 'Rating must be between 1 and 5.'}), 400

    conn = get_db_connection()
    conn.execute('BEGIN IMMEDIATE')
    cursor = conn.cursor()

    # Customer/plate/service are read from the reservation itself, not
    # trusted from the client - the only thing a submitter actually gets
    # to choose is which of their own completed visits to rate, plus the
    # rating and comment.
    cursor.execute("""
        SELECT r.reservation_id, r.user_id, r.customer_email, r.queue_status,
               r.vehicle_plate_number AS plate_number,
               COALESCE(r.customer_name, u.fullname, u.username, 'Customer') AS customer_name,
               s.service_name AS service_type
        FROM reservations r
        JOIN services s ON r.service_id = s.service_id
        LEFT JOIN users u ON r.user_id = u.user_id
        WHERE r.reservation_id = ?
    """, (reservation_id,))
    reservation = cursor.fetchone()

    if not reservation:
        conn.close()
        return jsonify({'status': 'error', 'message': 'Reservation not found.'}), 404

    if reservation['queue_status'] != 'completed':
        conn.close()
        return jsonify({'status': 'error', 'message': 'Feedback can only be left for a completed visit.'}), 400

    # A logged-in customer can only review their own reservation - the
    # same ones My Bookings lists for them (see utils.is_own_booking).
    # Staff/admin are exempt (e.g. entering feedback a walk-in customer
    # gave in person for a reservation with no linked account), but
    # they're still bound by the completed-status and
    # one-review-per-reservation checks around this.
    if session.get('role') == 'customer' and not is_own_booking(reservation['user_id'], reservation['customer_email']):
        conn.close()
        return jsonify({'status': 'error', 'message': 'You can only leave feedback for your own reservation.'}), 403

    cursor.execute("SELECT 1 FROM feedbacks WHERE reservation_id = ?", (reservation_id,))
    if cursor.fetchone():
        conn.close()
        return jsonify({'status': 'error', 'message': 'This reservation has already been reviewed.'}), 409

    cursor.execute("""
        INSERT INTO feedbacks (reservation_id, user_id, customer_name, plate_number, service_type, rating, comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        reservation['reservation_id'], reservation['user_id'], reservation['customer_name'],
        reservation['plate_number'], reservation['service_type'], rating, comment, now_str(),
    ))
    conn.commit()
    conn.close()

    log_activity(session.get('user', reservation['customer_name']), f"Submitted feedback for plate: {reservation['plate_number']}")
    return jsonify({'status': 'success', 'message': 'Thank you for your feedback!'}), 200


@feedback_api_bp.route('/api/get-feedbacks', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_get_feedbacks():
    """Public-facing testimonials wall (every customer sees every review,
    like a reviews page) - that's intentional for the name/comment/rating.
    The plate number isn't, though: it's a more identifying, replayable
    piece of PII than a first name on a review, and staff/admin are the
    only ones who actually need it (to match a review back to a booking),
    so it's masked for everyone else."""
    is_staff_or_admin = session.get('role') in ('admin', 'staff')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''SELECT f.feedback_id,f.reservation_id,f.customer_name,f.plate_number,
        f.service_type,f.rating,f.comment,f.created_at FROM feedbacks f
        LEFT JOIN reservations r ON r.reservation_id=f.reservation_id
        LEFT JOIN reservations_archive a ON a.reservation_id=f.reservation_id
        WHERE f.is_synthetic=0 AND COALESCE(r.is_synthetic,a.is_synthetic,0)=0 ORDER BY f.feedback_id DESC''')
    rows = [dict(row) for row in cursor.fetchall()]
    if not is_staff_or_admin:
        for row in rows:
            row['plate_number'] = mask_plate(row.get('plate_number'))
    conn.close()
    return jsonify({'status': 'success', 'data': rows}), 200
