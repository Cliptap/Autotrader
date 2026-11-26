---
name: ccxt-mcp
description: Crypto exchange execution via CCXT unified API. Place real orders, check balances, monitor positions. Safety-first: sandbox by default, live must be armed. 8 pre-trade gates + idempotency + rate limiting. Use ONLY after paper-trade validation in AI-Trader.
---

# CCXT MCP — Crypto Exchange Execution

Executes spot trades on crypto exchanges via the CCXT unified API (100+ exchanges supported). This is the **execution layer** — AI-Trader remains the paper-trading and social layer.

## Safety Posture

### Environment Gates (before any order)

| Gate | Default | What it does |
|------|---------|--------------|
| `DRY_RUN` | `true` | Logs intent without sending orders |
| `ALLOW_LIVE` | `false` | Blocks all real orders unless armed |
| `LIVE_MODE_REQUIRES_ARMING` | `true` | Even with ALLOW_LIVE, requires `arm_live_session()` call |
| `MAX_ORDER_VALUE_USD` | `100` | Per-order cap in USD |
| `MAX_SESSION_VALUE_USD` | `0` | Session cumulative cap (0=unlimited) |
| `RATE_LIMIT_ORDER_SEC` | `2.0` | Minimum seconds between orders |
| `RATE_LIMIT_DATA_PER_SEC` | `10` | Max data requests per second |

### Pre-Trade Validation Gates (on every `create_order` call)

All 8 gates run before any order reaches the exchange. If any fails, the order is blocked with a specific error code.

| Gate | Error Code | What it checks |
|------|-----------|----------------|
| **Live Armed** | `LIVE_SESSION_NOT_ARMED` | Real-money orders require `arm_live_session()` first (sandbox bypasses) |
| **Clock Sync** | `CLOCK_OUT_OF_SYNC` | System clock must be within `CLOCK_MAX_DRIFT_MS` (5s) of exchange time |
| **Balance Check** | `INSUFFICIENT_FUNDS` | Free balance must cover order cost + 0.5% fee buffer |
| **Lot Size** | `LOT_SIZE_VIOLATION` | Order notional >= `MIN_NOTIONAL_USD` ($10) and meets exchange minimums |
| **Order Value** | `ORDER_VALUE_EXCEEDED` | Order must be under `MAX_ORDER_VALUE_USD` |
| **Slippage Guard** | `SLIPPAGE_EXCEEDED` | Market orders: price must not have moved > `MAX_SLIPPAGE_PCT` (1%) since last ticker |
| **Dedup** | `DUPLICATE_ORDER` | Identical order (symbol+side+qty+price) rejected within `DEDUP_WINDOW_SECONDS` (30s) |
| **Rate Limit** | `RATE_LIMITED` | Orders spaced >= `RATE_LIMIT_ORDER_SEC` (2s); data capped at `RATE_LIMIT_DATA_PER_SEC`/s |

### Idempotency & Reconciliation

Every order carries a unique `client_order_id` (format: `cm-{timestamp}-{random}`). If a network timeout occurs:
1. The error response includes `client_order_id` and a `retry_hint`
2. Call `reconcile_order(client_order_id, symbol)` to check if the order landed
3. If not found, it's safe to retry. If found, the existing order is returned.
Binance's `newClientOrderId` provides idempotency for limit orders (prevents duplicates on retry).

### Full Safety Knobs

| Env Var | Default | Purpose |
|---------|---------|---------|
| `MIN_NOTIONAL_USD` | `10` | Reject orders below this USD value (dust protection) |
| `MAX_SLIPPAGE_PCT` | `1.0` | Max % price movement tolerated for market orders |
| `CLOCK_MAX_DRIFT_MS` | `5000` | Max ms difference between local and exchange clock |
| `DEDUP_WINDOW_SECONDS` | `30` | Time window for duplicate order detection |
| `RATE_LIMIT_ORDER_SEC` | `2.0` | Minimum interval between orders |
| `RATE_LIMIT_DATA_PER_SEC` | `10` | Max data requests per second (sliding window) |
| `LIVE_MODE_REQUIRES_ARMING` | `true` | Require explicit `arm_live_session()` before real-money orders |

**Real money only flows when ALL conditions are met:**
1. `ALLOW_LIVE=true` in `ccxt-mcp/.env`
2. `DRY_RUN=false` in `ccxt-mcp/.env`
3. Exchange API keys configured with trade permissions
4. `arm_live_session()` called (sandbox bypasses this)
5. Windows NTP clock sync active (`w32tm /resync` — clock drift < 5s)
6. All 8 pre-trade gates pass

---

## Tools

### `exchange_info()`
Get current exchange status, mode, limits, and safety config. Use this FIRST in any session.

Returns: `exchange`, `mode` (DRY-RUN/SANDBOX/LIVE), `dry_run`, `sandbox`, `authenticated`, `clock_ok`, `clock_offset_ms`, `live_armed`, `live_arming_required`, `max_order_usd`, `max_session_usd`, `session_spent_usd`, `max_slippage_pct`, `min_notional_usd`, `clock_max_drift_ms`, `dedup_window_sec`, `rate_limit_order_sec`, `rate_limit_data_per_sec`, `timeout_seconds`.

---

### `arm_live_session()`
Arm the session for real-money execution. Only required when NOT in sandbox mode. This is a deliberate safety gate — you must explicitly call this before any real-money order goes through.

Returns: confirmation message with warning. If already armed or in sandbox, returns status.
Error: `LIVE_NOT_CONFIGURED` if `ALLOW_LIVE` is not `true`.

---

### `disarm_live_session()`
Disarm the live session. After this, real-money orders are blocked until `arm_live_session()` is called again.

---

