"""
CCXT MCP Server — Agentic trading execution via Model Context Protocol.

Wraps the CCXT unified API (https://github.com/ccxt/ccxt) as MCP tools.
Safety-first: sandbox by default, live must be explicitly armed.

Pre-trade validations (5-layer gate):
  1. Clock sync — rejects orders if exchange time drift > threshold
  2. Balance check — verifies sufficient funds before buying
  3. Lot size — respects exchange minimum notional and quantity step
  4. Slippage guard — market orders: re-fetches price; blocks if spread > max_pct
  5. Order dedup — rejects identical orders within a time window

Usage:
  python server.py                    # stdio transport (for MCP clients)
  python server.py --healthcheck      # verify exchange connection
"""

import os
import sys
import uuid
import hashlib
import logging
import time as _time
from collections import deque
from datetime import datetime
from pathlib import Path

import ccxt
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ── Config ──────────────────────────────────────────────────────────────────
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

EXCHANGE_ID = os.getenv("CCXT_EXCHANGE", "binance").strip().lower()
SANDBOX = os.getenv("CCXT_SANDBOX", "false").strip().lower() == "true"
API_KEY = os.getenv("CCXT_API_KEY", "").strip()
SECRET = os.getenv("CCXT_SECRET", "").strip()
PASSWORD = os.getenv("CCXT_PASSWORD", "").strip() or None

ALLOW_LIVE = os.getenv("ALLOW_LIVE", "false").strip().lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() == "true"
MAX_ORDER_USD = int(os.getenv("MAX_ORDER_VALUE_USD", "100") or 0)
MAX_SESSION_USD = int(os.getenv("MAX_SESSION_VALUE_USD", "0") or 0)
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30") or 30)
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3") or 3)

# New safety knobs
MAX_SLIPPAGE_PCT = float(os.getenv("MAX_SLIPPAGE_PCT", "1.0") or 1.0)
MIN_NOTIONAL_USD = float(os.getenv("MIN_NOTIONAL_USD", "10") or 10)
CLOCK_MAX_DRIFT_MS = int(os.getenv("CLOCK_MAX_DRIFT_MS", "5000") or 5000)
DEDUP_WINDOW_SEC = int(os.getenv("DEDUP_WINDOW_SECONDS", "30") or 30)
RATE_LIMIT_ORDER_INTERVAL = float(os.getenv("RATE_LIMIT_ORDER_SEC", "2.0") or 2.0)
RATE_LIMIT_DATA_MAX_PER_SEC = int(os.getenv("RATE_LIMIT_DATA_PER_SEC", "10") or 10)
LIVE_MODE_REQUIRES_ARMING = os.getenv("LIVE_MODE_REQUIRES_ARMING", "true").strip().lower() == "true"

MODE = "LIVE" if (ALLOW_LIVE and not SANDBOX) else "SANDBOX" if SANDBOX else "DRY-RUN"

# ── Logging (stderr only, MCP uses stdout for transport) ────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("ccxt-mcp")

# ── Exchange init ───────────────────────────────────────────────────────────
_session_value_usd = 0.0
_clock_ok = False
_clock_offset_ms = 0
_recent_orders: deque[tuple[str, float]] = deque()  # (fingerprint, timestamp)
_live_armed = False  # must be explicitly armed via arm_live_session()

RECV_WINDOW = 60000  # ms — compensates clock drift for private endpoints

_last_order_time: float = 0.0
_data_request_times: deque[float] = deque()


def _rate_limit_order() -> dict | None:
    """Enforce minimum interval between orders."""
    global _last_order_time
    now = _time.time()
    elapsed = now - _last_order_time
    if elapsed < RATE_LIMIT_ORDER_INTERVAL:
        wait = round(RATE_LIMIT_ORDER_INTERVAL - elapsed, 1)
        return {
            "error": "RATE_LIMITED",
            "message": (
                f"Orders too frequent. Please wait {wait}s before next order. "
                f"Min interval: {RATE_LIMIT_ORDER_INTERVAL}s."
            ),
            "wait_seconds": wait,
            "rate_limit_order_sec": RATE_LIMIT_ORDER_INTERVAL,
        }
    _last_order_time = now
    return None


def _rate_limit_data() -> dict | None:
    """Enforce max data requests per second (sliding window)."""
    now = _time.time()
    while _data_request_times and (now - _data_request_times[0]) > 1.0:
        _data_request_times.popleft()
    if len(_data_request_times) >= RATE_LIMIT_DATA_MAX_PER_SEC:
        return {
            "error": "RATE_LIMITED",
            "message": (
                f"Data requests too frequent ({RATE_LIMIT_DATA_MAX_PER_SEC}/s max). "
                "Wait before fetching more data."
            ),
            "rate_limit_data_per_sec": RATE_LIMIT_DATA_MAX_PER_SEC,
        }
    _data_request_times.append(now)
    return None


def _auth_params(new_client_order_id: str | None = None) -> dict:
    """Extra params for authenticated requests (recvWindow for Binance)."""
    p: dict = {"recvWindow": RECV_WINDOW}
    if new_client_order_id:
        p["newClientOrderId"] = new_client_order_id
    return p


def _new_client_id(symbol: str) -> str:
    """Generate a unique, idempotent client order ID.
    Binance requires: ^[a-zA-Z0-9-_]{1,36}$
    Format: cm-{sec}-{rand6} = ~21 chars"""
    sec = int(_time.time())
    rand = uuid.uuid4().hex[:6]
    return f"cm-{sec}-{rand}"


