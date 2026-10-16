"""Owner Gmail verification, single-use recovery and legacy authenticator migration."""
import base64
import hashlib
import hmac
import secrets
import struct
import time

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash

from database import get_db_connection
from login_security import is_locked_out, record_login_attempt
from protected_data import decrypt
import owner_email_codes as email_codes
from utils import role_required, owner_only, log_activity, now_str

mfa_bp=Blueprint('mfa',__name__)


def totp(secret,counter,digits=6):
    key=base64.b32decode(secret)
    digest=hmac.new(key,struct.pack('>Q',counter),hashlib.sha1).digest()
    offset=digest[-1]&15
    return str((struct.unpack('>I',digest[offset:offset+4])[0]&0x7fffffff)%10**digits).zfill(digits)


def enabled(user_id):
    conn=get_db_connection()
    try:
        return bool(conn.execute('SELECT 1 FROM owner_mfa WHERE user_id=? AND enabled=1',(user_id,)).fetchone())
    finally: conn.close()


def recovery_hash(user_id,code):
    return hmac.new(str(current_app.secret_key).encode(),f'mfa-recovery:{user_id}:{code.strip().upper()}'.encode(),hashlib.sha256).hexdigest()


def verify(conn,user_id,code,purpose='verify',challenge_id=None):
    if not isinstance(code,str) or len(code)>64: return False
    state=conn.execute('SELECT * FROM owner_mfa WHERE user_id=?',(user_id,)).fetchone()
    if not state or not state['enabled']: return False
    changed=conn.execute('UPDATE mfa_recovery_codes SET used_at=? WHERE user_id=? AND code_hash=? AND used_at IS NULL',
                         (now_str(),user_id,recovery_hash(user_id,code))).rowcount
    if changed: return True
    if state['method']=='email':
        user=conn.execute('SELECT * FROM users WHERE user_id=?',(user_id,)).fetchone()
        return email_codes.verify(conn,user,purpose,challenge_id,code,expected_email=state['email'])
    key=decrypt(state['secret'])
    counter=int(time.time()//30)
    for step in (counter-1,counter,counter+1):
        if step>state['last_counter'] and hmac.compare_digest(code,totp(key,step)):
            conn.execute('UPDATE owner_mfa SET last_counter=? WHERE user_id=?',(step,user_id))
            return True
    return False


def issue_recovery(conn,user_id):
    codes=[secrets.token_hex(10).upper() for _ in range(10)]
    conn.execute('DELETE FROM mfa_recovery_codes WHERE user_id=?',(user_id,))
    conn.executemany('INSERT INTO mfa_recovery_codes(user_id,code_hash) VALUES (?,?)',
                     [(user_id,recovery_hash(user_id,c)) for c in codes])
    return codes


def require_recent():
    if enabled(session['user_id']) and not 0<=time.time()-session.get('mfa_verified_at',0)<=300:
        if request.method=='GET' and request.headers.get('Sec-Fetch-Mode')=='navigate':
            return redirect('/account-security')
        return jsonify(status='error',code='step_up_required',message='Verify your emailed security code in Account Security before this sensitive action.'),403


def pending_login(user, target=None):
    pending={'user_id':user['user_id'],'version':user['session_version'],'at':time.time(),'next':target}
    conn=get_db_connection()
    try: state=conn.execute('SELECT * FROM owner_mfa WHERE user_id=?',(user['user_id'],)).fetchone()
    finally: conn.close()
    if state and state['method']=='email':
        try: pending['challenge_id']=email_codes.issue(user,'login',email_codes.address(user,state))
        except email_codes.EmailCodeError as error: pending['delivery_error']=str(error)
    return pending


def current_identity(conn,data):
    user=conn.execute('SELECT * FROM users WHERE user_id=?',(session['user_id'],)).fetchone()
    if not user or user['disabled_at'] or user['session_version']!=session.get('session_version',0):
        raise ValueError('Your access changed. Sign in again.')
    if user['auth_provider']=='google':
        if not 0<=time.time()-session.get('authenticated_at',0)<=300:
            raise ValueError('Sign in again with Google before changing security settings.')
    elif not isinstance(data.get('current_password'),str) or not check_password_hash(user['password_hash'],data['current_password']):
        raise ValueError('Your current password is incorrect.')
    return user


@mfa_bp.route('/account-security')
@role_required('admin')
@owner_only
def security_page():
    return render_template('account_security.html')


@mfa_bp.route('/api/security/mfa',methods=['GET','POST'])
@role_required('admin')
@owner_only
def security_api():
    user_id=session['user_id']
    if request.method=='GET':
        conn=get_db_connection()
        try:
            user=conn.execute('SELECT * FROM users WHERE user_id=?',(user_id,)).fetchone()
            state=conn.execute('SELECT * FROM owner_mfa WHERE user_id=?',(user_id,)).fetchone()
        finally: conn.close()
        active=bool(state and state['enabled'])
        return jsonify(enabled=active,method=state['method'] if active else 'email',
                       email=state['email'] if state and state['method']=='email' and state['email'] else user['notify_email'] or '',
                       fresh=0<=time.time()-session.get('mfa_verified_at',0)<=300)
    data=request.get_json(silent=True)
    if (not isinstance(data,dict) or set(data)-{'action','code','current_password','email','purpose'}
            or not isinstance(data.get('action'),str)
            or ('purpose' in data and not isinstance(data['purpose'],str))):
        return jsonify(status='error',message='Use the security form fields.'),400
    login_key='mfa:'+str(user_id)
    if is_locked_out(login_key,request.remote_addr)[0]:
        return jsonify(status='error',message='Too many attempts. Try again in 15 minutes.'),429
    conn=get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        user=current_identity(conn,data)
        action=data.get('action')
        state=conn.execute('SELECT * FROM owner_mfa WHERE user_id=?',(user_id,)).fetchone()
        if action in ('setup','send'):
            purpose='enable' if action=='setup' else data.get('purpose','verify')
            if action=='setup':
                if state and state['enabled']:
                    if state['method']=='email': raise ValueError('Email verification is already enabled.')
                    if not verify(conn,user_id,data.get('code')):
                        raise ValueError('Verify your existing authenticator or recovery code before switching to email.')
                target=email_codes.gmail(data.get('email') or (state['email'] if state else None) or user['notify_email'])
            else:
                if not state or not state['enabled'] or state['method']!='email':
                    raise ValueError('Enable Gmail verification first.')
                if purpose not in ('verify','disable','recovery'): raise ValueError('Choose a supported security action.')
                target=email_codes.address(user,state)
            conn.commit();conn.close();conn=None
            challenge_id=email_codes.issue(user,purpose,target)
            challenges=dict(session.get('owner_email_challenges',{}));challenges[purpose]=challenge_id
            session['owner_email_challenges']=challenges
            return jsonify(status='success',message='Code sent to '+email_codes.masked(target)+'. It expires in five minutes.',
                           destination=email_codes.masked(target))
        if action=='enable':
            if state and state['enabled'] and state['method']=='email': raise ValueError('Email verification is already enabled.')
            challenge_id=session.get('owner_email_challenges',{}).get('enable')
            valid=email_codes.verify(conn,user,'enable',challenge_id,data.get('code'))
            target=conn.execute('SELECT email FROM owner_email_challenges WHERE challenge_id=?',(challenge_id,)).fetchone()
        elif action in ('verify','disable','recovery'):
            if not state or not state['enabled']: raise ValueError('Enable Gmail verification first.')
            valid=verify(conn,user_id,data.get('code'),purpose=action,
                         challenge_id=session.get('owner_email_challenges',{}).get(action))
        else: raise ValueError('Choose a supported security action.')
        if not valid:
            conn.commit()  # Persist per-code failures rather than rolling them back.
            raise ValueError('Incorrect, expired or already used email/recovery code.')
        codes=None
        if action=='enable':
            conn.execute('''INSERT INTO owner_mfa(user_id,enabled,method,email) VALUES (?,1,'email',?)
                ON CONFLICT(user_id) DO UPDATE SET enabled=1,method='email',email=excluded.email,
                secret=NULL,pending_secret=NULL,pending_until=NULL,last_counter=-1''',(user_id,target['email']))
            # The security destination is distinct from login/recovery contacts;
            # it may also belong to the same person's separate customer account.
            conn.execute('UPDATE password_resets SET used=1 WHERE user_id=?',(user_id,))
            conn.execute('UPDATE employee_invitations SET revoked_at=? WHERE user_id=? AND used_at IS NULL', (now_str(),user_id))
            codes=issue_recovery(conn,user_id)
        elif action=='disable':
            conn.execute('DELETE FROM owner_mfa WHERE user_id=?',(user_id,))
            conn.execute('DELETE FROM mfa_recovery_codes WHERE user_id=?',(user_id,))
        elif action=='recovery': codes=issue_recovery(conn,user_id)
        if action!='verify':
            conn.execute('DELETE FROM owner_email_challenges WHERE user_id=?',(user_id,))
            conn.execute('UPDATE users SET session_version=session_version+1 WHERE user_id=?',(user_id,))
        conn.commit()
    except ValueError as error:
        if conn is not None: conn.rollback(); conn.close()
        if not isinstance(error,email_codes.EmailCodeError): record_login_attempt(login_key,request.remote_addr,False)
        return jsonify(status='error',message=str(error)),getattr(error,'status',400)
    except Exception:
        if conn is not None: conn.rollback(); conn.close()
        raise
    else: conn.close()
    record_login_attempt(login_key,request.remote_addr,True)
    session['mfa_verified_at']=time.time()
    log_activity(session['user'],f'Owner email security action: {action}')
    if action!='verify': session.clear()
    return jsonify(status='success',message='Security settings updated. Save recovery codes privately before signing in again.' if codes else 'Email verification completed.' if action=='verify' else 'Email verification disabled; sign in again.',
                   recovery_codes=codes,signed_out=action!='verify')


@mfa_bp.route('/login/verify',methods=['GET','POST'])
def login_verify():
    pending=session.get('mfa_pending')
    if not pending or not 0<=time.time()-pending.get('at',0)<=300:
        session.clear(); return redirect('/login')
    error=pending.get('delivery_error')
    notice=None
    conn=get_db_connection()
    try:
        user=conn.execute('SELECT * FROM users WHERE user_id=?',(pending['user_id'],)).fetchone()
        state=conn.execute('SELECT * FROM owner_mfa WHERE user_id=?',(pending['user_id'],)).fetchone()
    finally: conn.close()
    if not user or user['disabled_at'] or user['session_version']!=pending['version'] or not user['is_owner']:
        session.clear();return redirect('/login?error=Account+access+changed.+Sign+in+again.')
    email_mode=bool(state and state['method']=='email')
    if request.method=='POST':
        error=None
        key='mfa:'+str(pending['user_id'])
        if request.form.get('action')=='resend':
            try:
                if is_locked_out(key,request.remote_addr)[0]:
                    raise email_codes.EmailCodeError('Too many attempts. Try again in 15 minutes.',429)
                if not email_mode: raise email_codes.EmailCodeError('Email verification is not configured.')
                challenge_id=email_codes.issue(user,'login',email_codes.address(user,state))
                pending={**pending,'challenge_id':challenge_id};pending.pop('delivery_error',None)
                session['mfa_pending']=pending
                notice='A new code was sent. Only the newest code will work.'
            except email_codes.EmailCodeError as failure: error=str(failure)
            return render_template('mfa_login.html',error=error,notice=notice,email_mode=email_mode,
                                   destination=email_codes.masked(state['email']) if email_mode else None)
        conn=get_db_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            user=conn.execute('SELECT * FROM users WHERE user_id=?',(pending['user_id'],)).fetchone()
            key='mfa:'+str(pending['user_id'])
            if is_locked_out(key,request.remote_addr)[0]:
                error='Too many attempts. Try again in 15 minutes.'
            elif not user or user['disabled_at'] or user['session_version']!=pending['version'] or not user['is_owner']:
                error='Account access changed. Sign in again.'
            elif not verify(conn,user['user_id'],request.form.get('code'),purpose='login',challenge_id=pending.get('challenge_id')):
                error='Incorrect, expired or already used code.'
            conn.commit()
        finally: conn.close()
        record_login_attempt(key,request.remote_addr,error is None)
        if not error:
            from blueprints.auth import _start_session, _is_local_path
            if not _start_session(user,mfa_verified=True): return redirect('/login')
            log_activity(user['username'],'Owner signed in with email verification or recovery code' if email_mode else 'Owner signed in with legacy authenticator or recovery code')
            target=pending.get('next')
            return redirect(target if _is_local_path(target) else '/')
    return render_template('mfa_login.html',error=error,notice=notice,email_mode=email_mode,
                           destination=email_codes.masked(state['email']) if email_mode else None)
