"""
Tests for risk rules. Run with: python -m pytest tests/
(or just: python tests/test_rules.py)
"""

import sys
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from risk_engine import Order, Side, MaxOrderSizeRule, RateLimitRule, PositionLimitRule


def make_order(quantity, price, account_id="test"):
    return Order(symbol="BTCUSDT", side=Side.BUY, quantity=quantity, price=price, account_id=account_id)


def test_order_under_limit_passes():
    rule = MaxOrderSizeRule(max_notional=50_000)
    result = rule.check(make_order(quantity=0.5, price=65_000))  # $32,500
    assert result.passed


def test_order_over_limit_fails():
    rule = MaxOrderSizeRule(max_notional=50_000)
    result = rule.check(make_order(quantity=15, price=65_000))  # $975,000
    assert not result.passed


def test_order_exactly_at_limit_passes():
    rule = MaxOrderSizeRule(max_notional=10_000)
    result = rule.check(make_order(quantity=1, price=10_000))  # exactly $10,000
    assert result.passed


def test_rate_limit_allows_up_to_max():
    rule = RateLimitRule(max_orders=3, per_seconds=1.0)
    order = make_order(quantity=0.1, price=100)
    for _ in range(3):
        assert rule.check(order).passed


def test_rate_limit_blocks_over_max():
    rule = RateLimitRule(max_orders=3, per_seconds=1.0)
    order = make_order(quantity=0.1, price=100)
    for _ in range(3):
        rule.check(order)
    result = rule.check(order)  # 4th order in the same window
    assert not result.passed


def test_rate_limit_is_per_account():
    rule = RateLimitRule(max_orders=2, per_seconds=1.0)
    order_a = make_order(quantity=0.1, price=100, account_id="acct_a")
    order_b = make_order(quantity=0.1, price=100, account_id="acct_b")
    assert rule.check(order_a).passed
    assert rule.check(order_a).passed
    assert not rule.check(order_a).passed   # acct_a now over its limit
    assert rule.check(order_b).passed        # acct_b has its own separate limit


def test_rate_limit_window_clears_over_time():
    rule = RateLimitRule(max_orders=1, per_seconds=0.2)
    order = make_order(quantity=0.1, price=100)
    assert rule.check(order).passed
    assert not rule.check(order).passed  # immediately over the limit
    time.sleep(0.25)
    assert rule.check(order).passed       # window has cleared


def test_position_limit_allows_under_cap():
    rule = PositionLimitRule(max_position=2.0, position_lookup=lambda a, s: 1.0)
    result = rule.check(make_order(quantity=0.5, price=65_000))  # -> 1.5, under 2.0
    assert result.passed


def test_position_limit_blocks_cumulative_breach():
    # Account already holds 1.6; one more 0.8 order would push it to 2.4, over the 2.0 cap -
    # even though 0.8 alone looks like a small, harmless order.
    rule = PositionLimitRule(max_position=2.0, position_lookup=lambda a, s: 1.6)
    result = rule.check(make_order(quantity=0.8, price=65_000))
    assert not result.passed


def test_position_limit_allows_reducing_sells_near_cap():
    from risk_engine import Side
    rule = PositionLimitRule(max_position=2.0, position_lookup=lambda a, s: 1.9)
    sell_order = Order(symbol="BTCUSDT", side=Side.SELL, quantity=0.5, price=65_000, account_id="test")
    result = rule.check(sell_order)
    assert result.passed  # selling reduces exposure, should never be blocked by this rule


if __name__ == "__main__":
    test_order_under_limit_passes()
    test_order_over_limit_fails()
    test_order_exactly_at_limit_passes()
    test_rate_limit_allows_up_to_max()
    test_rate_limit_blocks_over_max()
    test_rate_limit_is_per_account()
    test_rate_limit_window_clears_over_time()
    test_position_limit_allows_under_cap()
    test_position_limit_blocks_cumulative_breach()
    test_position_limit_allows_reducing_sells_near_cap()
    print("All tests passed.")
