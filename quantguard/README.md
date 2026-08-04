# QuantGuard - Working Prototype

Three pieces, wired together:

```
quantguard/
├── risk_engine/     -> checks orders against rules (fat-finger guard so far)
├── broker/           -> sends approved orders to an exchange
│   connector.py       - MockConnector (simulated, no internet needed)
│                        BinanceConnector (real, via ccxt - needs your API keys)
├── backend/          -> the ONE API a trader's strategy calls
│   main.py            - POST /orders, GET /orders
├── frontend/
│   index.html         -> dashboard: submit test orders, watch them get
│                          checked and filled in real time
├── demo.py            -> original standalone demo (still works, no server needed)
└── tests/
```

## Authentication (API keys)

Every trader gets their own API key, and every order request must
include it. This is what keeps traders' accounts separate - a key
determines whose account an order belongs to, so nobody can submit
orders as someone else, and nobody can see another account's history.

**Getting a key** (do this once per trader):
```bash
curl -X POST http://localhost:8000/accounts \
  -H "Content-Type: application/json" \
  -d '{"account_id": "trader_alice"}'
```
This returns an API key. **It's shown only once** - save it.

**Using it** - every `/orders` and `/positions` request needs the
`X-API-Key` header:
```bash
curl -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -H "X-API-Key: qg_yourkeyhere" \
  -d '{"symbol": "BTCUSDT", "side": "BUY", "quantity": 0.5, "price": 65000}'
```

The dashboard (`frontend/index.html`) has a bar at the top to create
an account and key, or paste in an existing one - it's saved in your
browser so you don't need to re-enter it every time.

## Database

Orders and positions are stored in `backend/quantguard.db`, a SQLite
file created automatically the first time you start the server -
nothing to install or configure. It survives restarts, so your order
history and positions stick around.

Two tables:
- **orders** - every order submitted, approved or rejected, with the
  full rule results and execution outcome. This is your audit trail.
- **positions** - running position per account+symbol, updated
  whenever an order actually fills. This is what future rules (max
  position size, drawdown kill-switch) will check against.

Want to peek inside it directly? Any SQLite browser works (e.g. "DB
Browser for SQLite", free), or from Python:
```python
from backend import db
print(db.get_orders())
```

## How the pieces connect

```
trader's strategy
      │  POST /orders  {symbol, side, quantity, price}
      ▼
  backend (FastAPI)
      │
      ▼
  risk_engine  ──reject──▶  order stops here, reason logged
      │ approve
      ▼
  broker connector  ──▶  MockConnector (simulated fill)  [default, works offline]
                     ──▶  BinanceConnector (real order)   [needs ccxt + API keys]
```

The trader never writes broker API code — they call `/orders` the same
way regardless of which broker is behind it.

## Run it

**1. Install dependencies** (needs internet access, which this sandbox doesn't have —
run this on your own machine):

```bash
pip install -r requirements.txt
```

**2. Start the backend:**

```bash
cd quantguard
uvicorn backend.main:app --reload
```

This starts the API at `http://localhost:8000`. Visit
`http://localhost:8000/docs` for an interactive API tester.

**3. Open the dashboard:**

Just open `frontend/index.html` in your browser (double-click it, or
`open frontend/index.html`). It talks to the backend automatically.

Submit an order from the dashboard — a normal-sized one gets approved
and "filled" by the mock exchange; a huge one gets rejected by the
risk engine, and you'll see why.

## Going from mock to real Binance testnet

The server automatically uses **Binance's testnet** (a real exchange,
fake money) instead of the mock exchange the moment you set two
environment variables - no code changes needed.

Get free testnet API keys at https://testnet.binance.vision (sign in
with GitHub, no real account or funds needed).

In PowerShell, before starting the server:
```powershell
$env:BINANCE_API_KEY = "your testnet key"
$env:BINANCE_API_SECRET = "your testnet secret"
py -m uvicorn backend.main:app --reload
```

Watch the terminal on startup - it will print which broker it picked:
```
[QuantGuard] Using BinanceConnector (testnet) - real exchange, fake funds.
```
or, if the keys aren't set:
```
[QuantGuard] No Binance API keys set - using MockConnector (simulated fills).
```

**Note on order type:** the Binance connector places MARKET orders, not
limit orders - meaning it fills immediately at whatever the current
price is, rather than sitting open on the order book waiting for the
price to reach an exact number. This is intentional for testing (you
get to see a real, complete fill right away) - a production version
would likely default to limit orders instead, since market orders can
fill at a worse price than expected during volatile moments.

