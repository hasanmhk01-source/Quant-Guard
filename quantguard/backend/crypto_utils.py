"""
Encrypts broker credentials (each trader's own exchange API key +
secret) before they ever touch the database. Even if the database
file leaked, the stored values would be useless without this
server's master key - which lives only in an environment variable,
never in the database itself.

Requires a QUANTGUARD_MASTER_KEY environment variable - generate one
ONCE with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
and set it as an environment variable on your server. Losing this key
means every trader's stored broker credentials become permanently
unrecoverable - they'd need to reconnect their broker. Back it up
somewhere safe (a password manager, not a code file).
"""

import os
from cryptography.fernet import Fernet, InvalidToken


def _get_cipher() -> Fernet:
    key = os.environ.get("QUANTGUARD_MASTER_KEY")
    if not key:
        raise RuntimeError(
            "QUANTGUARD_MASTER_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
            "and set it as an environment variable before storing or reading broker credentials."
        )
    return Fernet(key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _get_cipher().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _get_cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError(
            "Could not decrypt this credential - either QUANTGUARD_MASTER_KEY has "
            "changed since it was stored, or the stored value is corrupted."
        )
