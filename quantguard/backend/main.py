"""
QuantGuard backend.

This is the ONE API a trader's strategy talks to. Behind it:
order -> risk engine checks it -> if approved, broker connector
sends it -> result comes back, saved to the database. The trader
never writes exchange API code, and order history survives restarts.

Every order now requires an API key (header: X-API-Key). The key
determines whose account the order belongs to - a trader can never
submit orders as someone else, and can't even guess another account's
data, because there's nothing tying a request to an account except
a valid key.

Run with:
    pip install fastapi uvicorn pydantic
    uvicorn backend.main:app --reload

Then open http://localhost:8000/docs for an interactive API tester,
or use the frontend dashboard (frontend/index.html).
"""

import os
import asyncio
import pathlib
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, Header, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from risk_engine import Order, Side, RiskEngine, MaxOrderSizeRule, RateLimitRule, PositionLimitRule, KillSwitchRule
from broker import MockConnector, BinanceConnector, CcxtConnector, UnsupportedBrokerConnector, ExecutionResult
from backend.database import db
from strategy.parser import MockStrategyParser, ClaudeStrategyParser, GeminiStrategyParser, GrokStrategyParser
from strategy.models import StrategyConfig, StrategyStatus
from strategy.price_data import MockPriceDataSource, BinancePriceDataSource
from strategy.twelvedata_source import TwelveDataPriceSource, is_forex_symbol
from strategy.execution_engine import StrategyMonitor, OrderRequest as StrategyOrderRequest
from strategy.document import extract_text, UnsupportedFileType

app = FastAPI(title="QuantGuard API")

# Allow the local dashboard (or any frontend) to call this API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    # Creates quantguard.db and its tables if they don't exist yet.
    # Safe to run every time the server starts.
    db.init_db()


# --- Configuration -----------------------------------------------------
# One risk engine, one connector, shared across all requests.
# In mock mode by default - swap MockConnector() for BinanceConnector(...)
# once you have real API keys and are running somewhere with internet access.
risk_engine = RiskEngine(rules=[
    MaxOrderSizeRule(max_notional=50_000),
    RateLimitRule(max_orders=5, per_seconds=1.0),
    PositionLimitRule(max_position=2.0, position_lookup=db.get_position),
    KillSwitchRule(max_daily_loss=500, daily_pnl_lookup=db.get_daily_pnl),
])


def _select_broker():
    """
    Uses a real ccxt exchange (Binance testnet by default) if API keys
    are set as environment variables, otherwise falls back to the mock
    exchange. This means the app never breaks just because keys aren't
    set up yet - it just quietly stays in paper-trading mode until you
    add them. This is the SHARED fallback broker, used only for
    accounts that haven't connected their own via /broker/connect -
    see _build_broker_for_account below for the per-account path.

    To go live against Binance testnet, set these before starting the
    server (PowerShell):
        $env:BINANCE_API_KEY = "your testnet key"
        $env:BINANCE_API_SECRET = "your testnet secret"
    Get free testnet keys at https://testnet.binance.vision

    If Binance is blocked from your hosting region (HTTP 451), set
    BROKER_EXCHANGE to a different ccxt exchange id instead (e.g.
    "kraken", "bybit", "okx") - same BINANCE_API_KEY/SECRET env vars
    are reused as that exchange's key/secret, just naming stayed as-is
    for backward compatibility.
    """
    api_key = os.environ.get("BINANCE_API_KEY")
    api_secret = os.environ.get("BINANCE_API_SECRET")
    exchange_id = os.environ.get("BROKER_EXCHANGE", "binance")
    if api_key and api_secret:
        try:
            connector = CcxtConnector(exchange_id, api_key=api_key, api_secret=api_secret, testnet=True)
            print(f"[QuantGuard] Using CcxtConnector({exchange_id!r}) (testnet) - real exchange, fake funds.")
            return connector
        except ImportError:
            print("[QuantGuard] API keys set but ccxt isn't installed. "
                  "Run: pip install ccxt  -  falling back to MockConnector for now.")
        except ValueError as e:
            print(f"[QuantGuard] Couldn't set up '{exchange_id}': {e} Falling back to MockConnector.")
    else:
        print("[QuantGuard] No broker API keys set - using MockConnector (simulated fills).")
    return MockConnector()


broker = _select_broker()


# Exchange ids ccxt itself supports for real order execution. Anything
# a trader connects that ISN'T in this set (e.g. "deriv") gets an
# honest UnsupportedBrokerConnector instead of a silent Binance fallback.
CCXT_SUPPORTED_BROKER_NAMES = {
    "binance", "kraken", "bybit", "okx", "coinbase", "coinbaseadvanced",
    "kucoin", "bitfinex", "bitstamp", "gemini", "huobi",
}


