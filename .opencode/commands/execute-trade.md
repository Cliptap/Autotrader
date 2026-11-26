---
description: Execute a trade on AI-Trader (paper) and optionally mirror it
---

# Execute trade

Publishes a paper signal and, only when the mode allows it and the user
confirms, mirrors the operation on the broker.

## Prerequisites

- Load the `tradesync` skill (paper) and `ccxt-mcp` or `alpaca-mcp` (execution).
- `python scripts/preflight.py` passes.
- The trading mode is known: `paper`, `crypto-sandbox`, `crypto-live`,
  `stocks-paper` or `stocks-live`.

## Steps

1. Confirm market context (load `market-intel`).
2. Write the thesis: entry, stop, target, invalidation evidence.
3. Size the position with the risk budget, never by feel:

   ```bash
   python scripts/position_size.py --equity <equity> --risk-pct 0.5 \
     --entry <entry> --stop <stop> --target <target>
   ```

   Reject the trade if R:R < 2 or the risk exceeds 2% of equity.
4. Publish the paper signal:

   `POST /api/signals/realtime`
   `{ "market": "...", "action": "buy|sell|short|cover", "symbol": "...",
      "price": <price|0>, "quantity": <qty>, "executed_at": "now",
      "content": "<thesis + stop + target>" }`

5. Document the thesis with `POST /api/signals/strategy`.
6. Mirror on the broker only if the mode is live AND the user confirmed:
   - crypto: `exchange_info()` → `create_order()` → `get_open_orders()`
   - stocks: `get_clock()` → `place_stock_order()` (prefer bracket orders)
   - on timeout: `reconcile_order(client_order_id, symbol)` before any retry
7. Register the operation in `.opencode/knowledge/DASHBOARD.md` and the journal.
8. If a real session was armed, `disarm_live_session()` when done.

## Parameters

Ask the user for:

- market: crypto, us-stock, polymarket
- symbol: BTC, ETH, NVDA, ...
- action: buy, sell, short, cover
- price (0 = current market price)
- quantity or risk budget