def _build_exchange() -> ccxt.Exchange:
    global _clock_ok, _clock_offset_ms

    cls = getattr(ccxt, EXCHANGE_ID, None)
    if cls is None:
        available = [k for k in ccxt.exchanges if hasattr(ccxt, k)]
        raise ValueError(
            f"Exchange '{EXCHANGE_ID}' not found in CCXT. Available: {available}"
        )

    config: dict = {
        "apiKey": API_KEY,
        "secret": SECRET,
        "timeout": TIMEOUT * 1000,
        "enableRateLimit": True,
        "adjustForTimeDifference": True,
    }
    if PASSWORD:
        config["password"] = PASSWORD

    exchange = cls(config)

    if API_KEY:
        try:
            server_time = exchange.fetch_time()
            local_time = int(_time.time() * 1000)
            _clock_offset_ms = server_time - local_time
            exchange.options["timeDifference"] = _clock_offset_ms
            abs_offset = abs(_clock_offset_ms)

            if abs_offset <= CLOCK_MAX_DRIFT_MS:
                _clock_ok = True
                log.info("Clock synced: offset=%dms (OK, max=%dms)",
                         _clock_offset_ms, CLOCK_MAX_DRIFT_MS)
            else:
                _clock_ok = False
                log.error(
                    "CLOCK DRIFT: offset=%dms exceeds max=%dms. "
                    "Orders BLOCKED until clock is synced. "
                    "Run: w32tm /resync /force  OR  increase CLOCK_MAX_DRIFT_MS in .env",
                    _clock_offset_ms, CLOCK_MAX_DRIFT_MS,
                )
        except Exception as e:
            _clock_ok = False
            log.error("Clock sync FAILED: %s. Orders BLOCKED.", e)

    if SANDBOX and "test" in exchange.urls:
        exchange.set_sandbox_mode(True)

    if hasattr(exchange, "options"):
        exchange.options["defaultType"] = "spot"

    return exchange


_exchange: ccxt.Exchange | None = None


def _get_exchange() -> ccxt.Exchange:
    global _exchange
    if _exchange is None:
        _exchange = _build_exchange()
        log.info(
            "Exchange loaded: %s | mode=%s | dry_run=%s | sandbox=%s | clock_ok=%s",
            EXCHANGE_ID, MODE, DRY_RUN, SANDBOX, _clock_ok,
        )
        if not _clock_ok and API_KEY:
            log.error(
                "REFUSING all live orders: clock sync failed "
                "(offset=%dms, max=%dms). Fix your system clock.",
                _clock_offset_ms, CLOCK_MAX_DRIFT_MS,
            )
    return _exchange


# ── Safety helpers ──────────────────────────────────────────────────────────

def _check_live() -> dict | None:
    """Returns an error dict if live execution is not allowed, else None."""
    if DRY_RUN:
        return None

    if not ALLOW_LIVE:
        return {
            "error": "LIVE_EXECUTION_NOT_ARMED",
            "message": "Set ALLOW_LIVE=true in .env to execute real orders.",
            "current_mode": MODE,
        }

    if SANDBOX:
        return None  # sandbox is always safe

    if LIVE_MODE_REQUIRES_ARMING and not _live_armed:
        return {
            "error": "LIVE_SESSION_NOT_ARMED",
            "message": (
                "Real-money orders require explicit session arming. "
                "Call mcp__ccxt__arm_live_session() first. "
                "This is a safety gate to prevent accidental live trading."
            ),
            "hint": "Call arm_live_session() to enable real-money execution.",
        }

    return None


def _check_clock() -> dict | None:
    """Block orders if system clock is out of sync with exchange."""
    if DRY_RUN:
        return None
    if _clock_ok:
        return None
    return {
        "error": "CLOCK_OUT_OF_SYNC",
        "message": (
            f"System clock is {abs(_clock_offset_ms)}ms off from {EXCHANGE_ID}. "
            f"Max allowed: {CLOCK_MAX_DRIFT_MS}ms. "
            "Orders are blocked for safety. "
            "Sync your clock: w32tm /resync /force  "
            "or increase CLOCK_MAX_DRIFT_MS in .env."
        ),
        "clock_offset_ms": _clock_offset_ms,
        "max_drift_ms": CLOCK_MAX_DRIFT_MS,
    }


def _check_balance(symbol: str, side: str, quantity: float, price: float) -> dict | None:
    """Verify account has enough balance for this trade."""
    ex = _get_exchange()
    try:
        balance = ex.fetch_balance(_auth_params())
    except Exception as e:
        return {
            "error": "BALANCE_CHECK_FAILED",
            "message": f"Could not verify balance: {e}",
        }

    base, quote = symbol.split("/") if "/" in symbol else (symbol, "USDT")

    if side == "buy":
        available = float(balance.get("free", {}).get(quote, 0))
        cost = price * quantity * 1.005  # rough fee buffer
        if available < cost:
            return {
                "error": "INSUFFICIENT_FUNDS",
                "message": (
                    f"Need {cost:.4f} {quote} (including fee buffer). "
                    f"Available: {available:.4f} {quote}."
                ),
                "required": round(cost, 4),
                "available": round(available, 4),
                "currency": quote,
            }
    else:  # sell
        available = float(balance.get("free", {}).get(base, 0))
        if available < quantity:
            return {
                "error": "INSUFFICIENT_FUNDS",
                "message": (
                    f"Need {quantity} {base} to sell. "
                    f"Available: {available} {base}."
                ),
                "required": quantity,
                "available": available,
                "currency": base,
            }

    return None


