"""
Indicator calculations.

Every function here takes historical price data (a list of candles,
oldest first) and returns a single computed number - RSI, EMA, SMA,
etc. This is what turns a structured condition like "RSI(14) < 30"
from an abstract idea into an actual number that can be compared.

A candle is a dict: {"open": float, "high": float, "low": float,
"close": float, "volume": float}. Only `close`, `high`, `low`, and
`volume` are used here.

These are standard, well-known formulas - deliberately implemented
directly rather than pulled from a library, so there's no dependency
and the math is fully visible and testable.
"""

from .conditions import Indicator, IndicatorRef


def sma(closes: list[float], period: int) -> float:
    """Simple moving average of the last `period` closes."""
    if len(closes) < period:
        raise ValueError(f"Need at least {period} closes, got {len(closes)}")
    return sum(closes[-period:]) / period


def ema(closes: list[float], period: int) -> float:
    """
    Exponential moving average - weights recent prices more heavily
    than older ones. Standard formula: start from an SMA seed, then
    apply the smoothing multiplier forward through the remaining data.
    """
    if len(closes) < period:
        raise ValueError(f"Need at least {period} closes, got {len(closes)}")
    multiplier = 2 / (period + 1)
    ema_value = sum(closes[:period]) / period  # seed with SMA of the first `period` values
    for price in closes[period:]:
        ema_value = (price - ema_value) * multiplier + ema_value
    return ema_value


def rsi(closes: list[float], period: int = 14) -> float:
    """
    Relative Strength Index - standard Wilder's smoothing formula.
    Returns a value from 0 to 100. Above 70 is traditionally
    considered "overbought", below 30 "oversold" - but this function
    just computes the number; interpreting it is up to the strategy's
    condition (RSI < 30, RSI > 70, etc).
    """
    if len(closes) < period + 1:
        raise ValueError(f"Need at least {period + 1} closes, got {len(closes)}")

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing for the remaining data points
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def rolling_high(highs: list[float], period: int) -> float:
    """Highest high over the last `period` candles."""
    if len(highs) < period:
        raise ValueError(f"Need at least {period} highs, got {len(highs)}")
    return max(highs[-period:])


def rolling_low(lows: list[float], period: int) -> float:
    """Lowest low over the last `period` candles."""
    if len(lows) < period:
        raise ValueError(f"Need at least {period} lows, got {len(lows)}")
    return min(lows[-period:])


def percent_change_from_high(candles: list[dict], period: int) -> float:
    """
    How far the current price has fallen from the recent high, as a
    percentage. A 3% drop from the high returns -3.0 (negative, since
    it's a decrease) - this lets a condition like "<=  -3" mean
    "dropped at least 3% from the high", matching how a trader would
    naturally phrase "3% drop".
    """
    highs = [c["high"] for c in candles]
    current_price = candles[-1]["close"]
    high = rolling_high(highs, period)
    return ((current_price - high) / high) * 100


def average_volume(candles: list[dict], period: int) -> float:
    volumes = [c["volume"] for c in candles]
    return sma(volumes, period)


def compute_indicator(ref: IndicatorRef, candles: list[dict]) -> float:
    """
    The single dispatch point: given an IndicatorRef (e.g. RSI(14) or
    PRICE) and historical candles, returns the current computed value.
    This is what the condition evaluator (evaluator.py) calls for each
    side of a Condition.
    """
    closes = [c["close"] for c in candles]

    if ref.indicator == Indicator.PRICE:
        return closes[-1]
    elif ref.indicator == Indicator.RSI:
        return rsi(closes, ref.period or 14)
    elif ref.indicator == Indicator.EMA:
        return ema(closes, ref.period)
    elif ref.indicator == Indicator.SMA:
        return sma(closes, ref.period)
    elif ref.indicator == Indicator.VOLUME:
        return candles[-1]["volume"]
    elif ref.indicator == Indicator.HIGH:
        return rolling_high([c["high"] for c in candles], ref.period)
    elif ref.indicator == Indicator.LOW:
        return rolling_low([c["low"] for c in candles], ref.period)
    elif ref.indicator == Indicator.PERCENT_CHANGE_FROM_HIGH:
        return percent_change_from_high(candles, ref.period)
    else:
        raise ValueError(f"Unknown indicator: {ref.indicator}")
