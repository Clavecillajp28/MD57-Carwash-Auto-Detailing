"""Live employee work must stay separate from generated presentation history."""
from datetime import date, datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_Backend'))
os.environ['SECRET_KEY'] = 'source-view-tests-only'
os.environ['DEFAULT_ADMIN_PASSWORD'] = 'TestOwner!57060'
os.environ['MD57_SEED_LEGACY_ACCOUNTS'] = 'false'

from flask import Flask
import database
import analytics
from demo_history import seed_demo_history
from blueprints.queue_api import queue_api_bp
from blueprints.staff_api import staff_api_bp
from blueprints.feedback_api import feedback_api_bp


class SourceViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = database.DB_FILE
        database.DB_FILE = str(Path(self.temp.name) / 'views.db')
        database.init_db()
        self.batch = seed_demo_history(date(2026, 10, 16))
        conn = database.get_db_connection()
        self.owner = conn.execute('SELECT user_id FROM users WHERE is_owner=1').fetchone()[0]
        self.employee = conn.execute("INSERT INTO users(username,fullname,password_hash,role) VALUES ('real-staff','Actual Employee','unused-test-only','staff')").lastrowid
        self.real_job = conn.execute('''INSERT INTO reservations
            (vehicle_plate_number,vehicle_type,service_id,queue_status,assigned_staff_id,
             created_at,started_at,completed_at,booking_date,is_synthetic)
            VALUES ('REAL-5701','Small',(SELECT MIN(service_id) FROM services),'completed',?,
                '2026-10-16 09:00:00','2026-10-16 09:10:00','2026-10-16 09:45:00','2026-10-16',0)''',
            (self.employee,)).lastrowid
        conn.commit()
        conn.close()
        self.app = Flask(__name__)
        self.app.secret_key = 'source-view-tests-only'
        for blueprint in (queue_api_bp, staff_api_bp, feedback_api_bp):
            self.app.register_blueprint(blueprint)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user='owner', user_id=self.owner, role='admin', is_owner=True)

    def tearDown(self):
        database.DB_FILE = self.original_db
        self.temp.cleanup()

    def test_history_defaults_live_and_presentation_uses_original_catalog(self):
        real = self.client.get('/api/get-history').get_json()
        self.assertEqual(real['source']['key'], 'real')
        self.assertEqual([r['id'] for r in real['data']], [self.real_job])
        demo = self.client.get('/api/get-history?source=synthetic').get_json()
        self.assertEqual(demo['total'], self.batch['reservations'])
        self.assertLessEqual(len(demo['data']), demo['limit'])
        self.assertTrue(all(r['is_synthetic'] == 1 for r in demo['data']))
        conn = database.get_db_connection()
        catalog = {r[0] for r in conn.execute('SELECT service_name FROM services WHERE is_active=1')}
        conn.close()
        self.assertTrue(all(r['service_type'] in catalog for r in demo['data']))
        self.assertEqual(self.client.get('/api/get-history?source=bad').status_code, 400)

    def test_staff_rankings_do_not_mix_identities(self):
        real = analytics.build(date(2026, 7, 20), date(2026, 10, 16))
        self.assertEqual(real['source']['key'], 'real')
        self.assertEqual([r['name'] for r in real['staff']['rows']], ['Actual Employee'])
        self.assertEqual(real['staff']['rows'][0]['jobs'], 1)
        presentation = analytics.build(date(2026, 7, 20), date(2026, 10, 16), source='synthetic')
        self.assertEqual({r['name'] for r in presentation['staff']['rows']}, {'Demo Staff 1', 'Demo Staff 2', 'Demo Staff 3'})
        live_staff = self.client.get('/api/staff-real-metrics').get_json()['data']
        self.assertEqual(len(live_staff), 1)
        self.assertEqual(live_staff[0]['completed_jobs'], 1)

    def test_staff_cannot_open_presentation_and_feedback_stays_live(self):
        with self.client.session_transaction() as session:
            session.update(user='real-staff', user_id=self.employee, role='staff', is_owner=False)
        self.assertEqual(self.client.get('/api/get-history?source=synthetic').status_code, 403)
        self.assertEqual(len(self.client.get('/api/get-history').get_json()['data']), 1)
        reviews = self.client.get('/api/get-feedbacks').get_json()['data']
        self.assertFalse(any('Simulated feedback' in (r['comment'] or '') for r in reviews))

    def test_archived_presentation_records_keep_source(self):
        from cleanup import archive_expired_cancelled_bookings
        # Exercise the actual archiver while keeping its text log private.
        import utils
        original_log = utils.ACTIVITY_LOG_FILE
        utils.ACTIVITY_LOG_FILE = str(Path(self.temp.name) / 'activity.log')
        try:
            archive_expired_cancelled_bookings()
        finally:
            utils.ACTIVITY_LOG_FILE = original_log
        demo = self.client.get('/api/get-history?source=synthetic').get_json()
        self.assertEqual(demo['total'], self.batch['reservations'])
        self.assertTrue(all(r['is_synthetic'] == 1 for r in demo['data']))

    def test_live_duration_estimates_exclude_generated_history(self):
        from service_estimates import ServiceEstimates
        conn = database.get_db_connection()
        try:
            job = dict(conn.execute('''SELECT r.service_id,r.vehicle_type,
                s.base_duration_minutes AS base_minutes,
                s.base_duration_minutes+v.multiplier_minutes AS duration_minutes
                FROM reservations r JOIN services s ON s.service_id=r.service_id
                JOIN vehicle_types v ON v.type_name=r.vehicle_type
                WHERE r.is_synthetic=1 AND r.queue_status='completed' LIMIT 1''').fetchone())
            estimates = ServiceEstimates(conn.cursor(), None, now=datetime(2026, 10, 17))
            duration, source, samples = estimates._duration(job)
            self.assertEqual(source, 'service_standard')
            self.assertEqual(samples, 0)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
