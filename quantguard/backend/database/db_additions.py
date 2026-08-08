"""
ADD THESE to your existing backend/database/db.py

New tables:
- users: replaces bare API-key creation with real password login
- sessions: short-lived tokens issued at login (not JWTs - just
  random tokens looked up against this table, so revoking one is as
  simple as deleting its row)
- broker_credentials: each trader's OWN exchange API key/secret,
  encrypted at rest (see crypto_utils.py) - this is what makes
  Binance/exchange trading genuinely multi-tenant instead of
  everyone sharing your one account

The existing api_keys table is NOT removed - MT5's Expert Advisor and
TradingView's webhooks still need a long-lived credential (they can't
"log in" interactively). What changes: a trader now only ever sees
their own API key from inside their logged-in account - it's no
longer handed out to anyone who posts an account name.
"""

# --- Add to init_db()'s CREATE TABLE block ---
NEW_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    account_id TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_credentials (
    account_id TEXT PRIMARY KEY,
    broker_name TEXT NOT NULL,
    encrypted_api_key TEXT NOT NULL,
    encrypted_api_secret TEXT NOT NULL,
    testnet INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""

# --- Add these functions anywhere in db.py ---
PYTHON_FUNCTIONS = '''
from . import auth as auth_module  # adjust import path to wherever auth.py lives
from . import crypto_utils


# --- Users / login -----------------------------------------------------
def get_api_key_for_account(account_id: str) -> str | None:
    """Looks up an account's existing API key (for the logged-in
    /my-api-key endpoint), so a trader gets back the SAME key every
    time instead of accidentally generating duplicates."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT api_key FROM api_keys WHERE account_id = ? AND active = 1", (account_id,)
        ).fetchone()
    return row["api_key"] if row else None


def create_user(account_id: str, password: str) -> bool:
    """Creates a new trader account with a password. Returns False if
    the account_id is already taken (does not overwrite)."""
    pw_hash, salt = auth_module.hash_password(password)
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO users (account_id, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
                (account_id, pw_hash, salt, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # account_id already exists


def verify_login(account_id: str, password: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT password_hash, salt FROM users WHERE account_id = ?", (account_id,)
        ).fetchone()
    if not row:
        return False
    return auth_module.verify_password(password, row["password_hash"], row["salt"])


def create_session(account_id: str) -> str:
    token = auth_module.generate_session_token()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sessions (token, account_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, account_id, datetime.now(timezone.utc).isoformat(), auth_module.session_expiry_timestamp()),
        )
        conn.commit()
    return token


def get_account_for_session(token: str) -> str | None:
    """Returns the account_id for a valid, non-expired session token, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT account_id, expires_at FROM sessions WHERE token = ?", (token,)
        ).fetchone()
    if not row:
        return None
    if auth_module.is_session_expired(row["expires_at"]):
        return None
    return row["account_id"]


def delete_session(token: str):
    """Logout - deletes the session so the token can never be used again."""
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


# --- Per-trader broker credentials --------------------------------------
def save_broker_credentials(account_id: str, broker_name: str, api_key: str, api_secret: str, testnet: bool = True):
    """Stores a trader's OWN exchange credentials, encrypted. Overwrites
    any previously stored credentials for this account (one broker
    connection per account at a time, in this version)."""
    encrypted_key = crypto_utils.encrypt_secret(api_key)
    encrypted_secret = crypto_utils.encrypt_secret(api_secret)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO broker_credentials (account_id, broker_name, encrypted_api_key, encrypted_api_secret, testnet, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                broker_name = excluded.broker_name,
                encrypted_api_key = excluded.encrypted_api_key,
                encrypted_api_secret = excluded.encrypted_api_secret,
                testnet = excluded.testnet,
                created_at = excluded.created_at
            """,
            (account_id, broker_name, encrypted_key, encrypted_secret, 1 if testnet else 0,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_broker_credentials(account_id: str) -> dict | None:
    """Returns {'broker_name', 'api_key', 'api_secret', 'testnet'} with
    the DECRYPTED key/secret, or None if this account hasn't connected
    a broker yet. Only call this server-side, right before building a
    connector - never send the decrypted secret back to the browser."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM broker_credentials WHERE account_id = ?", (account_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "broker_name": row["broker_name"],
        "api_key": crypto_utils.decrypt_secret(row["encrypted_api_key"]),
        "api_secret": crypto_utils.decrypt_secret(row["encrypted_api_secret"]),
        "testnet": bool(row["testnet"]),
    }


def has_broker_connected(account_id: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM broker_credentials WHERE account_id = ?", (account_id,)
        ).fetchone()
    return row is not None


def delete_broker_credentials(account_id: str):
    with get_connection() as conn:
        conn.execute("DELETE FROM broker_credentials WHERE account_id = ?", (account_id,))
        conn.commit()
'''
