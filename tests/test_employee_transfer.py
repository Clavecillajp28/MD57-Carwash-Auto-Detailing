"""Private employee migration uses disposable databases and test credentials."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_Backend'))
os.environ['SECRET_KEY'] = 'employee-transfer-tests-only'
os.environ['DEFAULT_ADMIN_PASSWORD'] = 'TestOwner!57060'
os.environ['MD57_SEED_LEGACY_ACCOUNTS'] = 'false'

import database
from employee_transfer import export_employees, import_employees, _cipher
from werkzeug.security import generate_password_hash, check_password_hash

KEY = 'employee-transfer-tests-only'


class EmployeeTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_db = database.DB_FILE
        self.source = Path(self.temp.name) / 'source.db'
        database.DB_FILE = str(self.source)
        database.init_db()
        self.target = Path(self.temp.name) / 'target.db'
        conn = database.get_db_connection()
        target = sqlite3.connect(self.target)
        conn.backup(target)
        target.close()
        self.password_hash = generate_password_hash('OriginalPass!57')
        for username, name, role, disabled in [
            ('existing-staff', 'Original Employee', 'staff', None),
            ('existing-manager', 'Original Manager', 'admin', None),
            ('retired-staff', 'Retired Employee', 'staff', '2026-01-01'),
        ]:
            conn.execute('''INSERT INTO users(username,fullname,password_hash,role,disabled_at)
                VALUES (?,?,?,?,?)''', (username, name, self.password_hash, role, disabled))
        conn.commit()
        conn.close()
        self.token, self.count = export_employees(self.source, KEY)
        database.DB_FILE = str(self.target)

    def tearDown(self):
        database.DB_FILE = self.original_db
        self.temp.cleanup()

    def test_encrypted_additive_idempotent_and_passwords_preserved(self):
        self.assertEqual(self.count, 2)
        self.assertNotIn(b'existing-staff', self.token)
        self.assertEqual(import_employees(self.token, KEY), {'added': 2, 'skipped': 0})
        self.assertEqual(import_employees(self.token, KEY), {'added': 0, 'skipped': 2})
        conn = database.get_db_connection()
        try:
            for username in ['existing-staff', 'existing-manager']:
                user = conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
                self.assertTrue(check_password_hash(user['password_hash'], 'OriginalPass!57'))
                self.assertEqual(user['is_owner'], 0)
                self.assertIsNone(user['disabled_at'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users WHERE is_owner=1').fetchone()[0], 1)
            self.assertIsNone(conn.execute("SELECT 1 FROM users WHERE username='retired-staff'").fetchone())
        finally:
            conn.close()

    def test_tampering_wrong_key_oversize_and_role_escalation_rejected(self):
        for token, key in [(self.token, 'wrong-key'), (b'broken-file', KEY), (b'x' * (1024*1024+1), KEY)]:
            with self.assertRaises(ValueError):
                import_employees(token, key)
        body = json.loads(_cipher(KEY).decrypt(self.token))
        body['employees'][0]['role'] = 'owner'
        with self.assertRaises(ValueError):
            import_employees(_cipher(KEY).encrypt(json.dumps(body).encode()), KEY)

    def test_existing_credentials_not_overwritten(self):
        conn = database.get_db_connection()
        conn.execute("INSERT INTO users(username,password_hash,role) VALUES ('existing-staff',?,'staff')", (generate_password_hash('DeployedPass!57'),))
        conn.commit()
        conn.close()
        self.assertEqual(import_employees(self.token, KEY), {'added': 1, 'skipped': 1})
        conn = database.get_db_connection()
        try:
            hashed = conn.execute("SELECT password_hash FROM users WHERE username='existing-staff'").fetchone()[0]
            self.assertTrue(check_password_hash(hashed, 'DeployedPass!57'))
        finally:
            conn.close()

    def test_failed_import_rolls_back_all_accounts(self):
        conn = database.get_db_connection()
        count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        conn.execute("CREATE TRIGGER reject_manager BEFORE INSERT ON users WHEN NEW.username='existing-manager' BEGIN SELECT RAISE(ABORT,'test failure'); END")
        conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            import_employees(self.token, KEY)
        conn = database.get_db_connection()
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], count)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
