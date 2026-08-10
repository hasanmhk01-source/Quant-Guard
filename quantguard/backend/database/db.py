"""
QuantGuard database layer.

Uses SQLite. Stores orders, positions, daily P&L, API keys (for
machine integrations), strategies, MT5 signals, and now: user
accounts with real passwords, login sessions, and each trader's OWN
encrypted broker credentials (so exchange trading is genuinely
multi-tenant, not everyone sharing one account).
"""

import sqlite3
import json
import secrets
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

from .. import auth as auth_module
from .. import crypto_utils

DB_PATH = Path(__file__).parent / "quantguard.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
                active INTEGER NOT NULL DEFAULT 1
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                account_id TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broker_credentials (
                account_id TEXT PRIMARY KEY,
                broker_name TEXT NOT NULL,
                encrypted_api_key TEXT NOT NULL,
                encrypted_api_secret TEXT NOT NULL,
                testnet INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()


def save_order(entry: dict) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders (timestamp, account_id, symbol, side, quantity, price,
                                 approved, rule_results, execution)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (entry["timestamp"], entry["account_id"], entry["symbol"], entry["side"],
             entry["quantity"], entry["price"], 1 if entry["approved"] else 0,
             json.dumps(entry["rule_results"]),
             json.dumps(entry["execution"]) if entry["execution"] else None),
        )
        conn.commit()
        return cur.lastrowid


def get_orders(limit: int = 200) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_order_dict(r) for r in rows]


def _row_to_order_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "timestamp": row["timestamp"], "account_id": row["account_id"],
        "symbol": row["symbol"], "side": row["side"], "quantity": row["quantity"],
        "price": row["price"], "approved": bool(row["approved"]),
        "rule_results": json.loads(row["rule_results"]),
        "execution": json.loads(row["execution"]) if row["execution"] else None,
    }


def update_position(account_id: str, symbol: str, side: str, quantity: float, price: float) -> float:
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
            if new_qty != 0:
                new_avg = ((existing_qty * existing_avg) + (delta * price)) / new_qty
            else:
                new_avg = 0.0
        else:
            closed_qty = min(abs(delta), abs(existing_qty))
            if existing_qty > 0:
                realized_pnl = closed_qty * (price - existing_avg)
            else:
                realized_pnl = closed_qty * (existing_avg - price)

            if abs(delta) > abs(existing_qty):
                new_avg = price
            else:
                new_avg = existing_avg

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
    with get_connection() as conn:
        row = conn.execute(
            "SELECT quantity FROM positions WHERE account_id = ? AND symbol = ?", (account_id, symbol)
        ).fetchone()
        return row["quantity"] if row else 0.0


def get_position_detail(account_id: str, symbol: str) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT quantity, avg_price FROM positions WHERE account_id = ? AND symbol = ?",
            (account_id, symbol),
        ).fetchone()
        return {"quantity": row["quantity"], "avg_price": row["avg_price"]} if row else {"quantity": 0.0, "avg_price": 0.0}


def get_all_positions(account_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT symbol, quantity, avg_price FROM positions WHERE account_id = ? AND quantity != 0",
            (account_id,),
        ).fetchall()
        return [{"symbol": r["symbol"], "quantity": r["quantity"], "avg_price": r["avg_price"]} for r in rows]


# --- API keys (for machine integrations: MT5, TradingView, direct API use) ---
def create_api_key(account_id: str) -> str:
    api_key = "qg_" + secrets.token_urlsafe(32)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO api_keys (api_key, account_id, created_at, active) VALUES (?, ?, ?, 1)",
            (api_key, account_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return api_key


def get_api_key_for_account(account_id: str) -> str | None:
    """Looks up an account's existing API key so a logged-in trader
    gets back the SAME key every time (via GET /my-api-key) instead of
    accidentally generating duplicates."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT api_key FROM api_keys WHERE account_id = ? AND active = 1", (account_id,)
        ).fetchone()
    return row["api_key"] if row else None


def get_account_for_key(api_key: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT account_id FROM api_keys WHERE api_key = ? AND active = 1", (api_key,)
        ).fetchone()
        return row["account_id"] if row else None


def revoke_api_key(api_key: str):
    with get_connection() as conn:
        conn.execute("UPDATE api_keys SET active = 0 WHERE api_key = ?", (api_key,))
        conn.commit()


# --- Users / login (replaces the old unprotected account-creation flow) ---
def create_user(account_id: str, password: str) -> bool:
    """Creates a new trader account with a real password. Returns False
    if the account_id is already taken (does not overwrite)."""
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
        return False


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
    with get_connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


# --- Per-trader broker credentials (multi-tenant exchange trading) --------
def save_broker_credentials(account_id: str, broker_name: str, api_key: str, api_secret: str, testnet: bool = True):
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
    """Returns the DECRYPTED credentials, server-side only - never
    send these back to the browser, only use them to build a connector."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM broker_credentials WHERE account_id = ?", (account_id,)).fetchone()
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
        row = conn.execute("SELECT 1 FROM broker_credentials WHERE account_id = ?", (account_id,)).fetchone()
    return row is not None


