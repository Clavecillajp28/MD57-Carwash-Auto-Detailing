"""Per-service checklists with individual attribution and manager concern review."""
import json

from flask import Blueprint, jsonify, render_template, request, session
from database import get_db_connection
from utils import role_required, now_str, log_activity

quality_bp=Blueprint('quality',__name__)
STEPS={'service_done':'All steps of the requested service completed',
       'vehicle_checked':'Vehicle and work area checked for concerns',
       'final_check':'Final finish and quality checked'}


def allowed(booking):
    return booking and (session.get('role')=='admin' or booking['assigned_staff_id']==session.get('user_id'))


def readiness(conn,booking,manager=False):
    if not booking['checklist_required']: return None
    if session.get('role')=='staff' and booking['assigned_staff_id']!=session.get('user_id'):
        return jsonify(status='error',message='Only the assigned washer can mark this service done.'),403
    check=conn.execute('SELECT * FROM service_checks WHERE reservation_id=?',(booking['reservation_id'],)).fetchone()
    if not check or not all(json.loads(check['steps_json']).get(key) is True for key in STEPS):
        return jsonify(status='error',message='Complete the service quality checklist before marking this job done.',checklist_url=f'/service-checklist/{booking["reservation_id"]}'),409
    if manager and check['concern'] and not check['concern_resolved']:
        return jsonify(status='error',message='Review and resolve the recorded vehicle concern before confirming completion.'),409
    if manager:
        conn.execute('UPDATE service_checks SET manager_reviewed_by=?,manager_reviewed_at=? WHERE reservation_id=?',
                     (session['user_id'],now_str(),booking['reservation_id']))


@quality_bp.route('/service-checklist/<int:reservation_id>')
@role_required('admin','staff')
def quality_page(reservation_id):
    conn=get_db_connection()
    try: booking=conn.execute('SELECT * FROM reservations WHERE reservation_id=?',(reservation_id,)).fetchone()
    finally: conn.close()
    if not allowed(booking): return 'This checklist is not available to this account.',403
    return render_template('service_checklist.html',reservation_id=reservation_id,steps=STEPS)


@quality_bp.route('/api/service-checklist/<int:reservation_id>',methods=['GET','POST'])
@role_required('admin','staff')
def checklist_api(reservation_id):
    conn=get_db_connection()
    try:
        if request.method=='POST': conn.execute('BEGIN IMMEDIATE')
        booking=conn.execute('SELECT * FROM reservations WHERE reservation_id=?',(reservation_id,)).fetchone()
        if not allowed(booking): return jsonify(status='error',message='This is not an assigned job for this account.'),403
        check=conn.execute('SELECT * FROM service_checks WHERE reservation_id=?',(reservation_id,)).fetchone()
        if request.method=='POST':
            if booking['queue_status'] not in ('in_progress','pending_confirmation'):
                return jsonify(status='error',message='Only an active service checklist can be changed.'),409
            data=request.get_json(silent=True)
            if not isinstance(data,dict) or set(data)-{'steps','concern','concern_resolved','revision'}:
                return jsonify(status='error',message='Use only the checklist form fields.'),400
            steps=data.get('steps'); concern=data.get('concern','')
            if not isinstance(steps,dict) or set(steps)!=set(STEPS) or any(type(v) is not bool for v in steps.values()) or not isinstance(concern,str) or len(concern)>1000 or any(ord(c)<32 and c not in '\n\t' for c in concern):
                return jsonify(status='error',message='Enter valid steps and a concern of up to 1000 characters.'),400
            resolved=data.get('concern_resolved',False)
            if type(resolved) is not bool or (resolved and session['role']!='admin'):
                return jsonify(status='error',message='Only a manager or owner can resolve vehicle concerns.'),403
            revision=check['revision'] if check else 0
            if type(data.get('revision')) is not int or data['revision']!=revision:
                return jsonify(status='error',message='This checklist changed. Refresh before saving.'),409
            if check and check['concern'] and concern.strip()!=check['concern'] and session['role']!='admin':
                return jsonify(status='error',message='A recorded concern must be reviewed or revised by management.'),403
            if check and concern.strip()==check['concern'] and session['role']!='admin': resolved=bool(check['concern_resolved'])
            conn.execute('''INSERT INTO service_checks(reservation_id,steps_json,concern,concern_resolved,updated_by,updated_by_name,updated_at,revision)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(reservation_id) DO UPDATE SET steps_json=excluded.steps_json,
                concern=excluded.concern,concern_resolved=excluded.concern_resolved,updated_by=excluded.updated_by,
                updated_by_name=excluded.updated_by_name,updated_at=excluded.updated_at,revision=excluded.revision,
                manager_reviewed_by=NULL,manager_reviewed_at=NULL''',
                (reservation_id,json.dumps(steps),concern.strip(),int(resolved),session['user_id'],session['fullname'],now_str(),revision+1))
            conn.commit()
            check=conn.execute('SELECT * FROM service_checks WHERE reservation_id=?',(reservation_id,)).fetchone()
        data=dict(check) if check else dict(steps_json='{}',concern='',concern_resolved=False,revision=0)
        data['steps']=json.loads(data.pop('steps_json'))
        result=dict(plate=booking['vehicle_plate_number'],booking_status=booking['queue_status'],checklist=data,
                    can_edit=booking['queue_status'] in ('in_progress','pending_confirmation'),can_review=session['role']=='admin')
    finally: conn.close()
    if request.method=='POST': log_activity(session['user'],f'Saved quality checklist for booking MD57-{reservation_id}')
    return jsonify(status='success',**result)
