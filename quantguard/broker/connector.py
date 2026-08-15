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


class CcxtConnector(BrokerConnector):
    """
    Real connector for ANY exchange ccxt supports (Binance, Kraken,
    Bybit, OKX, etc) - not just Binance. This is what makes per-account
    'connect your own broker' actually work for more than one exchange,
    and is also the fix for Binance-specific problems (like a region
    block) - a trader can connect Kraken or Bybit instead, using this
    exact same class, with zero new code needed per exchange.

    exchange_id is ccxt's own identifier for the exchange, e.g.
    "binance", "kraken", "bybit", "okx" - see ccxt.exchanges for the
    full supported list. Not every exchange supports set_sandbox_mode
    (testnet) the same way; ones that don't will raise clearly rather
    than silently trading on mainnet.
    """

    def __init__(self, exchange_id: str, api_key: str, api_secret: str, testnet: bool = True):
        import ccxt  # imported here so the rest of the app works without ccxt installed

        exchange_id = exchange_id.lower().strip()
        if not hasattr(ccxt, exchange_id):
            raise ValueError(
                f"'{exchange_id}' isn't a ccxt-supported exchange. "
                f"See https://docs.ccxt.com/#/README?id=exchanges for the full list "
                f"(e.g. binance, kraken, bybit, okx)."
            )
        exchange_class = getattr(ccxt, exchange_id)
        self.name = exchange_id.capitalize()
        self.exchange = exchange_class({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {
                # See BinanceConnector's original comment - the same
                # clock-drift protection applies to any exchange.
                "adjustForTimeDifference": True,
            },
        })
        if testnet:
            try:
                self.exchange.set_sandbox_mode(True)
            except Exception as e:
                raise ValueError(
                    f"{self.name} doesn't support ccxt's sandbox mode the way this connector "
                    f"expects ({e}). Don't trade real funds on it without verifying testnet "
                    f"support manually first."
                )

    def send_order(self, order: Order) -> ExecutionResult:
        try:
            # Market order, not limit - see BinanceConnector's original
            # comment for why: this is what makes "order sent" actually
            # mean "order filled" for testing purposes.
            result = self.exchange.create_order(
                symbol=self._to_ccxt_symbol(order.symbol),
                type="market",
                side=order.side.value.lower(),
                amount=order.quantity,
            )

            raw_status = (result.get("status") or "").lower()
            if raw_status in ("closed", "filled"):
                status = "FILLED"
                message = f"Order filled on {self.name}"
            elif raw_status in ("open", "new", "partially_filled"):
                status = "OPEN"
                message = f"Order placed on {self.name} but not yet fully filled (status: {raw_status})"
            else:
                status = "UNKNOWN"
                message = f"Order placed on {self.name} - status unclear: {raw_status or 'not returned'}"

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


class BinanceConnector(CcxtConnector):
    """Kept as a thin, explicitly-named subclass for backward
    compatibility with existing code/tests that reference
    BinanceConnector directly - identical behavior to
    CcxtConnector('binance', ...)."""

    name = "Binance"

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        super().__init__("binance", api_key, api_secret, testnet)
        self.name = "Binance"


class OandaConnector(BrokerConnector):
    """
    Real OANDA connector - a forex/CFD broker with a genuine plain
    REST API. This is why OANDA, not Deriv, is the second real broker
    added here: Deriv requires holding open a persistent WebSocket
    connection and speaking its own message protocol to place orders,
    which is a fundamentally heavier integration than a request/response
    REST call. Brokers like OANDA, IG, and most MT5-bridge-style
    brokers fit this same simple REST pattern; Deriv-style
    WebSocket-only brokers each need their own dedicated connector as
    separate follow-up work.

    OANDA's auth is a single bearer token plus an account ID - it has
    no separate "secret" the way an exchange API key does. Since the
    broker_connections table only has two credential fields (api_key,
    api_secret), this connector reuses api_secret to carry the account
    ID rather than adding a third field to the schema. The dashboard's
    Broker Connection form should tell OANDA traders to put their
    Account ID in the "API Secret" box.

    "testnet" maps to OANDA's own "practice" vs "live" environments -
    different naming, same concept (fake funds vs real funds).

    NOT verified against a real OANDA account in this environment (no
    network access here to test it) - same honest caveat as every
    other real-broker connector in this project. Test on a practice
    account before ever pointing this at a live one.
    """

    def __init__(self, api_token: str, account_id: str, testnet: bool = True):
        import requests  # imported here so the app works without requests installed if OANDA isn't used
        self._requests = requests
        self.name = "OANDA"
        self.api_token = api_token
        self.account_id = account_id
        self.base_url = "https://api-fxpractice.oanda.com" if testnet else "https://api-fxtrade.oanda.com"

    def send_order(self, order: Order) -> ExecutionResult:
        instrument = self._to_oanda_instrument(order.symbol)
        # OANDA has no separate buy/sell field - a positive unit count
        # IS a buy, a negative unit count IS a sell.
        units = order.quantity if order.side.value == "BUY" else -order.quantity
        try:
            response = self._requests.post(
                f"{self.base_url}/v3/accounts/{self.account_id}/orders",
                headers={"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"},
                json={"order": {"units": str(units), "instrument": instrument,
                                 "type": "MARKET", "positionFill": "DEFAULT"}},
                timeout=10,
            )
            data = response.json()
            if not response.ok:
                return ExecutionResult(
                    order_id="", status="ERROR", broker=self.name,
                    message=str(data.get("errorMessage", data)),
                )
            fill = data.get("orderFillTransaction")
            if fill:
                return ExecutionResult(
                    order_id=str(fill.get("id", "")),
                    status="FILLED",
                    broker=self.name,
                    filled_price=float(fill.get("price", order.price)),
                    message="Order filled on OANDA",
                )
            # Order accepted but no immediate fill transaction (e.g.
            # pending/queued) - report honestly rather than guess FILLED.
            return ExecutionResult(
                order_id=str(data.get("orderCreateTransaction", {}).get("id", "")),
                status="UNKNOWN",
                broker=self.name,
                message=f"Order placed on OANDA - fill status unclear: {data}",
            )
        except Exception as e:
            return ExecutionResult(order_id="", status="ERROR", broker=self.name, message=str(e))

    @staticmethod
    def _to_oanda_instrument(symbol: str) -> str:
        # "EURUSD" -> "EUR_USD", which is what OANDA expects
        symbol = symbol.upper().replace("/", "")
        if len(symbol) == 6:
            return f"{symbol[:3]}_{symbol[3:]}"
        return symbol


class UnsupportedBrokerConnector(BrokerConnector):
    """
    For a broker a trader has connected credentials for, but that
    QuantGuard doesn't have a real connector for yet (e.g. Deriv, which
    uses its own WebSocket API, not ccxt/REST - a genuinely different
    integration, not a one-line addition).

    Deliberately fails LOUD and HONEST on every order - never silently
    routes to a different broker or pretends to fill. A trader who
    connected Deriv should see a clear 'not supported yet' error, not
    a fake fill or a mysterious Binance order.
    """

    def __init__(self, broker_name: str):
        self.name = broker_name

    def send_order(self, order: Order) -> ExecutionResult:
        return ExecutionResult(
            order_id="",
            status="ERROR",
            broker=self.name,
            message=(
                f"{self.name} isn't wired up for real order execution yet - "
                f"only ccxt-supported exchanges (Binance, Kraken, Bybit, OKX, etc) "
                f"work for live orders right now. Your credentials are saved, but "
                f"no order was sent anywhere."
            ),
        )
