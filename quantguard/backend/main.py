"""
QuantGuard backend.

Two ways to authenticate now:
1. LOGIN (for humans, via the dashboard): POST /signup or /login with
   account_id + password, get back a session token. Send it as
   `Authorization: Bearer sess_xxx` on requests that need it.
2. API KEY (for machines - MT5's Expert Advisor, TradingView webhooks,
   which can't "log in" interactively): a logged-in trader can view
   their own key at GET /my-api-key. Sent as `X-API-Key: qg_xxx`.

Every trader now connects their OWN broker credentials (POST
/broker/connect) - orders route through THEIR stored, encrypted keys,
not one shared account. Chart/price data is separate from all of this
since it's public market data - see _build_price_data_source().

Run with:
    pip install -r requirements.txt
    uvicorn backend.main:app --reload
"""

import os
import asyncio
from datetime import datetime, timezone
from typing import List

from fastapi import FastAPI, Header, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from risk_engine import Order, Side, RiskEngine, MaxOrderSizeRule, RateLimitRule, PositionLimitRule, KillSwitchRule
from broker import MockConnector, BinanceConnector, ExecutionResult
from backend.database import db
from backend import auth
from strategy.parser import MockStrategyParser, ClaudeStrategyParser, GeminiStrategyParser, GrokStrategyParser
from strategy.models import StrategyConfig, StrategyStatus
from strategy.price_data import MockPriceDataSource, BinancePriceDataSource
from strategy.twelvedata_source import TwelveDataPriceSource, is_forex_symbol
from strategy.execution_engine import StrategyMonitor, OrderRequest as StrategyOrderRequest
from strategy.document import extract_text, UnsupportedFileType

