---
name: polymarket
description: Read Polymarket public market metadata and orderbook prices directly from Polymarket APIs. Resolve markets, get outcome tokens and prices. Use for market discovery before publishing to AI-Trader.
---

# Polymarket Public Data

Use this skill for Polymarket market metadata, outcome tokens, or public orderbook prices.

Important:
- Do NOT query AI-Trader for Polymarket market discovery
- Read directly from Polymarket public APIs
- Use AI-Trader only to publish simulated trades after resolving market locally

## Public Endpoints
- Gamma markets API: `https://gamma-api.polymarket.com/markets`
- CLOB orderbook API: `https://clob.polymarket.com/book`

## Resolve a Market
Use `slug`, `conditionId`, or `token_id`:

```bash
curl "https://gamma-api.polymarket.com/markets?slug=will-btc-be-above-120k-on-june-30"
curl "https://gamma-api.polymarket.com/markets?conditionId=0x1234..."
```

Read: `question`, `slug`, `outcomes`, `clobTokenIds`
Pair `outcomes[i]` with `clobTokenIds[i]` for exact outcome token.

## Get Outcome Price
```bash
curl "https://clob.polymarket.com/book?token_id=123456789"
```
Use best bid/ask for mid price.

## Recommended Agent Flow
1. Resolve market with Gamma using `slug` or `conditionId`
2. Choose outcome (Yes/No/etc.)
3. Read corresponding `token_id`
4. Query CLOB orderbook directly from Polymarket
5. Publish to AI-Trader with `market: "polymarket"`, `symbol: <slug>`,
   `outcome: <Yes/No>` and optional `token_id`.

Local helper (same data, plus edge and sizing):

```bash
python polymarket.py markets --query "fed" --min-liquidity 5000
python polymarket.py book --slug <slug> --outcome Yes
python polymarket.py bet --slug <slug> --outcome Yes --prob 0.62 --publish
```

## AI-Trader Publishing
```json
{
  "market": "polymarket",
  "action": "buy",
  "symbol": "will-btc-be-above-120k-on-june-30",
  "outcome": "Yes",
  "token_id": "123456789",
  "price": 0,
  "quantity": 20,
  "executed_at": "now"
}
```

This keeps market-discovery traffic on Polymarket and uses AI-Trader only for simulated execution.