def _build_broker_for_account(account_id: str):
    """
    The per-account counterpart to _select_broker(): if this account
    has connected its OWN broker credentials (via /broker/connect),
    route their orders there instead of the single shared broker.
    Falls back to the shared `broker` if they haven't connected one,
    or if their connected broker isn't ccxt-supported (still gets a
    real, honest connector - UnsupportedBrokerConnector - not a
    silent redirect to Binance).
    """
    connection = db.get_broker_connection(account_id)
    if not connection:
        return broker

    broker_name = connection["broker_name"].lower().strip()
    if broker_name not in CCXT_SUPPORTED_BROKER_NAMES:
        return UnsupportedBrokerConnector(connection["broker_name"])

    try:
        return CcxtConnector(
            broker_name,
            api_key=connection["api_key"],
            api_secret=connection["api_secret"],
            testnet=connection["testnet"],
        )
    except (ImportError, ValueError) as e:
        print(f"[QuantGuard] Couldn't build '{broker_name}' connector for account {account_id}: {e}")
        return UnsupportedBrokerConnector(connection["broker_name"])


def _select_strategy_parser():
    """
    Tries real LLM providers in order, falling back to the rule-based
    MockStrategyParser if none are configured - same safety pattern as
    _select_broker: the app always works, it just uses better language
    understanding as you add keys.

    Checked in this order (first one found wins):
        1. ANTHROPIC_API_KEY  -> ClaudeStrategyParser
        2. GEMINI_API_KEY     -> GeminiStrategyParser (Google's free tier - no card needed)
        3. XAI_API_KEY        -> GrokStrategyParser
        4. (none set)          -> MockStrategyParser (rule-based, works offline)

    To use one, set it before starting the server (PowerShell):
        $env:GEMINI_API_KEY = "your key"
    """
    providers = [
        ("ANTHROPIC_API_KEY", ClaudeStrategyParser, "ClaudeStrategyParser", "anthropic"),
        ("GEMINI_API_KEY", GeminiStrategyParser, "GeminiStrategyParser", "google-generativeai"),
        ("XAI_API_KEY", GrokStrategyParser, "GrokStrategyParser", "openai"),
    ]
    for env_var, parser_class, label, pip_package in providers:
        api_key = os.environ.get(env_var)
        if not api_key:
            continue
        try:
            parser = parser_class(api_key=api_key)
            print(f"[QuantGuard] Using {label} for strategy descriptions.")
            return parser
        except ImportError:
            print(f"[QuantGuard] {env_var} is set but the '{pip_package}' package isn't installed. "
                  f"Run: pip install {pip_package}  -  trying the next option.")

    print("[QuantGuard] No LLM API key set (ANTHROPIC_API_KEY / GEMINI_API_KEY / XAI_API_KEY) - "
          "using MockStrategyParser (rule-based, limited).")
    return MockStrategyParser()


strategy_parser = _select_strategy_parser()


# --- Auth helper ------------------------------------------------------------
def require_account(x_api_key: str | None) -> str:
    """
    Every protected endpoint calls this with the X-API-Key header.
    Returns the account_id that key belongs to, or raises a 401 if
    the key is missing or invalid. This is the entire auth system -
    small on purpose, but it's the real thing: no key, no access.
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    account_id = db.get_account_for_key(x_api_key)
    if not account_id:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    return account_id


def require_session(authorization: str | None) -> str:
    """
    The dashboard's login/signup layer uses a separate 'Authorization:
    Bearer <session_token>' header, not X-API-Key - this is for
    account-level actions (viewing your own API key, connecting a
    broker), distinct from the API key which is used for actual
    order/strategy requests. Raises 401 if missing/invalid/expired.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    session_token = authorization[len("Bearer "):]
    account_id = db.get_account_for_session(session_token)
    if not account_id:
        raise HTTPException(status_code=401, detail="Session expired or invalid - please log in again")
    return account_id


# --- Request/response shapes --------------------------------------------
class OrderRequest(BaseModel):
    symbol: str
    side: str          # "BUY" or "SELL"
    quantity: float
    price: float
    # account_id is NOT taken from the request anymore - it comes from
    # the API key, so a trader can't submit an order pretending to be
    # a different account.


class OrderResponse(BaseModel):
    approved: bool
    rule_results: List[str]
    execution: dict | None = None


class NewAccountRequest(BaseModel):
    account_id: str


class NewAccountResponse(BaseModel):
    account_id: str
    api_key: str
    warning: str = "Save this key now - it will not be shown again."


class SignupRequest(BaseModel):
    account_id: str
    password: str


