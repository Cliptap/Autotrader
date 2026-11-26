#!/usr/bin/env python3
"""Inspect an exchange and validate an order against its rules.

Answers the two questions that break multi-exchange setups:

    1. What does this venue require?   -> profile
    2. Is my order valid here?         -> check

Usage:
    python scripts/exchange_profile.py list
    python scripts/exchange_profile.py profile --symbol BTC/USDT
    python scripts/exchange_profile.py profile --symbol ETH/USDT --exchange bybit
    python scripts/exchange_profile.py check --symbol BTC/USDT --qty 0.0002 --price 60000

Exit codes:
    0  the order is valid
    1  the order breaks a rule, or the exchange could not be reached
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from env import env, env_float, load_env  # noqa: E402
from exchange_rules import (  # noqa: E402
    min_orderable_qty,
    normalize_amount,
    rules_from_market,
    validate_order,
)

COMMON_EXCHANGES = ("binance", "bybit", "okx", "kucoin", "kraken", "coinbase", "gateio")


def _ccxt():
    try:
        import ccxt
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "ccxt no está instalado: pip install -r mcp/ccxt/requirements.txt"
        ) from exc
    return ccxt


def build_exchange(exchange_id: str, sandbox: bool):
    ccxt = _ccxt()
    factory = getattr(ccxt, exchange_id, None)
    if factory is None:
        raise SystemExit(f"exchange desconocido: {exchange_id}")
    exchange = factory(
        {
            "apiKey": env("CCXT_API_KEY"),
            "secret": env("CCXT_SECRET"),
            "password": env("CCXT_PASSWORD") or None,
            "enableRateLimit": True,
            "adjustForTimeDifference": True,
        }
    )
    if sandbox:
        exchange.set_sandbox_mode(True)
    return exchange


def cmd_list(args: argparse.Namespace) -> int:
    ccxt = _ccxt()
    rows = []
    for exchange_id in COMMON_EXCHANGES:
        factory = getattr(ccxt, exchange_id, None)
        if factory is None:
            continue
        try:
            exchange = factory({"enableRateLimit": True})
        except Exception as exc:  # noqa: BLE001
            rows.append((exchange_id, "?", "?", f"error: {exc}"))
            continue
        needs_password = bool((exchange.requiredCredentials or {}).get("password"))
        has_sandbox = bool((exchange.urls or {}).get("test"))
        rows.append(
            (
                exchange_id,
                "sí" if needs_password else "no",
                "sí" if has_sandbox else "no",
                "",
            )
        )

    print(f"ccxt {ccxt.__version__}")
    print(f"{'exchange':<12} {'password':<9} {'sandbox':<8} nota")
    print("-" * 60)
    for exchange_id, password, sandbox, note in rows:
        print(f"{exchange_id:<12} {password:<9} {sandbox:<8} {note}")
    print("\npassword: OKX y KuCoin lo requieren (passphrase / trading password).")
    print("sandbox: presencia de URL de test en ccxt; no implica que el venue la mantenga activa.")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    exchange_id = args.exchange or env("CCXT_EXCHANGE", "binance")
    sandbox = args.sandbox or env("CCXT_SANDBOX", "true").lower() == "true"
    exchange = build_exchange(exchange_id, sandbox)
    exchange.load_markets()
    if args.symbol not in exchange.markets:
        print(f"error: {args.symbol} no existe en {exchange_id}", file=sys.stderr)
        return 1

    rules = rules_from_market(exchange.market(args.symbol))
    price = None
    try:
        ticker = exchange.fetch_ticker(args.symbol)
        price = ticker.get("last") or ticker.get("close")
    except Exception:  # noqa: BLE001 - profile must work without market data
        price = None

    floor = env_float("MIN_NOTIONAL_USD", 10.0)
    detail = {
        "exchange": exchange_id,
        "sandbox": sandbox,
        "rules": asdict(rules),
        "price": price,
        "min_notional_usd": floor,
        "min_orderable_qty": (
            min_orderable_qty(rules, float(price), floor) if price else None
        ),
    }

    if args.json:
        print(json.dumps(detail, indent=2))
        return 0

    print(f"{exchange_id} {'(sandbox)' if sandbox else '(live)'}")
    print(rules.summary())
    if price:
        print(f"precio actual: {price}")
        print(f"mínimo operativo: {detail['min_orderable_qty']:g} unidades "
              f"(≈ ${detail['min_orderable_qty'] * price:,.2f})")
        if rules.taker_fee is not None:
            print(f"fee taker estimado: {rules.taker_fee * 100:.3f}% por lado")
    else:
        print("sin precio: no se pudo calcular el mínimo operativo")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    exchange_id = args.exchange or env("CCXT_EXCHANGE", "binance")
    sandbox = args.sandbox or env("CCXT_SANDBOX", "true").lower() == "true"
    exchange = build_exchange(exchange_id, sandbox)
    exchange.load_markets()
    if args.symbol not in exchange.markets:
        print(f"error: {args.symbol} no existe en {exchange_id}", file=sys.stderr)
        return 1

    rules = rules_from_market(exchange.market(args.symbol))
    normalized = normalize_amount(rules, args.qty)
    floor = args.min_notional or env_float("MIN_NOTIONAL_USD", 10.0)
    issues = validate_order(rules, normalized, args.price, floor)

    payload = {
        "exchange": exchange_id,
        "symbol": args.symbol,
        "requested_qty": args.qty,
        "normalized_qty": normalized,
        "price": args.price,
        "notional": normalized * args.price if args.price else None,
        "min_notional_usd": floor,
        "issues": issues,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"{exchange_id} · {args.symbol} · qty {args.qty:g} -> {normalized:g}")
        if issues:
            for issue in issues:
                print(f"  - {issue}")
        else:
            print("  orden válida")

    return 1 if issues else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reglas de exchange y validación de órdenes.")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="exchanges comunes y qué requieren")
    listing.set_defaults(func=cmd_list)

    profile = sub.add_parser("profile", help="límites y fees de un símbolo")
    profile.add_argument("--symbol", required=True)
    profile.add_argument("--exchange", default=None)
    profile.add_argument("--sandbox", action="store_true")
    profile.add_argument("--json", action="store_true")
    profile.set_defaults(func=cmd_profile)

    check = sub.add_parser("check", help="validar una orden")
    check.add_argument("--symbol", required=True)
    check.add_argument("--qty", type=float, required=True)
    check.add_argument("--price", type=float, required=True)
    check.add_argument("--exchange", default=None)
    check.add_argument("--sandbox", action="store_true")
    check.add_argument("--min-notional", type=float, default=None)
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_check)

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