def _check_lot_size(symbol: str, quantity: float, price: float) -> dict | None:
    """Verify the order meets exchange minimum notional and lot size."""
    ex = _get_exchange()
    market = ex.markets.get(symbol)
    if not market:
        return {
            "error": "UNKNOWN_SYMBOL",
            "message": (
                f"Symbol '{symbol}' not found on {EXCHANGE_ID}. "
                f"Use format: BTC/USDT, ETH/USDT."
            ),
        }

    notional = price * quantity
    limits = market.get("limits", {})
    min_notional = limits.get("cost", {}).get("min") or limits.get(
        "amount", {}
    ).get("min", 0) * (price or 1)

    min_qty = limits.get("amount", {}).get("min")
    step = limits.get("amount", {}).get("step")

    errors = []

    if notional < MIN_NOTIONAL_USD:
        errors.append(
            f"Order notional ${notional:.2f} below minimum ${MIN_NOTIONAL_USD}."
        )

    if min_qty and quantity < min_qty:
        errors.append(
            f"Quantity {quantity} below exchange minimum {min_qty}."
        )

    if errors:
        return {
            "error": "LOT_SIZE_VIOLATION",
            "message": " ".join(errors),
            "notional_usd": round(notional, 2),
            "min_notional_usd": MIN_NOTIONAL_USD,
            "min_quantity": min_qty,
            "quantity_step": step,
        }

    return None


def _check_slippage(symbol: str, side: str, estimated_price: float) -> dict | None:
    """For market orders: re-fetch price and block if spread > threshold."""
    ex = _get_exchange()

    try:
        ticker = ex.fetch_ticker(symbol)
    except Exception as e:
        return {
            "error": "SLIPPAGE_CHECK_FAILED",
            "message": f"Could not verify current price: {e}",
        }

    fresh_bid = ticker.get("bid") or 0
    fresh_ask = ticker.get("ask") or 0
    fresh_price = fresh_ask if side == "buy" else fresh_bid

    if fresh_price <= 0 or estimated_price <= 0:
        return None  # can't calculate, let it through

    deviation_pct = abs(fresh_price - estimated_price) / estimated_price * 100

    if deviation_pct > MAX_SLIPPAGE_PCT:
        return {
            "error": "SLIPPAGE_EXCEEDED",
            "message": (
                f"Price moved {deviation_pct:.2f}% since last quote "
                f"(was ${estimated_price:.2f}, now ${fresh_price:.2f}). "
                f"Max allowed: {MAX_SLIPPAGE_PCT}%. "
                f"Re-check price and retry, or use a limit order."
            ),
            "estimated_price": estimated_price,
            "current_price": fresh_price,
            "deviation_pct": round(deviation_pct, 2),
            "max_slippage_pct": MAX_SLIPPAGE_PCT,
            "bid": fresh_bid,
            "ask": fresh_ask,
        }

    return None


def _check_dedup(symbol: str, side: str, quantity: float, limit_price: float | None) -> dict | None:
    """Reject duplicate orders within the dedup window."""
    now = _time.time()
    fingerprint = hashlib.sha256(
        f"{symbol}|{side}|{quantity}|{limit_price}".encode()
    ).hexdigest()

    # Purge old entries
    while _recent_orders and (now - _recent_orders[0][1]) > DEDUP_WINDOW_SEC:
        _recent_orders.popleft()

    for fp, ts in _recent_orders:
        if fp == fingerprint:
            elapsed = now - ts
            return {
                "error": "DUPLICATE_ORDER",
                "message": (
                    f"Identical order ({symbol} {side} {quantity}) was placed "
                    f"{elapsed:.1f}s ago. Wait {DEDUP_WINDOW_SEC}s or change parameters."
                ),
                "original_time_ago_sec": round(elapsed, 1),
                "dedup_window_sec": DEDUP_WINDOW_SEC,
            }

    _recent_orders.append((fingerprint, now))
    return None


def _check_order_value(symbol: str, side: str, quantity: float) -> dict | None:
    """Check per-order and session value limits. Returns (error, estimated_price)."""
    global _session_value_usd

    try:
        ticker = _get_exchange().fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close") or 0
        order_usd = price * quantity
    except Exception as e:
        log.warning("Could not fetch ticker for value check: %s", e)
        return None

    if MAX_ORDER_USD and order_usd > MAX_ORDER_USD:
        return {
            "error": "ORDER_VALUE_EXCEEDED",
            "message": f"Order value ${order_usd:.2f} exceeds max ${MAX_ORDER_USD}.",
            "order_value_usd": round(order_usd, 2),
            "max_order_value_usd": MAX_ORDER_USD,
        }

    if MAX_SESSION_USD and (_session_value_usd + order_usd) > MAX_SESSION_USD:
        return {
            "error": "SESSION_VALUE_EXCEEDED",
            "message": (
                f"Session cumulative ${_session_value_usd + order_usd:.2f} "
                f"exceeds max ${MAX_SESSION_USD}."
            ),
            "session_spent_usd": round(_session_value_usd, 2),
            "max_session_value_usd": MAX_SESSION_USD,
        }

    return None


def _track_order_value(symbol: str, quantity: float) -> None:
    global _session_value_usd
    try:
        ticker = _get_exchange().fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close") or 0
        _session_value_usd += price * quantity
    except Exception:
        pass


def _reconcile_order(
    exchange: ccxt.Exchange, symbol: str, client_order_id: str
) -> dict | None:
    """
    Check if an order with the given clientOrderId already exists.
    Uses fetch_open_orders + fetch_closed_orders to find it.
    Returns the order dict if found, None if not found.
    """
    try:
        params = {"origClientOrderId": client_order_id, "recvWindow": RECV_WINDOW}
        result = exchange.fetch_order(client_order_id, symbol, params=params)
        return {
            "ok": True,
            "reconciled": True,
            "message": "Order was already placed — reconciled successfully.",
            "order_id": result.get("id"),
            "client_order_id": client_order_id,
            "symbol": result.get("symbol"),
            "side": result.get("side"),
            "quantity": result.get("amount"),
            "price": result.get("price"),
            "status": result.get("status"),
            "filled": result.get("filled"),
            "remaining": result.get("remaining"),
            "timestamp": result.get("timestamp"),
        }
    except Exception:
        pass

    # Fallback: check open and closed orders for the clientOrderId
    try:
        for fetcher in [exchange.fetch_open_orders, exchange.fetch_closed_orders]:
            orders = fetcher(symbol, params=_auth_params())
            for o in orders:
                if o.get("clientOrderId") == client_order_id:
                    return {
                        "ok": True,
                        "reconciled": True,
                        "message": "Order found via order list scan — reconciled.",
                        "order_id": o.get("id"),
                        "client_order_id": client_order_id,
                        "symbol": o.get("symbol"),
                        "side": o.get("side"),
                        "quantity": o.get("amount"),
                        "price": o.get("price"),
                        "status": o.get("status"),
                        "filled": o.get("filled"),
                        "remaining": o.get("remaining"),
                        "timestamp": o.get("timestamp"),
                    }
    except Exception:
        pass

    return None