def delete_broker_credentials(account_id: str):
    with get_connection() as conn:
        conn.execute("DELETE FROM broker_credentials WHERE account_id = ?", (account_id,))
        conn.commit()


# --- Strategies ------------------------------------------------------------
def save_strategy(account_id: str, conversation: list[str], strategy_dict: dict,
                   status: str, questions: list[str] = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO strategies (account_id, created_at, conversation, strategy_json, status, questions)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (account_id, datetime.now(timezone.utc).isoformat(), json.dumps(conversation),
             json.dumps(strategy_dict), status, json.dumps(questions or [])),
        )
        conn.commit()
        return cur.lastrowid or 0


def update_strategy(strategy_id: int, conversation: list[str], strategy_dict: dict,
                     status: str, questions: list[str] = None):
    with get_connection() as conn:
        conn.execute(
            "UPDATE strategies SET conversation = ?, strategy_json = ?, status = ?, questions = ? WHERE id = ?",
            (json.dumps(conversation), json.dumps(strategy_dict), status, json.dumps(questions or []), strategy_id),
        )
        conn.commit()


def get_strategy(strategy_id: int, account_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM strategies WHERE id = ? AND account_id = ?", (strategy_id, account_id)
        ).fetchone()
        return _row_to_strategy_dict(row) if row else None


def get_strategies(account_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM strategies WHERE account_id = ? ORDER BY id DESC", (account_id,)
        ).fetchall()
        return [_row_to_strategy_dict(r) for r in rows]


def get_all_active_strategies() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM strategies WHERE status = 'ACTIVE'").fetchall()
        return [_row_to_strategy_dict(r) for r in rows]


def _row_to_strategy_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "account_id": row["account_id"], "created_at": row["created_at"],
        "conversation": json.loads(row["conversation"]), "strategy": json.loads(row["strategy_json"]),
        "status": row["status"], "questions": json.loads(row["questions"]) if row["questions"] else [],
    }


# --- Daily realized P&L (feeds the kill-switch) ----------------------------
def _record_realized_pnl(account_id: str, amount: float, date: str = None):
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
    date = date or datetime.now(timezone.utc).date().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT realized_pnl FROM daily_pnl WHERE account_id = ? AND date = ?", (account_id, date)
        ).fetchone()
        return row["realized_pnl"] if row else 0.0


# --- MT5 signal queue --------------------------------------------------
def queue_mt5_signal(account_id: str, symbol: str, side: str, quantity: float, price: float) -> int:
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
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM mt5_signals WHERE account_id = ? AND status = 'PENDING' ORDER BY id", (account_id,)
        ).fetchall()
        return [_row_to_mt5_signal_dict(r) for r in rows]


def mark_mt5_signal_sent(signal_id: int):
    with get_connection() as conn:
        conn.execute(
            "UPDATE mt5_signals SET status = 'SENT', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), signal_id),
        )
        conn.commit()


def report_mt5_signal_result(signal_id: int, account_id: str, status: str,
                              mt5_ticket: str = None, fill_price: float = None,
                              error_message: str = None) -> bool:
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
            "SELECT * FROM mt5_signals WHERE id = ? AND account_id = ?", (signal_id, account_id)
        ).fetchone()
        return _row_to_mt5_signal_dict(row) if row else None


def get_all_mt5_signals(account_id: str, limit: int = 100) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM mt5_signals WHERE account_id = ? ORDER BY id DESC LIMIT ?", (account_id, limit)
        ).fetchall()
        return [_row_to_mt5_signal_dict(r) for r in rows]


def _row_to_mt5_signal_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "account_id": row["account_id"], "created_at": row["created_at"],
        "symbol": row["symbol"], "side": row["side"], "quantity": row["quantity"], "price": row["price"],
        "status": row["status"], "mt5_ticket": row["mt5_ticket"], "fill_price": row["fill_price"],
        "error_message": row["error_message"],
    }
