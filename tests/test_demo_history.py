"""Exercise demo generation against disposable databases, never shop records."""
from datetime import date
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_Backend'))
# config.py loads defaults from .env, but these explicit test values win.
os.environ['SECRET_KEY'] = 'demo-history-test-only'
os.environ['DEFAULT_ADMIN_PASSWORD'] = 'TestOwner!57060'
os.environ['MD57_SEED_LEGACY_ACCOUNTS'] = 'false'

import database
from demo_history import seed_demo_history, workdays


class DemoHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = database.DB_FILE
        database.DB_FILE = str(Path(self.temp.name) / 'test.db')
        database.init_db()

    def tearDown(self):
        database.DB_FILE = self.original_db
        self.temp.cleanup()

    def test_sixty_days_integrity_and_no_bay_or_staff_overlaps(self):
        result = seed_demo_history(date(2026, 10, 16))
        self.assertEqual(result['workdays'], 60)
        self.assertGreater(result['completed'], 600)
        self.assertGreater(result['cancelled'], 0)
        conn = database.get_db_connection()
        try:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('SELECT COUNT(DISTINCT booking_date) FROM reservations WHERE is_synthetic=1').fetchone()[0], 60)
            rows = conn.execute('SELECT * FROM reservations WHERE is_synthetic=1 ORDER BY started_at').fetchall()
            ends = {}
            for row in rows:
                self.assertLess(date.fromisoformat(row['booking_date']).weekday(), 5)
                self.assertIsNone(row['predicted_wait_minutes'])
                if row['queue_status'] == 'cancelled':
                    self.assertIsNone(row['started_at'])
                    continue
                self.assertLessEqual(row['arrived_at'], row['started_at'])
                self.assertLess(row['started_at'], row['completed_at'])
                self.assertLess(row['completed_at'], row['picked_up_at'])
                self.assertEqual(row['picked_up_by'], row['assigned_staff_id'])
                self.assertTrue(row['picked_up_by_name'].strip())
                for field in ('customer_name','vehicle_plate_number','vehicle_model','vehicle_type',
                              'booking_date','booking_time','created_at','completed_at','picked_up_at'):
                    self.assertTrue(row[field], field)
                key = (row['booking_date'], row['assigned_bay'])
                if key in ends:
                    self.assertLessEqual(ends[key], row['started_at'])
                ends[key] = row['completed_at']
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM staff_attendance WHERE is_synthetic=1').fetchone()[0], 180)
            for staff_id in result['staff_ids']:
                self.assertIsNotNone(conn.execute('SELECT disabled_at FROM users WHERE user_id=?', (staff_id,)).fetchone()[0])
        finally:
            conn.close()

    def test_idempotent_and_preserves_existing_accounts(self):
        conn = database.get_db_connection()
        before = [tuple(r) for r in conn.execute('SELECT * FROM users')]
        conn.close()
        first = seed_demo_history(date(2026, 10, 16))
        second = seed_demo_history(date(2026, 10, 15))
        self.assertTrue(second['already_loaded'])
        self.assertEqual(first['start'], second['start'])
        conn = database.get_db_connection()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM reservations').fetchone()[0], first['reservations'])
            self.assertEqual([tuple(conn.execute('SELECT * FROM users WHERE user_id=?', (r[0],)).fetchone()) for r in before], before)
        finally:
            conn.close()

    def test_failure_rolls_back_batch_and_staff(self):
        conn = database.get_db_connection()
        count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        # Fail after staff and attendance insertion, verifying the entire batch
        # rolls back rather than leaving a partially generated presentation.
        conn.execute("CREATE TRIGGER fail_demo BEFORE INSERT ON reservations BEGIN SELECT RAISE(ABORT,'test failure'); END")
        conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            seed_demo_history(date(2026, 10, 16))
        conn = database.get_db_connection()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], count)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM staff_attendance').fetchone()[0], 0)
        finally:
            conn.close()

    def test_existing_batch_pickup_details_repaired_without_changing_live_records(self):
        summary = seed_demo_history(date(2026, 10, 16))
        conn = database.get_db_connection()
        first = conn.execute("SELECT reservation_id FROM reservations WHERE is_synthetic=1 AND queue_status='completed' LIMIT 1").fetchone()[0]
        # Emulate an older deployed batch, plus an archived presentation row.
        conn.execute("UPDATE reservations SET picked_up_by=NULL,picked_up_by_name=NULL WHERE reservation_id=?", (first,))
        archived = first + 100000
        conn.execute('''INSERT INTO reservations_archive
            (reservation_id,vehicle_plate_number,queue_status,assigned_staff_id,
             booking_date,completed_at,is_synthetic) VALUES (?, 'DEMO-OLD-0001',
             'completed',?,'2026-10-16','2026-10-16 12:00:00',1)''', (archived, summary['staff_ids'][0]))
        real = conn.execute('''INSERT INTO reservations
            (vehicle_plate_number,vehicle_type,queue_status,assigned_staff_id,booking_date,completed_at,is_synthetic)
            VALUES ('REAL-5701','Small','completed',?,'2026-10-16','2026-10-16 12:00:00',0)''',
            (summary['staff_ids'][0],)).lastrowid
        conn.commit()
        conn.close()
        self.assertTrue(seed_demo_history(date(2026, 10, 16))['already_loaded'])
        conn = database.get_db_connection()
        try:
            for table, identity in [('reservations', first), ('reservations_archive', archived)]:
                row = conn.execute(f'SELECT * FROM {table} WHERE reservation_id=?', (identity,)).fetchone()
                self.assertTrue(row['picked_up_at'])
                self.assertTrue(row['picked_up_by_name'])
                self.assertIn(row['picked_up_by'], summary['staff_ids'])
            untouched = conn.execute('SELECT * FROM reservations WHERE reservation_id=?', (real,)).fetchone()
            self.assertIsNone(untouched['picked_up_by'])
            self.assertIsNone(untouched['picked_up_by_name'])
            self.assertIsNone(untouched['picked_up_at'])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        finally:
            conn.close()

    def test_synthetic_history_excluded_from_real_training(self):
        seed_demo_history(date(2026, 10, 16))
        from ai_retrain import _fetch_real_training_rows
        self.assertEqual(_fetch_real_training_rows(), [])


if __name__ == '__main__':
    unittest.main()