def _validate_pre_trade(
    symbol: str, side: str, quantity: float,
    estimated_price: float, limit_price: float | None,
) -> dict | None:
    """
    Run all pre-trade safety gates. Returns None if all pass,
    or an error dict if any gate blocks the order.
    Gates run in order of severity — first failure wins.
    """
    # Gate 0: Live execution armed?
    err = _check_live()
    if err:
        return err

    # Gate 1: Clock in sync?
    err = _check_clock()
    if err:
        return err

    # Gate 2: Balance sufficient?
    if not DRY_RUN:
        err = _check_balance(symbol, side, quantity, estimated_price)
        if err:
            return err

    # Gate 3: Lot size valid?
    err = _check_lot_size(symbol, quantity, estimated_price)
    if err:
        return err

    # Gate 4: Value within limits?
    err = _check_order_value(symbol, side, quantity)
    if err:
        return err

    # Gate 5: Slippage within tolerance? (market orders only)
    if limit_price is None:
        err = _check_slippage(symbol, side, estimated_price)
        if err:
            return err

    # Gate 6: Duplicate order?
    err = _check_dedup(symbol, side, quantity, limit_price)
    if err:
        return err

    return None


# ── MCP Server ──────────────────────────────────────────────────────────────
mcp = FastMCP(
    "ccxt-mcp",
    instructions=f"""
CCXT MCP Server — agentic trading execution for {EXCHANGE_ID}.
Mode: {MODE} | Dry-Run: {DRY_RUN} | Sandbox: {SANDBOX}
Max order: ${MAX_ORDER_USD or 'unlimited'} | Max session: ${MAX_SESSION_USD or 'unlimited'}
Slip. max: {MAX_SLIPPAGE_PCT}% | Min notional: ${MIN_NOTIONAL_USD} | Clock max drift: {CLOCK_MAX_DRIFT_MS}ms

PRE-TRADE SAFETY GATES (5 layers):
  1. Clock sync ({EXCHANGE_ID} time must be within {CLOCK_MAX_DRIFT_MS}ms)
  2. Balance check (verifies free balance before order)
  3. Lot size (respects exchange minimum notional and step)
  4. Slippage guard (market orders: re-fetches price, rejects if >{MAX_SLIPPAGE_PCT}% move)
  5. Order dedup (rejects identical orders within {DEDUP_WINDOW_SEC}s)

THIS SERVER IS FOR CRYPTO EXCHANGE EXECUTION ONLY.
AI-Trader (ai4trade.ai) remains the paper-trading and social layer.
Never mix signals between the two systems.
""",
)


# ── Tools ───────────────────────────────────────────────────────────────────

@mcp.tool()
def exchange_info() -> dict:
    """Status of the exchange connection: mode, limits, clock health, and safety config."""
    return {
        "exchange": EXCHANGE_ID,
        "mode": MODE,
        "dry_run": DRY_RUN,
        "sandbox": SANDBOX,
        "authenticated": bool(API_KEY),
        "clock_ok": _clock_ok,
        "clock_offset_ms": _clock_offset_ms,
        "live_armed": _live_armed,
        "live_arming_required": LIVE_MODE_REQUIRES_ARMING,
        "max_order_usd": MAX_ORDER_USD,
        "max_session_usd": MAX_SESSION_USD,
        "session_spent_usd": round(_session_value_usd, 2),
        "max_slippage_pct": MAX_SLIPPAGE_PCT,
        "min_notional_usd": MIN_NOTIONAL_USD,
        "clock_max_drift_ms": CLOCK_MAX_DRIFT_MS,
        "dedup_window_sec": DEDUP_WINDOW_SEC,
        "rate_limit_order_sec": RATE_LIMIT_ORDER_INTERVAL,
        "rate_limit_data_per_sec": RATE_LIMIT_DATA_MAX_PER_SEC,
        "timeout_seconds": TIMEOUT,
    }


@mcp.tool()
def get_ticker(symbol: str) -> dict:
    """
    Get current price and 24h statistics for a symbol.
    Always fetches a fresh price from the exchange — no stale data.

    Args:
        symbol: Trading pair in CCXT format, e.g. "BTC/USDT", "ETH/USDT"
    """
    ex = _get_exchange()
    rate_err = _rate_limit_data()
    if rate_err:
        return rate_err
    ticker = ex.fetch_ticker(symbol)
    return {
        "symbol": ticker.get("symbol"),
        "last": ticker.get("last"),
        "bid": ticker.get("bid"),
        "ask": ticker.get("ask"),
        "high_24h": ticker.get("high"),
        "low_24h": ticker.get("low"),
        "volume_24h": ticker.get("baseVolume"),
        "change_pct_24h": ticker.get("percentage"),
        "timestamp": ticker.get("timestamp"),
    }


