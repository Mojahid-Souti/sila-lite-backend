"""Fernet field-level encryption for secrets stored at rest.

Used for any sensitive value we must keep but never expose: n8n per-user
passwords and MCP server auth tokens. Keyed by SILA_FIELD_ENCRYPTION_KEY.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from sila.core.config import settings


class FieldDecryptionError(RuntimeError):
    """Raised when a stored ciphertext can't be decrypted (key rotated/corrupt)."""


def _fernet() -> Fernet:
    key = settings.sila_field_encryption_key
    if not key:
        raise RuntimeError(
            "SILA_FIELD_ENCRYPTION_KEY is unset — required to encrypt secrets at rest"
        )
    return Fernet(key.encode())


def encrypt_field(plaintext: str) -> bytes:
    """Encrypt a secret for storage. Returns Fernet ciphertext bytes."""
    return _fernet().encrypt(plaintext.encode())


def decrypt_field(ciphertext: bytes) -> str:
    """Decrypt a stored secret. Raises FieldDecryptionError if it can't."""
    try:
        return _fernet().decrypt(ciphertext).decode()
    except InvalidToken as err:
        raise FieldDecryptionError("field decryption failed (encryption key rotated?)") from err