class LoginRequest(BaseModel):
    account_id: str
    password: str


class SessionResponse(BaseModel):
    session_token: str
    account_id: str


class BrokerConnectRequest(BaseModel):
    broker_name: str
    api_key: str
    api_secret: str
    testnet: bool = True


# --- Frontend serving -------------------------------------------------
# The dashboard (frontend/index.html) is a single self-contained HTML
# file - no separate CSS/JS assets - so a plain FileResponse is enough,
# no StaticFiles mount needed. Falls back to the JSON status if the
# file isn't found (e.g. a bare API-only deployment), so this never
# hard-crashes the app over a missing frontend folder.
FRONTEND_DIR = pathlib.Path(__file__).resolve().parent.parent / "frontend"


# --- Endpoints ------------------------------------------------------------
@app.get("/")
def root():
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"service": "QuantGuard", "status": "running", "broker": broker.name}


@app.get("/api/status")
def api_status():
    """Machine-readable health check - what '/' returned before it
    started serving the dashboard. Kept at a separate path so uptime
    monitors or scripts hitting '/' for JSON don't silently break."""
    return {"service": "QuantGuard", "status": "running", "broker": broker.name}


@app.post("/accounts", response_model=NewAccountResponse)
def create_account(req: NewAccountRequest):
    """
    Creates a new trading account and issues its API key, with NO
    password - the bare-bones API-only path (e.g. for an MT5 EA or a
    script that never touches the web dashboard). The returned key is
    shown only this one time. For the dashboard's own login/signup
    screen, use /signup instead - that path sets a password too.
    """
    try:
        api_key = db.create_api_key(req.account_id)
    except Exception:
        raise HTTPException(status_code=400, detail="That account_id already has a key")
    return NewAccountResponse(account_id=req.account_id, api_key=api_key)


@app.post("/signup", response_model=SessionResponse)
def signup(req: SignupRequest):
    """
    What the dashboard's 'Create account' button calls. Creates the
    account AND its API key in one step (same key an EA/script would
    use), sets a password, and immediately logs the trader in by
    returning a session token - so signup flows straight into the
    dashboard with no separate login step.
    """
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    try:
        db.create_account_with_password(req.account_id, req.password)
    except Exception:
        raise HTTPException(status_code=400, detail="That account name is already taken")
    session_token = db.create_session(req.account_id)
    return SessionResponse(session_token=session_token, account_id=req.account_id)


@app.post("/login", response_model=SessionResponse)
def login(req: LoginRequest):
    """What the dashboard's 'Log in' button calls."""
    if not db.verify_login(req.account_id, req.password):
        # Same message for "no such account" and "wrong password" -
        # don't let a login form reveal which account names exist.
        raise HTTPException(status_code=401, detail="Incorrect account name or password")
    session_token = db.create_session(req.account_id)
    return SessionResponse(session_token=session_token, account_id=req.account_id)


@app.post("/logout")
def logout(authorization: str | None = Header(default=None)):
    """Invalidates the current session. Safe to call even with an
    already-invalid token - logging out never fails from the trader's
    point of view."""
    if authorization and authorization.startswith("Bearer "):
        db.delete_session(authorization[len("Bearer "):])
    return {"status": "logged_out"}


@app.get("/my-api-key")
def my_api_key(authorization: str | None = Header(default=None)):
    """
    Returns the logged-in account's API key, so the dashboard can
    display it and use it for order/chart/strategy requests without
    the trader having to copy-paste it in manually after signup.
    """
    account_id = require_session(authorization)
    api_key = db.get_api_key_for_account(account_id)
    if not api_key:
        raise HTTPException(status_code=404, detail="No API key found for this account")
    return {"api_key": api_key}


@app.get("/broker/status")
def broker_status(authorization: str | None = Header(default=None)):
    """
    Whether THIS account has connected its own broker credentials, AND
    whether that broker is one QuantGuard can actually execute orders
    through. A trader who connected an unsupported broker (e.g. Deriv)
    should see that clearly here, not just find out when an order fails.
    """
    account_id = require_session(authorization)
    connection = db.get_broker_connection(account_id)
    if not connection:
        return {"connected": False, "broker_name": None, "executable": False}
    executable = connection["broker_name"].lower().strip() in CCXT_SUPPORTED_BROKER_NAMES
    return {"connected": True, "broker_name": connection["broker_name"], "executable": executable}


@app.post("/broker/connect")
def broker_connect(req: BrokerConnectRequest, authorization: str | None = Header(default=None)):
    """
    Stores this account's own broker API key/secret. See the note on
    /broker/status: this saves the credentials, but the order
    execution pipeline needs a follow-up change to actually use them
    per-account instead of the single env-var-configured broker.
    """
    account_id = require_session(authorization)
    db.save_broker_connection(account_id, req.broker_name, req.api_key, req.api_secret, req.testnet)
    return {"status": "connected", "broker_name": req.broker_name}