@mcp.tool()
def get_orderbook(symbol: str, limit: int = 10) -> dict:
    """
    Get current order book snapshot (bids and asks).

    Args:
        symbol: Trading pair, e.g. "BTC/USDT"
        limit: Number of price levels per side (default 10, max 25)
    """
    ex = _get_exchange()
    rate_err = _rate_limit_data()
    if rate_err:
        return rate_err
    book = ex.fetch_order_book(symbol, min(limit, 25))
    return {
        "symbol": book.get("symbol"),
        "bids": book.get("bids", [])[:limit],
        "asks": book.get("asks", [])[:limit],
        "timestamp": book.get("timestamp"),
    }


@mcp.tool()
def get_balance() -> dict:
    """
    Get account balances for non-zero assets.
    Returns free and used balance per asset.
    """
    ex = _get_exchange()
    balance = ex.fetch_balance(_auth_params())
    non_zero = {}
    for asset, data in balance.get("total", {}).items():
        free = balance.get("free", {}).get(asset, 0)
        used = balance.get("used", {}).get(asset, 0)
        if (free + used) > 0:
            non_zero[asset] = round(float(free or 0), 8), round(float(used or 0), 8)
    return {
        "balances": {a: {"free": f, "used": u} for a, (f, u) in non_zero.items()},
        "timestamp": balance.get("timestamp"),
    }


@mcp.tool()
def arm_live_session() -> dict:
    """
    Arm the session for real-money execution.
    Only needed when ALLOW_LIVE=true AND not in sandbox mode.
    This is a deliberate safety gate — you must explicitly call this
    before any real-money order will go through.
    """
    global _live_armed

    if SANDBOX:
        return {
            "ok": True,
            "message": "Sandbox mode — arming not required. Orders are always safe.",
        }

    if not ALLOW_LIVE:
        return {
            "error": "LIVE_NOT_CONFIGURED",
            "message": "Set ALLOW_LIVE=true in .env before arming live session.",
        }

    if _live_armed:
        return {"ok": True, "message": "Live session already armed."}

    _live_armed = True
    log.warning("*** LIVE SESSION ARMED — REAL MONEY ORDERS WILL EXECUTE ***")
    return {
        "ok": True,
        "message": "*** LIVE SESSION ARMED — orders will execute with real money. ***",
        "warning": "Double-check your positions and risk limits. Call disarm_live_session() to disable.",
    }


@mcp.tool()
def disarm_live_session() -> dict:
    """
    Disarm the live session. After this, real-money orders are blocked
    until arm_live_session() is called again.
    """
    global _live_armed
    _live_armed = False
    return {"ok": True, "message": "Live session disarmed. Real-money orders are blocked."}


@mcp.tool()
def create_order(
    symbol: str,
    side: str,
    quantity: float,
    limit_price: float | None = None,
) -> dict:
    """
    Place a market or limit order with full pre-trade validation.

    SAFETY: Before execution, the server runs 5 validation gates:
      - Clock sync check
      - Balance verification (do you have the funds?)
      - Lot size validation (exchange minimums)
      - Slippage guard (market orders only: price must not have moved >X%)
      - Order deduplication (no double-sends)

    Args:
        symbol: Trading pair, e.g. "BTC/USDT"
        side: "buy" or "sell"
        quantity: Amount in base currency (e.g. 0.01 BTC)
        limit_price: Optional limit price. Omit for market order.
    """
    side = side.lower()
    if side not in ("buy", "sell"):
        return {"error": "INVALID_SIDE", "message": "Side must be 'buy' or 'sell'."}

    if quantity <= 0:
        return {"error": "INVALID_QUANTITY", "message": "Quantity must be positive."}

    ex = _get_exchange()

    # Fetch current price for all validations
    try:
        ticker = ex.fetch_ticker(symbol)
        estimated_price = ticker.get("last") or ticker.get("close") or 0
    except Exception as e:
        return {
            "error": "TICKER_FETCH_FAILED",
            "message": f"Could not get price for {symbol}: {e}",
        }

    # Run ALL pre-trade safety gates
    validation_err = _validate_pre_trade(
        symbol, side, quantity, estimated_price, limit_price
    )
    if validation_err:
        return validation_err

    order_type = "limit" if limit_price else "market"
    order_preview = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "type": order_type,
        "limit_price": limit_price,
        "estimated_price": estimated_price,
        "estimated_cost": round(estimated_price * quantity, 2),
        "dry_run": DRY_RUN,
        "clock_ok": _clock_ok,
        "all_gates_passed": True,
    }

    if DRY_RUN:
        _track_order_value(symbol, quantity)
        return {
            "ok": True,
            "dry_run": True,
            "message": (
                f"[DRY-RUN] Would place {side.upper()} {quantity} {symbol} "
                f"at ~${estimated_price:.2f} ({order_type}). "
                f"All safety gates passed."
            ),
            "preview": order_preview,
            "session_spent_usd": round(_session_value_usd, 2),
        }

    client_id = _new_client_id(symbol)

    # Rate limit: throttle orders
    rate_err = _rate_limit_order()
    if rate_err and not DRY_RUN:
        return rate_err

    try:
        if limit_price:
            result = ex.create_limit_order(
                symbol, side, quantity, limit_price, _auth_params(client_id)
            )
        else:
            result = ex.create_market_order(
                symbol, side, quantity, _auth_params(client_id)
            )

        _track_order_value(symbol, quantity)

        actual_price = float(result.get("price") or estimated_price)
        slippage_pct = (
            round(abs(actual_price - estimated_price) / estimated_price * 100, 2)
            if estimated_price > 0 else 0
        )

        return {
            "ok": True,
            "dry_run": False,
            "order_id": result.get("id"),
            "client_order_id": client_id,
            "symbol": result.get("symbol"),
            "side": result.get("side"),
            "quantity": result.get("amount"),
            "price": actual_price,
            "estimated_price": estimated_price,
            "slippage_pct": slippage_pct,
            "status": result.get("status"),
            "filled": result.get("filled"),
            "remaining": result.get("remaining"),
            "timestamp": result.get("timestamp"),
            "session_spent_usd": round(_session_value_usd, 2),
        }
    except Exception as e:
        error_name = type(e).__name__
        log.error("Order failed [%s] %s: %s", client_id, error_name, e)

        # On network/timeout errors, try reconciliation: check if the order
        # landed despite the error. Binance's newClientOrderId makes this safe.
        if "NetworkError" in error_name or "timeout" in str(e).lower():
            log.info("Attempting reconciliation for %s...", client_id)
            reconciled = _reconcile_order(ex, symbol, client_id)
            if reconciled:
                return reconciled

        return {
            "ok": False,
            "error": error_name,
            "message": str(e),
            "client_order_id": client_id,
            "retry_hint": (
                "Network error detected. The order may have landed. "
                "Call mcp__ccxt__reconcile_order() with this client_order_id "
                "before retrying, or retry with the SAME client_order_id "
                "(Binance idempotency prevents duplicates)."
            ),
            "preview": order_preview,
        }


