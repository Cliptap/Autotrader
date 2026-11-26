#!/usr/bin/env python3
"""Stop-loss engine for open positions.

The agent decides and the broker executes, but neither of them is awake at
3 AM. This process is: every cycle it reads the registered plans, fetches the
current price and closes any position whose stop was breached, with an
idempotent client order id so a restart never double-sells.

It never opens positions. It only reduces risk.

Usage:
    python risk_engine.py plan add --symbol BTC/USDT --venue ccxt --market crypto \\
        --side long --qty 0.01 --entry 60000 --stop 58000 --target 65000
    python risk_engine.py plan list
    python risk_engine.py plan remove --id <plan_id> --reason "closed manually"
    python risk_engine.py check
    python risk_engine.py run --once
    python risk_engine.py run --interval 60

Safety model:
    * sandbox / Alpaca paper: closes are sent (safe by definition)
    * live venues: closes are blocked unless RISK_ENGINE_LIVE=true
    * --dry-run: nothing is ever sent, actions are only logged
    * state/HALT does NOT stop the engine: the kill switch blocks new entries,
      stops still protect the positions already open.

Exit codes for ``check``:
    0  every plan is valid
    1  at least one plan is invalid or the store is corrupt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from env import env, env_int, load_env  # noqa: E402

STATE_DIR = ROOT / "state"
PLANS_FILE = STATE_DIR / "positions.json"
LOG_FILE = STATE_DIR / "risk-log.jsonl"
HALT_FILE = STATE_DIR / "HALT"

MARKETS = ("crypto", "us-stock")
VENUES = ("ccxt", "alpaca")
SIDES = ("long", "short")

# Terminal states: the engine never touches these again.
FINAL_STATES = ("closed", "canceled", "orphan", "failed")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or utcnow()).isoformat(timespec="seconds")


# ─── MODEL ────────────────────────────────────────────
@dataclass
class Plan:
    id: str
    venue: str
    symbol: str
    market: str
    side: str
    qty: float
    entry: float
    stop: float
    target: float | None = None
    status: str = "open"
    opened_at: str = field(default_factory=iso)
    closed_at: str | None = None
    last_price: float | None = None
    exit_price: float | None = None
    close_client_id: str | None = None
    close_order_id: str | None = None
    close_sent_at: str | None = None
    blocked_logged_at: str | None = None
    attempts: int = 0
    last_error: str | None = None
    note: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.venue, self.symbol.upper())


def validate_plan(plan: Plan) -> None:
    """Raise ValueError when the plan cannot be trusted to protect a position."""
    if plan.venue not in VENUES:
        raise ValueError(f"venue inválido: {plan.venue} (usar {', '.join(VENUES)})")
    if plan.market not in MARKETS:
        raise ValueError(f"market inválido: {plan.market} (usar {', '.join(MARKETS)})")
    if plan.side not in SIDES:
        raise ValueError(f"side inválido: {plan.side} (usar {', '.join(SIDES)})")
    if plan.qty <= 0:
        raise ValueError("qty debe ser mayor que cero")
    if plan.entry <= 0 or plan.stop <= 0:
        raise ValueError("entry y stop deben ser positivos")
    if plan.side == "long" and plan.stop >= plan.entry:
        raise ValueError("en long el stop debe estar por debajo de la entrada")
    if plan.side == "short" and plan.stop <= plan.entry:
        raise ValueError("en short el stop debe estar por encima de la entrada")
    if plan.target is not None:
        if plan.side == "long" and plan.target <= plan.entry:
            raise ValueError("en long el target debe estar por encima de la entrada")
        if plan.side == "short" and plan.target >= plan.entry:
            raise ValueError("en short el target debe estar por debajo de la entrada")


def risk_usd(plan: Plan) -> float:
    return abs(plan.entry - plan.stop) * plan.qty


def close_side(plan: Plan) -> str:
    return "sell" if plan.side == "long" else "buy"


def stop_breached(plan: Plan, price: float) -> bool:
    if plan.side == "long":
        return price <= plan.stop
    return price >= plan.stop


def target_reached(plan: Plan, price: float) -> bool:
    if plan.target is None:
        return False
    if plan.side == "long":
        return price >= plan.target
    return price <= plan.target


def close_client_id(plan: Plan) -> str:
    """Stable across restarts and within Binance's 36-char limit."""
    digest = hashlib.sha1(plan.id.encode("utf-8")).hexdigest()[:20]
    return f"rk-{digest}"


