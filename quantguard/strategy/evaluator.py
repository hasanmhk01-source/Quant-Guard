"""
Condition evaluation.

Takes a Condition or ConditionGroup (see conditions.py) and returns
True/False: has this condition actually been met right now? This is
the deterministic, mechanical piece that replaces "an LLM looks at
the market and decides" - no judgment calls happen here, just
computing indicator values and comparing them exactly as the
trader's approved strategy specified.

Timeframe-aware: different conditions in the same strategy can
reference different chart timeframes (e.g. "RSI(14) on the 1-minute
chart AND price above the 200-EMA on the daily chart"). Each
condition fetches candles for its OWN indicator's timeframe (falling
back to the strategy's default_timeframe if the condition didn't
specify one), and a small per-evaluation cache avoids re-fetching the
same timeframe's candles twice if multiple conditions share it.
"""

from .conditions import Condition, ConditionGroup, IndicatorRef
from .indicators import compute_indicator
from .price_data import PriceDataSource

CANDLE_LIMIT = 200  # enough history for any indicator period this schema supports


def _get_candles(price_data_source: PriceDataSource, symbol: str, timeframe: str, cache: dict) -> list[dict]:
    """Fetches candles for a timeframe, reusing a cached fetch within
    the same evaluation pass if another condition already needed the
    same timeframe."""
    if timeframe not in cache:
        cache[timeframe] = price_data_source.get_candles(symbol, timeframe=timeframe, limit=CANDLE_LIMIT)
    return cache[timeframe]


def _resolve_value(ref_or_number, price_data_source: PriceDataSource, symbol: str, default_timeframe: str, cache: dict):
    """A Condition's left/right side is either a plain number or an
    IndicatorRef - this resolves either into an actual number."""
    if isinstance(ref_or_number, IndicatorRef):
        timeframe = ref_or_number.timeframe or default_timeframe
        candles = _get_candles(price_data_source, symbol, timeframe, cache)
        return compute_indicator(ref_or_number, candles)
    return ref_or_number


def evaluate_condition(
    condition: Condition,
    price_data_source: PriceDataSource,
    symbol: str,
    default_timeframe: str = "1h",
    cache: dict = None,
) -> bool:
    cache = {} if cache is None else cache
    left_value = _resolve_value(condition.left, price_data_source, symbol, default_timeframe, cache)
    right_value = _resolve_value(condition.right, price_data_source, symbol, default_timeframe, cache)

    op = condition.operator.value
    if op == "<":
        return left_value < right_value
    elif op == ">":
        return left_value > right_value
    elif op == "<=":
        return left_value <= right_value
    elif op == ">=":
        return left_value >= right_value
    elif op == "==":
        return left_value == right_value
    else:
        raise ValueError(f"Unknown operator: {op}")


def evaluate_condition_group(
    group: ConditionGroup,
    price_data_source: PriceDataSource,
    symbol: str,
    default_timeframe: str = "1h",
    cache: dict = None,
) -> bool:
    """Recursively evaluates every item in the group (conditions or
    nested groups) and combines them with AND/OR logic. The same
    `cache` dict is threaded through the whole recursive call so
    candles for a given timeframe are only fetched once per pass,
    even if referenced by several conditions across nested groups."""
    cache = {} if cache is None else cache
    results = [
        evaluate_condition_group(item, price_data_source, symbol, default_timeframe, cache)
        if isinstance(item, ConditionGroup)
        else evaluate_condition(item, price_data_source, symbol, default_timeframe, cache)
        for item in group.items
    ]
    if not results:
        return False  # an empty group can never be "met" - fail safe, not open safe
    return all(results) if group.logic == "AND" else any(results)
