"""
QuantGuard MT5 Bridge (Python version).

An alternative to the MQL5 Expert Advisor (mt5/QuantGuardBridge.mq5) -
this does the same job (poll QuantGuard for pending orders, execute
them on MT5, report results back) but as a plain Python script instead
of code you compile inside MetaTrader's MetaEditor.

WHY THIS EXISTS: it takes exactly the three things a trader actually
has - MT5 account number, password, server name - and logs in
directly, instead of requiring them to manually log into the MT5 GUI
first and then attach/configure an EA by hand.

HOW IT WORKS: the official `MetaTrader5` Python package controls a
LOCALLY INSTALLED MT5 terminal from Python - it doesn't replace MT5,
it drives it. This is why it has the same hard requirement as the EA
approach: it must run on a Windows machine that has MT5 installed
(your own PC, or a Windows VPS for 24/7 uptime) - it CANNOT run on
Railway or any Linux server. This is a real limitation of the
MetaTrader5 package itself, not something we can work around.

SETUP:
    py -m pip install MetaTrader5 requests
    py quantguard_mt5_bridge.py

Then just answer the prompts - account number, password, server name,
your QuantGuard API key, and your QuantGuard server's URL. That's it.

⚠️ HONEST LIMITATION: this could not be tested against a real MT5
terminal or a real account while building it (no Windows/MT5 available
in that environment) - the same caveat as the EA. What WAS tested (see
test_bridge_logic.py) is everything that doesn't need a real MT5
connection: the polling loop, request building, and result-reporting
logic, using a fake stand-in for the MetaTrader5 package. Test on a
DEMO account first before ever pointing this at a funded one.
"""

import sys
import time
import getpass
import requests


def get_setup_info():
    """Prompts for everything needed - the whole point is these five
    inputs are ALL a trader needs to provide, nothing else."""
    print("=== QuantGuard MT5 Bridge Setup ===\n")
    account = input("MT5 Account Number: ").strip()
    password = getpass.getpass("MT5 Password: ")
    server = input("MT5 Server Name (e.g. Exness-MT5Trial9): ").strip()
    api_key = input("Your QuantGuard API Key: ").strip()
    server_url = input("QuantGuard Server URL [http://localhost:8000]: ").strip() or "http://localhost:8000"
    return {
        "account": account, "password": password, "server": server,
        "api_key": api_key, "server_url": server_url.rstrip("/"),
    }


def connect_mt5(mt5, account: str, password: str, server: str) -> bool:
    """Logs into MT5 directly with the three fields a trader actually has -
    no manual GUI login needed first."""
    initialized = mt5.initialize(login=int(account), password=password, server=server)
    if not initialized:
        error = mt5.last_error()
        print(f"[QuantGuard] Failed to connect to MT5: {error}")
        print("[QuantGuard] Common causes: wrong account number/password/server name, "
              "or MT5 isn't installed on this machine.")
        return False
    account_info = mt5.account_info()
    print(f"[QuantGuard] Connected to MT5 - account {account_info.login}, "
          f"balance {account_info.balance} {account_info.currency}")
    return True


def build_order_request(mt5, signal: dict) -> dict:
    """Turns a QuantGuard signal into the request dict MT5's order_send()
    expects - needs a REAL LIVE price from MT5 itself (not the price
    QuantGuard originally suggested), since MT5 will reject stale prices."""
    symbol = signal["symbol"]
    side = signal["side"]

    if not mt5.symbol_select(symbol, True):
        raise ValueError(f"Symbol {symbol} not found or not available on this account")

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise ValueError(f"Couldn't get a live price for {symbol}")

    price = tick.ask if side == "BUY" else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL

    return {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": signal["quantity"],
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": 20260101,
        "comment": "QuantGuard",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }


def execute_signal(mt5, signal: dict) -> dict:
    """Executes one signal on MT5. Returns a result dict ready to report
    back to QuantGuard - never raises, always returns something reportable,
    so one bad signal can't crash the whole polling loop."""
    try:
        request = build_order_request(mt5, signal)
    except ValueError as e:
        return {"status": "ERROR", "error_message": str(e)}

    result = mt5.order_send(request)
    if result is None:
        return {"status": "ERROR", "error_message": f"order_send returned None: {mt5.last_error()}"}

    if result.retcode == mt5.TRADE_RETCODE_DONE:
        return {"status": "FILLED", "mt5_ticket": str(result.order), "fill_price": result.price}
    else:
        return {"status": "ERROR", "error_message": f"retcode {result.retcode}: {result.comment}"}


def report_result(session: requests.Session, server_url: str, api_key: str, signal_id: int, result: dict):
    response = session.post(
        f"{server_url}/mt5/report/{api_key}/{signal_id}",
        json={
            "status": result["status"],
            "mt5_ticket": result.get("mt5_ticket"),
            "fill_price": result.get("fill_price"),
            "error_message": result.get("error_message"),
        },
        timeout=10,
    )
    if not response.ok:
        print(f"[QuantGuard] Warning: failed to report result for signal #{signal_id}: {response.text}")


def poll_loop(mt5, server_url: str, api_key: str, poll_interval_seconds: int = 5):
    session = requests.Session()
    print(f"[QuantGuard] Polling {server_url} every {poll_interval_seconds}s. Press Ctrl+C to stop.\n")
    while True:
        try:
            response = session.get(f"{server_url}/mt5/poll/{api_key}", timeout=10)
            response.raise_for_status()
            signals = response.json().get("signals", [])
            for signal in signals:
                print(f"[QuantGuard] Executing signal #{signal['id']}: {signal['side']} {signal['quantity']} {signal['symbol']}")
                result = execute_signal(mt5, signal)
                print(f"[QuantGuard] Result: {result}")
                report_result(session, server_url, api_key, signal["id"], result)
        except requests.RequestException as e:
            print(f"[QuantGuard] Poll failed (will retry): {e}")
        time.sleep(poll_interval_seconds)


def main():
    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("The MetaTrader5 package isn't installed. Run: pip install MetaTrader5")
        print("Note: this package only works on Windows, with MT5 installed on this machine.")
        sys.exit(1)

    info = get_setup_info()
    if not connect_mt5(mt5, info["account"], info["password"], info["server"]):
        sys.exit(1)

    try:
        poll_loop(mt5, info["server_url"], info["api_key"])
    except KeyboardInterrupt:
        print("\n[QuantGuard] Stopped.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
