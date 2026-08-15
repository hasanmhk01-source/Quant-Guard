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


def stddev(closes: list[float], period: int) -> float:
    """Population standard deviation of the last `period` closes -
    the volatility building block for Bollinger Bands and z-scores."""
    if len(closes) < period:
        raise ValueError(f"Need at least {period} closes, got {len(closes)}")
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    return variance ** 0.5


def _ema_series(closes: list[float], period: int) -> list[float]:
    """Same formula as ema(), but returns the FULL series of EMA values
    (one per closing price from `period` onward) instead of just the
    final number - MACD needs the whole series to build its own line,
    not a single snapshot value."""
    if len(closes) < period:
        raise ValueError(f"Need at least {period} closes, got {len(closes)}")
    multiplier = 2 / (period + 1)
    series = [sum(closes[:period]) / period]  # seed with SMA of the first `period` values
    for price in closes[period:]:
        series.append((price - series[-1]) * multiplier + series[-1])
    return series


def true_range(candles: list[dict]) -> list[float]:
    """True range for each candle from the 2nd onward: the largest of
    (high-low), (high - previous close), (low - previous close) -
    accounts for gaps between candles, which a plain high-low range
    would miss. First candle has no previous close, so it's skipped."""
    ranges = []
    for i in range(1, len(candles)):
        high, low = candles[i]["high"], candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return ranges


def atr(candles: list[dict], period: int = 14) -> float:
    """Average True Range - a simple (not Wilder-smoothed) average of
    true range over the last `period` candles. Standard volatility
    measure: bigger ATR = bigger typical price swings."""
    ranges = true_range(candles)
    if len(ranges) < period:
        raise ValueError(f"Need at least {period + 1} candles, got {len(candles)}")
    return sum(ranges[-period:]) / period


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float]:
    """
    Standard MACD: (macd_line, signal_line, histogram), using the
    conventional 12/26/9 periods. Needs the fast and slow EMA series
    aligned to the same closes, then a signal-period EMA of the
    resulting MACD line itself - not just three independent EMA calls.
    """
    if len(closes) < slow + signal:
        raise ValueError(f"Need at least {slow + signal} closes for MACD, got {len(closes)}")
    fast_series = _ema_series(closes, fast)
    slow_series = _ema_series(closes, slow)
    # fast_series is longer (starts earlier) than slow_series since fast < slow -
    # align both to end at the same point by trimming fast_series's head.
    fast_aligned = fast_series[-len(slow_series):]
    macd_series = [f - s for f, s in zip(fast_aligned, slow_series)]
    signal_series = _ema_series(macd_series, signal)
    macd_line = macd_series[-1]
    signal_line = signal_series[-1]
    return macd_line, signal_line, macd_line - signal_line


def vwap(candles: list[dict], period: int) -> float:
    """
    Volume-weighted average price over the last `period` candles - a
    ROLLING window, not the traditional session-reset-at-market-open
    VWAP. That's a deliberate simplification: this engine has no
    concept of exchange sessions/trading days, and a rolling VWAP is
    still a meaningful "average price weighted by how much actually
    traded there" for any lookback window.
    """
    window = candles[-period:]
    if len(window) < period:
        raise ValueError(f"Need at least {period} candles, got {len(candles)}")
    total_volume = sum(c["volume"] for c in window)
    if total_volume == 0:
        raise ValueError("Total volume is zero over this window - VWAP is undefined")
    return sum(c["close"] * c["volume"] for c in window) / total_volume


# --- SMC / ICT structural price action ------------------------------------
#
# These implement genuine structural concepts, but with real, stated
# simplifications - worth understanding before trusting them with money:
#
# - Swing points use a "fractal" definition: a candle whose high (or low)
#   is more extreme than `left` candles before it and `right` candles
#   after it. The most RECENT swing point needs `right` candles to
#   already exist after it to be "confirmed" - so very recent price
#   action can't have a confirmed swing point yet, by definition.
# - Fair value gap detection here is PRESENCE-only: it reports whether
#   a 3-candle imbalance pattern exists in the lookback window, but does
#   NOT track whether price already came back and "filled"/mitigated
#   that gap. A real ICT trader cares a lot about mitigation status;
#   this is a simpler first pass, not the full concept.
# - Market structure (HH/HL vs LH/LL) only looks at the two most recent
#   confirmed swing highs and lows - it doesn't do full multi-swing
#   trend mapping.

def _find_swing_highs(candles: list[dict], left: int = 2, right: int = 2) -> list[tuple[int, float]]:
    """Returns (index, price) for every confirmed swing high: a candle
    whose high is strictly greater than `left` candles before it and
    `right` candles after it."""
    highs = [c["high"] for c in candles]
    swings = []
    for i in range(left, len(highs) - right):
        window_before = highs[i - left:i]
        window_after = highs[i + 1:i + 1 + right]
        if all(highs[i] > h for h in window_before) and all(highs[i] > h for h in window_after):
            swings.append((i, highs[i]))
    return swings


def _find_swing_lows(candles: list[dict], left: int = 2, right: int = 2) -> list[tuple[int, float]]:
    """Mirror of _find_swing_highs for lows."""
    lows = [c["low"] for c in candles]
    swings = []
    for i in range(left, len(lows) - right):
        window_before = lows[i - left:i]
        window_after = lows[i + 1:i + 1 + right]
        if all(lows[i] < l for l in window_before) and all(lows[i] < l for l in window_after):
            swings.append((i, lows[i]))
    return swings


