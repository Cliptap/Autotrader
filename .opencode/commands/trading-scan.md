---
description: Run the daily trading analysis cycle without executing orders
---

# Trading daily routine

Full read-only analysis cycle. No orders are placed in this command.

## Steps

1. Load the `market-intel` and `ai-trader` skills.
2. Fetch market context:
   - `GET /api/market-intel/overview`
   - `GET /api/market-intel/news?category=crypto|equities|macro`
   - `GET /api/market-intel/macro-signals`
3. Fetch account state:
   - `GET /api/positions`
   - `GET /api/signals/feed?limit=20`
4. Read recent collector snapshots from `.opencode/knowledge/market-data/`
   and compare yesterday's predictions with what actually happened.
5. Report:
   - portfolio status: equity, exposure, PnL, stale or unprotected positions
   - market regime: bullish / bearish / neutral, with evidence
   - top 3 opportunities with thesis, stop, target and R:R
   - positions that need action (stop breached, thesis invalidated)
   - anything from `alerts.md` that matters
6. Stop there. Execution requires `/execute-trade` and explicit confirmation.