The execution result now reports Binance's ACTUAL order status
(`FILLED`, `OPEN`, or `UNKNOWN`) instead of assuming every accepted
order filled - so the dashboard tells you the truth about what really
happened on the exchange.

If you close that terminal and open a new one, the environment
variables are gone and it silently falls back to the mock exchange -
that's intentional (nothing breaks), just something to know.

## How a quant system actually connects

This is the core of the product: a trader's strategy - Python, Pine
Script, C++, whatever - never touches Binance/Alpaca/IBKR's APIs
directly. It calls QuantGuard's one endpoint instead.

**Step 1 - get an API key** (once per trader):
```bash
curl -X POST http://localhost:8000/accounts \
  -H "Content-Type: application/json" \
  -d '{"account_id": "trader_alice"}'
```

**Step 2 - the strategy calls one function, forever:**
```python
import requests

API_KEY = "qg_yourkeyhere"
QUANTGUARD_URL = "http://localhost:8000"

def send_order(symbol, side, quantity, price):
    response = requests.post(
        f"{QUANTGUARD_URL}/orders",
        headers={"X-API-Key": API_KEY},
        json={"symbol": symbol, "side": side, "quantity": quantity, "price": price},
    )
    return response.json()

# Anywhere in the strategy's logic:
if my_signal_says_buy:
    result = send_order("BTCUSDT", "BUY", 0.5, 65000)
```

Behind that one call: risk checks run (fat-finger, rate-limit,
position-limit), and if approved, the order goes to whichever broker
is configured (mock or real Binance testnet) - the trader's code
never changes either way.

**Current limitation:** this only works from the same machine running
the server (`localhost`). A trader on a different computer can't
reach it yet - that's the deployment step, still to come.

## Strategy assistant (describe your strategy in plain English)

A trader describes their strategy in plain English, and QuantGuard
turns it into a structured, reviewable rule set - asking specific
follow-up questions if anything required is missing (symbol, side,
entry condition, position size, stop-loss). Nothing trades until the
trader explicitly approves the final structured version.

**Two levels of "understood the strategy":**
- `entry_description` - a plain-English summary (always captured,
  required for every strategy)
- `entry_conditions` - a STRUCTURED, mechanically-executable version
  built from real indicators (RSI, EMA, SMA, price, volume, etc.),
  combinable with AND/OR, even nested (see `strategy/conditions.py`).
  This is what a live execution engine could actually check against
  real price data - not just read as a sentence.

A strategy can be **complete** (all required fields filled, approvable)
without being **executable** (having structured logic) - this is
intentional and shown honestly in the dashboard's review box. The
built-in rule-based parser can structure a few simple, common patterns
on its own (a single RSI threshold, or a percent-drop-from-a-recent-high)
without needing any LLM key. Anything more complex - multiple
indicators, AND/OR logic, crossovers - needs a real LLM parser
(Claude/Gemini/Grok) to structure reliably; the rule-based parser will
honestly leave `entry_conditions` empty rather than guess at a
structure it can't confidently build, and the strategy is still saved
and approvable as a description-only strategy either way.