def swing_high(candles: list[dict], period: int = 20, left: int = 2, right: int = 2) -> float:
    """Most recent confirmed swing-high price within the last `period` candles."""
    window = candles[-period:] if len(candles) > period else candles
    swings = _find_swing_highs(window, left, right)
    if not swings:
        raise ValueError(f"No confirmed swing high found in the last {period} candles - "
                          f"try a longer lookback or fewer candles held back for confirmation")
    return swings[-1][1]


def swing_low(candles: list[dict], period: int = 20, left: int = 2, right: int = 2) -> float:
    """Most recent confirmed swing-low price within the last `period` candles."""
    window = candles[-period:] if len(candles) > period else candles
    swings = _find_swing_lows(window, left, right)
    if not swings:
        raise ValueError(f"No confirmed swing low found in the last {period} candles - "
                          f"try a longer lookback or fewer candles held back for confirmation")
    return swings[-1][1]


def bos_bullish(candles: list[dict], period: int = 20) -> float:
    """1.0 if the CURRENT price has broken above the most recent
    confirmed swing high (a bullish break of structure), else 0.0.
    Returns 0.0 (not an error) if no swing high exists yet in the
    window, rather than crashing a strategy that just hasn't seen
    enough structure yet."""
    try:
        last_swing_high = swing_high(candles, period)
    except ValueError:
        return 0.0
    current_price = candles[-1]["close"]
    return 1.0 if current_price > last_swing_high else 0.0


def bos_bearish(candles: list[dict], period: int = 20) -> float:
    """Mirror of bos_bullish for a bearish break below the last swing low."""
    try:
        last_swing_low = swing_low(candles, period)
    except ValueError:
        return 0.0
    current_price = candles[-1]["close"]
    return 1.0 if current_price < last_swing_low else 0.0


def market_structure(candles: list[dict], period: int = 20) -> float:
    """
    1.0 = bullish structure (most recent swing high AND low are both
    higher than the ones before them - higher highs, higher lows).
    -1.0 = bearish structure (lower highs, lower lows).
    0.0 = mixed/unclear (not enough confirmed swings, or the pattern
    doesn't cleanly fit either case).
    """
    window = candles[-period:] if len(candles) > period else candles
    highs = _find_swing_highs(window)
    lows = _find_swing_lows(window)
    if len(highs) < 2 or len(lows) < 2:
        return 0.0
    higher_highs = highs[-1][1] > highs[-2][1]
    higher_lows = lows[-1][1] > lows[-2][1]
    lower_highs = highs[-1][1] < highs[-2][1]
    lower_lows = lows[-1][1] < lows[-2][1]
    if higher_highs and higher_lows:
        return 1.0
    if lower_highs and lower_lows:
        return -1.0
    return 0.0


def fvg_bullish(candles: list[dict], period: int = 20) -> float:
    """
    1.0 if a bullish fair value gap (3-candle imbalance where candle
    N-2's high sits below candle N's low, leaving a gap the middle
    candle's body didn't fully cover) exists anywhere in the last
    `period` candles, else 0.0. PRESENCE only - does not check whether
    price has already returned to fill/mitigate the gap since.
    """
    window = candles[-period:] if len(candles) > period else candles
    for i in range(2, len(window)):
        if window[i - 2]["high"] < window[i]["low"]:
            return 1.0
    return 0.0


def fvg_bearish(candles: list[dict], period: int = 20) -> float:
    """Mirror of fvg_bullish: candle N-2's low sits above candle N's high."""
    window = candles[-period:] if len(candles) > period else candles
    for i in range(2, len(window)):
        if window[i - 2]["low"] > window[i]["high"]:
            return 1.0
    return 0.0


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

    # --- SMC / ICT ---
    elif ref.indicator == Indicator.SWING_HIGH:
        return swing_high(candles, ref.period or 20)
    elif ref.indicator == Indicator.SWING_LOW:
        return swing_low(candles, ref.period or 20)
    elif ref.indicator == Indicator.BOS_BULLISH:
        return bos_bullish(candles, ref.period or 20)
    elif ref.indicator == Indicator.BOS_BEARISH:
        return bos_bearish(candles, ref.period or 20)
    elif ref.indicator == Indicator.MARKET_STRUCTURE:
        return market_structure(candles, ref.period or 20)
    elif ref.indicator == Indicator.FVG_BULLISH:
        return fvg_bullish(candles, ref.period or 20)
    elif ref.indicator == Indicator.FVG_BEARISH:
        return fvg_bearish(candles, ref.period or 20)

    # --- Quant / statistical ---
    elif ref.indicator == Indicator.BOLLINGER_UPPER:
        period = ref.period or 20
        return sma(closes, period) + 2 * stddev(closes, period)
    elif ref.indicator == Indicator.BOLLINGER_MID:
        return sma(closes, ref.period or 20)
    elif ref.indicator == Indicator.BOLLINGER_LOWER:
        period = ref.period or 20
        return sma(closes, period) - 2 * stddev(closes, period)
    elif ref.indicator == Indicator.ATR:
        return atr(candles, ref.period or 14)
    elif ref.indicator == Indicator.MACD_LINE:
        return macd(closes)[0]
    elif ref.indicator == Indicator.MACD_SIGNAL:
        return macd(closes)[1]
    elif ref.indicator == Indicator.MACD_HIST:
        return macd(closes)[2]
    elif ref.indicator == Indicator.STDDEV:
        return stddev(closes, ref.period or 20)
    elif ref.indicator == Indicator.ZSCORE:
        period = ref.period or 20
        return (closes[-1] - sma(closes, period)) / stddev(closes, period)
    elif ref.indicator == Indicator.VWAP:
        return vwap(candles, ref.period or 20)
    else:
        raise ValueError(f"Unknown indicator: {ref.indicator}")