app = FastAPI(title="QuantGuard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def serve_dashboard():
    return FileResponse("frontend/index.html")


@app.on_event("startup")
def on_startup():
    db.init_db()


# --- Configuration -----------------------------------------------------
risk_engine = RiskEngine(rules=[
    MaxOrderSizeRule(max_notional=50_000),
    RateLimitRule(max_orders=5, per_seconds=1.0),
    PositionLimitRule(max_position=2.0, position_lookup=db.get_position),
    KillSwitchRule(max_daily_loss=500, daily_pnl_lookup=db.get_daily_pnl),
])

# A safe, always-available fallback broker - used for any account that
# hasn't connected their own real broker credentials yet.
_default_broker = MockConnector()


def _get_broker_for_account(account_id: str):
    """
    Each trader's orders route through THEIR OWN stored, encrypted
    broker credentials - not one shared account. This is what makes
    the exchange-trading path genuinely multi-tenant: without this,
    every trader on the server would have been trading through
    whoever's keys happened to be set in the server's environment
    variables.
    """
    creds = db.get_broker_credentials(account_id)
    if not creds:
        return _default_broker  # safe paper-trading fallback until they connect a real broker
    if creds["broker_name"] == "binance":
        try:
            return BinanceConnector(api_key=creds["api_key"], api_secret=creds["api_secret"], testnet=creds["testnet"])
        except ImportError:
            print(f"[QuantGuard] Account {account_id} has Binance credentials but ccxt isn't installed.")
            return _default_broker
    return _default_broker


def _select_strategy_parser():
    """
    Tries real LLM providers in order, falling back to the rule-based
    MockStrategyParser if none are configured.
        1. ANTHROPIC_API_KEY  -> ClaudeStrategyParser
        2. GEMINI_API_KEY     -> GeminiStrategyParser (Google's free tier - no card needed)
        3. XAI_API_KEY        -> GrokStrategyParser
        4. (none set)          -> MockStrategyParser (rule-based, works offline)
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
    print("[QuantGuard] No LLM API key set - using MockStrategyParser (rule-based, limited).")
    return MockStrategyParser()


strategy_parser = _select_strategy_parser()


def _build_price_data_source(symbol: str = None):
    """
    Chart/price data is PUBLIC market data - it never needed anyone's
    private trading credentials, which is why the chart used to show
    simulated data even when nothing was actually wrong: it was
    checking whether a private broker was connected, an unrelated
    question. Real data is used whenever possible now, regardless of
    whether any trader has connected a broker for actual trading.
    """
    if symbol and is_forex_symbol(symbol):
        try:
            return TwelveDataPriceSource()
        except RuntimeError:
            pass  # no TWELVE_DATA_API_KEY set - fall through

    try:
        return BinancePriceDataSource(testnet=False)  # public Binance market data needs NO API key
    except Exception:
        pass

    return MockPriceDataSource()


# --- Auth helpers ------------------------------------------------------------
def require_account(x_api_key: str | None) -> str:
    """For machine integrations (MT5, TradingView-style, direct API use):
    every protected endpoint calls this with the X-API-Key header."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    account_id = db.get_account_for_key(x_api_key)
    if not account_id:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    return account_id


def require_session(authorization: str | None) -> str:
    """For the human dashboard: checks a session token from login
    instead of a raw API key. Expects: Authorization: Bearer sess_xxx"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization[len("Bearer "):]
    account_id = db.get_account_for_session(token)
    if not account_id:
        raise HTTPException(status_code=401, detail="Session expired or invalid - please log in again")
    return account_id


# --- Auth endpoints ------------------------------------------------------
class SignupRequest(BaseModel):
    account_id: str
    password: str


class LoginRequest(BaseModel):
    account_id: str
    password: str


class SessionResponse(BaseModel):
    session_token: str
    account_id: str


@app.post("/signup", response_model=SessionResponse)
def signup(req: SignupRequest):
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    created = db.create_user(req.account_id, req.password)
    if not created:
        raise HTTPException(status_code=400, detail="That account name is already taken")
    token = db.create_session(req.account_id)
    return SessionResponse(session_token=token, account_id=req.account_id)


@app.post("/login", response_model=SessionResponse)
def login(req: LoginRequest):
    if not db.verify_login(req.account_id, req.password):
        raise HTTPException(status_code=401, detail="Incorrect account name or password")
    token = db.create_session(req.account_id)
    return SessionResponse(session_token=token, account_id=req.account_id)


@app.post("/logout")
def logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        db.delete_session(authorization[len("Bearer "):])
    return {"ok": True}


@app.get("/my-api-key")
def get_my_api_key(authorization: str | None = Header(default=None)):
    """A logged-in trader's own API key, for connecting MT5/TradingView -
    the only way to get one now, replacing the old unprotected signup flow."""
    account_id = require_session(authorization)
    existing_key = db.get_api_key_for_account(account_id)
    if existing_key:
        return {"api_key": existing_key}
    new_key = db.create_api_key(account_id)
    return {"api_key": new_key}


# --- Broker connection endpoints -----------------------------------------
class ConnectBrokerRequest(BaseModel):
    broker_name: str = "binance"
    api_key: str
    api_secret: str
    testnet: bool = True


@app.post("/broker/connect")
def connect_broker(req: ConnectBrokerRequest, authorization: str | None = Header(default=None)):
    account_id = require_session(authorization)
    db.save_broker_credentials(account_id, req.broker_name, req.api_key, req.api_secret, req.testnet)
    return {"ok": True, "broker_name": req.broker_name, "testnet": req.testnet}


@app.get("/broker/status")
def broker_status(authorization: str | None = Header(default=None)):
    account_id = require_session(authorization)
    return {"connected": db.has_broker_connected(account_id)}


@app.delete("/broker/disconnect")
def disconnect_broker(authorization: str | None = Header(default=None)):
    account_id = require_session(authorization)
    db.delete_broker_credentials(account_id)
    return {"ok": True}


# --- Request/response shapes --------------------------------------------
class OrderRequest(BaseModel):
    symbol: str
    side: str
    quantity: float
    price: float


class OrderResponse(BaseModel):
    approved: bool
    rule_results: List[str]
    execution: dict | None = None


# --- Endpoints ------------------------------------------------------------
@app.get("/api/status")
def status():
    return {"service": "QuantGuard", "status": "running", "mode": "multi-tenant"}


@app.post("/orders", response_model=OrderResponse)
def submit_order(req: OrderRequest, x_api_key: str | None = Header(default=None)):
    account_id = require_account(x_api_key)
    result = _process_order(
        account_id=account_id, symbol=req.symbol.upper(), side=req.side.upper(),
        quantity=req.quantity, price=req.price,
    )
    return OrderResponse(**result)


class TradingViewAlertRequest(BaseModel):
    symbol: str
    side: str
    quantity: float
    price: float


@app.post("/webhooks/tradingview/{api_key}", response_model=OrderResponse)
def tradingview_webhook(api_key: str, req: TradingViewAlertRequest):
    account_id = db.get_account_for_key(api_key)
    if not account_id:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    result = _process_order(
        account_id=account_id, symbol=req.symbol.upper(), side=req.side.upper(),
        quantity=req.quantity, price=req.price,
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
    status: str
    mt5_ticket: str | None = None
    fill_price: float | None = None
    error_message: str | None = None


@app.post("/mt5/orders", response_model=MT5SignalResponse)
def submit_mt5_order(req: MT5OrderRequest, x_api_key: str | None = Header(default=None)):
    account_id = require_account(x_api_key)
    order = Order(
        symbol=req.symbol.upper(),
        side=Side.BUY if req.side.upper() == "BUY" else Side.SELL,
        quantity=req.quantity, price=req.price, account_id=account_id,
    )
    results = risk_engine.evaluate(order)
    approved = all(r.passed for r in results)

    if not approved:
        db.save_order({
            "timestamp": datetime.now(timezone.utc).isoformat(), "account_id": account_id,
            "symbol": order.symbol, "side": order.side.value, "quantity": order.quantity,
            "price": order.price, "approved": False,
            "rule_results": [str(r) for r in results], "execution": None,
        })
        raise HTTPException(status_code=400, detail="; ".join(str(r) for r in results if not r.passed))

    signal_id = db.queue_mt5_signal(account_id, order.symbol, order.side.value, order.quantity, order.price)
    return MT5SignalResponse(id=signal_id, status="PENDING")


@app.get("/mt5/poll/{api_key}")
def mt5_poll(api_key: str):
    account_id = db.get_account_for_key(api_key)
    if not account_id:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    pending = db.get_pending_mt5_signals(account_id)
    for signal in pending:
        db.mark_mt5_signal_sent(signal["id"])
    return {"signals": pending}


@app.post("/mt5/report/{api_key}/{signal_id}")
def mt5_report(api_key: str, signal_id: int, req: MT5ReportRequest):
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
        "quantity": signal["quantity"], "price": fill_price, "approved": True,
        "rule_results": [f"MT5 execution: {req.status}" + (f" (ticket {req.mt5_ticket})" if req.mt5_ticket else "")],
        "execution": {"order_id": req.mt5_ticket or "", "status": req.status, "broker": "MT5/Exness",
                       "filled_price": fill_price, "message": req.error_message or "Filled via MT5"},
    })
    if req.status == "FILLED":
        db.update_position(account_id, signal["symbol"], signal["side"], signal["quantity"], fill_price)
    return {"ok": True}


def _process_order(account_id: str, symbol: str, side: str, quantity: float, price: float) -> dict:
    """
    Risk checks, then (if approved) THIS ACCOUNT'S OWN broker
    connector, then database logging. Shared by /orders and the
    strategy execution engine - nothing bypasses the risk engine just
    because an approved strategy is the one asking.
    """
    order = Order(
        symbol=symbol, side=Side.BUY if side == "BUY" else Side.SELL,
        quantity=quantity, price=price, account_id=account_id,
    )
    results = risk_engine.evaluate(order)
    approved = all(r.passed for r in results)

    execution: ExecutionResult | None = None
    if approved:
        account_broker = _get_broker_for_account(account_id)
        execution = account_broker.send_order(order)
        if execution.status == "FILLED":
            db.update_position(order.account_id, order.symbol, order.side.value, order.quantity, order.price)

    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(), "account_id": order.account_id,
        "symbol": order.symbol, "side": order.side.value, "quantity": order.quantity,
        "price": order.price, "approved": approved,
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
    account_id = require_account(x_api_key)
    return [o for o in db.get_orders() if o["account_id"] == account_id]


@app.get("/positions")
def get_positions(x_api_key: str | None = Header(default=None)):
    account_id = require_account(x_api_key)
    positions = db.get_all_positions(account_id)

    enriched = []
    for pos in positions:
        price_source = _build_price_data_source(pos["symbol"])
        try:
            candles = price_source.get_candles(pos["symbol"], timeframe="1m", limit=1)
            current_price = candles[-1]["close"]
        except Exception:
            current_price = None

        unrealized_pnl = None
        unrealized_pct = None
        if current_price is not None and pos["avg_price"]:
            unrealized_pnl = (current_price - pos["avg_price"]) * pos["quantity"]
            unrealized_pct = ((current_price - pos["avg_price"]) / pos["avg_price"]) * 100 * (1 if pos["quantity"] > 0 else -1)

        enriched.append({**pos, "current_price": current_price, "unrealized_pnl": unrealized_pnl, "unrealized_pct": unrealized_pct})
    return enriched


@app.get("/market/{symbol}/candles")
def get_candles(symbol: str, timeframe: str = "1h", limit: int = 100, x_api_key: str | None = Header(default=None)):
    require_account(x_api_key)
    price_source = _build_price_data_source(symbol.upper())
    candles = price_source.get_candles(symbol.upper(), timeframe=timeframe, limit=limit)
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
    account_id = require_account(x_api_key)
    result = strategy_parser.parse(req.description)
    strategy_id = db.save_strategy(
        account_id=account_id, conversation=[req.description],
        strategy_dict=result.strategy.to_dict(), status=result.status.value, questions=result.questions,
    )
    return StrategyResponse(id=strategy_id, status=result.status.value, strategy=result.strategy.to_dict(),
                             questions=result.questions, warnings=result.warnings)


@app.post("/strategies/upload", response_model=StrategyResponse)
async def upload_strategy(file: UploadFile = File(...), x_api_key: str | None = Header(default=None)):
    account_id = require_account(x_api_key)
    content = await file.read()
    try:
        text = extract_text(file.filename, content)
    except (UnsupportedFileType, ImportError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not text.strip():
        raise HTTPException(status_code=400, detail="Couldn't extract any text from that file.")

    result = strategy_parser.parse(text)
    strategy_id = db.save_strategy(
        account_id=account_id, conversation=[f"[uploaded file: {file.filename}]", text],
        strategy_dict=result.strategy.to_dict(), status=result.status.value, questions=result.questions,
    )
    return StrategyResponse(id=strategy_id, status=result.status.value, strategy=result.strategy.to_dict(),
                             questions=result.questions, warnings=result.warnings)


@app.post("/strategies/{strategy_id}/clarify", response_model=StrategyResponse)
def clarify_strategy(strategy_id: int, req: StrategyClarifyRequest, x_api_key: str | None = Header(default=None)):
    account_id = require_account(x_api_key)
    existing = db.get_strategy(strategy_id, account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Strategy not found")

    current_config = StrategyConfig.from_dict(existing["strategy"])
    result = strategy_parser.parse(req.answer, existing=current_config)
    conversation = existing["conversation"] + [req.answer]
    db.update_strategy(strategy_id, conversation, result.strategy.to_dict(), result.status.value, result.questions)
    return StrategyResponse(id=strategy_id, status=result.status.value, strategy=result.strategy.to_dict(),
                             questions=result.questions, warnings=result.warnings)


@app.post("/strategies/{strategy_id}/approve", response_model=StrategyResponse)
def approve_strategy(strategy_id: int, x_api_key: str | None = Header(default=None)):
    account_id = require_account(x_api_key)
    existing = db.get_strategy(strategy_id, account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if existing["status"] != StrategyStatus.READY_FOR_REVIEW.value:
        raise HTTPException(status_code=400, detail=f"Strategy isn't ready for approval yet (current status: {existing['status']})")

    db.update_strategy(strategy_id, existing["conversation"], existing["strategy"], StrategyStatus.APPROVED.value, [])
    return StrategyResponse(id=strategy_id, status=StrategyStatus.APPROVED.value, strategy=existing["strategy"], questions=[])


@app.get("/strategies")
def list_strategies(x_api_key: str | None = Header(default=None)):
    account_id = require_account(x_api_key)
    return db.get_strategies(account_id)


# --- Live strategy execution ------------------------------------------
active_monitors: dict[int, StrategyMonitor] = {}
STRATEGY_CHECK_INTERVAL_SECONDS = 60


def _strategy_order_callback(account_id: str):
    def submit(order: StrategyOrderRequest) -> dict:
        return _process_order(account_id=account_id, symbol=order.symbol, side=order.side,
                               quantity=order.quantity, price=order.price)
    return submit


@app.post("/strategies/{strategy_id}/activate", response_model=StrategyResponse)
def activate_strategy(strategy_id: int, x_api_key: str | None = Header(default=None)):
    account_id = require_account(x_api_key)
    existing = db.get_strategy(strategy_id, account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Strategy not found")
    if existing["status"] not in (StrategyStatus.APPROVED.value, StrategyStatus.PAUSED.value):
        raise HTTPException(status_code=400, detail=f"Strategy must be APPROVED or PAUSED before activating (current status: {existing['status']})")

    strategy_config = StrategyConfig.from_dict(existing["strategy"])
    if not strategy_config.is_executable():
        raise HTTPException(status_code=400, detail="This strategy has no structured, executable entry condition - it can't be run live.")

    price_source = _build_price_data_source(strategy_config.symbol)
    monitor = StrategyMonitor(strategy_id, strategy_config, price_source, _strategy_order_callback(account_id))

    position = db.get_position_detail(account_id, strategy_config.symbol)
    if position["quantity"] != 0:
        monitor.in_position = True
        monitor.entry_price = position["avg_price"]

    active_monitors[strategy_id] = monitor
    db.update_strategy(strategy_id, existing["conversation"], existing["strategy"], StrategyStatus.ACTIVE.value, [])
    return StrategyResponse(id=strategy_id, status=StrategyStatus.ACTIVE.value, strategy=existing["strategy"], questions=[])


@app.post("/strategies/{strategy_id}/pause", response_model=StrategyResponse)
def pause_strategy(strategy_id: int, x_api_key: str | None = Header(default=None)):
    account_id = require_account(x_api_key)
    existing = db.get_strategy(strategy_id, account_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Strategy not found")
    active_monitors.pop(strategy_id, None)
    db.update_strategy(strategy_id, existing["conversation"], existing["strategy"], StrategyStatus.PAUSED.value, [])
    return StrategyResponse(id=strategy_id, status=StrategyStatus.PAUSED.value, strategy=existing["strategy"], questions=[])


async def _strategy_monitoring_loop():
    while True:
        await asyncio.sleep(STRATEGY_CHECK_INTERVAL_SECONDS)
        for strategy_id, monitor in list(active_monitors.items()):
            try:
                result = monitor.check_and_maybe_trade()
                print(f"[QuantGuard] Strategy {strategy_id} check: {result}")
            except Exception as e:
                print(f"[QuantGuard] Strategy {strategy_id} check failed: {e}")


def _rehydrate_active_strategies():
    active_strategies = db.get_all_active_strategies()
    for row in active_strategies:
        strategy_config = StrategyConfig.from_dict(row["strategy"])
        if not strategy_config.is_executable():
            print(f"[QuantGuard] Skipping rehydration of strategy {row['id']} - not executable.")
            continue

        price_source = _build_price_data_source(strategy_config.symbol)
        monitor = StrategyMonitor(row["id"], strategy_config, price_source, _strategy_order_callback(row["account_id"]))

        position = db.get_position_detail(row["account_id"], strategy_config.symbol)
        if position["quantity"] != 0:
            monitor.in_position = True
            monitor.entry_price = position["avg_price"]

        active_monitors[row["id"]] = monitor
        print(f"[QuantGuard] Rehydrated active strategy {row['id']} (account={row['account_id']}, in_position={monitor.in_position})")

    if active_strategies:
        print(f"[QuantGuard] Resumed monitoring {len(active_strategies)} active strategy(ies) after restart.")


@app.on_event("startup")
async def _start_strategy_monitoring():
    _rehydrate_active_strategies()
    asyncio.create_task(_strategy_monitoring_loop())