@app.post("/orders", response_model=OrderResponse)
def submit_order(req: OrderRequest, x_api_key: str | None = Header(default=None)):
    """
    The single endpoint a trader's strategy calls to place an order.
    No broker-specific code required on their end. Requires a valid
    X-API-Key header - the account is derived from the key, not from
    anything the caller can set themselves.
    """
    account_id = require_account(x_api_key)
    result = _process_order(
        account_id=account_id,
        symbol=req.symbol.upper(),
        side=req.side.upper(),
        quantity=req.quantity,
        price=req.price,
    )
    return OrderResponse(**result)


class TradingViewAlertRequest(BaseModel):
    symbol: str
    side: str
    quantity: float
    price: float


@app.post("/webhooks/tradingview/{api_key}", response_model=OrderResponse)
def tradingview_webhook(api_key: str, req: TradingViewAlertRequest):
    """
    TradingView-compatible entry point. TradingView's alert webhooks
    can ONLY send a URL and a JSON message body - they cannot send
    custom HTTP headers, so the X-API-Key header used everywhere else
    won't work here. Instead, the key goes directly in the URL path:
    the trader sets their TradingView alert's webhook URL to
    http://your-server/webhooks/tradingview/qg_theirkey and the alert
    message body to something like:

        {"symbol": "{{ticker}}", "side": "buy", "quantity": "0.1", "price": "{{close}}"}

    (TradingView fills in {{ticker}} and {{close}} automatically from
    the chart the alert fired on.) Behind this URL, it's the exact
    same risk-checked order pipeline as every other order source -
    nothing about coming from TradingView skips any risk check.
    """
    account_id = db.get_account_for_key(api_key)
    if not account_id:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    result = _process_order(
        account_id=account_id,
        symbol=req.symbol.upper(),
        side=req.side.upper(),
        quantity=req.quantity,
        price=req.price,
    )
    return OrderResponse(**result)


class MT5OrderRequest(BaseModel):
    symbol: str
    side: str
    quantity: float
    price: float


class MT5SignalResponse(BaseModel):
    id: int
    status: str


class MT5ReportRequest(BaseModel):
    status: str          # "FILLED" or "ERROR"
    mt5_ticket: str | None = None
    fill_price: float | None = None
    error_message: str | None = None


@app.post("/mt5/orders", response_model=MT5SignalResponse)
def submit_mt5_order(req: MT5OrderRequest, x_api_key: str | None = Header(default=None)):
    """
    Submits an order to be executed on MT5/Exness - runs through the
    EXACT SAME risk checks (fat-finger, rate-limit, position-limit,
    kill-switch) as every other order source, but instead of sending
    to a broker connector directly (there's no simple REST API for
    MT5/Exness), it's queued for the trader's MT5 Expert Advisor to
    pick up on its next poll and execute using MT5's own native
    trading functions.

    Unlike the Binance path, the position/order log do NOT update
    immediately here - MT5 hasn't actually executed anything yet.
    That only happens once the EA reports a result back via
    /mt5/report/{api_key}.
    """
    account_id = require_account(x_api_key)
    order = Order(
        symbol=req.symbol.upper(),
        side=Side.BUY if req.side.upper() == "BUY" else Side.SELL,
        quantity=req.quantity,
        price=req.price,
        account_id=account_id,
    )
    results = risk_engine.evaluate(order)
    approved = all(r.passed for r in results)

    if not approved:
        db.save_order({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "account_id": account_id, "symbol": order.symbol, "side": order.side.value,
            "quantity": order.quantity, "price": order.price, "approved": False,
            "rule_results": [str(r) for r in results], "execution": None,
        })
        raise HTTPException(status_code=400, detail="; ".join(str(r) for r in results if not r.passed))

    signal_id = db.queue_mt5_signal(account_id, order.symbol, order.side.value, order.quantity, order.price)
    return MT5SignalResponse(id=signal_id, status="PENDING")


@app.get("/mt5/poll/{api_key}")
def mt5_poll(api_key: str):
    """
    Called by the MT5 Expert Advisor (see mt5/QuantGuardBridge.mq5) on
    a timer, e.g. every 5-10 seconds. Returns every pending signal for
    this account and immediately marks them SENT, so the same signal
    isn't handed out again on the next poll before the EA has had a
    chance to execute it and report back.

    Key is in the URL, not a header - MQL5's WebRequest() can set
    headers, but keeping this URL-based mirrors the TradingView
    webhook pattern and keeps the EA's code simpler.
    """
    account_id = db.get_account_for_key(api_key)
    if not account_id:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    pending = db.get_pending_mt5_signals(account_id)
    for signal in pending:
        db.mark_mt5_signal_sent(signal["id"])
    return {"signals": pending}