# ─── STORE ────────────────────────────────────────────
def load_plans() -> list[Plan]:
    if not PLANS_FILE.is_file():
        return []
    raw = json.loads(PLANS_FILE.read_text(encoding="utf-8"))
    items = raw.get("positions", raw) if isinstance(raw, dict) else raw
    return [Plan(**item) for item in items]


def save_plans(plans: list[Plan]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": iso(),
        "positions": [asdict(plan) for plan in plans],
    }
    tmp = PLANS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PLANS_FILE)


def log_event(plan: Plan | None, action: str, **extra: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": iso(),
        "action": action,
        "plan_id": plan.id if plan else None,
        "symbol": plan.symbol if plan else None,
        "venue": plan.venue if plan else None,
        **extra,
    }
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ─── VENUES ───────────────────────────────────────────
class VenueError(RuntimeError):
    pass


class CcxtVenue:
    """Crypto execution through CCXT, reading the same .env as the MCP server."""

    name = "ccxt"

    def __init__(self) -> None:
        self._exchange = None
        self.sandbox = env("CCXT_SANDBOX", "true").lower() == "true"
        self.exchange_id = env("CCXT_EXCHANGE", "binance").lower()

    @property
    def exchange(self):
        if self._exchange is None:
            try:
                import ccxt  # imported lazily so tests and `plugin` commands do not need it
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise VenueError("ccxt no está instalado (pip install -r mcp/ccxt/requirements.txt)") from exc
            factory = getattr(ccxt, self.exchange_id, None)
            if factory is None:
                raise VenueError(f"exchange desconocido: {self.exchange_id}")
            self._exchange = factory(
                {
                    "apiKey": env("CCXT_API_KEY"),
                    "secret": env("CCXT_SECRET"),
                    "password": env("CCXT_PASSWORD") or None,
                    "enableRateLimit": True,
                    "adjustForTimeDifference": True,
                }
            )
            if self.sandbox:
                self._exchange.set_sandbox_mode(True)
        return self._exchange

    def price(self, symbol: str) -> float:
        ticker = self.exchange.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if not price:
            raise VenueError(f"sin precio para {symbol}")
        return float(price)

    def position_qty(self, symbol: str) -> float | None:
        # Spot has no position objects: the exchange rejects an oversized sell.
        return None

    def close(self, plan: Plan, client_id: str) -> dict:
        params = {"recvWindow": 10000}
        if self.exchange_id == "binance":
            params["newClientOrderId"] = client_id
        else:
            params["clientOrderId"] = client_id
        order = self.exchange.create_order(
            plan.symbol, "market", close_side(plan), plan.qty, None, params
        )
        return {
            "id": order.get("id"),
            "status": order.get("status", "open"),
            "price": order.get("average") or order.get("price"),
        }

    def order_status(self, plan: Plan, client_id: str) -> dict | None:
        params = {"recvWindow": 10000}
        if self.exchange_id == "binance":
            params["origClientOrderId"] = client_id
        try:
            order = self.exchange.fetch_order(client_id, plan.symbol, params)
        except Exception:  # noqa: BLE001 - not found is a normal state here
            return None
        return {
            "id": order.get("id"),
            "status": order.get("status", "open"),
            "price": order.get("average") or order.get("price"),
        }


