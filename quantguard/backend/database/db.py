"""
QuantGuard database layer.

Uses SQLite - no server to install, no connection string, built into
Python already. This stores exactly the two things that matter most
right now:

1. orders  - every order submitted, its risk-check result, and its
             execution result. This is also your audit trail.
2. positions - running position per (account, symbol), so future
             rules (drawdown kill-switch, position limits) have real
             data to check against instead of looking at one order
             in isolation.

Upgrading to PostgreSQL later means changing the connection string in
one place (get_connection) - the rest of the app doesn't need to know
or care which database is underneath.
"""

import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "quantguard.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets us access columns by name
    return conn


def init_db():
    """Creates the tables if they don't exist yet. Safe to call every startup."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                account_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                approved INTEGER NOT NULL,
                rule_results TEXT NOT NULL,
                execution TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                account_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 0,
                avg_price REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (account_id, symbol)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_pnl (
                account_id TEXT NOT NULL,
                date TEXT NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (account_id, date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                api_key TEXT PRIMARY KEY,
                account_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                password_hash TEXT
            )
        """)
        # Migration for databases created before password_hash existed -
        # CREATE TABLE IF NOT EXISTS above only applies to brand-new DBs,
        # so an existing quantguard.db needs the column added explicitly.
        # SQLite has no "ADD COLUMN IF NOT EXISTS", so check first.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
        if "password_hash" not in existing_cols:
            conn.execute("ALTER TABLE api_keys ADD COLUMN password_hash TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_token TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broker_connections (
                account_id TEXT PRIMARY KEY,
                broker_name TEXT NOT NULL,
                api_key TEXT NOT NULL,
                api_secret TEXT NOT NULL,
                testnet INTEGER NOT NULL DEFAULT 1,
                connected_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                conversation TEXT NOT NULL,
                strategy_json TEXT NOT NULL,
                status TEXT NOT NULL,
                questions TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mt5_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                mt5_ticket TEXT,
                fill_price REAL,
                error_message TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()


def save_order(entry: dict) -> int:
    """Saves one order record. Returns its new row id."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders (timestamp, account_id, symbol, side, quantity, price,
                                 approved, rule_results, execution)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["timestamp"],
                entry["account_id"],
                entry["symbol"],
                entry["side"],
                entry["quantity"],
                entry["price"],
                1 if entry["approved"] else 0,
                json.dumps(entry["rule_results"]),
                json.dumps(entry["execution"]) if entry["execution"] else None,
            ),
        )
        conn.commit()
        return cur.lastrowid


def get_orders(limit: int = 200) -> list[dict]:
    """Returns most recent orders first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_order_dict(r) for r in rows]


def _row_to_order_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "account_id": row["account_id"],
        "symbol": row["symbol"],
        "side": row["side"],
        "quantity": row["quantity"],
        "price": row["price"],
        "approved": bool(row["approved"]),
        "rule_results": json.loads(row["rule_results"]),
        "execution": json.loads(row["execution"]) if row["execution"] else None,
    }


def update_position(account_id: str, symbol: str, side: str, quantity: float, price: float) -> float:
    """
    Updates the running position for an account+symbol after a filled
    order, tracking a real average cost basis (not just quantity) so
    realized profit/loss can be computed correctly - this is what the
    kill-switch (see risk_engine.rules.KillSwitchRule) checks against.

    Handles four cases, same as standard average-cost-basis accounting:
    - Opening a new position from flat: cost basis = this trade's price.
    - Adding to an existing position (same direction): cost basis
      becomes the weighted average of old and new.
    - Partially or fully closing a position (opposite direction, not
      flipping): realizes P&L on the closed portion; cost basis of any
      remaining open portion is unchanged.
    - Flipping direction (opposite direction, MORE than closes the
      existing position): realizes P&L on the old position, then opens
      a new position in the other direction at this trade's price.

    Returns the realized P&L from this specific trade (0 if the trade
    only opened or added to a position, since nothing was closed).
    """
    delta = quantity if side == "BUY" else -quantity

    with get_connection() as conn:
        row = conn.execute(
            "SELECT quantity, avg_price FROM positions WHERE account_id = ? AND symbol = ?",
            (account_id, symbol),
        ).fetchone()
        existing_qty = row["quantity"] if row else 0.0
        existing_avg = row["avg_price"] if row else 0.0

        new_qty = existing_qty + delta
        realized_pnl = 0.0

        same_direction = existing_qty == 0 or (existing_qty > 0) == (delta > 0)

        if same_direction:
            # Opening from flat, or adding to an existing position in the
            # same direction - no closing happens, just a new weighted
            # average cost basis.
            if new_qty != 0:
                new_avg = ((existing_qty * existing_avg) + (delta * price)) / new_qty
            else:
                new_avg = 0.0  # a same-direction trade landing exactly at 0 only happens if delta was 0
        else:
            # Opposite direction: this trade is closing some or all of
            # the existing position (and possibly flipping past zero
            # into a new position on the other side).
            closed_qty = min(abs(delta), abs(existing_qty))
            if existing_qty > 0:
                realized_pnl = closed_qty * (price - existing_avg)   # was long, selling - profit if price rose
            else:
                realized_pnl = closed_qty * (existing_avg - price)   # was short, covering - profit if price fell

            if abs(delta) > abs(existing_qty):
                new_avg = price  # flipped past zero - the new leftover exposure's cost basis is this trade's price
            else:
                new_avg = existing_avg  # any remaining open portion keeps the same cost basis

        conn.execute(
            """
            INSERT INTO positions (account_id, symbol, quantity, avg_price)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(account_id, symbol)
            DO UPDATE SET quantity = excluded.quantity, avg_price = excluded.avg_price
            """,
            (account_id, symbol, new_qty, new_avg),
        )
        conn.commit()

    if realized_pnl != 0.0:
        _record_realized_pnl(account_id, realized_pnl)

    return realized_pnl


