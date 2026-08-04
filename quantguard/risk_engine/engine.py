"""
The RiskEngine ties rules together. This is the piece that will
eventually sit between "strategy generates a signal" and
"order goes to an exchange."

For now it just runs an order through every rule it knows about
and returns whether the order should pass or be rejected.
"""

from typing import List
from .models import Order, RuleResult
from .rules import Rule


class RiskEngine:
    def __init__(self, rules: List[Rule]):
        self.rules = rules

    def evaluate(self, order: Order) -> List[RuleResult]:
        """
        Run the order through every rule. We deliberately run ALL
        rules (not stop-on-first-fail) so you can see every problem
        with an order at once, not just the first one.
        """
        return [rule.check(order) for rule in self.rules]

    def is_approved(self, order: Order) -> bool:
        results = self.evaluate(order)
        return all(r.passed for r in results)