class AlpacaVenue:
    name = "alpaca"

    def __init__(self) -> None:
        self.key = env("ALPACA_API_KEY")
        self.secret = env("ALPACA_SECRET_KEY")
        self.paper = env("ALPACA_PAPER_TRADE", "true").lower() == "true"
        self.host = "https://paper-api.alpaca.markets" if self.paper else "https://api.alpaca.markets"

    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        import urllib.error
        import urllib.request

        if not self.key or not self.secret:
            raise VenueError("faltan ALPACA_API_KEY / ALPACA_SECRET_KEY")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.host}{path}",
            data=data,
            method=method,
            headers={
                "APCA-API-KEY-ID": self.key,
                "APCA-API-SECRET-KEY": self.secret,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code == 404:
                return None
            raise VenueError(f"HTTP {exc.code}: {detail}") from exc

    def price(self, symbol: str) -> float:
        position = self.position(symbol)
        if not position:
            raise VenueError(f"sin posición abierta para {symbol}")
        return float(position["current_price"])

    def position(self, symbol: str) -> dict | None:
        return self._request("GET", f"/v2/positions/{symbol}")

    def position_qty(self, symbol: str) -> float | None:
        position = self.position(symbol)
        if not position:
            return 0.0
        return abs(float(position.get("qty", 0)))

    def close(self, plan: Plan, client_id: str) -> dict:
        order = self._request(
            "POST",
            "/v2/orders",
            {
                "symbol": plan.symbol,
                "qty": str(abs(plan.qty)),
                "side": close_side(plan),
                "type": "market",
                "time_in_force": "day",
                "client_order_id": client_id,
            },
        )
        return {
            "id": order.get("id"),
            "status": order.get("status", "new"),
            "price": order.get("filled_avg_price"),
        }

    def order_status(self, plan: Plan, client_id: str) -> dict | None:
        return self._request("GET", f"/v2/orders:by_client_id/{client_id}")


def build_venue(name: str):
    if name == "ccxt":
        return CcxtVenue()
    if name == "alpaca":
        return AlpacaVenue()
    raise ValueError(f"venue desconocido: {name}")


# ─── ENGINE ───────────────────────────────────────────
def venue_is_live(venue) -> bool:
    """True when the venue can move real money."""
    if getattr(venue, "sandbox", False):
        return False
    if isinstance(venue, AlpacaVenue) and venue.paper:
        return False
    return True


def can_send(venue, allow_live: bool, dry_run: bool) -> tuple[bool, str]:
    if dry_run:
        return False, "dry-run"
    if venue_is_live(venue) and not allow_live:
        return False, "live venue sin RISK_ENGINE_LIVE=true"
    return True, ""


def process_plan(
    plan: Plan,
    venue,
    *,
    allow_live: bool = False,
    dry_run: bool = False,
    close_on_target: bool = True,
    not_found_grace: int = 15,
) -> str:
    """Advance one plan by one cycle. Returns a short action label.

    The function is deliberately synchronous and side-effect free except for
    mutating ``plan`` and calling the venue, so it can be tested with a fake.
    """
    if plan.status in FINAL_STATES:
        return "skip"

    # 1. A close was already sent: only reconcile its final state.
    if plan.status == "closing":
        status = venue.order_status(plan, plan.close_client_id or close_client_id(plan))
        if status is None:
            sent_at = plan.close_sent_at or plan.opened_at
            try:
                age = (utcnow() - datetime.fromisoformat(sent_at)).total_seconds()
            except ValueError:
                age = not_found_grace + 1
            if age < not_found_grace:
                return "wait"
            # The order never landed: retry, but not forever.
            plan.attempts += 1
            if plan.attempts >= 3:
                plan.status = "failed"
                plan.last_error = "close order never landed after 3 attempts"
                log_event(plan, "close_failed", attempts=plan.attempts)
                return "failed"
            plan.status = "open"
            plan.last_error = "close order not found, retrying"
            log_event(plan, "retry_close", reason="not_found", attempts=plan.attempts)
            return "retry"

        state = (status.get("status") or "").lower()
        if state in ("closed", "filled"):
            plan.status = "closed"
            plan.closed_at = iso()
            plan.exit_price = status.get("price")
            plan.close_order_id = status.get("id") or plan.close_order_id
            log_event(plan, "closed", exit_price=plan.exit_price, order_id=plan.close_order_id)
            return "closed"
        if state in ("canceled", "cancelled", "expired", "rejected"):
            plan.attempts += 1
            plan.status = "failed" if plan.attempts >= 3 else "open"
            plan.last_error = f"close order {state}"
            log_event(plan, "close_rejected", state=state, attempts=plan.attempts)
            return "rejected"
        return "wait"

    # 2. A position that no longer exists cannot be protected.
    qty = venue.position_qty(plan.symbol)
    if qty is not None and qty <= 0:
        plan.status = "orphan"
        plan.closed_at = iso()
        plan.last_error = "sin posición en el venue"
        log_event(plan, "orphan")
        return "orphan"

    # 3. Fetch price.
    try:
        price = venue.price(plan.symbol)
    except Exception as exc:  # noqa: BLE001 - a price hiccup must not kill the loop
        plan.last_error = f"price: {exc}"
        log_event(plan, "price_error", error=str(exc))
        return "price_error"

    plan.last_price = price

    # 4. Risk triggers.
    trigger: str | None = None
    if stop_breached(plan, price):
        trigger = "stop"
    elif close_on_target and target_reached(plan, price):
        trigger = "target"

    if trigger is None:
        return "ok"

    allowed, reason = can_send(venue, allow_live, dry_run)
    if not allowed:
        plan.last_error = f"{trigger} breached at {price}, blocked: {reason}"
        # Do not flood the log: one line per 10 minutes is enough to notice.
        recently = plan.blocked_logged_at is not None and (
            (utcnow() - datetime.fromisoformat(plan.blocked_logged_at)).total_seconds() < 600
        )
        if not recently:
            plan.blocked_logged_at = iso()
            log_event(plan, "blocked", trigger=trigger, price=price, reason=reason)
        return "blocked"

    # 5. Send the close exactly once.
    client_id = close_client_id(plan)
    plan.close_client_id = client_id
    plan.close_sent_at = iso()
    plan.status = "closing"
    try:
        order = venue.close(plan, client_id)
    except Exception as exc:  # noqa: BLE001
        # Keep status=closing: the next cycle reconciles by client id instead
        # of firing a second sell. This is the whole point of the id.
        plan.last_error = f"close failed: {exc}"
        log_event(plan, "close_error", trigger=trigger, error=str(exc), client_order_id=client_id)
        return "error"

    plan.close_order_id = order.get("id")
    log_event(
        plan,
        "close_sent",
        trigger=trigger,
        price=price,
        order_id=plan.close_order_id,
        client_order_id=client_id,
    )
    return "sent"


def run_cycle(
    *,
    allow_live: bool,
    dry_run: bool,
    close_on_target: bool,
    verbose: bool = True,
    venues: dict[str, Any] | None = None,
) -> dict[str, int]:
    plans = load_plans()
    if not plans:
        return {}

    venues = venues if venues is not None else {}
    counts: dict[str, int] = {}
    for plan in plans:
        venue = venues.get(plan.venue)
        if venue is None:
            try:
                venue = build_venue(plan.venue)
            except Exception as exc:  # noqa: BLE001
                plan.last_error = f"venue: {exc}"
                log_event(plan, "venue_error", error=str(exc))
                counts["venue_error"] = counts.get("venue_error", 0) + 1
                continue
            venues[plan.venue] = venue
        outcome = process_plan(
            plan,
            venue,
            allow_live=allow_live,
            dry_run=dry_run,
            close_on_target=close_on_target,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
        if verbose and outcome not in ("ok", "skip"):
            print(f"  {plan.symbol:<14} {plan.status:<8} {outcome:<12} {plan.last_error or ''}")

    save_plans(plans)
    return counts


# ─── CLI ──────────────────────────────────────────────
def cmd_plan_add(args: argparse.Namespace) -> int:
    plan = Plan(
        id=args.id or f"p-{uuid.uuid4().hex[:10]}",
        venue=args.venue,
        symbol=args.symbol.upper() if args.market == "us-stock" else args.symbol,
        market=args.market,
        side=args.side,
        qty=args.qty,
        entry=args.entry,
        stop=args.stop,
        target=args.target,
        note=args.note or "",
    )
    try:
        validate_plan(plan)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    plans = load_plans()
    if any(p.key == plan.key and p.status == "open" for p in plans) and not args.force:
        print(
            f"error: ya existe un plan abierto para {plan.symbol} en {plan.venue} "
            "(usar --force para agregar igual)",
            file=sys.stderr,
        )
        return 1

    if args.equity:
        pct = risk_usd(plan) / args.equity * 100
        if pct > 2:
            print(
                f"error: riesgo {pct:.2f}% del equity supera el máximo de 2%",
                file=sys.stderr,
            )
            return 1
        print(f"riesgo: ${risk_usd(plan):,.2f} ({pct:.2f}% del equity)")

    plans.append(plan)
    save_plans(plans)
    log_event(plan, "plan_added", stop=plan.stop, qty=plan.qty)
    print(f"plan {plan.id} registrado: {plan.symbol} {plan.side} qty={plan.qty} stop={plan.stop}")
    return 0


def cmd_plan_list(args: argparse.Namespace) -> int:
    plans = load_plans()
    if not plans:
        print("sin planes registrados")
        return 0
    print(f"{'id':<14} {'símbolo':<12} {'venue':<7} {'lado':<6} {'qty':>10} {'stop':>10} {'estado':<8} error")
    for plan in plans:
        print(
            f"{plan.id:<14} {plan.symbol:<12} {plan.venue:<7} {plan.side:<6} "
            f"{plan.qty:>10.6g} {plan.stop:>10.6g} {plan.status:<8} {plan.last_error or ''}"
        )
    return 0


def cmd_plan_remove(args: argparse.Namespace) -> int:
    plans = load_plans()
    for plan in plans:
        if plan.id == args.id:
            plan.status = "canceled"
            plan.closed_at = iso()
            plan.note = args.reason or plan.note
            save_plans(plans)
            log_event(plan, "plan_canceled", reason=args.reason)
            print(f"plan {plan.id} cancelado ({args.reason or 'sin motivo'})")
            return 0
    print(f"error: plan {args.id} no encontrado", file=sys.stderr)
    return 1


def cmd_check(args: argparse.Namespace) -> int:
    try:
        plans = load_plans()
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: no se pudo leer {PLANS_FILE}: {exc}", file=sys.stderr)
        return 1
    problems = 0
    active = [p for p in plans if p.status not in FINAL_STATES]
    for plan in active:
        try:
            validate_plan(plan)
        except ValueError as exc:
            problems += 1
            print(f"INVALIDO {plan.id} {plan.symbol}: {exc}")
    print(f"{len(plans)} planes · {len(active)} activos · {problems} inválidos")
    if HALT_FILE.exists():
        print("HALT presente: nuevas entradas bloqueadas (los stops siguen activos)")
    return 1 if problems else 0


def cmd_run(args: argparse.Namespace) -> int:
    allow_live = env("RISK_ENGINE_LIVE", "false").lower() == "true"
    print(
        f"risk engine · intervalo={args.interval}s · "
        f"live={'sí' if allow_live else 'no'} · dry-run={'sí' if args.dry_run else 'no'}"
    )
    if HALT_FILE.exists():
        print("HALT presente: no se bloquean los stops, solo las entradas nuevas")

    venues: dict[str, Any] = {}
    while True:
        try:
            counts = run_cycle(
                allow_live=allow_live,
                dry_run=args.dry_run,
                close_on_target=not args.no_take_profit,
                venues=venues,
            )
            if counts:
                summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
                print(f"[{iso()}] {summary}")
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            print(f"[{iso()}] ERROR: {exc}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Motor de stop-loss para posiciones abiertas.")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("plan", help="gestionar planes de posición")
    add_sub = add.add_subparsers(dest="plan_command", required=True)

    plan_add = add_sub.add_parser("add", help="registrar un plan")
    plan_add.add_argument("--symbol", required=True)
    plan_add.add_argument("--venue", choices=VENUES, default="ccxt")
    plan_add.add_argument("--market", choices=MARKETS, default="crypto")
    plan_add.add_argument("--side", choices=SIDES, default="long")
    plan_add.add_argument("--qty", type=float, required=True)
    plan_add.add_argument("--entry", type=float, required=True)
    plan_add.add_argument("--stop", type=float, required=True)
    plan_add.add_argument("--target", type=float, default=None)
    plan_add.add_argument("--equity", type=float, default=None, help="valida el límite de 2%")
    plan_add.add_argument("--id", default=None)
    plan_add.add_argument("--note", default="")
    plan_add.add_argument("--force", action="store_true")
    plan_add.set_defaults(func=cmd_plan_add)

    plan_list = add_sub.add_parser("list", help="listar planes")
    plan_list.set_defaults(func=cmd_plan_list)

    plan_remove = add_sub.add_parser("remove", help="cancelar un plan")
    plan_remove.add_argument("--id", required=True)
    plan_remove.add_argument("--reason", default="")
    plan_remove.set_defaults(func=cmd_plan_remove)

    check = sub.add_parser("check", help="validar los planes registrados")
    check.set_defaults(func=cmd_check)

    run = sub.add_parser("run", help="ejecutar el motor")
    run.add_argument("--once", action="store_true", help="un solo ciclo")
    run.add_argument(
        "--interval",
        type=int,
        default=env_int("RISK_POLL_INTERVAL", 60),
        help="segundos entre ciclos (default 60)",
    )
    run.add_argument("--dry-run", action="store_true", help="no envía órdenes")
    run.add_argument("--no-take-profit", action="store_true", help="solo stops, ignorar targets")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
