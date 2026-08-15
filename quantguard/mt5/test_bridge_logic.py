"""
Tests for the MT5 bridge logic.

Everything that can be tested without a real MT5 terminal is tested
here - the polling loop, signal execution flow, and result reporting,
using a fake stand-in for the MetaTrader5 package.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mt5_python_bridge.quantguard_mt5_bridge import execute_signal, build_order_request

passed, total = 0, 0
def check(name, cond):
    global passed, total
    total += 1
    print(("PASS" if cond else "FAIL") + ":", name)
    if cond: passed += 1


class FakeMT5:
    """Stand-in for the MetaTrader5 package - returns realistic but
    controlled data so the bridge logic can be tested without a real terminal."""
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    TRADE_RETCODE_DONE = 10009

    def symbol_select(self, symbol, enable): return True
    def symbol_info_tick(self, symbol):
        class Tick:
            ask = 1.08500
            bid = 1.08490
        return Tick()

    def order_send(self, request):
        class Result:
            retcode = FakeMT5.TRADE_RETCODE_DONE
            order = 123456789
            price = 1.08500
            comment = "Request executed"
        return Result()

    def last_error(self): return (0, "no error")


mt5 = FakeMT5()

# --- BUY signal ---
buy_signal = {"id": 1, "symbol": "EURUSD", "side": "BUY", "quantity": 0.1, "price": 1.085}
result = execute_signal(mt5, buy_signal)
check("BUY signal executes and returns FILLED", result["status"] == "FILLED")
check("BUY result contains a ticket number", result.get("mt5_ticket") is not None)
check("BUY result contains fill price", result.get("fill_price") == 1.085)

# --- SELL signal ---
sell_signal = {"id": 2, "symbol": "XAUUSD", "side": "SELL", "quantity": 0.01, "price": 1950.0}
result_sell = execute_signal(mt5, sell_signal)
check("SELL signal executes and returns FILLED", result_sell["status"] == "FILLED")

# --- Order request uses LIVE price from MT5, not stale QuantGuard price ---
request = build_order_request(mt5, buy_signal)
check("BUY request uses ask price from live MT5 tick (not stale QuantGuard price)", request["price"] == 1.08500)
request_sell = build_order_request(mt5, {"id": 3, "symbol": "EURUSD", "side": "SELL", "quantity": 0.1, "price": 999})
check("SELL request uses bid price from live MT5 tick", request_sell["price"] == 1.08490)
check("ORDER type is BUY for BUY signal", request["type"] == FakeMT5.ORDER_TYPE_BUY)
check("ORDER type is SELL for SELL signal", request_sell["type"] == FakeMT5.ORDER_TYPE_SELL)

# --- Bad symbol returns ERROR without crashing ---
class BadSymbolMT5(FakeMT5):
    def symbol_select(self, symbol, enable): return False

bad_result = execute_signal(BadSymbolMT5(), {"id": 4, "symbol": "FAKEPAIR", "side": "BUY", "quantity": 0.1, "price": 1.0})
check("unknown symbol returns ERROR without crashing the loop", bad_result["status"] == "ERROR")
check("ERROR result contains a readable message", "not found" in bad_result.get("error_message", "").lower())

# --- MT5 rejection (bad retcode) returns ERROR without crashing ---
class RejectMT5(FakeMT5):
    def order_send(self, request):
        class Result:
            retcode = 10006  # rejected
            order = 0
            price = 0
            comment = "No money"
        return Result()

reject_result = execute_signal(RejectMT5(), buy_signal)
check("rejected order returns ERROR (not FILLED)", reject_result["status"] == "ERROR")
check("rejection message includes retcode info", "retcode" in reject_result.get("error_message", ""))

print()
print(f"{passed}/{total} passed")
