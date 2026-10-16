"""Persisted encrypted email payloads with atomic claims and bounded retries."""
from shop_clock import datetime
import hashlib
import json
import secrets
import time

from database import get_db_connection
from protected_data import encrypt, decrypt


def reconcile_events(conn, provider_id, recipient):
    """A callback may arrive before the send response; retain and apply it later."""
    from shop_clock import SHOP_ZONE
    event=conn.execute('''SELECT * FROM mail_delivery_events WHERE provider_message_id=? AND recipient=?
        ORDER BY event_timestamp DESC,state='bounced' DESC LIMIT 1''',(provider_id,recipient)).fetchone()
    if not event:
        return
    conn.execute('''UPDATE mail_outbox SET status=?,event_timestamp=?,
        delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END,
        last_error=CASE WHEN ?='bounced' THEN 'provider_delivery_rejected' ELSE NULL END
        WHERE provider_message_id=? AND recipient=? AND event_timestamp<=?
          AND status IN ('accepted','delivered','bounced')''',
        (event['state'],event['event_timestamp'],event['state'],
         datetime.fromtimestamp(event['event_timestamp'],SHOP_ZONE).strftime('%Y-%m-%d %H:%M:%S'),event['state'],
         provider_id,recipient,event['event_timestamp']))


def send(subject,recipient,heading,plain_text_body,highlight=None,*,dedupe_key=None,retryable=None):
    payload=dict(subject=subject,recipient=recipient,heading=heading,plain_text_body=plain_text_body,highlight=highlight)
    security=bool(highlight) or 'employee account access' in subject.lower()
    retryable=not security if retryable is None else retryable
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    key=dedupe_key or datetime.now().strftime('%Y-%m-%d')+':'+digest
    conn=get_db_connection()
    try:
        conn.execute('''INSERT OR IGNORE INTO mail_outbox
            (dedupe_key,recipient,subject,payload,created_at,expires_at,retryable)
            VALUES (?,?,?,?,?,?,?)''',(key,recipient,subject,encrypt(json.dumps(payload)),
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),time.time()+(600 if security else 86400),int(retryable)))
        row=conn.execute('SELECT mail_id,status FROM mail_outbox WHERE dedupe_key=?',(key,)).fetchone()
        conn.commit()
    finally:
        conn.close()
    if row['status'] in ('sent','accepted','delivered'): return row['mail_id']
    deliver(row['mail_id'],raise_errors=True)
    return row['mail_id']


def deliver(mail_id,raise_errors=False):
    now=time.time(); claim=secrets.token_hex(16)
    conn=get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM mail_outbox WHERE mail_id=?',(mail_id,)).fetchone()
        if not row or row['status'] in ('sent','accepted','delivered','bounced'): return False
        if row['expires_at']<=now:
            conn.execute("UPDATE mail_outbox SET status='expired',payload=NULL WHERE mail_id=?",(mail_id,)); conn.commit()
            if raise_errors: raise ValueError('This email has expired. Request a new message.')
            return False
        if row['status'] not in ('pending','failed') or row['next_attempt']>now or row['attempts']>=5 or not row['payload']:
            if raise_errors: raise ValueError('Email is already being delivered or awaiting retry. Check delivery status.')
            return False
        conn.execute("UPDATE mail_outbox SET status='sending',attempts=attempts+1,claim_token=?,lease_until=? WHERE mail_id=?",(claim,now+300,mail_id))
        conn.commit()
    finally:
        conn.close()
    error=None
    uncertain=False
    retryable=False
    provider_id=None
    try:
        if row['dedupe_key'].startswith('ready:'):
            booking_id=int(row['dedupe_key'].split(':')[1])
            conn=get_db_connection()
            try: booking=conn.execute('SELECT queue_status FROM reservations WHERE reservation_id=?',(booking_id,)).fetchone()
            finally: conn.close()
            if not booking or booking['queue_status']!='ready':
                raise ValueError('Vehicle is no longer awaiting pickup.')
        from utils import _deliver_branded_email
        provider_id=_deliver_branded_email(**json.loads(decrypt(row['payload'])),message_id=f'<md57-{mail_id}-{hashlib.sha256(row["dedupe_key"].encode()).hexdigest()[:16]}@notifications.md57>')
    except Exception as exc:
        # SMTP exceptions may include addresses or provider responses. Keep only
        # the exception class in the manager view, never codes, links or secrets.
        from email_transport import DeliveryError
        import smtplib
        if isinstance(exc, DeliveryError):
            error,uncertain,retryable=exc.code,exc.uncertain,exc.retryable
        else:
            error=type(exc).__name__
            # SMTP disconnects and unknown failures may happen after acceptance.
            uncertain=isinstance(exc,(OSError,smtplib.SMTPServerDisconnected))
            retryable=isinstance(exc,smtplib.SMTPConnectError)
    conn=get_db_connection()
    try:
        if error:
            conn.execute('''UPDATE mail_outbox SET status=?,last_error=?,next_attempt=?,lease_until=0,
                retryable=CASE WHEN ? THEN retryable ELSE 0 END,
                payload=CASE WHEN retryable=0 THEN NULL ELSE payload END
                WHERE mail_id=? AND claim_token=?''',('uncertain' if uncertain else 'failed',error,now+min(3600,60*2**row['attempts']),int(retryable or uncertain),mail_id,claim))
        else:
            sent_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("UPDATE mail_outbox SET status='accepted',sent_at=?,provider_message_id=?,payload=NULL,last_error=NULL,lease_until=0 WHERE mail_id=? AND claim_token=? AND status='sending'",
                         (sent_at,provider_id,mail_id,claim))
            if provider_id:
                reconcile_events(conn,provider_id,row['recipient'])
            if row['dedupe_key'].startswith('ready:'):
                conn.execute("UPDATE reservations SET ready_notified_at=? WHERE reservation_id=? AND queue_status='ready'",
                             (sent_at,int(row['dedupe_key'].split(':')[1])))
        conn.commit()
    finally: conn.close()
    if error and raise_errors:
        raise RuntimeError('Email delivery failed. Eligible operational messages will be retried automatically.')
    return not error


def process_outbox():
    now=time.time(); conn=get_db_connection()
    try:
        # A crash after SMTP accepted a message is ambiguous. Do not blindly
        # resend: leave it for a manager to inspect before requesting a retry.
        conn.execute("UPDATE mail_outbox SET status='uncertain',last_error='Delivery confirmation lost' WHERE status='sending' AND lease_until<?",(now,))
        conn.execute("UPDATE mail_outbox SET status='expired',payload=NULL WHERE expires_at<? AND status NOT IN ('sent','accepted','delivered','bounced','expired')",(now,))
        rows=conn.execute("SELECT mail_id FROM mail_outbox WHERE status IN ('pending','failed') AND retryable=1 AND attempts<5 AND next_attempt<=? AND expires_at>? ORDER BY mail_id LIMIT 20",(now,now)).fetchall()
        conn.commit()
    finally: conn.close()
    for row in rows: deliver(row['mail_id'])
