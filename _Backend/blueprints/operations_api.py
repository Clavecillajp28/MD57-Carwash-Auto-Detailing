"""Owner recovery controls and manager delivery/audit visibility."""
from datetime import datetime
import json
import time

from flask import Blueprint, current_app, jsonify, render_template, request, send_file, session
from database import get_db_connection
from utils import role_required, owner_only, is_owner, log_activity
import backups

operations_bp=Blueprint('operations',__name__)


@operations_bp.route('/api/operations/employees/import', methods=['POST'])
@role_required('admin')
@owner_only
def employee_import():
    from employee_transfer import import_employees, MAX_BYTES
    # Bound uploads before parsing multipart data, independently of other routes.
    request.max_content_length = MAX_BYTES + 16384
    upload = request.files.get('file')
    if not upload:
        return jsonify(status='error', message='Choose your encrypted employee transfer file.'), 400
    try:
        result = import_employees(upload.read(MAX_BYTES + 1), current_app.secret_key)
    except ValueError as error:
        return jsonify(status='error', message=str(error)), 400
    log_activity(session['user'], f'Imported employee logins: {result["added"]} added, {result["skipped"]} already present')
    return jsonify(status='success', **result)


@operations_bp.route('/operations')
@role_required('admin')
def operations_page():
    return render_template('operations.html')


@operations_bp.route('/api/operations/status')
@role_required('admin')
def operations_status():
    from runtime import version
    runtime=current_app.config.get('RUNTIME_INFO',{})
    conn=get_db_connection()
    try:
        jobs=[dict(row) for row in conn.execute('SELECT name,last_started,last_success,last_error FROM background_jobs ORDER BY name')] if is_owner() else []
        counts={r['status']:r['count'] for r in conn.execute('SELECT status,COUNT(*) count FROM mail_outbox WHERE ?=1 OR retryable=1 GROUP BY status',(int(is_owner()),))}
    finally: conn.close()
    return jsonify(runtime={k:v for k,v in runtime.items() if k not in ('workspace',)},
                   disk_version=version(),restart_needed=bool(runtime) and runtime['version']!=version(),
                   jobs=jobs,delivery_counts=counts,can_manage_backups=is_owner(),can_inspect_jobs=is_owner())


@operations_bp.route('/api/operations/backups',methods=['GET','POST'])
@role_required('admin')
@owner_only
def backup_list():
    if request.method=='POST':
        result=backups.create_backup()
        drill=backups.recovery_drill(result['name'])
        log_activity(session['user'],f'Created and verified backup {result["name"]}')
        return jsonify(status='success',backup=result,recovery_drill=drill)
    return jsonify(backups=backups.list_backups())


@operations_bp.route('/api/operations/backups/<name>/<action>',methods=['GET','POST'])
@role_required('admin')
@owner_only
def backup_action(name,action):
    try:
        if action=='download' and request.method=='GET':
            path=backups.resolve(name)
            backups.recovery_drill(name)
            log_activity(session['user'],f'Downloaded verified backup {name}')
            return send_file(path,as_attachment=True,download_name=name,mimetype='application/octet-stream')
        if action=='verify' and request.method=='POST':
            drill=backups.recovery_drill(name)
            log_activity(session['user'],f'Verified backup recovery for {name}')
            return jsonify(status='success',**drill)
    except (ValueError,OSError,KeyError,json.JSONDecodeError):
        return jsonify(status='error',message='Backup is missing or failed validation.'),400
    return jsonify(status='error',message='Unsupported backup action.'),400


@operations_bp.route('/api/operations/deliveries')
@role_required('admin')
def delivery_list():
    status=request.args.get('status','')
    if status and status not in ('pending','sending','sent','failed','uncertain','expired'):
        return jsonify(status='error',message='Unknown delivery status.'),400
    conn=get_db_connection()
    try:
        rows=conn.execute('''SELECT mail_id,recipient,subject,status,attempts,created_at,sent_at,last_error,
            retryable,expires_at FROM mail_outbox WHERE (?='' OR status=?) AND (?=1 OR retryable=1)
            ORDER BY mail_id DESC LIMIT 100''',(status,status,int(is_owner()))).fetchall()
    finally: conn.close()
    return jsonify(deliveries=[dict(row) for row in rows])


@operations_bp.route('/api/operations/deliveries/<int:mail_id>/retry',methods=['POST'])
@role_required('admin')
def retry_delivery(mail_id):
    data=request.get_json(silent=True) or {}
    conn=get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM mail_outbox WHERE mail_id=?',(mail_id,)).fetchone()
        if not row or not row['retryable'] or row['expires_at']<=time.time() or not row['payload'] or row['status'] not in ('failed','uncertain'):
            return jsonify(status='error',message='This email cannot be retried. Expired verification links and codes require a new request.'),409
        if row['status']=='uncertain' and (not isinstance(data,dict) or data.get('acknowledge_duplicate_risk') is not True):
            return jsonify(status='error',message='Delivery is uncertain. Confirm that resending could create a duplicate.'),400
        conn.execute("UPDATE mail_outbox SET status='pending',next_attempt=0,attempts=0,claim_token=NULL WHERE mail_id=?",(mail_id,))
        conn.commit()
    finally: conn.close()
    log_activity(session['user'],f'Requested retry for operational email #{mail_id}')
    return jsonify(status='success',message='Retry queued. The delivery worker will attempt it shortly.')


@operations_bp.route('/api/operations/audit')
@role_required('admin')
def audit_list():
    clauses=[]; params=[]
    for key,column in (('entity','entity'),('entity_id','entity_id'),('actor_id','actor_id')):
        if request.args.get(key): clauses.append(column+'=?'); params.append(request.args[key][:100])
    for key,operator in (('start','>='),('end','<=')):
        if request.args.get(key):
            try: day=datetime.strptime(request.args[key],'%Y-%m-%d').strftime('%Y-%m-%d')
            except ValueError: return jsonify(status='error',message='Use YYYY-MM-DD dates.'),400
            clauses.append('occurred_at '+operator+' ?'); params.append(day+(' 23:59:59' if key=='end' else ' 00:00:00'))
    if request.args.get('search'):
        clauses.append('(actor_name LIKE ? OR action LIKE ? OR reason LIKE ?)')
        params.extend(['%'+request.args['search'][:100]+'%']*3)
    limit=min(100,max(1,request.args.get('limit',50,type=int)))
    offset=max(0,request.args.get('offset',0,type=int))
    where=' AND '.join(clauses) or '1=1'
    conn=get_db_connection()
    try:
        total=conn.execute('SELECT COUNT(*) FROM audit_records WHERE '+where,params).fetchone()[0]
        rows=conn.execute('SELECT * FROM audit_records WHERE '+where+' ORDER BY audit_id DESC LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
    finally: conn.close()
    return jsonify(records=[dict(row) for row in rows],total=total,limit=limit,offset=offset)
