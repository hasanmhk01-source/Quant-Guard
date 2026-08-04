"""
Structured trading conditions.

This is what separates "the LLM understood the strategy" from "the
system can actually execute it." A sentence like "buy when RSI drops
below 30 and price is above the 200-day EMA" is easy for an LLM to
read - but a live execution engine can't act on a sentence, it needs
something like:

    ConditionGroup(logic="AND", items=[
        Condition(left=IndicatorRef(RSI, period=14), operator=LT, right=30),
        Condition(left=IndicatorRef(PRICE), operator=GT, right=IndicatorRef(EMA, period=200)),
    ])

That's what this file defines: a small, composable indicator/condition
schema that CAN be checked mechanically against real price data, and
that supports nesting (AND/OR groups of conditions, including groups
of groups) so multi-part strategies are representable, not just
single "price drops X%" cases.

Deliberately kept to the handful of indicators most strategies
actually use. Adding a new indicator later is one line in the enum;
the Condition/ConditionGroup machinery around it doesn't change.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Union


class Indicator(str, Enum):
    PRICE = "PRICE"
    RSI = "RSI"
    EMA = "EMA"
    SMA = "SMA"
    VOLUME = "VOLUME"
    HIGH = "HIGH"                              # highest price over a lookback period
    LOW = "LOW"                                 # lowest price over a lookback period
    PERCENT_CHANGE_FROM_HIGH = "PERCENT_CHANGE_FROM_HIGH"  # e.g. "3% drop from the 20-day high"


class Operator(str, Enum):
    LT = "<"
    GT = ">"
    LTE = "<="
    GTE = ">="
    EQ = "=="


@dataclass
class IndicatorRef:
    """
    A reference to a computable value, e.g. RSI(14) on the 1-hour
    chart, EMA(200) on the daily chart, or just PRICE (no period
    needed). `period` is the lookback window where the indicator
    needs one (RSI, EMA, SMA, HIGH, LOW, PERCENT_CHANGE_FROM_HIGH) -
    left as None for indicators that don't need one (PRICE, VOLUME).

    `timeframe` is the candle size to compute it from (e.g. "1m",
    "5m", "1h", "1d") - left as None to use the strategy's default
    timeframe rather than repeating it on every single condition.
    """
    indicator: Indicator
    period: int | None = None
    timeframe: str | None = None

    def __str__(self) -> str:
        base = f"{self.indicator.value}({self.period})" if self.period else self.indicator.value
        return f"{base}[{self.timeframe}]" if self.timeframe else base

    def to_dict(self) -> dict:
        return {"indicator": self.indicator.value, "period": self.period, "timeframe": self.timeframe}

    @classmethod
    def from_dict(cls, data: dict) -> "IndicatorRef":
        return cls(
            indicator=Indicator(data["indicator"]),
            period=data.get("period"),
            timeframe=data.get("timeframe"),
        )


@dataclass
class Condition:
    """
    A single comparison: left OPERATOR right, e.g. RSI(14) < 30, or
    PRICE > EMA(200). `right` can be a plain number (a threshold) or
    another IndicatorRef (comparing two computed values against each
    other), covering both "RSI below 30" and "price above its 200-EMA"
    style conditions with the same structure.
    """
    left: IndicatorRef
    operator: Operator
    right: Union[float, IndicatorRef]

    def __str__(self) -> str:
        right_str = str(self.right) if isinstance(self.right, IndicatorRef) else f"{self.right:g}"
        return f"{self.left} {self.operator.value} {right_str}"

    def to_dict(self) -> dict:
        right_dict = (
            {"type": "indicator", "value": self.right.to_dict()}
            if isinstance(self.right, IndicatorRef)
            else {"type": "number", "value": self.right}
        )
        return {"left": self.left.to_dict(), "operator": self.operator.value, "right": right_dict}

    @classmethod
    def from_dict(cls, data: dict) -> "Condition":
        right_data = data["right"]
        right = (
            IndicatorRef.from_dict(right_data["value"])
            if right_data["type"] == "indicator"
            else float(right_data["value"])
        )
        return cls(left=IndicatorRef.from_dict(data["left"]), operator=Operator(data["operator"]), right=right)


@dataclass
class ConditionGroup:
    """
    A group of conditions (or nested groups) combined with AND/OR.
    Nesting groups inside groups lets you represent things like
    "(RSI < 30 OR price < 20-day low) AND volume > 1.5x average".
    """
    logic: str  # "AND" or "OR"
    items: list[Union[Condition, "ConditionGroup"]] = field(default_factory=list)

    def __str__(self) -> str:
        parts = []
        for item in self.items:
            text = str(item)
            # Parenthesize nested groups so precedence is unambiguous when printed.
            parts.append(f"({text})" if isinstance(item, ConditionGroup) else text)
        return f" {self.logic} ".join(parts)

    def to_dict(self) -> dict:
        return {
            "logic": self.logic,
            "items": [
                {"kind": "group", **item.to_dict()} if isinstance(item, ConditionGroup)
                else {"kind": "condition", **item.to_dict()}
                for item in self.items
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConditionGroup":
        items = []
        for item in data["items"]:
            if item["kind"] == "group":
                items.append(cls.from_dict(item))
            else:
                items.append(Condition.from_dict(item))
        return cls(logic=data["logic"], items=items)
