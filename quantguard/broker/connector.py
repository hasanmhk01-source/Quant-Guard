"""
Broker connectors.

The whole point of this file: a trader calls the SAME function
(send_order) no matter which exchange is actually behind it.
QuantGuard handles the translation to each broker's real API.

BrokerConnector is the interface every broker plugs into.
BinanceConnector is the first real implementation, using ccxt
(the industry-standard library that already speaks to 100+
exchanges) so we don't hand-roll Binance's raw REST API.

MockConnector simulates fills with no network calls at all -
useful for testing the whole pipeline without touching a real
exchange, and it's what powers the demo/dashboard by default.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from risk_engine.models import Order


@dataclass
class ExecutionResult:
    order_id: str
    status: str          # "FILLED", "REJECTED", "ERROR"
    broker: str
    filled_price: float = 0.0
    message: str = ""
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


class BrokerConnector(ABC):
    """Every broker connector implements this same interface."""

    name: str = "UnnamedBroker"

    @abstractmethod
    def send_order(self, order: Order) -> ExecutionResult:
        ...


class MockConnector(BrokerConnector):
    """
    Simulates order execution with no real exchange connection.
    Used for local testing, demos, and this dashboard - since this
    environment has no internet access to reach a real exchange.

    Fills every order instantly at the order's own limit price.
    """

    name = "MockExchange (paper trading)"

    def send_order(self, order: Order) -> ExecutionResult:
        return ExecutionResult(
            order_id=str(uuid.uuid4())[:8],
            status="FILLED",
            broker=self.name,
            filled_price=order.price,
            message=f"Simulated fill: {order.side.value} {order.quantity} {order.symbol} @ ${order.price:,.2f}",
        )


class BinanceConnector(BrokerConnector):
    """
    Real Binance connector using ccxt.

    Requires: pip install ccxt
    Requires: a Binance (or Binance testnet) API key + secret.

    This is written to run against Binance's TESTNET by default -
    never live funds - until you explicitly flip testnet=False
    with a real, funded account.
    """

    name = "Binance"

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        import ccxt  # imported here so the rest of the app works without ccxt installed

        self.exchange = ccxt.binance({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {
                # Auto-corrects for small system-clock drift by asking
                # Binance for its server time and adjusting requests
                # accordingly - without this, even a ~1 second clock
                # difference causes Binance to reject every order with
                # a "Timestamp ... ahead of the server's time" error
                # (code -1021). Syncing the OS clock is still worth
                # doing, but this makes the connector resilient even
                # when the clock drifts again later.
                "adjustForTimeDifference": True,
            },
        })
        if testnet:
            self.exchange.set_sandbox_mode(True)

    def send_order(self, order: Order) -> ExecutionResult:
        try:
            # Market order, not limit: fills immediately at whatever the
            # current live price is, instead of sitting open/unfilled on
            # the order book waiting for the market to reach an exact
            # limit price. This is what makes "order sent" actually mean
            # "order filled" for testing purposes. (A limit order is more
            # realistic for production trading, but market orders are
            # what let you SEE the pipeline actually complete right now.)
            result = self.exchange.create_order(
                symbol=self._to_ccxt_symbol(order.symbol),
                type="market",
                side=order.side.value.lower(),
                amount=order.quantity,
            )

            # Binance's own status tells us what really happened - don't
            # assume FILLED just because the API call didn't raise an
            # exception. A market order should fill immediately, but this
            # keeps the reported status honest either way.
            raw_status = (result.get("status") or "").lower()
            if raw_status in ("closed", "filled"):
                status = "FILLED"
                message = "Order filled on Binance"
            elif raw_status in ("open", "new", "partially_filled"):
                status = "OPEN"
                message = f"Order placed on Binance but not yet fully filled (status: {raw_status})"
            else:
                status = "UNKNOWN"
                message = f"Order placed on Binance - status unclear: {raw_status or 'not returned'}"

            filled_price = result.get("average") or result.get("price") or order.price

            return ExecutionResult(
                order_id=str(result.get("id", "")),
                status=status,
                broker=self.name,
                filled_price=float(filled_price),
                message=message,
            )
        except Exception as e:
            return ExecutionResult(
                order_id="",
                status="ERROR",
                broker=self.name,
                message=str(e),
            )

    @staticmethod
    def _to_ccxt_symbol(symbol: str) -> str:
        # "BTCUSDT" -> "BTC/USDT", which is what ccxt expects
        if "/" in symbol:
            return symbol
        for quote in ("USDT", "USDC", "BUSD", "BTC"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return f"{symbol[:-len(quote)]}/{quote}"
        return symbol
