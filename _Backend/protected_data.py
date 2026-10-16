"""Authenticated encryption for authenticator secrets and pending mail payloads."""
import base64
import hashlib

from cryptography.fernet import Fernet
from flask import current_app


def cipher():
    secret = current_app.secret_key
    if isinstance(secret, str):
        secret = secret.encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b'md57-private-data-v1:' + secret).digest()))


def encrypt(value):
    return cipher().encrypt(value.encode()).decode()


def decrypt(value):
    return cipher().decrypt(value.encode()).decode()
