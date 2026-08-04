"""
Strategy execution engine.

This is what turns an APPROVED strategy into one that actually trades.
It is deliberately the LAST piece in the whole pipeline to make a
decision with real consequences, and even then, it makes NO judgment
calls - it only mechanically checks the trader's own approved,
structured conditions against real price data, exactly as specified.

No LLM is involved anywhere in this file. By the time a strategy
reaches here, the natural-language understanding step is long done;
this only ever evaluates numbers against numbers.

One StrategyMonitor per active strategy. Call check_and_maybe_trade()
on an interval (e.g. every few minutes) - each call either does
nothing (condition not met), enters a position (submits an order),
or exits one (stop-loss or take-profit hit).
"""

from dataclasses import dataclass
from typing import Callable, Optional

from .models import StrategyConfig
from .price_data import PriceDataSource
from .evaluator import evaluate_condition_group


@dataclass
class OrderRequest:
    """What the monitor asks the caller to submit - deliberately the
    same shape as the existing /orders payload, so this plugs straight
    into the risk engine + broker pipeline already built, with zero
    special-casing for strategy-originated orders."""
    symbol: str
    side: str
    quantity: float
    price: float


class StrategyMonitor:
    """
    Tracks ONE strategy's live state (whether it currently holds a
    position) and decides, on each check, whether to enter, exit, or
    do nothing - purely by evaluating the strategy's own approved
    rules against current price data.
    """

    def __init__(
        self,
        strategy_id: int,
        strategy: StrategyConfig,
        price_data_source: PriceDataSource,
        submit_order_callback: Callable[[OrderRequest], dict],
    ):
        if not strategy.is_executable():
            raise ValueError(
                "Cannot monitor a strategy with no structured entry_conditions - "
                "it isn't executable yet (see StrategyConfig.is_executable)."
            )
        self.strategy_id = strategy_id
        self.strategy = strategy
        self.price_data_source = price_data_source
        self.submit_order = submit_order_callback
        self.in_position = False
        self.entry_price: Optional[float] = None

    def check_and_maybe_trade(self) -> dict:
        """
        One "tick" of the engine. Returns a dict describing what
        happened - useful for logging, testing, and showing the
        trader a live status ("waiting", "entered", "holding",
        "exited"), never a black-box silent action.
        """
        # Current price for P&L tracking always comes from the
        # strategy's own default timeframe, regardless of which
        # timeframe(s) the entry condition itself references - this
        # keeps stop-loss/take-profit tracking consistent even for
        # strategies whose entry logic mixes multiple timeframes.
        reference_candles = self.price_data_source.get_candles(
            self.strategy.symbol, timeframe=self.strategy.default_timeframe, limit=1
        )
        current_price = reference_candles[-1]["close"]

        if not self.in_position:
            return self._check_entry(current_price)
        else:
            return self._check_exit(current_price)

    def _check_entry(self, current_price: float) -> dict:
        condition_met = evaluate_condition_group(
            self.strategy.entry_conditions,
            self.price_data_source,
            self.strategy.symbol,
            default_timeframe=self.strategy.default_timeframe,
        )
        if not condition_met:
            return {"action": "waiting", "strategy_id": self.strategy_id}

        order = OrderRequest(
            symbol=self.strategy.symbol,
            side=self.strategy.side,
            quantity=self.strategy.position_size,
            price=current_price,
        )
        execution = self.submit_order(order)
        self.in_position = True
        self.entry_price = current_price
        return {
            "action": "entered",
            "strategy_id": self.strategy_id,
            "price": current_price,
            "execution": execution,
        }

    def _check_exit(self, current_price: float) -> dict:
        pct_change = ((current_price - self.entry_price) / self.entry_price) * 100

        # For a BUY (long) position: a price DROP hurts you, a price RISE helps.
        # For a SELL (short) position: it's the exact opposite - a price RISE
        # hurts you, a price DROP is the profit direction. Getting this
        # inverted would silently turn stop-losses into stop-gains on shorts,
        # so it's worth being explicit and tested for both directions.
        if self.strategy.side == "BUY":
            stop_hit = pct_change <= -self.strategy.stop_loss_pct
            profit_hit = self.strategy.take_profit_pct is not None and pct_change >= self.strategy.take_profit_pct
        else:  # SELL / short
            stop_hit = pct_change >= self.strategy.stop_loss_pct
            profit_hit = self.strategy.take_profit_pct is not None and pct_change <= -self.strategy.take_profit_pct

        if not (stop_hit or profit_hit):
            return {"action": "holding", "strategy_id": self.strategy_id, "unrealized_pct": pct_change}

        exit_side = "SELL" if self.strategy.side == "BUY" else "BUY"
        order = OrderRequest(
            symbol=self.strategy.symbol,
            side=exit_side,
            quantity=self.strategy.position_size,
            price=current_price,
        )
        execution = self.submit_order(order)
        reason = "stop_loss" if stop_hit else "take_profit"
        self.in_position = False
        self.entry_price = None
        return {
            "action": "exited",
            "strategy_id": self.strategy_id,
            "reason": reason,
            "price": current_price,
            "realized_pct": pct_change,
            "execution": execution,
        }