**Important design choice:** the LLM (or the built-in rule-based
parser, if you haven't added an API key yet) is used ONLY to translate
a description into structured rules, ONE TIME per strategy. It is
never called again to make live trading decisions. A separate,
deterministic strategy engine (not built yet - this is the next step)
is what would actually watch prices and fire orders, mechanically
following the approved rules - no AI judgment calls happen at
execution time.

**Try it (works right now, no API key needed - uses a rule-based
parser):**
```bash
curl -X POST http://localhost:8000/strategies \
  -H "Content-Type: application/json" -H "X-API-Key: qg_yourkey" \
  -d '{"description": "Buy BTCUSDT when it dips 3 percent"}'
```
This will likely come back `NEEDS_CLARIFICATION` with specific
questions (e.g. "what stop-loss?"). Answer with:
```bash
curl -X POST http://localhost:8000/strategies/1/clarify \
  -H "Content-Type: application/json" -H "X-API-Key: qg_yourkey" \
  -d '{"answer": "position size 0.1 BTC, stop loss 2 percent"}'
```
Once it comes back `READY_FOR_REVIEW`, approve it:
```bash
curl -X POST http://localhost:8000/strategies/1/approve \
  -H "X-API-Key: qg_yourkey"
```

**Using a real LLM instead of the rule-based parser:**

Three providers are supported - set ONE of these environment
variables before starting the server (checked in this priority order:
Claude, then Gemini, then Grok):

| Provider | Env var | Install | Notes |
|---|---|---|---|
| Claude | `ANTHROPIC_API_KEY` | `pip install anthropic` | New accounts get ~$5 free trial credit at console.anthropic.com |
| Gemini | `GEMINI_API_KEY` | `pip install google-generativeai` | Google offers a genuinely free ongoing tier - no credit card needed, best for open-ended testing |
| Grok | `XAI_API_KEY` | `pip install openai` (Grok's API is OpenAI-compatible) | xAI gives new accounts some free trial credit too |

Example (PowerShell, using Gemini):
```powershell
py -m pip install google-generativeai
$env:GEMINI_API_KEY = "your key"
py -m uvicorn backend.main:app --reload
```
Startup log will confirm which one it picked, same pattern as the
Binance broker selection - e.g. `Using GeminiStrategyParser for
strategy descriptions.`

**What this does NOT do yet:** actually trade. Approving a strategy
only marks it as reviewed and correct - there's no live market-watching
engine wired up yet to mechanically execute it. That's the natural
next step once this piece is solid.

## Live strategy execution engine

Once a strategy is APPROVED and has structured, executable entry
conditions (see above), it can be activated - meaning QuantGuard
actually watches real prices and mechanically fires orders when the
approved conditions are met.

```bash
curl -X POST http://localhost:8000/strategies/1/activate -H "X-API-Key: qg_yourkey"
```

This starts a background check every 60 seconds. If the entry
condition fires, an order is submitted through the EXACT SAME
risk-checked pipeline as any manual order - fat-finger, rate-limit,
and position-limit rules all still apply. Once in a position, the
same check monitors for the stop-loss or take-profit threshold and
exits automatically.

To stop it:
```bash
curl -X POST http://localhost:8000/strategies/1/pause -H "X-API-Key: qg_yourkey"
```
(Pausing stops new decisions - it does NOT automatically close an
open position.)

**⚠️ Important, honest limitations before trusting this with anything
real:**
- **This can only be genuinely tested with real network access on
  your machine** - the indicator math, condition evaluation, and
  entry/exit decision logic have all been unit-tested with synthetic
  price data and are verified correct (including the tricky BUY vs
  SELL stop-loss/take-profit direction logic). What has NOT been
  tested end-to-end yet is the actual live loop pulling real Binance
  prices continuously and reacting to them in real time - that
  requires running it live and watching it work.
- Start with the mock broker/mock price data, or Binance TESTNET with
  small amounts, before ever pointing this at anything real.
- Even with the kill-switch and restart-persistence now in place, the
  live monitoring loop pulling REAL continuous Binance prices has only
  been tested with synthetic data in this environment (no internet
  access here) - it needs to actually run, live, on your machine
  before being trusted unattended.

## Kill-switch (drawdown protection)

The last of the original spec's four risk pillars. Every account has
a running daily realized P&L, computed from actual closed trades
(real average-cost-basis accounting - not just a rough estimate).
Currently set to a $500/day max loss (`backend/main.py`) - once an
account crosses that, **every** new order for that account is blocked,
regardless of size or symbol, until the next day.

This means the risk engine now has 4 active rules: fat-finger,
rate-limit, position-limit, and kill-switch - all four from the
original spec, all tested.

## Multi-timeframe strategies

A strategy's entry conditions can now reference different chart
timeframes per indicator (e.g. "RSI(14) on the 1-minute chart AND
price above the 200-day EMA") - each condition fetches its own
timeframe's data, with a strategy-level default for anything that
doesn't specify one. The rule-based parser also now picks up an
overall timeframe mention (like "on a 1-minute timeframe") even for
simple strategies.

## Restart-persistence for active strategies

Previously, an ACTIVE strategy would silently stop being monitored
if the server restarted - it would still SHOW as active in the
dashboard, but nothing was actually watching it. Fixed: on startup,
every strategy the database says is ACTIVE gets its monitor rebuilt
automatically - and critically, if it was already holding a position
when the server stopped, the rehydrated monitor resumes KNOWING that
(using the position's real average cost as its entry price), instead
of assuming it's flat and potentially firing a duplicate entry order
on top of an existing position.

## TradingView webhook support

A trader can wire a TradingView alert straight into QuantGuard - no
code, just an alert configuration.

**Important quirk this accounts for:** TradingView's alert webhooks
can only send a URL and a JSON message body - they can't send custom
HTTP headers, so the `X-API-Key` header used everywhere else in this
API won't work for TradingView specifically. Instead, there's a
dedicated endpoint with the key built into the URL itself:

```
POST http://your-server/webhooks/tradingview/qg_yourapikeyhere
```

**In TradingView**, when creating an alert:
1. Set the **Webhook URL** to the address above (with your real key)
2. Set the **Message** to:
```json
{"symbol": "{{ticker}}", "side": "buy", "quantity": "0.1", "price": "{{close}}"}
```
   TradingView automatically fills in `{{ticker}}` and `{{close}}`
   from the chart the alert fired on. Change `"side"` to `"sell"` for
   a separate exit/short alert, and `"quantity"` to whatever position
   size you want that alert to trade.

Behind this URL, it's the **exact same risk-checked pipeline** as
every other order source - fat-finger, rate-limit, position-limit,
and kill-switch all still apply. Nothing about coming from
TradingView skips any check.

## Open positions view

The dashboard now shows a real **Open Positions** panel - not just
order history. Each position shows your entry (average cost basis),
the current market price, and unrealized P&L (correctly signed for
both long and short positions - tested explicitly, since this is easy
to get backwards for shorts).

## Advisory pushback on risky strategies

The Strategy Assistant no longer just fills in blanks - once a
strategy is complete, it flags specific concerns before you approve
it (still not blocking; you decide). Examples: a stop-loss so tight
normal price noise could trigger it, a take-profit smaller than the
stop-loss (meaning even a 50% win rate loses money overall), or no
take-profit at all. These checks run for every parser - the
rule-based one uses fixed thresholds, and the LLM-backed ones
(Claude/Gemini/Grok) add their own contextual judgment on top of the
same baseline.

## Document upload for strategies

Instead of typing a description, upload a file (.txt, .md, .pdf, or
.docx) from the Strategy Assistant panel - the extracted text goes
through the exact same parser as typed text. Requires `pypdf` for PDF
files and `python-docx` for Word files (both optional - .txt/.md
always work with zero extra dependencies), plus `python-multipart`
for FastAPI to handle the upload itself (all three are now in
requirements.txt).

## MT5/Exness support

MT5/Exness (like most forex/CFD brokers) has no simple REST API for
placing trades from outside the terminal - unlike Binance, there's
no direct "send an HTTP request, get a fill" path. The real bridge
is an **Expert Advisor** (a script that runs INSIDE MetaTrader 5)
that polls QuantGuard for pending orders and executes them using
MT5's own native trading functions.

**How it works:**
```
QuantGuard order/strategy → risk checks (same 4 rules as everything else)
   → queued as a "pending signal" in the database
        ↓
MT5's Expert Advisor polls every few seconds: "anything for me?"
   → executes the trade using MT5's own CTrade functions
        ↓
Reports the result back to QuantGuard (fill price, ticket number, or an error)
   → order log and position tracking update only once MT5 confirms
```

**Setup:**
1. Get the EA file: `mt5/QuantGuardBridge.mq5`
2. Open it in MetaEditor (comes with MT5), compile it, then attach it to any chart in MT5 (the chart's own symbol doesn't matter - the EA trades whatever symbol each signal specifies)
3. **Critical step people miss:** in MT5, go to Tools → Options → Expert Advisors → check "Allow WebRequest for listed URL" and add your QuantGuard server's address to the list. MT5 blocks all outbound web requests by default for security - the EA will silently fail to connect until this is done.
4. Set the EA's inputs: your server URL, your QuantGuard API key, and (if your broker appends a suffix to symbol names, like `EURUSD.a`) the `SymbolSuffix` input.
5. Enable **AutoTrading** in the MT5 toolbar - without this, MT5 blocks all EA trading regardless of anything else.

**⚠️ Honest limitation - this could not be tested end-to-end:** the
database queue, risk-check integration, and API endpoints (poll/report)
were all tested directly and work correctly. The `.mq5` Expert Advisor
itself was written correctly to the best available knowledge of MQL5,
but could not be compiled or run against a real MT5 terminal while
building it - no MetaTrader is available in that environment. **Test
carefully on a demo account first**, the same way Binance was proven
out on testnet before ever touching real funds - do not point this at
a live funded MT5 account until you've watched it work correctly on a
demo account first.

## What's still missing (from the full spec)

- Real broker connectors beyond Binance (Alpaca, IBKR, Bybit, etc.)
- TradingView webhook support, MT5/Exness (needs a separate MQL5 Expert Advisor bridge - no simple REST API for these)
- Deployment to a real server (still runs on localhost only)
- Billing (Stripe)
- The Rust fast-path (this is plain Python — fine for now, revisit once
  the rules and flows are proven out)

## Suggested next step

Pick ONE: either add the rate-limit rule (protects against runaway
loops) or wire up a second broker (proves the "unified router" idea
actually works across more than one exchange). Which matters more to
you right now?
