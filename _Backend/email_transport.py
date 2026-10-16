"""HTTPS transactional mail for hosts that block outbound SMTP."""
from email.utils import parseaddr

import requests
from flask import current_app


def send_brevo(subject, recipient, text, html, message_id=None):
    config = current_app.config
    key = config.get('BREVO_API_KEY')
    sender = config.get('MAIL_DEFAULT_SENDER')
    if isinstance(sender, (list, tuple)) and len(sender) == 2:
        name, address = sender
    else:
        name, address = parseaddr(sender or '')
    if not key or not address or '@' not in address:
        raise RuntimeError('Configure BREVO_API_KEY and a verified MAIL_DEFAULT_SENDER.')
    payload = {
        'sender': {'name': name or 'MD57 Car Wash', 'email': address},
        'to': [{'email': recipient}], 'subject': subject,
        'textContent': text, 'htmlContent': html,
    }
    if message_id:
        payload['headers'] = {'X-Mailin-custom': f'md57-message-id:{message_id}'}
    # One attempt only. Existing outbox logic handles errors and retries;
    # never fall back to SMTP after an uncertain provider response.
    try:
        response = requests.post('https://api.brevo.com/v3/smtp/email',
            headers={'api-key': key, 'accept': 'application/json'},
            json=payload, timeout=(5, 15), allow_redirects=False)
    except requests.RequestException:
        raise RuntimeError('HTTPS email delivery failed. Check provider availability.') from None
    if response.status_code != 201:
        # Do not log response bodies: they can contain private account details.
        raise RuntimeError(f'Email provider rejected delivery (HTTP {response.status_code}).')
