"""
Strategy models.

A trader describes their strategy in plain English. This file defines
what that description gets turned INTO: a structured, auditable set
of rules - not a black box. Every field here is something a human can
read and verify before anything trades on it.

Two levels of "understood the entry condition":
- `entry_description`: the plain-English summary (always required -
  every strategy needs SOME record of when it should trigger).
- `entry_conditions`: the STRUCTURED, mechanically-executable version
  (optional) - a ConditionGroup built from real indicators (RSI, EMA,
  etc, see conditions.py). This is what a live execution engine could
  actually check against real price data. A simple strategy ("RSI
  below 30") or even the rule-based mock parser can often build this.
  A complex, ambiguous, or highly idiosyncratic strategy might only
  ever get a description, not a structured version - that's an honest
  signal to the trader that this strategy isn't executable yet, not a
  silent failure.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .conditions import ConditionGroup


class StrategyStatus(str, Enum):
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"  # missing required info, questions pending
    READY_FOR_REVIEW = "READY_FOR_REVIEW"          # fully parsed, waiting on trader approval
    APPROVED = "APPROVED"                            # trader confirmed it's correct
    ACTIVE = "ACTIVE"                                 # live and being executed mechanically
    PAUSED = "PAUSED"


# The fields a strategy MUST have before it can ever go live. This is
# deliberately opinionated: a strategy with no stop-loss or no position
# sizing is exactly the kind of thing that turns into an emotional,
# unmanaged trade - the entire point of this feature is to prevent that.
#
# Note: `entry_conditions` (the STRUCTURED version) is intentionally
# NOT required here - only `entry_description` (the plain-English
# summary) is mandatory. Requiring a fully structured condition tree
# for every strategy would block simple/valid strategies whenever the
# parser can't confidently build one; instead, a strategy can be
# approved with just a description, and its `is_executable` flag
# honestly reports whether it's ready for real automated execution.
REQUIRED_FIELDS = [
    "symbol",
    "side",
    "entry_description",
    "position_size",
    "stop_loss_pct",
]


@dataclass
class StrategyConfig:
    """
    The structured, machine-executable form of a trader's strategy.
    Every field here is something the strategy engine can check
    mechanically against live prices - no LLM judgment calls happen
    at execution time, only at the one-time translation step.
    """
    symbol: Optional[str] = None
    side: Optional[str] = None                          # "BUY" or "SELL"
    entry_description: Optional[str] = None              # human-readable, e.g. "price drops 3% from 20-day high"
    entry_conditions: Optional[ConditionGroup] = None      # structured, executable version (may be None - see above)
    default_timeframe: str = "1h"                            # used by any condition that doesn't specify its own
    position_size: Optional[float] = None                       # quantity to trade when the condition fires
    stop_loss_pct: Optional[float] = None                        # e.g. 2.0 means exit if position drops 2%
    take_profit_pct: Optional[float] = None                       # optional
    max_daily_loss: Optional[float] = None                          # optional account-level guardrail

    def missing_fields(self) -> list[str]:
        return [f for f in REQUIRED_FIELDS if getattr(self, f) in (None, "")]

    def is_complete(self) -> bool:
        return len(self.missing_fields()) == 0

    def is_executable(self) -> bool:
        """
        True only if this strategy has a STRUCTURED entry condition a
        live execution engine could actually check against real data -
        not just a plain-English description. Being complete and being
        executable are different things: a strategy can be complete
        (all required fields filled) but not yet executable, if its
        entry logic was too complex/ambiguous for the parser to
        structure confidently.
        """
        return self.is_complete() and self.entry_conditions is not None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "entry_description": self.entry_description,
            "entry_conditions": self.entry_conditions.to_dict() if self.entry_conditions else None,
            "default_timeframe": self.default_timeframe,
            "position_size": self.position_size,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "max_daily_loss": self.max_daily_loss,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StrategyConfig":
        entry_conditions_data = data.get("entry_conditions")
        entry_conditions = None
        if entry_conditions_data:
            try:
                entry_conditions = ConditionGroup.from_dict(entry_conditions_data)
            except (ValueError, KeyError, TypeError):
                # An LLM-generated entry_conditions can reference an
                # indicator or shape we don't support (e.g. it tried to
                # use "BOLLINGER_UPPER" or "ZSCORE", which aren't in our
                # Indicator enum) - despite being told not to. Rather
                # than crash the whole request over a malformed
                # structured condition, fail safe: keep the plain-English
                # entry_description (still useful, still reviewable) and
                # leave entry_conditions None (correctly marks the
                # strategy as not-yet-executable instead of silently
                # running on logic that doesn't match what was described).
                entry_conditions = None
        return cls(
            symbol=data.get("symbol"),
            side=data.get("side"),
            entry_description=data.get("entry_description"),
            entry_conditions=entry_conditions,
            default_timeframe=data.get("default_timeframe") or "1h",
            position_size=data.get("position_size"),
            stop_loss_pct=data.get("stop_loss_pct"),
            take_profit_pct=data.get("take_profit_pct"),
            max_daily_loss=data.get("max_daily_loss"),
        )


@dataclass
class ParseResult:
    """
    What the parser returns after looking at a trader's description
    (plus any previous answers). Either it has everything it needs
    (READY_FOR_REVIEW) or it doesn't (NEEDS_CLARIFICATION) - in which
    case `questions` is what gets shown back to the trader.

    `warnings` is different from `questions`: it's advisory, not
    blocking. A strategy with warnings is still complete and
    approvable - warnings are the parser pushing back on something
    that LOOKS risky or possibly a mistake (an unusually tight
    stop-loss, no take-profit at all), the way a careful person would
    flag a concern before you commit real money, without refusing to
    let you proceed if you've genuinely decided that's what you want.
    """
    status: StrategyStatus
    strategy: StrategyConfig
    questions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
