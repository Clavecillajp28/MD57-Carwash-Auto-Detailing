"""No real emails: validate HTTPS payloads and failure behavior with mocks."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_Backend'))
os.environ['SECRET_KEY'] = 'mail-tests-only'

from flask import Flask
import requests
from email_transport import send_brevo


class EmailTransportTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(BREVO_API_KEY='test-key-only',
                               MAIL_DEFAULT_SENDER='MD57 <sender@example.test>')

    def test_payload_uses_https_and_verified_sender(self):
        with self.app.app_context(), patch('email_transport.requests.post', return_value=Mock(status_code=201)) as post:
            send_brevo('Invitation', 'employee@example.test', 'Private invitation', '<p>Private invitation</p>')
            args, options = post.call_args
            self.assertEqual(args[0], 'https://api.brevo.com/v3/smtp/email')
            self.assertEqual(options['json']['sender']['email'], 'sender@example.test')
            self.assertEqual(options['json']['to'], [{'email': 'employee@example.test'}])
            self.assertEqual(options['headers']['api-key'], 'test-key-only')
            self.assertEqual(options['timeout'], (5, 15))
            self.assertFalse(options['allow_redirects'])

    def test_missing_configuration_never_calls_provider(self):
        with self.app.app_context(), patch('email_transport.requests.post') as post:
            self.app.config['BREVO_API_KEY'] = ''
            with self.assertRaises(RuntimeError):
                send_brevo('Subject', 'employee@example.test', 'Body', '<p>Body</p>')
            post.assert_not_called()

    def test_rejection_and_timeout_are_errors_without_retries(self):
        for response in [Mock(status_code=401), Mock(status_code=429), Mock(status_code=500)]:
            with self.app.app_context(), patch('email_transport.requests.post', return_value=response) as post:
                with self.assertRaises(RuntimeError):
                    send_brevo('Subject', 'employee@example.test', 'Body', '<p>Body</p>')
                self.assertEqual(post.call_count, 1)
        with self.app.app_context(), patch('email_transport.requests.post', side_effect=requests.Timeout('private-provider-detail')):
            with self.assertRaises(RuntimeError) as error:
                send_brevo('Subject', 'employee@example.test', 'Body', '<p>Body</p>')
            self.assertNotIn('private-provider-detail', str(error.exception))

    def test_branded_api_email_does_not_call_smtp(self):
        from utils import _deliver_branded_email
        self.app.config['MAIL_TRANSPORT'] = 'brevo'
        with self.app.app_context(), patch('email_transport.send_brevo') as send, patch('utils.mail.send') as smtp:
            _deliver_branded_email('Subject', 'employee@example.test', 'Verify account', 'Your code', highlight='<123>')
            smtp.assert_not_called()
            html = send.call_args.args[3]
            self.assertIn('MD57 Car Wash', html)
            self.assertIn('&lt;123&gt;', html)
            self.assertNotIn('cid:', html)


if __name__ == '__main__':
    unittest.main()