@app.post("/mt5/report/{api_key}/{signal_id}")
def mt5_report(api_key: str, signal_id: int, req: MT5ReportRequest):
    """
    Called by the Expert Advisor after it actually executes (or fails
    to execute) a signal from /mt5/poll. This is where the order log
    and position tracking finally update - MT5 has now confirmed what
    really happened, not just that a signal was sent.
    """
    account_id = db.get_account_for_key(api_key)
    if not account_id:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    signal = db.get_mt5_signal(signal_id, account_id)
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found for this account")

    ok = db.report_mt5_signal_result(
        signal_id, account_id, status=req.status,
        mt5_ticket=req.mt5_ticket, fill_price=req.fill_price, error_message=req.error_message,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Could not update signal")

    fill_price = req.fill_price or signal["price"]
    db.save_order({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account_id": account_id, "symbol": signal["symbol"], "side": signal["side"],
        "quantity": signal["quantity"], "price": fill_price,
        "approved": True,
        "rule_results": [f"MT5 execution: {req.status}" + (f" (ticket {req.mt5_ticket})" if req.mt5_ticket else "")],
        "execution": {"order_id": req.mt5_ticket or "", "status": req.status, "broker": "MT5/Exness",
                       "filled_price": fill_price, "message": req.error_message or "Filled via MT5"},
    })

    if req.status == "FILLED":
        db.update_position(account_id, signal["symbol"], signal["side"], signal["quantity"], fill_price)

    return {"ok": True}


def _process_order(account_id: str, symbol: str, side: str, quantity: float, price: float) -> dict:
    """
    The actual order pipeline: risk checks, then (if approved) the
    broker, then database logging. Shared by the /orders endpoint AND
    the strategy execution engine below - a strategy-triggered order
    goes through EXACTLY the same fat-finger/rate-limit/position-limit
    checks as a manually-submitted one. Nothing bypasses the risk
    engine just because an approved strategy is the one asking.
    """
    order = Order(
        symbol=symbol,
        side=Side.BUY if side == "BUY" else Side.SELL,
        quantity=quantity,
        price=price,
        account_id=account_id,
    )

    results = risk_engine.evaluate(order)
    approved = all(r.passed for r in results)

    execution: ExecutionResult | None = None
    if approved:
        account_broker = _build_broker_for_account(account_id)
        execution = account_broker.send_order(order)
        if execution.status == "FILLED":
            db.update_position(order.account_id, order.symbol, order.side.value, order.quantity, order.price)

    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account_id": order.account_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "quantity": order.quantity,
        "price": order.price,
        "approved": approved,
        "rule_results": [str(r) for r in results],
        "execution": execution.__dict__ if execution else None,
    }
    db.save_order(log_entry)

    return {
        "approved": approved,
        "rule_results": [str(r) for r in results],
        "execution": execution.__dict__ if execution else None,
    }


@app.get("/orders")
def get_order_history(x_api_key: str | None = Header(default=None)):
    """Returns every order submitted by YOUR account, most recent first."""
    account_id = require_account(x_api_key)
    return [o for o in db.get_orders() if o["account_id"] == account_id]


@app.get("/positions")
def get_positions(x_api_key: str | None = Header(default=None)):
    """
    Returns current holdings for the account that owns this API key -
    including current market price and unrealized P&L per position,
    not just the raw quantity. Uses the same price source the
    execution engine itself checks against.
    """
    account_id = require_account(x_api_key)
    positions = db.get_all_positions(account_id)

    enriched = []
    for pos in positions:
        try:
            price_source = _build_price_data_source(pos["symbol"])
            candles = price_source.get_candles(pos["symbol"], timeframe="1m", limit=1)
            current_price = candles[-1]["close"]
        except Exception:
            current_price = None

        unrealized_pnl = None
        unrealized_pct = None
        if current_price is not None and pos["avg_price"]:
            unrealized_pnl = (current_price - pos["avg_price"]) * pos["quantity"]
            unrealized_pct = ((current_price - pos["avg_price"]) / pos["avg_price"]) * 100 * (1 if pos["quantity"] > 0 else -1)

        enriched.append({
            **pos,
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pct": unrealized_pct,
        })
    return enriched