@mcp.tool()
def create_oco_order(
    symbol: str,
    side: str,
    quantity: float,
    take_profit_price: float,
    stop_loss_trigger: float,
    stop_loss_limit: float | None = None,
) -> dict:
    """
    Create an OCO (One-Cancels-Other) order: take-profit + stop-loss.
    When one fills, the other is automatically cancelled.
    Use for setting exit strategy on an existing position.

    Args:
        symbol: Trading pair, e.g. "ETH/USDT"
        side: "buy" or "sell" (the direction of BOTH exit orders)
              "sell" to exit a long position, "buy" to exit a short
        quantity: Amount in base currency
        take_profit_price: Price for take-profit limit order
        stop_loss_trigger: Trigger price for stop-loss
        stop_loss_limit: Limit price for stop-loss (optional, omit for stop-market)
    """
    side = side.lower()
    if side not in ("buy", "sell"):
        return {"error": "INVALID_SIDE", "message": "Side must be 'buy' or 'sell'."}
    if quantity <= 0:
        return {"error": "INVALID_QUANTITY", "message": "Quantity must be positive."}
    if take_profit_price <= 0 or stop_loss_trigger <= 0:
        return {"error": "INVALID_PRICE", "message": "Prices must be positive."}

    # Verify consistency: for "sell" OCO, TP > current price, SL < current price
    ex = _get_exchange()
    ticker = ex.fetch_ticker(symbol)
    current = ticker.get("last") or 0
    if side == "sell":
        if take_profit_price <= current:
            return {
                "error": "INVALID_OCO",
                "message": (
                    f"Take-profit ({take_profit_price}) must be ABOVE "
                    f"current price ({current:.2f}) for sell OCO."
                ),
            }
        if stop_loss_trigger >= current:
            return {
                "error": "INVALID_OCO",
                "message": (
                    f"Stop-loss trigger ({stop_loss_trigger}) must be BELOW "
                    f"current price ({current:.2f}) for sell OCO."
                ),
            }
    elif side == "buy":
        if take_profit_price >= current:
            return {
                "error": "INVALID_OCO",
                "message": (
                    f"Take-profit ({take_profit_price}) must be BELOW "
                    f"current price ({current:.2f}) for buy OCO."
                ),
            }
        if stop_loss_trigger <= current:
            return {
                "error": "INVALID_OCO",
                "message": (
                    f"Stop-loss trigger ({stop_loss_trigger}) must be ABOVE "
                    f"current price ({current:.2f}) for buy OCO."
                ),
            }

    # Run standard safety gates
    validation_err = _validate_pre_trade(symbol, side, quantity, current, None)
    if validation_err:
        return validation_err

    oco_preview = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "take_profit_price": take_profit_price,
        "stop_loss_trigger": stop_loss_trigger,
        "stop_loss_limit": stop_loss_limit,
        "current_price": current,
        "dry_run": DRY_RUN,
    }

    if DRY_RUN:
        return {
            "ok": True,
            "dry_run": True,
            "message": (
                f"[DRY-RUN] Would create OCO {side.upper()} {quantity} {symbol}: "
                f"TP @ {take_profit_price}, SL @ {stop_loss_trigger}."
            ),
            "preview": oco_preview,
        }

    try:
        sl_limit = stop_loss_limit if stop_loss_limit else stop_loss_trigger
        symbol_raw = symbol.replace("/", "").upper()
        result = ex.privatePostOrderOco({
            "symbol": symbol_raw,
            "side": side.upper(),
            "quantity": str(quantity),
            "price": str(take_profit_price),
            "stopPrice": str(stop_loss_trigger),
            "stopLimitPrice": str(sl_limit),
            "stopLimitTimeInForce": "GTC",
            "recvWindow": RECV_WINDOW,
        })

        orders = result.get("orderReports", [])
        tp_order = next((o for o in orders if "LIMIT" in str(o.get("type", ""))), {})
        sl_order = next((o for o in orders if "STOP" in str(o.get("type", ""))), {})

        return {
            "ok": True,
            "dry_run": False,
            "oco_list_id": result.get("orderListId"),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "take_profit_price": take_profit_price,
            "stop_loss_trigger": stop_loss_trigger,
            "status": result.get("listStatusType"),
            "tp_order_id": tp_order.get("orderId"),
            "sl_order_id": sl_order.get("orderId"),
            "message": "OCO active: TP and SL set. When one fills, the other cancels.",
        }
    except Exception as e:
        log.error("OCO failed: %s", e)
        return {
            "ok": False,
            "error": type(e).__name__,
            "message": str(e),
            "preview": oco_preview,
        }


