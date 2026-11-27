#!/usr/bin/env python3
"""Polymarket research and paper execution.

Reads market metadata and order books from Polymarket's public APIs, computes
implied probability and edge, and publishes paper positions to AI-Trader.

Real execution is intentionally NOT implemented here: it needs a funded Polygon
wallet, USDC allowances and signed CLOB orders (py-clob-client). See
docs/polymarket.md for the requirements and the reason.

Usage:
    python polymarket.py markets --query "fed" --min-liquidity 5000
    python polymarket.py book --slug will-btc-be-above-120k-on-june-30 --outcome Yes
    python polymarket.py bet --slug <slug> --outcome Yes --prob 0.62 --equity 1000
    python polymarket.py bet --slug <slug> --outcome Yes --prob 0.62 --size 50 --publish
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from env import env, load_env  # noqa: E402

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_URL = "https://clob.polymarket.com"
AI_TRADER_URL = "https://ai4trade.ai/api/signals/realtime"
USER_AGENT = "autotrader-polymarket/1.0"

# Polymarket charges no trading fee on standard markets today. Keep the knob so
# the math stays honest if that changes or for markets with fees.
DEFAULT_FEE = 0.0


# ─── MODEL ────────────────────────────────────────────
@dataclass
class Outcome:
    name: str
    token_id: str


@dataclass
class Market:
    id: str
    question: str
    slug: str
    condition_id: str
    outcomes: list[Outcome]
    liquidity: float
    volume_24h: float
    end_date: datetime | None
    closed: bool
    last_price: float | None
    best_bid: float | None
    best_ask: float | None

    def token_for(self, outcome_name: str) -> Outcome | None:
        wanted = outcome_name.strip().lower()
        for outcome in self.outcomes:
            if outcome.name.strip().lower() == wanted:
                return outcome
        return None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def parse_market(raw: dict[str, Any]) -> Market:
    names = [str(item) for item in _as_list(raw.get("outcomes"))]
    tokens = [str(item) for item in _as_list(raw.get("clobTokenIds"))]
    outcomes = [
        Outcome(name=name, token_id=tokens[index] if index < len(tokens) else "")
        for index, name in enumerate(names)
    ]
    return Market(
        id=str(raw.get("id", "")),
        question=str(raw.get("question", "")),
        slug=str(raw.get("slug", "")),
        condition_id=str(raw.get("conditionId", "") or ""),
        outcomes=outcomes,
        liquidity=_as_float(raw.get("liquidity")) or 0.0,
        volume_24h=_as_float(raw.get("volume24hr")) or 0.0,
        end_date=_as_datetime(raw.get("endDate")),
        closed=bool(raw.get("closed")),
        last_price=_as_float(raw.get("lastTradePrice")),
        best_bid=_as_float(raw.get("bestBid")),
        best_ask=_as_float(raw.get("bestAsk")),
    )


def filter_markets(
    markets: list[Market],
    query: str | None = None,
    min_liquidity: float = 0.0,
    max_hours: float | None = None,
    now: datetime | None = None,
) -> list[Market]:
    now = now or datetime.now(timezone.utc)
    result = []
    for market in markets:
        if market.closed:
            continue
        if market.liquidity < min_liquidity:
            continue
        if query and query.lower() not in f"{market.question} {market.slug}".lower():
            continue
        if max_hours is not None and market.end_date is not None:
            hours_left = (market.end_date - now).total_seconds() / 3600
            if hours_left <= 0 or hours_left > max_hours:
                continue
        result.append(market)
    return sorted(result, key=lambda market: market.liquidity, reverse=True)


# ─── ORDER BOOK ───────────────────────────────────────
def _levels(raw_levels: list[dict[str, Any]]) -> list[tuple[float, float]]:
    levels = []
    for level in raw_levels or []:
        price = _as_float(level.get("price"))
        size = _as_float(level.get("size"))
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        levels.append((price, size))
    return levels


def book_summary(book: dict[str, Any], depth_levels: int = 5) -> dict[str, Any]:
    bids = _levels(book.get("bids") or [])
    asks = _levels(book.get("asks") or [])
    best_bid = max((price for price, _ in bids), default=None)
    best_ask = min((price for price, _ in asks), default=None)

    mid = None
    spread = None
    spread_pct = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        spread_pct = (spread / mid * 100) if mid else None

    def depth(levels: list[tuple[float, float]], take: int, best_first: bool) -> float:
        if not levels:
            return 0.0
        ordered = sorted(levels, key=lambda item: item[0], reverse=best_first)
        return sum(price * size for price, size in ordered[:take])

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "spread_pct": spread_pct,
        "bid_depth_usd": depth(bids, depth_levels, True),
        "ask_depth_usd": depth(asks, depth_levels, False),
        "bids": len(bids),
        "asks": len(asks),
    }


# ─── EDGE ─────────────────────────────────────────────
def edge(prob: float, price: float, fee: float = DEFAULT_FEE) -> dict[str, float]:
    """EV per share and Kelly fraction for buying at ``price`` with belief ``prob``."""
    if not 0 < price < 1:
        raise ValueError("el precio debe estar entre 0 y 1")
    if not 0 <= prob <= 1:
        raise ValueError("la probabilidad debe estar entre 0 y 1")
    ev = prob - price - fee
    payout = 1 - price
    kelly = ev / payout if payout > 0 else 0.0
    return {
        "ev_per_share": ev,
        "roi": ev / price,
        "kelly": kelly,
    }


def stake(
    prob: float,
    price: float,
    equity: float,
    max_pct: float = 2.0,
    kelly_fraction: float = 0.5,
    fee: float = DEFAULT_FEE,
) -> dict[str, float]:
    """Fractional Kelly capped by the per-position risk limit."""
    if equity <= 0:
        raise ValueError("equity debe ser mayor que cero")
    metrics = edge(prob, price, fee)
    fraction = max(0.0, metrics["kelly"]) * kelly_fraction
    cap = equity * max_pct / 100
    amount = min(fraction * equity, cap)
    return {
        **metrics,
        "kelly_fraction": kelly_fraction,
        "stake_usd": amount,
        "stake_pct": amount / equity * 100,
        "capped": fraction * equity > cap,
    }


def paper_payload(market: Market, outcome: Outcome, price: float, size_usd: float) -> dict[str, Any]:
    """AI-Trader paper signal for a Polymarket outcome."""
    if price <= 0:
        raise ValueError("precio inválido")
    qty = size_usd / price
    return {
        "market": "polymarket",
        "action": "buy",
        "symbol": market.slug,
        "outcome": outcome.name,
        "token_id": outcome.token_id,
        "price": round(price, 4),
        "quantity": round(qty, 6),
        "executed_at": "now",
        "content": f"{market.question} -> {outcome.name} @ {price:.3f}",
    }


# ─── HTTP ─────────────────────────────────────────────
def _get(url: str, timeout: int = 20) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def fetch_markets(limit: int = 20, closed: bool = False) -> list[Market]:
    url = (
        f"{GAMMA_URL}?limit={limit}&closed={'true' if closed else 'false'}"
        "&order=liquidity&ascending=false"
    )
    data = _get(url)
    return [parse_market(item) for item in data if isinstance(item, dict)]


def fetch_market(slug: str) -> Market | None:
    data = _get(f"{GAMMA_URL}?slug={urllib.parse.quote(slug)}")
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict):
            return parse_market(item)
    return None


def fetch_book(token_id: str) -> dict[str, Any]:
    return _get(f"{CLOB_URL}/book?token_id={urllib.parse.quote(token_id)}")


def publish_paper(payload: dict[str, Any]) -> dict[str, Any]:
    token = env("CLIPTAP_AI_TOKEN")
    if not token:
        raise SystemExit("CLIPTAP_AI_TOKEN no configurado: no se puede publicar en AI-Trader")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        AI_TRADER_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


# ─── CLI ──────────────────────────────────────────────
def _fmt_probability(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def cmd_markets(args: argparse.Namespace) -> int:
    markets = filter_markets(
        fetch_markets(limit=max(args.limit * 4, 40)),
        query=args.query,
        min_liquidity=args.min_liquidity,
        max_hours=args.max_hours,
    )[: args.limit]

    if args.json:
        print(json.dumps([{**asdict(market), "end_date": market.end_date.isoformat() if market.end_date else None} for market in markets], indent=2, default=str))
        return 0

    if not markets:
        print("sin mercados que cumplan el filtro")
        return 0

    print(f"{'liquidez':>12} {'vol 24h':>10} {'cierra':<12} {'últ.':>6}  pregunta")
    print("-" * 110)
    for market in markets:
        yes = next((outcome for outcome in market.outcomes if outcome.name.lower() in ("yes", "sí")), None)
        if yes is None and market.outcomes:
            yes = market.outcomes[0]
        yes_price = market.last_price if yes and market.last_price is not None else None
        closes = market.end_date.strftime("%Y-%m-%d %H:%M") if market.end_date else "—"
        print(
            f"{market.liquidity:>12,.0f} {market.volume_24h:>10,.0f} {closes:<12} "
            f"{_fmt_probability(yes_price):>6}  {market.question[:60]}"
        )
    return 0


def cmd_book(args: argparse.Namespace) -> int:
    market = fetch_market(args.slug) if args.slug else None
    token_id = args.token_id
    if market and not token_id:
        outcome = market.token_for(args.outcome or (market.outcomes[0].name if market.outcomes else "Yes"))
        if outcome is None:
            print(f"error: outcome '{args.outcome}' no existe en {market.slug}", file=sys.stderr)
            return 1
        token_id = outcome.token_id
    if not token_id:
        print("error: falta --token-id o --slug", file=sys.stderr)
        return 1

    summary = book_summary(fetch_book(token_id))
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"market: {market.question if market else token_id}")
    print(f"best bid {summary['best_bid']} · best ask {summary['best_ask']} · mid {summary['mid']}")
    print(f"spread {summary['spread']} ({summary['spread_pct']:.2f}%)")
    print(f"profundidad top 5: bids ${summary['bid_depth_usd']:,.0f} · asks ${summary['ask_depth_usd']:,.0f}")
    return 0


def cmd_bet(args: argparse.Namespace) -> int:
    market = fetch_market(args.slug)
    if market is None:
        print(f"error: mercado '{args.slug}' no encontrado", file=sys.stderr)
        return 1
    outcome = market.token_for(args.outcome)
    if outcome is None:
        names = ", ".join(item.name for item in market.outcomes)
        print(f"error: outcome '{args.outcome}' no existe. Opciones: {names}", file=sys.stderr)
        return 1

    try:
        summary = book_summary(fetch_book(outcome.token_id))
    except Exception as exc:  # noqa: BLE001 - a thin book should not abort the command
        print(f"aviso: no se pudo leer el libro ({exc}); se usa el último precio", file=sys.stderr)
        summary = {"best_ask": None, "mid": None}

    price = args.price or summary.get("best_ask") or summary.get("mid") or market.last_price
    if price is None:
        print("error: sin precio disponible; pasá --price", file=sys.stderr)
        return 1
    price = float(price)

    if args.size:
        size = args.size
        sizing = None
    else:
        sizing = stake(args.prob, price, args.equity, max_pct=args.max_pct, kelly_fraction=args.kelly_fraction)
        size = sizing["stake_usd"]

    if size <= 0:
        print(f"sin edge a favor: prob {args.prob:.2%} vs precio {price:.2%}. No conviene apostar.")
        return 0

    payload = paper_payload(market, outcome, price, size)
    detail = {
        "market": market.slug,
        "question": market.question,
        "outcome": outcome.name,
        "price": price,
        "implied_probability": price,
        "model_probability": args.prob,
        "edge": edge(args.prob, price),
        "sizing": sizing,
        "size_usd": round(size, 2),
        "shares": payload["quantity"],
        "payload": payload,
    }

    if args.json:
        print(json.dumps(detail, indent=2, default=str))
    else:
        print(f"{market.question}")
        print(f"  outcome {outcome.name} @ {price:.3f} (implícita {price:.1%}) · modelo {args.prob:.1%}")
        print(f"  EV por share {detail['edge']['ev_per_share']:+.4f} · ROI {detail['edge']['roi']:+.2%} · Kelly {detail['edge']['kelly']:.2%}")
        if sizing:
            print(f"  stake ${size:,.2f} ({sizing['stake_pct']:.2f}% del equity)"
                  + (" [tope de riesgo]" if sizing["capped"] else ""))
        print(f"  shares {payload['quantity']}")

    if args.publish:
        response = publish_paper(payload)
        print(f"publicado en AI-Trader: {response.get('signal_id') or response}")
    else:
        print("(paper) Agregá --publish para registrarlo en AI-Trader.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Investigación y paper trading de Polymarket.")
    sub = parser.add_subparsers(dest="command", required=True)

    markets = sub.add_parser("markets", help="buscar mercados abiertos")
    markets.add_argument("--query", default=None)
    markets.add_argument("--min-liquidity", type=float, default=0.0)
    markets.add_argument("--max-hours", type=float, default=None, help="cierre dentro de N horas")
    markets.add_argument("--limit", type=int, default=10)
    markets.add_argument("--json", action="store_true")
    markets.set_defaults(func=cmd_markets)

    book = sub.add_parser("book", help="order book y probabilidad implícita")
    book.add_argument("--slug", default=None)
    book.add_argument("--outcome", default=None)
    book.add_argument("--token-id", default=None)
    book.add_argument("--json", action="store_true")
    book.set_defaults(func=cmd_book)

    bet = sub.add_parser("bet", help="calcular edge y registrar paper")
    bet.add_argument("--slug", required=True)
    bet.add_argument("--outcome", required=True)
    bet.add_argument("--prob", type=float, required=True, help="tu probabilidad (0-1)")
    bet.add_argument("--price", type=float, default=None)
    bet.add_argument("--equity", type=float, default=1000.0)
    bet.add_argument("--max-pct", type=float, default=2.0)
    bet.add_argument("--kelly-fraction", type=float, default=0.5)
    bet.add_argument("--size", type=float, default=None, help="stake fijo en USD")
    bet.add_argument("--publish", action="store_true")
    bet.add_argument("--json", action="store_true")
    bet.set_defaults(func=cmd_bet)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    load_env()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