def get_position(account_id: str, symbol: str) -> float:
    """Returns current position size for an account+symbol (0 if none)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT quantity FROM positions WHERE account_id = ? AND symbol = ?",
            (account_id, symbol),
        ).fetchone()
        return row["quantity"] if row else 0.0


def get_position_detail(account_id: str, symbol: str) -> dict:
    """Returns {'quantity': ..., 'avg_price': ...} for an account+symbol
    (both 0 if no position exists). Used when rehydrating a strategy's
    live monitoring state after a server restart - the quantity tells
    us whether it's currently in a position, and avg_price becomes the
    resumed entry_price for stop-loss/take-profit tracking."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT quantity, avg_price FROM positions WHERE account_id = ? AND symbol = ?",
            (account_id, symbol),
        ).fetchone()
        return {"quantity": row["quantity"], "avg_price": row["avg_price"]} if row else {"quantity": 0.0, "avg_price": 0.0}


def get_all_positions(account_id: str) -> list[dict]:
    """Returns all NON-ZERO positions (a symbol you've fully closed out
    of doesn't show up) with quantity and average cost basis - avg_price
    is what lets the dashboard show unrealized P&L, not just "you hold X"."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT symbol, quantity, avg_price FROM positions WHERE account_id = ? AND quantity != 0",
            (account_id,),
        ).fetchall()
        return [{"symbol": r["symbol"], "quantity": r["quantity"], "avg_price": r["avg_price"]} for r in rows]


# --- API keys -------------------------------------------------------------
# One key per account. The key is how a trader's strategy authenticates -
# every order request must include it, and it determines whose account
# the order belongs to. No key, no orders.

import secrets
from datetime import datetime, timezone


def create_api_key(account_id: str) -> str:
    """
    Generates a new API key for an account and stores it.
    If this account already has a key, raises an error rather than
    silently creating a second one (one key per account, on purpose -
    keeps "which key belongs to whom" unambiguous).
    """
    api_key = "qg_" + secrets.token_urlsafe(32)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, account_id, created_at, active) VALUES (?, ?, ?, 1)",
            (api_key, account_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return api_key


def get_account_for_key(api_key: str) -> str | None:
    """Returns the account_id that owns this key, or None if the key is invalid/inactive."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT account_id FROM api_keys WHERE api_key = ? AND active = 1",
            (api_key,),
        ).fetchone()
        return row["account_id"] if row else None


def revoke_api_key(api_key: str):
    """Deactivates a key without deleting it, so past orders keep their audit trail."""
    with get_connection() as conn:
        conn.execute("UPDATE api_keys SET active = 0 WHERE api_key = ?", (api_key,))
        conn.commit()


# --- Passwords & sessions ---------------------------------------------------
# Login/session layer for the dashboard (separate from the API key system
# above, which stays as-is for order/strategy requests). A session_token
# proves "this browser is logged in as this account"; the account's API
# key is then handed to the frontend once (via /my-api-key) and cached
# client-side, same as before.
#
# Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib only, no new
# dependency) - salted per-account, 200k iterations. Never store or
# compare plaintext passwords.

import hashlib


def _hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt.hex() + "$" + digest.hex()