### `get_ticker(symbol: str)`
Current price and 24h statistics. Fresh fetch every call — no stale data. Subject to rate limiting (max 10/s).

| Param | Type | Example |
|-------|------|---------|
| `symbol` | `str` | `"BTC/USDT"`, `"ETH/USDT"`, `"SOL/USDT"` |

Returns: `last`, `bid`, `ask`, `high_24h`, `low_24h`, `volume_24h`, `change_pct_24h`.

---

### `get_orderbook(symbol: str, limit: int = 10)`
Current order book snapshot (bids and asks). Subject to rate limiting.

| Param | Type | Default | Example |
|-------|------|---------|---------|
| `symbol` | `str` | — | `"BTC/USDT"` |
| `limit` | `int` | `10` | `5` (max 25) |

Returns: `bids` [[price, qty], ...], `asks` [[price, qty], ...].

---

### `get_balance()`
Account balances for non-zero assets. Only works when API keys are configured.

Returns: `balances` as `{asset: {free, used}, ...}`.

---

### `create_order(symbol: str, side: str, quantity: float, limit_price: float | None = None)`
Place a market or limit spot order. **8 safety gates run before execution.** Every order carries a `client_order_id` for idempotency.

| Param | Type | Required | Example |
|-------|------|----------|---------|
| `symbol` | `str` | Yes | `"ETH/USDT"` |
| `side` | `str` | Yes | `"buy"` or `"sell"` |
| `quantity` | `float` | Yes | `0.01` (0.01 ETH) |
| `limit_price` | `float` | No | `3100.00` (limit order; omit for market) |

**Success response:** `order_id`, `client_order_id`, `symbol`, `side`, `quantity`, `price`, `estimated_price`, `slippage_pct`, `status`, `filled`, `remaining`, `timestamp`, `session_spent_usd`.

**On network error:** includes `client_order_id` + `retry_hint`. Call `reconcile_order()` before retrying.

**Examples:**
```
mcp__ccxt__create_order("ETH/USDT", "buy", 0.01)           # market buy
mcp__ccxt__create_order("ETH/USDT", "buy", 0.01, 3100.00)  # limit buy
mcp__ccxt__create_order("BTC/USDT", "sell", 0.001)          # market sell
```

**IMPORTANT:** Never call this tool before the paper-trade equivalent is published in AI-Trader.

---

### `reconcile_order(client_order_id: str, symbol: str)`
Check if an order with a given `client_order_id` was placed. Use after a network timeout to determine if the order landed before retrying.

| Param | Type | Example |
|-------|------|---------|
| `client_order_id` | `str` | `"cm-1783018547-a7c450"` (from create_order response) |
| `symbol` | `str` | `"ETH/USDT"` |

Returns: `reconciled: true/false`, order details if found, or safe-to-retry message if not.

---

### `cancel_order(order_id: str, symbol: str)`
Cancel an open order by exchange ID.

| Param | Type | Example |
|-------|------|---------|
| `order_id` | `str` | `"1234567890"` (from `create_order` response) |
| `symbol` | `str` | `"ETH/USDT"` |

---

### `get_open_orders(symbol: str | None = None)`
List all open (unfilled) orders. Optional symbol filter.

---

### `get_closed_orders(symbol: str | None = None, limit: int = 20)`
Recent order history. Returns `id`, `symbol`, `side`, `quantity`, `price`, `cost`, `fee`, `status`, `timestamp`.

---

## Execution Protocol (MUST FOLLOW)

```
1. Paper trade in AI-Trader (POST /api/signals/realtime)
2. Wait for paper PnL validation (3+ profitable trades on strategy)
3. Run CCXT MCP in SANDBOX testnet → place test orders, verify gates
4. When ready for LIVE:
   a. Configure Binance live API keys in .env (CCXT_SANDBOX=false)
   b. Verify clock sync: python ccxt-mcp/server.py --healthcheck
   c. Call mcp__ccxt__arm_live_session() before each session
   d. Start with MAX_ORDER_VALUE_USD low ($20-50)
5. After session: call mcp__ccxt__disarm_live_session()
```

**Red lines:**
- CCXT MCP is NEVER for experimentation or "testing ideas"
- CCXT MCP is NEVER called before the paper-trade signal is published
- On network error: call `reconcile_order()` before retrying — NEVER blind retry
- If exchange returns a non-network error, ABORT. Do not retry without human review
- Position sizing from `trading-rules.md` applies: max 2% capital per trade
- `arm_live_session()` must be called before every live session. Disarm after.

## Supported Exchanges

Any CCXT-supported exchange. Most tested: **Binance, Bybit, OKX, Kraken, KuCoin**.

Configure via `CCXT_EXCHANGE` in `ccxt-mcp/.env`.

For **Binance testnet** (free, no deposit):
```
CCXT_EXCHANGE=binance
CCXT_SANDBOX=true
CCXT_API_KEY=<testnet_key>
CCXT_SECRET=<testnet_secret>
```

Get testnet keys: https://testnet.binance.vision

## Symbol Format

Always use CCXT unified format: `BTC/USDT`, `ETH/USDT`, `SOL/USDT`.

Common mistakes: `BTCUSDT` → wrong. `btc/usdt` → wrong. Use exactly `BTC/USDT`.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Clock offset > 5s | `w32tm /resync /force` (requires admin) |
| `LIVE_SESSION_NOT_ARMED` | Call `arm_live_session()` first (sandbox bypasses) |
| `RATE_LIMITED` | Wait the specified seconds before next order |
| `SLIPPAGE_EXCEEDED` | Use a limit order, or increase `MAX_SLIPPAGE_PCT` |
| Network timeout | Call `reconcile_order(client_order_id, symbol)` before retry |
| `CLOCK_OUT_OF_SYNC` | Run Windows Time service: `Start-Service w32time` |