@app.get("/market/{symbol}/candles")
def get_candles(symbol: str, timeframe: str = "1h", limit: int = 100, x_api_key: str | None = Header(default=None)):
    """
    Returns recent candles for a symbol - what the dashboard's price
    chart calls. Uses the SAME price data source the strategy engine
    itself checks conditions against (real Binance testnet if
    configured, otherwise synthetic mock data) - so the chart shows
    exactly what the engine is actually seeing, not a different feed.
    """
    require_account(x_api_key)  # still requires a valid key, even though it's read-only market data
    price_source = _build_price_data_source(symbol.upper())
    try:
        candles = price_source.get_candles(symbol.upper(), timeframe=timeframe, limit=limit)
    except Exception as e:
        # Surface a real, readable reason instead of a bare 500 - this is
        # exactly the message the frontend's "Couldn't load chart: ..." shows.
        raise HTTPException(status_code=502, detail=f"Couldn't fetch candles for {symbol.upper()}: {e}")
    return {"symbol": symbol.upper(), "timeframe": timeframe, "candles": candles}


# --- Strategy request/response shapes ---------------------------------
class StrategyDescriptionRequest(BaseModel):
    description: str


class StrategyClarifyRequest(BaseModel):
    answer: str


class StrategyResponse(BaseModel):
    id: int
    status: str
    strategy: dict
    questions: List[str]
    warnings: List[str] = []


# --- Strategy endpoints -------------------------------------------------
@app.post("/strategies", response_model=StrategyResponse)
def submit_strategy(req: StrategyDescriptionRequest, x_api_key: str | None = Header(default=None)):
    """
    Trader describes their strategy in plain English. Returns either
    the fully structured strategy (ready for review) or specific
    clarifying questions if something required is missing - never a
    guess. Nothing here trades yet; this only produces a structured,
    reviewable definition.
    """
    account_id = require_account(x_api_key)

    result = strategy_parser.parse(req.description)
    strategy_id = db.save_strategy(
        account_id=account_id,
        conversation=[req.description],
        strategy_dict=result.strategy.to_dict(),
        status=result.status.value,
        questions=result.questions,
    )
    return StrategyResponse(
        id=strategy_id,
        status=result.status.value,
        strategy=result.strategy.to_dict(),
        questions=result.questions,
        warnings=result.warnings,
    )


@app.post("/strategies/upload", response_model=StrategyResponse)
async def upload_strategy(file: UploadFile = File(...), x_api_key: str | None = Header(default=None)):
    """
    Upload a document (.txt, .md, .pdf, .docx) instead of typing a
    strategy description. The extracted text goes through the EXACT
    SAME parser as a typed message - this is just a different front
    door into the same pipeline, not a separate code path with its
    own rules.
    """
    account_id = require_account(x_api_key)
    content = await file.read()

    try:
        text = extract_text(file.filename, content)
    except UnsupportedFileType as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ImportError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not text.strip():
        raise HTTPException(status_code=400, detail="Couldn't extract any text from that file.")

    result = strategy_parser.parse(text)
    strategy_id = db.save_strategy(
        account_id=account_id,
        conversation=[f"[uploaded file: {file.filename}]", text],
        strategy_dict=result.strategy.to_dict(),
        status=result.status.value,
        questions=result.questions,
    )
    return StrategyResponse(
        id=strategy_id,
        status=result.status.value,
        strategy=result.strategy.to_dict(),
        questions=result.questions,
        warnings=result.warnings,
    )


