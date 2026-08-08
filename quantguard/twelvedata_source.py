"""
Forex and metals price data via Twelve Data - Binance (crypto-only)
has no EURUSD, GBPUSD, XAUUSD, XAGUSD, etc., so these symbols need a
different provider.

Free tier: twelvedata.com - sign up, no card required, includes
forex majors and metals (XAU/USD, XAG/USD) with a free API key.
Rate-limited (check their current free-tier limits on signup - this
was NOT verified against a live account from this environment, same
honest caveat as the MT5 bridge: verify it actually works on your
machine before relying on it).

Symbol routing: main.py should send crypto pairs (BTCUSDT, ETHUSDT,
...) to BinancePriceDataSource, and forex/metals symbols (EURUSD,
XAUUSD, XAGUSD, ...) to this class instead - see is_forex_symbol()
below for the routing rule.
"""

import os
from .price_data import PriceDataSource

# Symbols this app treats as forex/metals (routed to Twelve Data)
# rather than crypto (routed to Binance). Extend this set as needed -
# it's deliberately a simple allow-list, not an attempt to auto-detect
# every possible symbol correctly.
FOREX_AND_METALS_SYMBOLS = {
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    "EURGBP", "EURJPY", "GBPJPY", "EURCHF", "AUDJPY", "EURAUD", "USDMXN",
    "XAUUSD",  # gold
    "XAGUSD",  # silver
}


def is_forex_symbol(symbol: str) -> bool:
    return symbol.upper() in FOREX_AND_METALS_SYMBOLS


class TwelveDataPriceSource(PriceDataSource):
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("TWELVE_DATA_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "TWELVE_DATA_API_KEY is not set. Get a free key (no card needed) "
                "at https://twelvedata.com and set it as an environment variable."
            )
        import requests  # imported here so the app works without `requests` installed
                          # if forex/metals aren't being used
        self._requests = requests

    def get_candles(self, symbol: str, timeframe: str = "1h", limit: int = 100) -> list[dict]:
        # Twelve Data wants "EUR/USD" style symbols, and its own interval codes.
        td_symbol = self._to_twelvedata_symbol(symbol)
        td_interval = self._to_twelvedata_interval(timeframe)

        response = self._requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": td_symbol,
                "interval": td_interval,
                "outputsize": limit,
                "apikey": self.api_key,
            },
            timeout=10,
        )
        data = response.json()

        if data.get("status") == "error" or "values" not in data:
            raise ValueError(f"Twelve Data error for {symbol}: {data.get('message', data)}")

        # Twelve Data returns most-recent-first; our PriceDataSource
        # contract is oldest-first, so reverse it.
        candles = [
            {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0) or 0),
            }
            for row in reversed(data["values"])
        ]
        return candles

    @staticmethod
    def _to_twelvedata_symbol(symbol: str) -> str:
        symbol = symbol.upper()
        if "/" in symbol:
            return symbol
        # EURUSD -> EUR/USD, XAUUSD -> XAU/USD (all our forex/metals symbols are 6 characters, 3+3)
        if len(symbol) == 6:
            return f"{symbol[:3]}/{symbol[3:]}"
        return symbol

    @staticmethod
    def _to_twelvedata_interval(timeframe: str) -> str:
        # Our short codes (1m, 5m, 1h, 4h, 1d) map directly to Twelve Data's
        # own interval strings, with one difference: they use "1min" not "1m" for minutes.
        mapping = {
            "1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min",
            "1h": "1h", "4h": "4h", "1d": "1day",
        }
        return mapping.get(timeframe, "1h")
