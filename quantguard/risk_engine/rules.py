"""
Risk rules. Each rule takes an Order and returns a RuleResult.

Every rule follows the same shape on purpose: it makes rules easy to
test in isolation, easy to add to, and easy to reorder or disable later.
"""

import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import Callable
from .models import Order, RuleResult


class Rule(ABC):
    """Base class every risk rule implements."""

    name: str = "UnnamedRule"

    @abstractmethod
    def check(self, order: Order) -> RuleResult:
        ...


class MaxOrderSizeRule(Rule):
    """
    Rejects an order if its notional value (quantity * price) is
    larger than a configured maximum.

    This is the classic "fat-finger" guard: a strategy bug or typo
    that tries to send a 100x-too-large order gets stopped here
    before it ever reaches an exchange.
    """

    name = "MaxOrderSizeRule"

    def __init__(self, max_notional: float):
        self.max_notional = max_notional

    def check(self, order: Order) -> RuleResult:
        notional = order.quantity * order.price
        if notional > self.max_notional:
            return RuleResult(
                passed=False,
                rule_name=self.name,
                reason=(
                    f"order notional ${notional:,.2f} exceeds max "
                    f"allowed ${self.max_notional:,.2f}"
                ),
            )
        return RuleResult(passed=True, rule_name=self.name)


class RateLimitRule(Rule):
    """
    Rejects an order if the account has already sent too many orders
    within a sliding time window.

    This is the runaway-loop guard: a strategy bug that gets stuck
    firing orders in a tight loop (or a bad "while True: buy()") gets
    stopped here, instead of hammering an exchange until it triggers
    a rate-limit ban or worse.

    Tracks order timestamps per account_id in memory, using a sliding
    window (not a fixed per-second bucket) - so "10 orders in the last
    1 second" is checked continuously, not reset at second boundaries.
    """

    name = "RateLimitRule"

    def __init__(self, max_orders: int, per_seconds: float = 1.0):
        self.max_orders = max_orders
        self.per_seconds = per_seconds
        # account_id -> deque of timestamps of recent orders (any result,
        # not just approved ones - a loop spamming rejected orders is
        # still a loop and still worth catching).
        self._history: dict[str, deque] = defaultdict(deque)

    def check(self, order: Order) -> RuleResult:
        now = time.monotonic()
        history = self._history[order.account_id]

        # Drop timestamps that have aged out of the window.
        while history and now - history[0] > self.per_seconds:
            history.popleft()

        if len(history) >= self.max_orders:
            return RuleResult(
                passed=False,
                rule_name=self.name,
                reason=(
                    f"{len(history)} orders already sent in the last "
                    f"{self.per_seconds}s (limit: {self.max_orders}) - "
                    f"possible runaway loop"
                ),
            )

        history.append(now)
        return RuleResult(passed=True, rule_name=self.name)


class PositionLimitRule(Rule):
    """
    Rejects an order if it would push the account's TOTAL holding in
    that symbol beyond a configured cap - not just the size of this
    one order.

    This catches something MaxOrderSizeRule can't: a strategy that
    stays under the per-order limit but builds a dangerously large
    position over many small orders (10 separate 0.3 BTC buys add up
    to 3 BTC, even if each one looks harmless alone).

    Deliberately decoupled from any specific database: you give it a
    `position_lookup` function (account_id, symbol) -> current position,
    and it doesn't care where that number comes from. In this project
    that function is backed by the positions table in backend/database/db.py.
    """

    name = "PositionLimitRule"

    def __init__(self, max_position: float, position_lookup: Callable[[str, str], float]):
        self.max_position = max_position
        self.position_lookup = position_lookup

    def check(self, order: Order) -> RuleResult:
        current = self.position_lookup(order.account_id, order.symbol)
        delta = order.quantity if order.side.value == "BUY" else -order.quantity
        projected = current + delta

        if abs(projected) > self.max_position:
            return RuleResult(
                passed=False,
                rule_name=self.name,
                reason=(
                    f"this order would bring {order.symbol} position to "
                    f"{projected:g} (current: {current:g}), exceeding the "
                    f"max allowed of {self.max_position:g}"
                ),
            )
        return RuleResult(passed=True, rule_name=self.name)


class KillSwitchRule(Rule):
    """
    Rejects ALL orders for an account once its realized loss for the
    day crosses a configured maximum - the automated drawdown circuit
    breaker from the original spec's four pillars.

    Unlike the other rules, this doesn't evaluate anything about the
    specific order being checked (its size, symbol, etc.) - once an
    account has crossed its daily loss limit, EVERY order for that
    account is blocked until the limit resets (a new day, or the
    account is manually cleared) - the entire point is to stop a
    losing account from digging deeper, not to selectively allow
    "smaller" losing trades through.

    Deliberately decoupled from any specific database, same pattern as
    PositionLimitRule: you give it a `daily_pnl_lookup` function
    (account_id) -> today's realized P&L, and it doesn't care where
    that number comes from. In this project that's backed by
    backend/database/db.py's daily_pnl tracking.
    """

    name = "KillSwitchRule"

    def __init__(self, max_daily_loss: float, daily_pnl_lookup: Callable[[str], float]):
        self.max_daily_loss = max_daily_loss
        self.daily_pnl_lookup = daily_pnl_lookup

    def check(self, order: Order) -> RuleResult:
        daily_pnl = self.daily_pnl_lookup(order.account_id)
        if daily_pnl <= -self.max_daily_loss:
            return RuleResult(
                passed=False,
                rule_name=self.name,
                reason=(
                    f"account has realized ${daily_pnl:,.2f} in losses today, "
                    f"exceeding the max daily loss of ${self.max_daily_loss:,.2f} - "
                    f"all trading is halted for this account until tomorrow"
                ),
            )
        return RuleResult(passed=True, rule_name=self.name)
