"""
Price data sources.

Same pattern as broker/connector.py: one interface, swap the
implementation. The execution engine asks for "recent candles for
this symbol" without caring whether they come from a real exchange
or test data.

- MockPriceDataSource: fixed/generated candles, no network needed -
  used for testing the engine's decision logic in isolation.
- BinancePriceDataSource: real historical candles from Binance via
  ccxt. This can only be verified with real network access (on your
  machine, not this sandbox) - same honest limitation as
  BinanceConnector in the broker module.
"""

from abc import ABC, abstractmethod


class PriceDataSource(ABC):
    @abstractmethod
    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list[dict]:
        """
        Returns a list of candles, OLDEST FIRST, each a dict with
        keys: open, high, low, close, volume. `limit` is how many
        candles to fetch - callers need enough history for whatever
        indicator period they're computing (e.g. RSI(14) needs at
        least 15 candles).
        """
        ...


class MockPriceDataSource(PriceDataSource):
    """
    Returns pre-set candles (for deterministic tests) or, if none were
    given, generates a simple synthetic series - useful for manually
    trying out the engine without any real exchange connection.
    """

    def __init__(self, fixed_candles: list[dict] = None):
        self.fixed_candles = fixed_candles

    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list[dict]:
        if self.fixed_candles is not None:
            return self.fixed_candles
        # Simple synthetic uptrend if nothing else was specified.
        return [{"open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100 + i, "volume": 1000}
                for i in range(limit)]


class BinancePriceDataSource(PriceDataSource):
    """
    Real historical candles from Binance (or Binance testnet) via ccxt.
    Requires the same setup as BinanceConnector - an API key isn't
    even strictly necessary for public price data, but reusing the
    same exchange connection keeps things simple.
    """

    def __init__(self, testnet: bool = True):
        import ccxt  # imported here so the app works without ccxt installed
        self.exchange = ccxt.binance({"enableRateLimit": True})
        if testnet:
            self.exchange.set_sandbox_mode(True)

    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list[dict]:
        ccxt_symbol = self._to_ccxt_symbol(symbol)
        raw = self.exchange.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, limit=limit)
        # ccxt OHLCV rows are [timestamp, open, high, low, close, volume], oldest first
        return [
            {"open": row[1], "high": row[2], "low": row[3], "close": row[4], "volume": row[5]}
            for row in raw
        ]

    @staticmethod
    def _to_ccxt_symbol(symbol: str) -> str:
        if "/" in symbol:
            return symbol
        for quote in ("USDT", "USDC", "BUSD", "BTC"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return f"{symbol[:-len(quote)]}/{quote}"
        return symbol