@app.post("/strategies/{strategy_id}/clarify", response_model=StrategyResponse)
def clarify_strategy(strategy_id: int, req: StrategyClarifyRequest, x_api_key: str | None = Header(default=None)):
    """
    Trader answers a clarifying question (or adds more detail). Combines
    it with what's already known and re-checks whether the strategy is
    now complete.
    """
    account_id = require_account(x_api_key)

    existing = db.get_strategy(strategy_id, account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Strategy not found")

    current_config = StrategyConfig.from_dict(existing["strategy"])
    result = strategy_parser.parse(req.answer, existing=current_config)

    conversation = existing["conversation"] + [req.answer]
    db.update_strategy(
        strategy_id=strategy_id,
        conversation=conversation,
        strategy_dict=result.strategy.to_dict(),
        status=result.status.value,
        questions=result.questions,
    )
    return StrategyResponse(
        id=strategy_id,
        status=result.status.value,
        strategy=result.strategy.to_dict(),
        questions=result.questions,
        warnings=result.warnings,
    )


@app.post("/strategies/{strategy_id}/approve", response_model=StrategyResponse)
def approve_strategy(strategy_id: int, x_api_key: str | None = Header(default=None)):
    """
    Trader confirms the structured strategy is correct. This is the
    human-in-the-loop checkpoint - a strategy can never move toward
    live trading without the trader explicitly approving what the
    parser understood, in case it misunderstood something.

    Note: approving a strategy does NOT make it trade yet. That's a
    separate step (a live monitoring/execution engine) not built yet -
    this only marks it as reviewed and correct.
    """
    account_id = require_account(x_api_key)
    existing = db.get_strategy(strategy_id, account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if existing["status"] != StrategyStatus.READY_FOR_REVIEW.value:
        raise HTTPException(
            status_code=400,
            detail=f"Strategy isn't ready for approval yet (current status: {existing['status']})",
        )

    db.update_strategy(
        strategy_id=strategy_id,
        conversation=existing["conversation"],
        strategy_dict=existing["strategy"],
        status=StrategyStatus.APPROVED.value,
        questions=[],
    )
    return StrategyResponse(
        id=strategy_id,
        status=StrategyStatus.APPROVED.value,
        strategy=existing["strategy"],
        questions=[],
    )


@app.get("/strategies")
def list_strategies(x_api_key: str | None = Header(default=None)):
    """Returns every strategy conversation for your account, most recent first."""
    account_id = require_account(x_api_key)
    return db.get_strategies(account_id)


# --- Live strategy execution ------------------------------------------
# Holds one StrategyMonitor per currently-active strategy, in memory.
# Like RateLimitRule's state, this resets if the server restarts - an
# ACTIVE strategy in the database won't resume monitoring automatically
# on a fresh startup yet. That's a known gap, not a silent bug: worth
# fixing before this is relied on unattended, by re-hydrating active
# monitors from the database on startup.
active_monitors: dict[int, StrategyMonitor] = {}

STRATEGY_CHECK_INTERVAL_SECONDS = 60  # how often each active strategy's conditions are re-checked


def _build_price_data_source(symbol: str = None):
    """
    Mirrors _select_broker: if real Binance is configured, use real
    price data too (a strategy's decisions should be based on the same
    reality its orders execute against). Otherwise, synthetic mock data -
    fine for testing the engine's logic, but NOT a real market signal.

    Symbol-aware routing: Binance has no forex/metals markets (EURUSD,
    XAUUSD, etc), so those symbols must go to Twelve Data instead,
    regardless of which crypto broker is active. Falls back to Mock
    data for forex/metals if TWELVE_DATA_API_KEY isn't set, rather
    than crashing - same "always works, just gets better with keys"
    pattern as the rest of this file.
    """
    if symbol and is_forex_symbol(symbol):
        try:
            return TwelveDataPriceSource()
        except RuntimeError as e:
            print(f"[QuantGuard] {e} Falling back to MockPriceDataSource for {symbol}.")
            return MockPriceDataSource()
    if isinstance(broker, BinanceConnector):
        return BinancePriceDataSource(testnet=True)
    return MockPriceDataSource()


def _strategy_order_callback(account_id: str):
    """Returns a function StrategyMonitor can call to submit an order -
    routes through the exact same _process_order pipeline as a manual
    order, so every risk check still applies."""
    def submit(order: StrategyOrderRequest) -> dict:
        return _process_order(
            account_id=account_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
        )
    return submit


@app.post("/strategies/{strategy_id}/activate", response_model=StrategyResponse)
def activate_strategy(strategy_id: int, x_api_key: str | None = Header(default=None)):
    """
    Starts (or resumes) live monitoring for an EXECUTABLE strategy that
    is either freshly APPROVED or currently PAUSED. Once active, it's
    checked every STRATEGY_CHECK_INTERVAL_SECONDS - if its entry
    condition is met, a real order is submitted through the same
    risk-checked pipeline as any manual order.

    Deliberately blocked for strategies that aren't executable (no
    structured entry_conditions) - there is nothing for the engine to
    mechanically check in that case, so activating it would either
    silently do nothing forever or require guessing, neither of which
    is acceptable for something that trades real money.
    """
    account_id = require_account(x_api_key)
    existing = db.get_strategy(strategy_id, account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if existing["status"] not in (StrategyStatus.APPROVED.value, StrategyStatus.PAUSED.value):
        raise HTTPException(
            status_code=400,
            detail=f"Strategy must be APPROVED or PAUSED before activating (current status: {existing['status']})",
        )

    strategy_config = StrategyConfig.from_dict(existing["strategy"])
    if not strategy_config.is_executable():
        raise HTTPException(
            status_code=400,
            detail="This strategy has no structured, executable entry condition - it can't be run live. "
                   "Try rephrasing the entry condition (e.g. in terms of RSI or a percent drop from a recent "
                   "high), or use a real LLM parser (Claude/Gemini/Grok) which can structure more complex logic.",
        )

    price_source = _build_price_data_source(strategy_config.symbol)
    monitor = StrategyMonitor(strategy_id, strategy_config, price_source, _strategy_order_callback(account_id))

    # Same safety check as server-restart rehydration: don't assume this
    # strategy is flat just because we're building a fresh monitor for
    # it. If it already holds a position (e.g. it entered one, then got
    # paused), resuming needs to know that - otherwise it could fire a
    # SECOND entry order on top of an existing position.
    position = db.get_position_detail(account_id, strategy_config.symbol)
    if position["quantity"] != 0:
        monitor.in_position = True
        monitor.entry_price = position["avg_price"]

    active_monitors[strategy_id] = monitor

    db.update_strategy(strategy_id, existing["conversation"], existing["strategy"], StrategyStatus.ACTIVE.value, [])
    return StrategyResponse(id=strategy_id, status=StrategyStatus.ACTIVE.value, strategy=existing["strategy"], questions=[])


@app.post("/strategies/{strategy_id}/pause", response_model=StrategyResponse)
def pause_strategy(strategy_id: int, x_api_key: str | None = Header(default=None)):
    """Stops live monitoring for a strategy. Any open position it holds is NOT
    automatically closed - pausing stops new decisions, it doesn't liquidate."""
    account_id = require_account(x_api_key)
    existing = db.get_strategy(strategy_id, account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Strategy not found")

    active_monitors.pop(strategy_id, None)
    db.update_strategy(strategy_id, existing["conversation"], existing["strategy"], StrategyStatus.PAUSED.value, [])
    return StrategyResponse(id=strategy_id, status=StrategyStatus.PAUSED.value, strategy=existing["strategy"], questions=[])


async def _strategy_monitoring_loop():
    """
    Background loop: every STRATEGY_CHECK_INTERVAL_SECONDS, checks every
    currently-active strategy and lets its monitor decide whether to
    enter, exit, or wait. Runs for the lifetime of the server process.

    NOTE: this loop's correctness (does it actually run, does it fetch
    real prices, does it fire real orders on schedule) can only be
    verified with a live server and real network access - this sandbox
    has neither. What IS verified (see the test suite) is the DECISION
    LOGIC each monitor uses once called - the indicator math, condition
    evaluation, and entry/exit rules. This loop is just the scheduler
    that calls that already-tested logic on a timer.
    """
    while True:
        await asyncio.sleep(STRATEGY_CHECK_INTERVAL_SECONDS)
        for strategy_id, monitor in list(active_monitors.items()):
            try:
                result = monitor.check_and_maybe_trade()
                print(f"[QuantGuard] Strategy {strategy_id} check: {result}")
            except Exception as e:
                print(f"[QuantGuard] Strategy {strategy_id} check failed: {e}")


def _rehydrate_active_strategies():
    """
    Runs once, on server startup: rebuilds an in-memory StrategyMonitor
    for every strategy the DATABASE still says is ACTIVE. Without this,
    an active strategy would silently stop being watched after any
    server restart, while still showing as "ACTIVE" in the dashboard -
    a dangerous, misleading gap.

    Critically, this doesn't just naively assume every rehydrated
    monitor starts flat (in_position=False) - it checks the REAL
    position data. If the strategy was actually holding a position
    when the server stopped, the monitor resumes knowing that, using
    the position's real average cost as entry_price - otherwise it
    could think it's flat and enter a SECOND position on top of an
    existing one, doubling exposure by mistake.
    """
    active_strategies = db.get_all_active_strategies()
    for row in active_strategies:
        strategy_config = StrategyConfig.from_dict(row["strategy"])
        if not strategy_config.is_executable():
            # Shouldn't normally happen (activation requires executability),
            # but skip defensively rather than crash startup over one bad row.
            print(f"[QuantGuard] Skipping rehydration of strategy {row['id']} - not executable.")
            continue

        price_source = _build_price_data_source(strategy_config.symbol)
        monitor = StrategyMonitor(row["id"], strategy_config, price_source, _strategy_order_callback(row["account_id"]))

        position = db.get_position_detail(row["account_id"], strategy_config.symbol)
        if position["quantity"] != 0:
            monitor.in_position = True
            monitor.entry_price = position["avg_price"]

        active_monitors[row["id"]] = monitor
        print(f"[QuantGuard] Rehydrated active strategy {row['id']} "
              f"(account={row['account_id']}, in_position={monitor.in_position})")

    if active_strategies:
        print(f"[QuantGuard] Resumed monitoring {len(active_strategies)} active strategy(ies) after restart.")


@app.on_event("startup")
async def _start_strategy_monitoring():
    _rehydrate_active_strategies()
    asyncio.create_task(_strategy_monitoring_loop())