def _verify_password(password: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    salt_hex, _ = stored.split("$", 1)
    return _hash_password(password, bytes.fromhex(salt_hex)) == stored


def create_account_with_password(account_id: str, password: str) -> str:
    """
    Creates a new account with a password AND issues its API key in the
    same row, in one step - this is what /signup calls. Raises if the
    account_id is already taken (same one-account-one-key rule as
    create_api_key).
    """
    api_key = "qg_" + secrets.token_urlsafe(32)
    password_hash = _hash_password(password)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, account_id, created_at, active, password_hash) "
            "VALUES (?, ?, ?, 1, ?)",
            (api_key, account_id, datetime.now(timezone.utc).isoformat(), password_hash),
        )
        conn.commit()
    return api_key


def verify_login(account_id: str, password: str) -> bool:
    """Checks a login attempt's password against the stored hash. Returns
    False (not an error) for both 'wrong password' and 'no such account' -
    the caller should show the same generic message either way, so a
    login form can't be used to discover which account names exist."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT password_hash FROM api_keys WHERE account_id = ? AND active = 1",
            (account_id,),
        ).fetchone()
        if not row:
            return False
        return _verify_password(password, row["password_hash"])


def create_session(account_id: str) -> str:
    session_token = "sess_" + secrets.token_urlsafe(32)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sessions (session_token, account_id, created_at) VALUES (?, ?, ?)",
            (session_token, account_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return session_token


def get_account_for_session(session_token: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT account_id FROM sessions WHERE session_token = ?",
            (session_token,),
        ).fetchone()
        return row["account_id"] if row else None


def delete_session(session_token: str):
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE session_token = ?", (session_token,))
        conn.commit()


def get_api_key_for_account(account_id: str) -> str | None:
    """Looks up an account's existing API key - what /my-api-key calls
    after login, since the key itself was already issued at signup."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT api_key FROM api_keys WHERE account_id = ? AND active = 1",
            (account_id,),
        ).fetchone()
        return row["api_key"] if row else None


# --- Per-account broker connections -----------------------------------------
# NOTE: storing these lets /broker/connect and /broker/status work, but
# actually EXECUTING orders through each account's own connected broker
# (instead of the single shared broker built from env vars in main.py)
# is a separate change still needed in main.py/broker.py - see the
# accompanying explanation. This layer is safe to add now regardless.

def save_broker_connection(account_id: str, broker_name: str, api_key: str, api_secret: str, testnet: bool):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO broker_connections (account_id, broker_name, api_key, api_secret, testnet, connected_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "broker_name=excluded.broker_name, api_key=excluded.api_key, "
            "api_secret=excluded.api_secret, testnet=excluded.testnet, connected_at=excluded.connected_at",
            (account_id, broker_name, api_key, api_secret, 1 if testnet else 0,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_broker_connection(account_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT broker_name, api_key, api_secret, testnet, connected_at FROM broker_connections "
            "WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "broker_name": row["broker_name"],
            "api_key": row["api_key"],
            "api_secret": row["api_secret"],
            "testnet": bool(row["testnet"]),
            "connected_at": row["connected_at"],
        }


# --- Strategies ------------------------------------------------------------
# A strategy conversation: the trader's description(s), the structured
# result the parser produced, and its current status. `conversation`
# stores the full back-and-forth (as a JSON list of strings) so there's
# always a record of exactly what the trader said that led to this
# strategy - important for auditing an automated system.

def save_strategy(account_id: str, conversation: list[str], strategy_dict: dict,
                   status: str, questions: list[str] = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO strategies (account_id, created_at, conversation, strategy_json, status, questions)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(conversation),
                json.dumps(strategy_dict),
                status,
                json.dumps(questions or []),
            ),
        )
        conn.commit()
        return cur.lastrowid or 0


def update_strategy(strategy_id: int, conversation: list[str], strategy_dict: dict,
                     status: str, questions: list[str] = None):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE strategies
            SET conversation = ?, strategy_json = ?, status = ?, questions = ?
            WHERE id = ?
            """,
            (
                json.dumps(conversation),
                json.dumps(strategy_dict),
                status,
                json.dumps(questions or []),
                strategy_id,
            ),
        )
        conn.commit()


def get_strategy(strategy_id: int, account_id: str) -> dict | None:
    """Returns None if the strategy doesn't exist OR doesn't belong to this account -
    a trader should never be able to read or modify another account's strategy."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM strategies WHERE id = ? AND account_id = ?",
            (strategy_id, account_id),
        ).fetchone()
        return _row_to_strategy_dict(row) if row else None


def get_strategies(account_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM strategies WHERE account_id = ? ORDER BY id DESC",
            (account_id,),
        ).fetchall()
        return [_row_to_strategy_dict(r) for r in rows]


