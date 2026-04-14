"""
apps/dashboards/webhook_crypto.py
===================================
Symmetric encryption for WebhookEndpoint.secret using Fernet.

Why encryption (not hashing)?
------------------------------
Webhook secrets are used as HMAC keys when verifying incoming request
signatures.  Unlike passwords, we need to recover the original value at
verification time, so a one-way hash (bcrypt, PBKDF2) is not suitable.

Fernet provides AES-128-CBC with HMAC-SHA256 authentication, using a
key derived from Django's SECRET_KEY.  A DB dump now only exposes
ciphertext — an attacker also needs ``SECRET_KEY`` to recover secrets.

Key derivation
--------------
The Fernet key is the base64url-encoded SHA-256 digest of Django's
``SECRET_KEY``.  Rotating ``SECRET_KEY`` will invalidate all stored
secrets (existing webhooks will need regeneration) — this is documented
in the migration runbook.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _get_fernet() -> Fernet:
    raw = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt *plaintext* and return a base64-encoded ciphertext string."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """
    Decrypt *ciphertext* and return the original plaintext.

    Raises
    ------
    cryptography.fernet.InvalidToken
        If *ciphertext* was not produced by ``encrypt_secret`` with the
        current ``SECRET_KEY`` (e.g. after a key rotation).
    """
    return _get_fernet().decrypt(ciphertext.encode()).decode()
