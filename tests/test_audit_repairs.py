"""Audit regressions against disposable storage; external delivery is mocked."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'_Backend'))
import database
from shop_clock import datetime


class AuditRepairsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap=tempfile.TemporaryDirectory()
        original=database.DB_FILE
        database.DB_FILE=str(Path(cls.bootstrap.name)/'bootstrap.db')
        with patch.dict(os.environ,dict(SECRET_KEY='isolated-audit-test-key',DEFAULT_ADMIN_PASSWORD='AuditOwner!5706',
                MD57_DEMO_HISTORY='false',MD57_BACKGROUND_JOBS='false',GOOGLE_CLIENT_ID='',GOOGLE_CLIENT_SECRET='',
                SESSION_COOKIE_SECURE='false',WEATHER_API_KEY='',PUBLIC_BASE_URL='')):
            from app import app
            cls.app=app
        database.DB_FILE=original

    @classmethod
    def tearDownClass(cls):
        cls.bootstrap.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.original=database.DB_FILE
        database.DB_FILE=str(Path(self.temp.name)/'audit.db')
        self.addCleanup(setattr,database,'DB_FILE',self.original)
        self.addCleanup(self.temp.cleanup)
        import utils
        self.log=patch.object(utils,'ACTIVITY_LOG_FILE',str(Path(self.temp.name)/'activity.log'))
        self.log.start();self.addCleanup(self.log.stop)
        with patch.dict(os.environ,DEFAULT_ADMIN_PASSWORD='AuditOwner!5706'):
            database.init_db()
        self.app.config.update(TESTING=True,WTF_CSRF_ENABLED=False,LIVE_UPDATES=False,
                               BREVO_WEBHOOK_TOKEN='webhook-test-only',MD57_EMPLOYEE_TRANSFER_KEY=None)
        self.deliver=patch('utils._deliver_branded_email',return_value='<test-provider-id>')
        self.deliver.start();self.addCleanup(self.deliver.stop)
        self.weather=patch('blueprints.queue_api.get_weather_reading',return_value={'condition':'Clear','rain_mm':0})
        self.weather.start();self.addCleanup(self.weather.stop)
        conn=database.get_db_connection()
        self.owner=dict(conn.execute('SELECT * FROM users WHERE is_owner=1').fetchone())
        self.customer=conn.execute("INSERT INTO users(username,password_hash,role,fullname) VALUES ('race-customer','unused','customer','Race Customer')").lastrowid
        self.staff=conn.execute("INSERT INTO users(username,password_hash,role,fullname) VALUES ('worker','unused','staff','Worker')").lastrowid
        self.service=conn.execute("SELECT service_id FROM services WHERE is_active=1 AND default_bay='Bay #1' LIMIT 1").fetchone()[0]
        conn.commit();conn.close()
        self.client=self.app.test_client()
        self.sign_in(self.client,self.owner['user_id'])

    def sign_in(self,client,identity):
        conn=database.get_db_connection()
        user=dict(conn.execute('SELECT * FROM users WHERE user_id=?',(identity,)).fetchone());conn.close()
        with client.session_transaction() as session:
            session.clear();session.update(user=user['username'],user_id=identity,role=user['role'],
                is_owner=bool(user['is_owner']),session_version=user['session_version'])

    def test_reset_request_email_recipient_cooldown_and_password_change(self):
        from werkzeug.security import check_password_hash
        conn=database.get_db_connection()
        conn.execute("UPDATE users SET notify_email='recovery@example.test' WHERE user_id=?",(self.customer,))
        conn.commit();conn.close()
        with patch('blueprints.auth.send_branded_email') as send:
            response=self.client.post('/forgot-password',data={'username':'race-customer'})
            self.assertEqual(response.status_code,200)
            packet=send.call_args.kwargs
            self.assertEqual(packet['recipient'],'recovery@example.test')
            self.assertEqual(len(packet['highlight']),6)
            self.assertTrue(packet['highlight'].isdigit())
            self.assertIn('10 minutes',packet['plain_text_body'])
            self.client.post('/forgot-password',data={'username':'race-customer'})
            send.assert_called_once()
        response=self.client.post('/reset-password',data=dict(username='race-customer',otp=packet['highlight'],
            password='ResetGood!5706',confirm_password='ResetGood!5706'))
        self.assertEqual(response.status_code,302)
        conn=database.get_db_connection()
        self.assertTrue(check_password_hash(conn.execute('SELECT password_hash FROM users WHERE user_id=?',(self.customer,)).fetchone()[0],'ResetGood!5706'))
        conn.close()

    def test_reset_expiry_attempt_limit_and_delivery_failure(self):
        with patch('blueprints.auth.send_branded_email',side_effect=RuntimeError('test delivery failed')),patch('blueprints.auth.log_activity') as audit:
            response=self.client.post('/forgot-password',data={'username':'race-customer'})
            self.assertEqual(response.status_code,200)
            audit.assert_called_with('race-customer','Password reset requested - email delivery failed')
        conn=database.get_db_connection()
        row=conn.execute('SELECT * FROM password_resets WHERE user_id=?',(self.customer,)).fetchone()
        token=row['token'];reset_id=row['reset_id']
        conn.execute('UPDATE password_resets SET expires_at=? WHERE reset_id=?',
            ((datetime.now()-timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S'),reset_id))
        conn.commit();conn.close()
        packet=dict(username='race-customer',otp=token,password='ResetGood!5706',confirm_password='ResetGood!5706')
        self.assertIn(b'Invalid or expired code',self.client.post('/reset-password',data=packet).data)
        conn=database.get_db_connection()
        conn.execute('UPDATE password_resets SET expires_at=?,attempts=5 WHERE reset_id=?',
            ((datetime.now()+timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S'),reset_id))
        conn.commit();conn.close()
        self.assertIn(b'Too many incorrect attempts',self.client.post('/reset-password',data=packet).data)

    def test_reset_is_single_use_under_concurrency_and_revokes_sessions(self):
        import blueprints.auth as auth
        conn=database.get_db_connection()
        conn.execute('INSERT INTO password_resets(user_id,token,expires_at) VALUES (?,?,?)',
            (self.customer,'570601',(datetime.now()+timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit();conn.close()
        barrier=threading.Barrier(2)
        original=auth.generate_password_hash
        def synchronized_hash(value):
            barrier.wait(timeout=10)
            return original(value)
        def reset(password):
            with self.app.test_client() as client:
                return client.post('/reset-password',data=dict(username='race-customer',otp='570601',password=password,confirm_password=password)).status_code
        with patch.object(auth,'generate_password_hash',side_effect=synchronized_hash),ThreadPoolExecutor(2) as pool:
            results=list(pool.map(reset,['FirstPass!5706','SecondPass!5706']))
        self.assertEqual(sorted(results),[200,302])
        conn=database.get_db_connection()
        self.assertEqual(conn.execute('SELECT session_version FROM users WHERE user_id=?',(self.customer,)).fetchone()[0],1)
        conn.close()

    def test_reschedules_cannot_claim_the_same_slot(self):
        tomorrow=(datetime.now()+timedelta(days=1)).strftime('%Y-%m-%d')
        conn=database.get_db_connection();ids=[]
        for plate,slot in [('RACE-1','08:00 AM'),('RACE-2','09:00 AM')]:
            ids.append(conn.execute('''INSERT INTO reservations(user_id,vehicle_plate_number,vehicle_type,service_id,assigned_bay,booking_date,booking_time,queue_status,booking_source)
                VALUES (?,?,'Small',?,'Bay #1',?,?,'waiting','Online Booking')''',(self.customer,plate,self.service,tomorrow,slot)).lastrowid)
        conn.commit();conn.close()
        barrier=threading.Barrier(2)
        def move(identity):
            with self.app.test_client() as client:
                self.sign_in(client,self.customer)
                barrier.wait(timeout=10)
                return client.post('/api/reschedule-booking',json=dict(reservation_id=identity,booking_date=tomorrow,booking_time='12:00 PM')).status_code
        with patch('blueprints.queue_api.is_date_staffed',return_value=True),ThreadPoolExecutor(2) as pool:
            results=list(pool.map(move,ids))
        self.assertEqual(sorted(results),[200,400])

    def test_booking_retry_replays_and_key_conflict_is_rejected(self):
        packet=dict(customer_name='Audit Customer',plate_number='AUD-5701',vehicle_type='Small',service_id=self.service,booking_source='Walk-in Customer')
        with patch('blueprints.queue_api.is_within_business_hours',return_value=True):
            first=self.client.post('/api/submit-booking',json=packet,headers={'Idempotency-Key':'audit-retry-1'})
            second=self.client.post('/api/submit-booking',json=packet,headers={'Idempotency-Key':'audit-retry-1'})
            conflict=self.client.post('/api/submit-booking',json={**packet,'plate_number':'OTHER-1'},headers={'Idempotency-Key':'audit-retry-1'})
        self.assertEqual(first.status_code,200,first.get_json())
        self.assertEqual(first.get_json(),second.get_json())
        self.assertEqual(conflict.status_code,409)
        conn=database.get_db_connection()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM reservations').fetchone()[0],1);conn.close()

    def test_invalid_json_and_field_types_are_client_errors(self):
        for path in ['/api/admin/invitations','/register','/api/submit-booking','/api/staff-attendance']:
            for packet in [[],['invalid'],{'fullname':12},{'email':{'invalid':1}}]:
                self.assertEqual(self.client.post(path,json=packet).status_code,400,(path,packet))

    def test_attendance_rejects_bad_clock_and_backwards_times(self):
        base=dict(staff_user_id=self.staff,date_str=(datetime.now()-timedelta(days=1)).strftime('%Y-%m-%d'),status='Present')
        for times in [('not-time','--:--'),('09:00 AM','08:00 AM'),('--:--','--:--')]:
            result=self.client.post('/api/staff-attendance',json={**base,'time_in':times[0],'time_out':times[1]})
            self.assertEqual(result.status_code,400)

    def test_catalog_deletion_and_empty_feedback_survive_restart(self):
        conn=database.get_db_connection()
        conn.execute("DELETE FROM vehicle_models WHERE model_name='Toyota Vios'")
        conn.commit();conn.close()
        database.init_db()
        conn=database.get_db_connection()
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM feedbacks').fetchone()[0],0)
        self.assertIsNone(conn.execute("SELECT 1 FROM vehicle_models WHERE model_name='Toyota Vios'").fetchone());conn.close()

    def test_optional_google_and_csrf_enforcement(self):
        self.assertEqual(self.client.get('/auth/google').status_code,503)
        self.assertNotIn(b'class="google-btn"',self.client.get('/login').data)
        self.app.config['WTF_CSRF_ENABLED']=True
        try:
            result=self.client.post('/api/admin/invitations',json={})
            self.assertEqual(result.status_code,400)
            self.assertIn('CSRF',result.get_json()['message'])
            # Session cookies cannot authenticate the provider endpoint.
            self.assertEqual(self.client.post('/api/mail/brevo-events',json={}).status_code,401)
        finally:
            self.app.config['WTF_CSRF_ENABLED']=False

    def test_staff_can_refresh_missing_csrf_then_request_clock_in(self):
        self.sign_in(self.client,self.staff)
        self.app.config['WTF_CSRF_ENABLED']=True
        try:
            response=self.client.post('/api/staff-clock-in-request',json={},headers={'X-CSRFToken':'stale'})
            self.assertEqual(response.status_code,400)
            self.assertEqual(response.get_json()['code'],'csrf_failed')
            fresh=self.client.get('/api/csrf-token')
            self.assertEqual(fresh.status_code,200)
            self.assertIn('no-store',fresh.headers['Cache-Control'])
            with patch('blueprints.staff_api.is_clock_in_open',return_value=True):
                response=self.client.post('/api/staff-clock-in-request',json={},
                    headers={'X-CSRFToken':fresh.get_json()['csrf_token']})
            self.assertEqual(response.status_code,200,response.get_json())
            self.assertEqual(self.app.test_client().get('/api/csrf-token').status_code,401)
        finally:
            self.app.config['WTF_CSRF_ENABLED']=False

    def test_mail_acceptance_webhook_replay_and_authentication(self):
        from mail_outbox import send
        with self.app.app_context():
            identity=send('Notice','employee@example.test','Notice','Body')
        conn=database.get_db_connection()
        self.assertEqual(conn.execute('SELECT status FROM mail_outbox WHERE mail_id=?',(identity,)).fetchone()[0],'accepted');conn.close()
        payload={'event':'delivered','email':'employee@example.test','message-id':'<test-provider-id>','ts_event':time.time()}
        self.assertEqual(self.client.post('/api/mail/brevo-events',json=payload).status_code,401)
        headers={'Authorization':'Bearer webhook-test-only'}
        self.assertEqual(self.client.post('/api/mail/brevo-events',json=payload,headers=headers).status_code,200)
        self.client.post('/api/mail/brevo-events',json={**payload,'event':'hard_bounce','ts_event':payload['ts_event']-1},headers=headers)
        conn=database.get_db_connection()
        row=conn.execute('SELECT status,delivered_at FROM mail_outbox WHERE mail_id=?',(identity,)).fetchone()
        self.assertEqual(row['status'],'delivered');self.assertTrue(row['delivered_at']);conn.close()

    def test_unknown_delivery_is_not_automatically_retried(self):
        from email_transport import DeliveryError
        from mail_outbox import send, process_outbox
        with self.app.app_context(),patch('utils._deliver_branded_email',side_effect=DeliveryError('provider_response_unknown',uncertain=True)) as delivery:
            with self.assertRaises(RuntimeError):send('Notice','employee@example.test','Notice','Body')
            process_outbox()
            self.assertEqual(delivery.call_count,1)
        conn=database.get_db_connection()
        self.assertEqual(conn.execute('SELECT status,last_error FROM mail_outbox').fetchone()['status'],'uncertain');conn.close()

    def test_delivery_callback_before_send_response_is_not_lost(self):
        from mail_outbox import send
        headers={'Authorization':'Bearer webhook-test-only'}
        def deliver(**kwargs):
            response=self.client.post('/api/mail/brevo-events',json={'event':'delivered',
                'email':'employee@example.test','message-id':'<early-event>','ts_event':time.time()},headers=headers)
            self.assertEqual(response.status_code,200)
            return '<early-event>'
        with self.app.app_context(),patch('utils._deliver_branded_email',side_effect=deliver):
            identity=send('Notice','employee@example.test','Notice','Body')
        conn=database.get_db_connection()
        self.assertEqual(conn.execute('SELECT status FROM mail_outbox WHERE mail_id=?',(identity,)).fetchone()[0],'delivered');conn.close()

    def test_direct_app_entrypoint_passes_existing_application_to_runtime(self):
        import runpy
        with patch('runtime.main') as main,patch('database.init_db') as initialize,patch.dict(os.environ,PUBLIC_BASE_URL='',MD57_BACKGROUND_JOBS='false',MD57_DEMO_HISTORY='false'):
            namespace=runpy.run_path(str(Path(__file__).resolve().parents[1]/'_Backend/app.py'),run_name='__main__')
        initialize.assert_called_once()
        main.assert_called_once_with(namespace['app'])

    def test_shop_clock_and_spreadsheet_cells(self):
        from spreadsheet import safe_cell
        local=datetime.now();utc=datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertAlmostEqual((local-utc).total_seconds()/3600,8,places=2)
        self.assertEqual(safe_cell(' =HYPERLINK("bad")'),'\' =HYPERLINK("bad")')
        self.assertEqual(safe_cell(-42),-42)

    def test_simultaneous_clock_in_approvals_only_create_one_attendance(self):
        today=datetime.now().strftime('%Y-%m-%d')
        conn=database.get_db_connection()
        conn.execute("INSERT INTO staff_attendance(staff_user_id,staff_name,date_str,status,time_in,time_out) VALUES (?,'Worker',?,'Pending','08:00 AM','--:--')",(self.staff,today))
        conn.commit();conn.close()
        barrier=threading.Barrier(2)
        def confirm(_):
            with self.app.test_client() as client:
                self.sign_in(client,self.owner['user_id']);barrier.wait(timeout=10)
                return client.post('/api/confirm-clock-in',json={'staff_user_id':self.staff}).status_code
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(confirm,range(2)))
        self.assertEqual(results.count(200),1)
        self.assertTrue(all(status in (200,400,409) for status in results))
        conn=database.get_db_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM staff_attendance WHERE status='Present'").fetchone()[0],1);conn.close()

    def test_booking_to_checklist_confirmation_and_single_pickup(self):
        today=datetime.now().strftime('%Y-%m-%d')
        conn=database.get_db_connection()
        conn.execute("INSERT INTO staff_attendance(staff_user_id,staff_name,date_str,status,time_in,time_out) VALUES (?,'Worker',?,'Present','08:00 AM','--:--')",(self.staff,today))
        conn.commit();conn.close()
        with patch('blueprints.queue_api.is_within_business_hours',return_value=True):
            response=self.client.post('/api/submit-booking',json=dict(customer_name='Customer',plate_number='FLOW-5701',vehicle_type='Small',service_id=self.service,booking_source='Walk-in Customer'))
        self.assertEqual(response.status_code,200,response.get_json())
        identity=int(response.get_json()['booking_id'].split('-')[1]);packet={'reservation_id':identity}
        self.assertEqual(self.client.post('/api/start-service',json=packet).status_code,200)
        self.sign_in(self.client,self.staff)
        self.assertEqual(self.client.post('/api/complete-service',json=packet).status_code,409)
        from blueprints.quality_api import STEPS
        result=self.client.post(f'/api/service-checklist/{identity}',json={'steps':dict.fromkeys(STEPS,True),'concern':'','concern_resolved':False,'revision':0})
        self.assertEqual(result.status_code,200,result.get_json())
        self.assertEqual(self.client.post('/api/complete-service',json=packet).status_code,200)
        self.assertEqual(self.client.post('/api/confirm-complete',json=packet).status_code,403)
        self.sign_in(self.client,self.owner['user_id'])
        self.assertEqual(self.client.post('/api/confirm-complete',json=packet).status_code,200)
        self.assertEqual(self.client.post('/api/confirm-pickup',json=packet).status_code,200)
        self.assertIn(self.client.post('/api/confirm-pickup',json=packet).status_code,(404,409))
        history=self.client.get('/api/get-history?limit=1').get_json()
        self.assertEqual(history['total'],1)
        self.assertEqual(history['data'][0]['picked_up_by'],self.owner['user_id'])
        self.assertTrue(history['data'][0]['picked_up_at'])

    def test_pending_confirmation_holds_bay_and_verified_backup_is_separate(self):
        import scheduling
        import backups
        now=datetime.now().replace(hour=10,minute=0,second=0,microsecond=0)
        conn=database.get_db_connection()
        conn.execute("INSERT INTO reservations(vehicle_plate_number,vehicle_type,service_id,assigned_bay,queue_status,started_at) VALUES ('HOLD-1','Small',?,'Bay #1','pending_confirmation',?)",(self.service,now.strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        problem=scheduling.check_time(conn.cursor(),now.strftime('%Y-%m-%d'),'12:00 PM',30,'Bay #1',now=now)
        self.assertEqual(problem['reason'],'busy');conn.close()
        with patch.dict(os.environ,MD57_BACKUP_DIR=str(Path(self.temp.name)/'backups')):
            result=backups.create_backup()
            self.assertTrue(backups.recovery_drill(result['name'])['verified'])
            with self.assertRaises(ValueError):backups.restore_copy(result['name'],database.DB_FILE)

    def test_history_paginates_filters_and_exports_full_result(self):
        conn=database.get_db_connection()
        for index in range(8):
            conn.execute("INSERT INTO reservations(customer_name,vehicle_plate_number,vehicle_type,service_id,queue_status,completed_at) VALUES (? ,?,'Small',?,'completed','2026-10-16 10:00:00')",("=Formula" if index==0 else f'Customer {index}',f'PAGE-{index}',self.service))
        conn.commit();conn.close()
        first=self.client.get('/api/get-history?limit=3').get_json()
        second=self.client.get('/api/get-history?limit=3&page=2').get_json()
        self.assertEqual(first['total'],8)
        self.assertEqual(len(first['data']),3)
        self.assertTrue(set(r['id'] for r in first['data']).isdisjoint(r['id'] for r in second['data']))
        self.assertEqual(self.client.get('/api/get-history?search=PAGE-0').get_json()['total'],1)
        response=self.client.get('/api/get-history?format=csv')
        import csv,io
        rows=list(csv.reader(io.StringIO(response.data.decode('utf-8-sig'))))
        self.assertEqual(len(rows),9)
        self.assertEqual(rows[-1][0],"'=Formula")


if __name__=='__main__':unittest.main()