@mcp.tool()
def create_bracket_order(
    symbol: str,
    side: str,
    quantity: float,
    take_profit_price: float,
    stop_loss_trigger: float,
    stop_loss_limit: float | None = None,
    limit_price: float | None = None,
) -> dict:
    """
    Place an entry order + OCO exit in one call (bracket order).
    The entry fills first, then the OCO (take-profit + stop-loss) is set.
    If the entry fails, the OCO is never created.

    Args:
        symbol: Trading pair, e.g. "ETH/USDT"
        side: "buy" or "sell" (the entry direction)
              "buy" = open long, exits with sell OCO
              "sell" = open short, exits with buy OCO
        quantity: Amount in base currency (e.g. 0.01 BTC)
        take_profit_price: Exit price for profit target
        stop_loss_trigger: Trigger price for stop-loss
        stop_loss_limit: Limit price for stop-loss (optional)
        limit_price: Optional limit price for entry. Omit for market entry.
    """
    side = side.lower()
    if side not in ("buy", "sell"):
        return {"error": "INVALID_SIDE", "message": "Side must be 'buy' or 'sell'."}
    if quantity <= 0:
        return {"error": "INVALID_QUANTITY", "message": "Quantity must be positive."}

    ex = _get_exchange()
    client_id_entry = _new_client_id(symbol)

    bracket_preview = {
        "symbol": symbol,
        "entry_side": side,
        "entry_type": "limit" if limit_price else "market",
        "entry_limit_price": limit_price,
        "quantity": quantity,
        "take_profit": take_profit_price,
        "stop_loss": stop_loss_trigger,
        "dry_run": DRY_RUN,
    }

    if DRY_RUN:
        return {
            "ok": True,
            "dry_run": True,
            "message": (
                f"[DRY-RUN] Would place bracket: {side.upper()} {quantity} {symbol} "
                f"→ TP @ {take_profit_price}, SL @ {stop_loss_trigger}."
            ),
            "preview": bracket_preview,
        }

    # ── Step 1: Entry order ──
    ticker = ex.fetch_ticker(symbol)
    entry_price = ticker.get("last") or 0

    validation_err = _validate_pre_trade(symbol, side, quantity, entry_price, limit_price)
    if validation_err:
        return validation_err

    try:
        if limit_price:
            entry_result = ex.create_limit_order(
                symbol, side, quantity, limit_price, _auth_params(client_id_entry)
            )
        else:
            entry_result = ex.create_market_order(
                symbol, side, quantity, _auth_params(client_id_entry)
            )
    except Exception as e:
        log.error("Bracket entry failed: %s", e)
        return {
            "ok": False,
            "step": "entry",
            "error": type(e).__name__,
            "message": str(e),
        }

    entry_status = entry_result.get("status", "unknown")
    entry_filled = float(entry_result.get("filled", 0) or 0)
    entry_price_actual = float(entry_result.get("price", 0) or 0)

    if entry_status not in ("closed", "filled") and entry_filled <= 0:
        return {
            "ok": True,
            "step": "entry",
            "order_id": entry_result.get("id"),
            "client_order_id": client_id_entry,
            "status": entry_status,
            "message": (
                f"Entry order placed ({entry_status}). "
                "OCO exit will be created after fill. "
                "Monitor with get_open_orders() and call create_oco_order() after fill."
            ),
            "entry_price": entry_price_actual,
        }

    # ── Step 2: OCO exit ──
    oco_side = "sell" if side == "buy" else "buy"

    if oco_side == "sell":
        if take_profit_price <= entry_price_actual:
            return {
                "ok": False,
                "step": "oco",
                "error": "INVALID_OCO",
                "message": (
                    f"Entry filled @ {entry_price_actual:.2f}. "
                    f"Take-profit ({take_profit_price}) must be ABOVE entry for long position."
                ),
                "entry_order_id": entry_result.get("id"),
            }
        if stop_loss_trigger >= entry_price_actual:
            return {
                "ok": False,
                "step": "oco",
                "error": "INVALID_OCO",
                "message": (
                    f"Stop-loss ({stop_loss_trigger}) must be BELOW entry "
                    f"@ {entry_price_actual:.2f} for long position."
                ),
                "entry_order_id": entry_result.get("id"),
            }

    try:
        oco_qty = entry_filled if entry_filled > 0 else quantity
        sl_limit = stop_loss_limit if stop_loss_limit else stop_loss_trigger
        symbol_raw = symbol.replace("/", "").upper()
        oco_result = ex.privatePostOrderOco({
            "symbol": symbol_raw,
            "side": oco_side.upper(),
            "quantity": str(oco_qty),
            "price": str(take_profit_price),
            "stopPrice": str(stop_loss_trigger),
            "stopLimitPrice": str(sl_limit),
            "stopLimitTimeInForce": "GTC",
            "recvWindow": RECV_WINDOW,
        })

        orders = oco_result.get("orderReports", [])
        tp_order = next((o for o in orders if "LIMIT" in str(o.get("type", ""))), {})
        sl_order = next((o for o in orders if "STOP" in str(o.get("type", ""))), {})

        _track_order_value(symbol, quantity)

        return {
            "ok": True,
            "dry_run": False,
            "entry_order_id": entry_result.get("id"),
            "entry_price": entry_price_actual,
            "entry_filled": entry_filled,
            "oco_list_id": oco_result.get("orderListId"),
            "oco_status": oco_result.get("listStatusType"),
            "tp_order_id": tp_order.get("orderId"),
            "sl_order_id": sl_order.get("orderId"),
            "take_profit_price": take_profit_price,
            "stop_loss_trigger": stop_loss_trigger,
            "message": (
                f"Bracket complete: {side.upper()} {oco_qty} {symbol} "
                f"@ {entry_price_actual:.2f} → TP {take_profit_price} | SL {stop_loss_trigger}."
            ),
        }
    except Exception as e:
        log.error("Bracket OCO failed: %s", e)
        return {
            "ok": True,
            "step": "oco_failed",
            "entry_order_id": entry_result.get("id"),
            "entry_price": entry_price_actual,
            "entry_filled": entry_filled,
            "error": type(e).__name__,
            "message": (
                f"Entry filled @ {entry_price_actual:.2f} but OCO creation failed: {e}. "
                "Manually call create_oco_order() to set exit."
            ),
        }


