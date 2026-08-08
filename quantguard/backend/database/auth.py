"""
Authentication: password hashing and session tokens.

This replaces "here's your raw API key, keep it safe" as the primary
way a human accesses their account, with normal login (username +
password -> session token) - the pattern people actually expect from
a real app.

API keys don't go away entirely - MT5's Expert Advisor and
TradingView's webhooks can't "log in" interactively, so they still
need a long-lived credential. What changes: a raw API key is no
longer handed out to anyone who posts an account name (that was a
real, unprotected gap). Now, only a logged-in, authenticated trader
can view/generate their own API key from within their account.

Password hashing uses PBKDF2-HMAC-SHA256 with a random salt per
user and 600,000 iterations (in line with current OWASP guidance for
PBKDF2) - deliberately using only Python's standard library (hashlib),
no extra dependency needed for this part.
"""

import hashlib
import hmac
import secrets
import time

PBKDF2_ITERATIONS = 600_000
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


def hash_password(password: str) -> tuple[str, str]:
    """Returns (password_hash_hex, salt_hex). Store both - you need
    the salt again to verify a future login attempt."""
    salt = secrets.token_hex(16)
    pw_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    )
    return pw_hash.hex(), salt


def verify_password(password: str, stored_hash_hex: str, salt_hex: str) -> bool:
    """Re-hashes the attempted password with the SAME salt and compares
    against the stored hash, using a constant-time comparison
    (hmac.compare_digest) so the comparison itself can't leak timing
    information about how much of the hash matched."""
    attempt_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(attempt_hash.hex(), stored_hash_hex)


def generate_session_token() -> str:
    """A random, unguessable session token - not a JWT, just a
    high-entropy random string looked up against a sessions table.
    Simpler to reason about and to revoke (just delete the row) than
    a self-contained signed token."""
    return "sess_" + secrets.token_urlsafe(32)


def session_expiry_timestamp() -> float:
    return time.time() + SESSION_TTL_SECONDS


def is_session_expired(expires_at: float) -> bool:
    return time.time() > expires_at
