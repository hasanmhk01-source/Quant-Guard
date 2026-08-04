"""
QuantGuard - Step 1 Demo

Run this with:  python demo.py

This proves the core idea works end to end, with fake orders:
an order comes in -> the risk engine checks it -> it's approved
or rejected, with a reason.

No brokers, no APIs, no dashboard yet - just the core logic.
"""

from risk_engine import Order, Side, RiskEngine, MaxOrderSizeRule


def main():
    # Configure the engine: for now, one rule.
    # Max order size = $50,000 notional.
    engine = RiskEngine(rules=[
        MaxOrderSizeRule(max_notional=50_000),
    ])

    # A normal, sensible order.
    good_order = Order(
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=0.5,
        price=65_000,       # notional = $32,500 -> under the limit
        account_id="acct_001",
    )

    # A fat-finger order: someone meant to buy 0.5 BTC but typed 15.
    bad_order = Order(
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=15,
        price=65_000,        # notional = $975,000 -> way over the limit
        account_id="acct_001",
    )

    for label, order in [("GOOD ORDER", good_order), ("FAT-FINGER ORDER", bad_order)]:
        print(f"\n--- {label} ---")
        print(f"Order: {order.side.value} {order.quantity} {order.symbol} @ ${order.price:,.2f}")

        results = engine.evaluate(order)
        for r in results:
            print(r)

        approved = engine.is_approved(order)
        print(f"=> ORDER {'APPROVED, would be sent to exchange' if approved else 'REJECTED, never leaves QuantGuard'}")


if __name__ == "__main__":
    main()
