"""KMS & cryptographic key management for secrets at rest.

Provides authenticated AES-128-CBC encryption (via Fernet) with HMAC-SHA256
for signing secrets stored in PostgreSQL, preventing exposure via database dumps,
unprivileged replicas, or SQL injection vectors.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)

ENCRYPTION_PREFIX = "enc:v1:"


def _derive_fernet_key() -> bytes:
    """Derives a safe 32-byte URL-safe base64-encoded key for Fernet.

    Uses WEBHOOK_ENCRYPTION_KEY if provided in configuration/environment;
    otherwise derives deterministically from settings.SECRET_KEY.
    """
    raw_key = getattr(settings, "WEBHOOK_ENCRYPTION_KEY", None)
    if raw_key and isinstance(raw_key, str) and raw_key.strip():
        key_str = raw_key.strip()
        # If it's already a valid Fernet key (32 urlsafe base64 bytes), test it
        try:
            raw_bytes = key_str.encode("utf-8")
            Fernet(raw_bytes)
            return raw_bytes
        except Exception:
            # Derive 32-byte key via SHA-256
            return base64.urlsafe_b64encode(hashlib.sha256(key_str.encode("utf-8")).digest())

    # Non-development environments MUST supply a separate WEBHOOK_ENCRYPTION_KEY
    if not settings.IS_DEV:
        raise RuntimeError(
            "WEBHOOK_ENCRYPTION_KEY must be configured in non-development environments. "
            "Do not fall back to SECRET_KEY."
        )

    # Fallback: derive deterministically from application SECRET_KEY (development/test mode only)
    secret_bytes = settings.SECRET_KEY.encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(b"pulse-kms-webhook-v1:" + secret_bytes).digest())


def _get_fernet() -> Fernet:
    return Fernet(_derive_fernet_key())


def encrypt_webhook_secret(secret_plain: str) -> str:
    """Encrypts a plaintext webhook signing secret at rest.

    If the secret is already encrypted (starts with 'enc:v1:'), returns as-is.
    Returns: 'enc:v1:<fernet_ciphertext>'
    """
    if not secret_plain:
        return secret_plain
    if secret_plain.startswith(ENCRYPTION_PREFIX):
        return secret_plain

    fernet = _get_fernet()
    token = fernet.encrypt(secret_plain.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTION_PREFIX}{token}"


def decrypt_webhook_secret(secret_cipher: str) -> str:
    """Decrypts an encrypted webhook signing secret.

    If the secret lacks the 'enc:v1:' prefix (e.g., legacy unencrypted secrets
    created prior to migration), returns the string as-is for non-destructive backwards
    compatibility.
    """
    if not secret_cipher:
        return secret_cipher
    if not secret_cipher.startswith(ENCRYPTION_PREFIX):
        logger.warning(
            "Accessing legacy unencrypted webhook secret. Plaintext secrets are deprecated "
            "and will be rejected after the migration window. Run the data migration command "
            "('python -m app.commands.encrypt_webhook_secrets') to encrypt existing secrets at rest."
        )
        return secret_cipher

    token = secret_cipher[len(ENCRYPTION_PREFIX):]
    fernet = _get_fernet()
    try:
        return fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt webhook secret with current KMS key")
        raise ValueError("Decryption failed: corrupted ciphertext or invalid KMS key")


def mask_webhook_secret(secret: Optional[str]) -> str:
    """Returns a safe preview of the secret without exposing raw key material.

    Always reveals only the known prefix and masked bullets, e.g. 'whsec_••••••••••••••••'.
    """
    if not secret:
        return "••••••••••••••••"
    try:
        plain = decrypt_webhook_secret(secret)
    except Exception:
        return "••••••••••••••••"

    if plain.startswith("whsec_"):
        return "whsec_••••••••••••••••"
    return "••••••••••••••••"
