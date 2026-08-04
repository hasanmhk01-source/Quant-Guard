"""
Core data models for QuantGuard's risk engine.

An Order is the thing a strategy wants to send to an exchange.
A RuleResult is what a risk rule returns after checking that order.
"""

from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Order:
    symbol: str          # e.g. "BTCUSDT"
    side: Side            # BUY or SELL
    quantity: float        # how much
    price: float           # limit price (or reference price for market orders)
    account_id: str        # which trading account this order belongs to


@dataclass
class RuleResult:
    passed: bool
    rule_name: str
    reason: str = ""

    def __repr__(self):
        status = "PASS" if self.passed else "REJECT"
        return f"[{status}] {self.rule_name}: {self.reason or 'ok'}"