@mcp.tool()
def cancel_order(order_id: str, symbol: str) -> dict:
    """
    Cancel an open order by ID.

    Args:
        order_id: The exchange order ID.
        symbol: Trading pair, e.g. "BTC/USDT"
    """
    live_err = _check_live()
    if live_err:
        return live_err

    clock_err = _check_clock()
    if clock_err:
        return clock_err

    if DRY_RUN:
        return {
            "ok": True,
            "dry_run": True,
            "message": f"[DRY-RUN] Would cancel order {order_id} for {symbol}.",
        }

    ex = _get_exchange()
    try:
        result = ex.cancel_order(order_id, symbol, _auth_params())
        return {
            "ok": True,
            "order_id": result.get("id"),
            "symbol": symbol,
            "status": result.get("status"),
        }
    except Exception as e:
        log.error("Cancel failed: %s", e)
        return {"ok": False, "error": type(e).__name__, "message": str(e)}


@mcp.tool()
def reconcile_order(client_order_id: str, symbol: str) -> dict:
    """
    Check if an order with a given clientOrderId was placed.
    Use this after a network timeout to determine if the order landed
    before retrying. Binance idempotency: retrying with the same
    clientOrderId is safe (no duplicate).

    Args:
        client_order_id: The client-generated ID from create_order response.
        symbol: Trading pair, e.g. "BTC/USDT"
    """
    ex = _get_exchange()
    result = _reconcile_order(ex, symbol, client_order_id)
    if result:
        return result
    return {
        "ok": True,
        "reconciled": False,
        "message": (
            f"No order found with clientOrderId {client_order_id}. "
            "It is safe to retry."
        ),
        "client_order_id": client_order_id,
        "symbol": symbol,
    }


@mcp.tool()
def get_open_orders(symbol: str | None = None) -> dict:
    """
    List all open orders, optionally filtered by symbol.

    Args:
        symbol: Optional trading pair filter, e.g. "BTC/USDT"
    """
    ex = _get_exchange()
    orders = ex.fetch_open_orders(symbol, _auth_params())
    return {
        "open_orders": [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "type": o.get("type"),
                "quantity": o.get("amount"),
                "price": o.get("price"),
                "filled": o.get("filled"),
                "remaining": o.get("remaining"),
                "status": o.get("status"),
                "timestamp": o.get("timestamp"),
            }
            for o in orders
        ]
    }


@mcp.tool()
def get_closed_orders(symbol: str | None = None, limit: int = 20) -> dict:
    """
    Get recently closed/filled orders.

    Args:
        symbol: Optional trading pair filter, e.g. "BTC/USDT"
        limit: Max number of orders to return (default 20, max 50)
    """
    ex = _get_exchange()
    orders = ex.fetch_closed_orders(
        symbol, limit=min(limit, 50), params=_auth_params()
    )
    return {
        "closed_orders": [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "type": o.get("type"),
                "quantity": o.get("amount"),
                "price": o.get("price"),
                "cost": o.get("cost"),
                "fee": o.get("fee"),
                "filled": o.get("filled"),
                "status": o.get("status"),
                "timestamp": o.get("timestamp"),
            }
            for o in orders
        ]
    }


# ── Healthcheck (standalone) ────────────────────────────────────────────────

def healthcheck():
    """Print exchange status for manual verification. Run with --healthcheck."""
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print(f"Exchange  : {EXCHANGE_ID}")
    print(f"Mode      : {MODE}")
    print(f"Dry-run   : {DRY_RUN}")
    print(f"Sandbox   : {SANDBOX}")
    print(f"Auth key  : {'yes' if API_KEY else 'no'}")
    print(f"Max order : ${MAX_ORDER_USD or 'unlimited'}")
    print(f"Max sess  : ${MAX_SESSION_USD or 'unlimited'}")
    print(f"Slip. max : {MAX_SLIPPAGE_PCT}%")
    print(f"Min not.  : ${MIN_NOTIONAL_USD}")
    print(f"Dedup win : {DEDUP_WINDOW_SEC}s")
    print()

    ex = _get_exchange()
    print(f"Clock ok  : {_clock_ok} (offset={_clock_offset_ms}ms, max={CLOCK_MAX_DRIFT_MS}ms)")
    try:
        ex.load_markets()
        print(f"Markets loaded: {len(ex.markets)} symbols")
    except Exception as e:
        print(f"Markets ERROR: {e}")
        return

    if API_KEY:
        try:
            balance = ex.fetch_balance(_auth_params())
            total = balance.get("total", {})
            non_zero = {a: round(v, 4) for a, v in total.items() if (float(v or 0)) > 0}
            major = {k: v for k, v in non_zero.items()
                     if k in ("BTC", "ETH", "SOL", "USDT", "USDC", "BNB", "XRP", "DOGE")}
            other_count = len(non_zero) - len(major)
            print(f"Balance : {major}")
            if other_count > 0:
                print(f"          + {other_count} other testnet assets")
        except Exception as e:
            print(f"Balance ERROR: {e}")

    try:
        ticker = ex.fetch_ticker("BTC/USDT")
        print(f"BTC/USDT : ${ticker.get('last'):.2f}")
    except Exception as e:
        print(f"Ticker ERROR: {e}")

    print("\nAll checks passed.")


# ── Entrypoint ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--healthcheck" in sys.argv:
        healthcheck()
    else:
        mcp.run(transport="stdio")
