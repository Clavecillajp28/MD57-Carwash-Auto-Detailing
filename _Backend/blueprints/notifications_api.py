from flask import Blueprint, Response, jsonify, request, session

from database import get_db_connection
from live_updates import stream
from utils import role_required

notifications_api_bp = Blueprint('notifications_api', __name__)

# Which staff/admin page lists a booking, by where it is right now - a
# "New Booking" notification from this morning should open that booking
# wherever it has got to since, not the page it started on.
_PAGE_BY_BOOKING_STATUS = {
    'waiting': '/queue', 'in_progress': '/queue', 'pending_confirmation': '/queue',
    'ready': '/ready', 'completed': '/history', 'cancelled': '/cancelled',
}


def _notification_link(role, category, reservation_id, reservation_status):
    """Where clicking a notification takes this account. '?booking=<id>'
    tells the page which booking to bring up (common.js's
    takeFocusBookingId). reservation_status is None when the booking no
    longer exists in `reservations` (e.g. archived)."""
    if role == 'customer':
        # Everything a customer is told about is one of their own bookings.
        return f'/my-bookings?booking={reservation_id}' if reservation_id else '/my-bookings'
    if category == 'attendance':
        return '/staff'
    if reservation_status in _PAGE_BY_BOOKING_STATUS:
        return f'{_PAGE_BY_BOOKING_STATUS[reservation_status]}?booking={reservation_id}'
    return '/cancelled' if category == 'booking_cancelled' else '/queue'


@notifications_api_bp.route('/api/live', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_live_updates():
    """Server-Sent Events stream for this session's open page (see
    live_updates.py) - replaces the pages' fast polling."""
    return Response(
        stream(session.get('user_id'), session.get('session_version', 0)),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@notifications_api_bp.route('/api/notifications', methods=['GET'])
@role_required('admin', 'staff', 'customer')
def api_get_notifications():
    """Every account (customer/staff/admin) gets its own private notification
    feed, scoped entirely by the session's own user_id - there is no way for
    one customer to see another's notifications, or for staff to see
    admin-only ones, since a notification is only ever inserted for one
    specific user_id (see utils.create_notification/notify_role) and this
    query never reads anyone else's rows."""
    user_id = session.get('user_id')
    limit = min(max(request.args.get('limit', 30, type=int), 1), 100)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT notification_id, title, message, category, related_reservation_id, is_read, created_at
        FROM notifications
        WHERE user_id = ?
        ORDER BY notification_id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = [dict(row) for row in cursor.fetchall()]

    reservation_ids = sorted({row['related_reservation_id'] for row in rows if row['related_reservation_id']})
    status_by_reservation = {}
    if reservation_ids:
        cursor.execute(
            f"SELECT reservation_id, queue_status FROM reservations WHERE reservation_id IN ({','.join('?' * len(reservation_ids))})",
            reservation_ids,
        )
        status_by_reservation = {r['reservation_id']: r['queue_status'] for r in cursor.fetchall()}
    for row in rows:
        row['link'] = _notification_link(
            session.get('role'), row['category'], row['related_reservation_id'],
            status_by_reservation.get(row['related_reservation_id']),
        )

    cursor.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE user_id = ? AND is_read = 0",
        (user_id,),
    )
    unread_count = cursor.fetchone()['c']
    conn.close()
    return jsonify({'status': 'success', 'data': rows, 'unread_count': unread_count}), 200


@notifications_api_bp.route('/api/notifications/mark-read', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def api_mark_notification_read():
    """Ownership is enforced directly in the UPDATE's WHERE clause, not
    checked separately beforehand - a notification_id belonging to a
    different account simply matches zero rows here, the same
    read-it-from-the-record pattern the rest of this app uses rather than
    trusting a client-supplied owner."""
    data = request.get_json() or {}
    notification_id = data.get('notification_id')
    if not notification_id:
        return jsonify({'status': 'error', 'message': 'notification_id is required.'}), 400

    user_id = session.get('user_id')
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE notifications SET is_read = 1 WHERE notification_id = ? AND user_id = ?",
        (notification_id, user_id),
    )
    updated = cursor.rowcount
    conn.commit()
    conn.close()

    if updated == 0:
        return jsonify({'status': 'error', 'message': 'Notification not found.'}), 404
    return jsonify({'status': 'success'}), 200


@notifications_api_bp.route('/api/notifications/mark-all-read', methods=['POST'])
@role_required('admin', 'staff', 'customer')
def api_mark_all_notifications_read():
    user_id = session.get('user_id')
    conn = get_db_connection()
    conn.execute(
        "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0",
        (user_id,),
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'}), 200
