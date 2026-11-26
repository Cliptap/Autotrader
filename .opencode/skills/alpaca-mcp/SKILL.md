---
name: alpaca-mcp
description: Alpaca Markets official MCP server. Trade US stocks, ETFs, crypto, and options. Paper trading by default with live gated behind ALPACA_PAPER_TRADE=false. 60+ tools across 13 toolsets. Use ONLY after paper-trade validation in AI-Trader.
---

# Alpaca MCP — US Stock & ETF Execution

Official MCP server by Alpaca Markets (857+ stars, MIT license). Executes trades on US equities via Alpaca's brokerage API. **Paper trading by default** — no real money until explicitly configured.

## Setup

### 1. Get API Keys (Paper Trading — Free)
1. Go to https://app.alpaca.markets/paper/dashboard/overview
2. Create a free paper trading account (no deposit needed)
3. Generate API keys from the dashboard
4. Add them to `opencode.json` under `mcp.alpaca.environment`:
   ```json
   "ALPACA_API_KEY": "PK...",
   "ALPACA_SECRET_KEY": "..."
   ```

### 2. Verify
Ask the agent: "Check my Alpaca account status using mcp__alpaca__get_account_info"

Paper trading is enabled by default (`ALPACA_PAPER_TRADE=true`).

## Enabled Toolsets

Only these are enabled (via `ALPACA_TOOLSETS` in config):
- `trading` — Orders, positions
- `account` — Balance, buying power, account status
- `stock-data` — Stock bars, quotes, trades, snapshots
- `positions` — View and close positions
- `assets` — Asset lookup, calendar, clock
- `watchlists` — Watchlist management
- `news` — News articles by ticker

## Key Tools

### Account & Portfolio
| Tool | Purpose |
|------|---------|
| `get_account_info` | Balance, buying power, margin status |
| `get_portfolio_history` | Equity and P/L over time |
| `get_all_positions` | All current positions |
| `get_open_position` | Details for a specific position |

### Trading (stocks & ETFs)
| Tool | Purpose |
|------|---------|
| `place_stock_order` | Market, limit, stop, stop-limit, trailing-stop, bracket orders |
| `close_position` | Close a specific position (full or partial) |
| `close_all_positions` | Liquidate entire portfolio |
| `cancel_order_by_id` | Cancel specific order |
| `cancel_all_orders` | Cancel all open orders |
| `get_orders` | Order history with filters |
| `get_order_by_client_id` | Find order by client ID |

### Market Data
| Tool | Purpose |
|------|---------|
| `get_stock_bars` | Historical OHLCV bars |
| `get_stock_latest_quote` | Latest bid/ask |
| `get_stock_latest_trade` | Last trade price |
| `get_stock_snapshot` | Comprehensive snapshot (quote + trade + bar) |
| `get_most_active_stocks` | Most active by volume |
| `get_market_movers` | Top gainers and losers |
| `get_clock` | Market open/close times |
| `get_calendar` | Market holiday calendar |

### News & Watchlists
| Tool | Purpose |
|------|---------|
| `get_news` | News articles filtered by ticker |
| `create_watchlist` / `get_watchlists` | Manage watchlists |

## Trading Hours

US stock market: **9:30 AM – 4:00 PM ET**, Monday–Friday.
Check market status with `get_clock` before placing orders.

Orders placed outside market hours will queue for the next open.

## Execution Protocol

```
1. Research: get_stock_latest_quote("AAPL"), get_stock_bars(...), get_news(...)
2. Paper-trade in AI-Trader (POST /api/signals/realtime)
3. Validate strategy (3+ profitable paper trades)
4. If validated → replicate via mcp__alpaca__place_stock_order()
5. Monitor with get_all_positions() + get_orders()
```

## Place Stock Order Format

```
mcp__alpaca__place_stock_order(
    symbol="AAPL",
    qty=1,                    // shares (fractional: 0.5)
    side="buy",               // "buy" or "sell"
    type="market",            // "market", "limit", "stop", "stop_limit", "trailing_stop"
    time_in_force="day"       // "day", "gtc", "ioc"
)
```

For limit orders add `limit_price=185.00`.
For fractional shares use fractional qty (e.g., `qty=0.25`).

## Safety

| Gate | Default | What it does |
|------|---------|--------------|
| `ALPACA_PAPER_TRADE` | `true` | Paper trading — no real money |
| Live mode | Requires `ALPACA_PAPER_TRADE=false` + live API keys |
| Toolset filtering | Only 7/13 toolsets enabled |

**IMPORTANT:** Never call place_stock_order before the paper-trade equivalent is published in AI-Trader.

## Supported Assets

- US stocks: AAPL, TSLA, NVDA, MSFT, GOOGL, AMZN, META, SPY, QQQ...
- ETFs: SPY, QQQ, IWM, VTI, VOO, ARKK...
- Fractional shares supported on all stocks and ETFs