def get_all_active_strategies() -> list[dict]:
    """
    Returns every ACTIVE strategy across ALL accounts - used once, on
    server startup, to rebuild the in-memory monitors that were lost
    when the process last stopped. Without this, a strategy marked
    ACTIVE in the database would silently NOT be monitored after a
    restart, even though the dashboard would still show it as active -
    a dangerous, misleading gap this closes.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM strategies WHERE status = 'ACTIVE'"
        ).fetchall()
        return [_row_to_strategy_dict(r) for r in rows]


def _row_to_strategy_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "created_at": row["created_at"],
        "conversation": json.loads(row["conversation"]),
        "strategy": json.loads(row["strategy_json"]),
        "status": row["status"],
        "questions": json.loads(row["questions"]) if row["questions"] else [],
    }


# --- Daily realized P&L (feeds the kill-switch) ----------------------------
def _record_realized_pnl(account_id: str, amount: float, date: str = None):
    """Adds `amount` (positive or negative) to an account's realized P&L
    for the given date (today, UTC, if not specified). Called
    automatically by update_position whenever a trade closes some or
    all of an existing position."""
    date = date or datetime.now(timezone.utc).date().isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO daily_pnl (account_id, date, realized_pnl)
            VALUES (?, ?, ?)
            ON CONFLICT(account_id, date)
            DO UPDATE SET realized_pnl = realized_pnl + excluded.realized_pnl
            """,
            (account_id, date, amount),
        )
        conn.commit()


def get_daily_pnl(account_id: str, date: str = None) -> float:
    """Returns an account's total realized P&L for the given date
    (today, UTC, if not specified). This is what KillSwitchRule checks -
    if it's too negative, new orders get blocked for the rest of the day."""
    date = date or datetime.now(timezone.utc).date().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT realized_pnl FROM daily_pnl WHERE account_id = ? AND date = ?",
            (account_id, date),
        ).fetchone()
        return row["realized_pnl"] if row else 0.0


# --- MT5 signal queue --------------------------------------------------
# MT5/Exness has no simple REST API for placing trades - the real
# bridge is an Expert Advisor (MQL5 script) running INSIDE the
# trader's MT5 terminal, which periodically polls this queue and
# executes anything pending using MT5's own native trading functions,
# then reports back what happened. This table is that queue.

def queue_mt5_signal(account_id: str, symbol: str, side: str, quantity: float, price: float) -> int:
    """Adds a pending order for an MT5 Expert Advisor to pick up on its
    next poll. Returns the signal's id."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO mt5_signals (account_id, created_at, symbol, side, quantity, price, status)
            VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (account_id, datetime.now(timezone.utc).isoformat(), symbol, side, quantity, price),
        )
        conn.commit()
        return cur.lastrowid or 0


def get_pending_mt5_signals(account_id: str) -> list[dict]:
    """What an Expert Advisor's poll call returns - every signal still
    waiting to be picked up for this account."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM mt5_signals WHERE account_id = ? AND status = 'PENDING' ORDER BY id",
            (account_id,),
        ).fetchall()
        return [_row_to_mt5_signal_dict(r) for r in rows]


def mark_mt5_signal_sent(signal_id: int):
    """Called right after a poll returns a signal, so the SAME signal
    doesn't get handed to the EA again on its next poll a few seconds
    later, before it's had a chance to report a result back."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE mt5_signals SET status = 'SENT', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), signal_id),
        )
        conn.commit()


def report_mt5_signal_result(signal_id: int, account_id: str, status: str,
                              mt5_ticket: str = None, fill_price: float = None,
                              error_message: str = None) -> bool:
    """Called by the Expert Advisor after it actually executes (or
    fails to execute) a signal. Scoped to account_id too, so one
    trader's EA can never report results for a different trader's
    signal. Returns False if no matching signal was found."""
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE mt5_signals
            SET status = ?, mt5_ticket = ?, fill_price = ?, error_message = ?, updated_at = ?
            WHERE id = ? AND account_id = ?
            """,
            (status, mt5_ticket, fill_price, error_message,
             datetime.now(timezone.utc).isoformat(), signal_id, account_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_mt5_signal(signal_id: int, account_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM mt5_signals WHERE id = ? AND account_id = ?",
            (signal_id, account_id),
        ).fetchone()
        return _row_to_mt5_signal_dict(row) if row else None


def get_all_mt5_signals(account_id: str, limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM mt5_signals WHERE account_id = ? ORDER BY id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
        return [_row_to_mt5_signal_dict(r) for r in rows]


def _row_to_mt5_signal_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "created_at": row["created_at"],
        "symbol": row["symbol"],
        "side": row["side"],
        "quantity": row["quantity"],
        "price": row["price"],
        "status": row["status"],
        "mt5_ticket": row["mt5_ticket"],
        "fill_price": row["fill_price"],
        "error_message": row["error_message"],
    }
