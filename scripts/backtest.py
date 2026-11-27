#!/usr/bin/env python3
"""Replay collector snapshots and simulate the consensus strategy.

The collector stores a snapshot every 5 minutes: prices, the signal feed and
macro signals. This script walks them in chronological order, applies the rules
and reports what would have happened, with fees and slippage.

Rules (same shape as the scanner):
    entry   >= N distinct agents buying the same symbol inside the window
    filter  macro bullish_count >= M (0 disables it)
    exit    stop, target or max holding time
    size    fixed notional per trade

Usage:
    python scripts/backtest.py
    python scripts/backtest.py --consensus 3 --stop 1.5 --take 3
    python scripts/backtest.py --sweep
    python scripts/backtest.py --json

No lookahead: a signal is only visible from the snapshot that reported it and
entries fill at that snapshot's price.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from env import env, load_env  # noqa: E402

DEFAULT_DIR = ROOT / ".opencode" / "knowledge" / "market-data"
BUY_SIDES = {"buy", "long"}


@dataclass
class Params:
    consensus: int = 2
    window_hours: float = 6.0
    macro_min: int = 0
    stop_pct: float = 2.0
    take_pct: float = 4.0
    max_hold_hours: float = 72.0
    reentry_cooldown_hours: float = 6.0
    notional: float = 1_000.0
    fee_pct: float = 0.1
    fee_schedule: dict[str, float] = field(default_factory=dict)
    slippage_pct: float = 0.05
    start_equity: float = 10_000.0


@dataclass
class Position:
    symbol: str
    market: str
    entry: float
    qty: float
    stop: float
    target: float
    opened_at: datetime


@dataclass
class Result:
    params: Params
    trades: list[Any] = field(default_factory=list)
    metrics: Any = None
    snapshots: int = 0
    signals: int = 0
    first: datetime | None = None
    last: datetime | None = None


# ─── LOADING ──────────────────────────────────────────
def _parse_dt(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def load_snapshots(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        raise FileNotFoundError(f"no existe el directorio de snapshots: {directory}")
    snapshots: list[dict[str, Any]] = []
    for path in directory.rglob("snapshot_*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("collected_at"):
            continue
        data["_path"] = str(path)
        snapshots.append(data)
    snapshots.sort(key=lambda item: _parse_dt(item["collected_at"]))
    return snapshots


def _signal_key(signal: dict[str, Any]) -> Any:
    if signal.get("id") is not None:
        return ("id", signal["id"])
    return (
        signal.get("agent_id"),
        signal.get("symbol"),
        signal.get("side"),
        signal.get("created_at") or signal.get("timestamp"),
    )


def _signal_symbols(signal: dict[str, Any]) -> list[str]:
    if signal.get("symbol"):
        return [str(signal["symbol"]).upper()]
    return [str(item).upper() for item in (signal.get("symbols") or []) if item]


def tracked_symbols(snapshots: list[dict[str, Any]]) -> list[str]:
    """Crypto base symbols present in the snapshots (BTC, ETH, ...)."""
    symbols: set[str] = set()
    for snapshot in snapshots:
        for item in (snapshot.get("prices") or {}).values():
            if not isinstance(item, dict) or item.get("market") != "crypto":
                continue
            symbol = str(item.get("symbol") or "").upper()
            if symbol:
                symbols.add(symbol)
    return sorted(symbols)


def load_fee_schedule(
    symbols: list[str],
    exchange_id: str = "binance",
    quote: str = "USDT",
) -> dict[str, float]:
    """Taker fee per base symbol, read from the venue's market metadata."""
    try:
        import ccxt
    except ImportError:
        return {}
    factory = getattr(ccxt, exchange_id, None)
    if factory is None:
        return {}
    exchange = factory({"enableRateLimit": True})
    exchange.load_markets()
    schedule: dict[str, float] = {}
    for symbol in symbols:
        market = exchange.markets.get(f"{symbol.upper()}/{quote}")
        if market and market.get("taker") is not None:
            schedule[symbol.upper()] = float(market["taker"])
    return schedule


def fee_rate(params: Params, symbol: str) -> float:
    """Fraction per side: the venue fee when known, the fixed default otherwise."""
    known = params.fee_schedule.get(str(symbol).upper())
    if known is not None:
        return known
    return params.fee_pct / 100


def _prices_from(snapshot: dict[str, Any]) -> dict[tuple[str, str], float]:
    prices: dict[tuple[str, str], float] = {}
    for item in (snapshot.get("prices") or {}).values():
        if not isinstance(item, dict):
            continue
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            continue
        if price > 0 and item.get("symbol"):
            prices[(str(item["symbol"]).upper(), str(item.get("market", "")))] = price
    return prices


# ─── ENGINE ───────────────────────────────────────────
def consensus(history: list[tuple[datetime, str, int, str]], now: datetime, window_hours: float) -> dict[tuple[str, str], int]:
    """Distinct agents buying each symbol inside the window."""
    cutoff = now - timedelta(hours=window_hours)
    agents: dict[tuple[str, str], set[int]] = {}
    for observed_at, symbol, market, agent_id in history:
        if observed_at < cutoff:
            continue
        agents.setdefault((symbol, market), set()).add(agent_id)
    return {key: len(value) for key, value in agents.items()}


def run_backtest(snapshots: list[dict[str, Any]], params: Params) -> Result:
    import metrics  # local import: keeps the module importable without the script dir

    result = Result(params=params, snapshots=len(snapshots))
    if not snapshots:
        result.metrics = metrics.compute_metrics([], params.start_equity)
        return result

    prices: dict[tuple[str, str], float] = {}
    open_positions: dict[tuple[str, str], Position] = {}
    last_exit: dict[tuple[str, str], datetime] = {}
    seen: set[Any] = set()
    history: list[tuple[datetime, str, str, int]] = []
    trades: list[Any] = []
    last_time: datetime | None = None

    for snapshot in snapshots:
        now = _parse_dt(snapshot["collected_at"])
        if result.first is None:
            result.first = now
        result.last = now
        last_time = now

        prices.update(_prices_from(snapshot))

        feed = snapshot.get("signal_feed") or {}
        for signal in feed.get("signals", []) if isinstance(feed, dict) else []:
            key = _signal_key(signal)
            if key in seen:
                continue
            seen.add(key)
            if str(signal.get("side") or "").lower() not in BUY_SIDES:
                continue
            market = str(signal.get("market") or "crypto")
            agent = signal.get("agent_id")
            if agent is None:
                continue
            # Observed time, not creation time: this is the first snapshot in
            # which the signal could have influenced a decision.
            for symbol in _signal_symbols(signal):
                history.append((now, symbol, market, int(agent)))
                result.signals += 1

        # Exits first: a stop can trigger on the same snapshot as an entry
        # signal, and the position must be closed before opening another.
        for key in list(open_positions):
            position = open_positions[key]
            price = prices.get(key)
            if price is None:
                continue
            exit_price: float | None = None
            reason = ""
            if price <= position.stop:
                exit_price, reason = position.stop, "stop"
            elif price >= position.target:
                exit_price, reason = position.target, "target"
            elif (now - position.opened_at) >= timedelta(hours=params.max_hold_hours):
                exit_price, reason = price, "time"
            if exit_price is None:
                continue

            exit_price *= 1 - params.slippage_pct / 100
            fees = (position.entry + exit_price) * position.qty * fee_rate(params, position.symbol)
            trades.append(
                metrics.Trade(
                    id=f"bt-{len(trades) + 1}",
                    closed_at=now,
                    side="long",
                    qty=position.qty,
                    entry=position.entry,
                    exit=exit_price,
                    opened_at=position.opened_at,
                    symbol=position.symbol,
                    market=position.market,
                    fees=fees,
                    stop=position.stop,
                    target=position.target,
                    notes=f"backtest:{reason}",
                )
            )
            last_exit[key] = now
            del open_positions[key]

        # Entries.
        counts = consensus(history, now, params.window_hours)
        macro = snapshot.get("macro_signals") or {}
        bullish = int(macro.get("bullish_count") or 0)
        macro_ok = params.macro_min <= 0 or bullish >= params.macro_min

        for key, count in counts.items():
            if count < params.consensus or not macro_ok or key in open_positions:
                continue
            previous = last_exit.get(key)
            if previous is not None and (
                now - previous < timedelta(hours=params.reentry_cooldown_hours)
            ):
                continue
            price = prices.get(key)
            if price is None or price <= 0:
                continue
            entry = price * (1 + params.slippage_pct / 100)
            qty = params.notional / entry
            open_positions[key] = Position(
                symbol=key[0],
                market=key[1],
                entry=entry,
                qty=qty,
                stop=entry * (1 - params.stop_pct / 100),
                target=entry * (1 + params.take_pct / 100),
                opened_at=now,
            )

    # Anything still open is marked to the last known price.
    if last_time is not None:
        for key, position in list(open_positions.items()):
            price = prices.get(key)
            if price is None:
                continue
            exit_price = price * (1 - params.slippage_pct / 100)
            fees = (position.entry + exit_price) * position.qty * fee_rate(params, position.symbol)
            trades.append(
                metrics.Trade(
                    id=f"bt-{len(trades) + 1}",
                    closed_at=last_time,
                    side="long",
                    qty=position.qty,
                    entry=position.entry,
                    exit=exit_price,
                    opened_at=position.opened_at,
                    symbol=position.symbol,
                    market=position.market,
                    fees=fees,
                    stop=position.stop,
                    target=position.target,
                    notes="backtest:open_at_end",
                )
            )
            del open_positions[key]

    result.trades = trades
    result.metrics = metrics.compute_metrics(trades, params.start_equity)
    return result


# ─── RENDERING ────────────────────────────────────────
def warnings_for(result: Result) -> list[str]:
    warnings: list[str] = []
    if result.first and result.last:
        days = (result.last - result.first).total_seconds() / 86400
        if days < 7:
            warnings.append(f"el rango cubre {days:.1f} días: muestra insuficiente para concluir")
    if result.trades:
        at_end = sum(1 for t in result.trades if "open_at_end" in (t.notes or ""))
        if at_end * 2 >= len(result.trades):
            warnings.append(
                f"{at_end}/{len(result.trades)} operaciones se cerraron al final del rango, "
                "sin tiempo para resolverse"
            )
    return warnings


def render_summary(result: Result) -> str:
    m = result.metrics
    span = "sin datos"
    if result.first and result.last:
        span = f"{result.first:%Y-%m-%d %H:%M} → {result.last:%Y-%m-%d %H:%M}"
    lines = [
        "Backtest de consenso",
        "--------------------",
        f"Snapshots        {result.snapshots}",
        f"Rango            {span}",
        f"Señales de compra {result.signals}",
        "",
        f"Entradas         {m.closed}",
        f"Win rate         {m.win_rate:.1f}%",
        f"Profit factor    {'—' if m.profit_factor is None else f'{m.profit_factor:.2f}'}",
        f"PnL total        ${m.total_pnl:,.2f}",
        f"Max drawdown     {m.max_drawdown_pct:.2f}%",
        f"R medio          {'—' if m.avg_r is None else f'{m.avg_r:.2f}'}",
        "",
        "Parámetros",
        f"  consenso={result.params.consensus} ventana={result.params.window_hours:g}h "
        f"macro>={result.params.macro_min}",
        f"  stop={result.params.stop_pct:g}% target={result.params.take_pct:g}% "
        f"hold={result.params.max_hold_hours:g}h cooldown={result.params.reentry_cooldown_hours:g}h",
        f"  notional=${result.params.notional:,.0f} fee={result.params.fee_pct:g}% "
        f"slippage={result.params.slippage_pct:g}%",
    ]
    if result.params.fee_schedule:
        known = ", ".join(
            f"{symbol} {rate * 100:.3f}%" for symbol, rate in sorted(result.params.fee_schedule.items())
        )
        lines.append(f"  fees por exchange: {known}")
    if result.trades:
        lines += ["", "Primeras operaciones"]
        for trade in result.trades[:5]:
            lines.append(
                f"  {trade.closed_at:%Y-%m-%d %H:%M} {trade.symbol:<6} "
                f"{trade.return_pct:+.2f}%  ({trade.notes})"
            )
    warnings = warnings_for(result)
    if warnings:
        lines += ["", "Advertencias"] + [f"  - {warning}" for warning in warnings]
    return "\n".join(lines)


def sweep_rows(snapshots: list[dict[str, Any]], base: Params) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for count in (1, 2, 3):
        for macro_min in (0, 3):
            params = Params(**{**asdict(base), "consensus": count, "macro_min": macro_min})
            result = run_backtest(snapshots, params)
            metrics = result.metrics
            rows.append(
                {
                    "consensus": count,
                    "macro_min": macro_min,
                    "trades": metrics.closed,
                    "win_rate": round(metrics.win_rate, 1),
                    "profit_factor": None if metrics.profit_factor is None else round(metrics.profit_factor, 2),
                    "pnl": round(metrics.total_pnl, 2),
                    "max_dd": round(metrics.max_drawdown_pct, 2),
                }
            )
    return rows


def render_sweep(rows: list[dict[str, Any]]) -> str:
    header = f"{'cons':>5} {'macro':>6} {'trades':>7} {'WR%':>6} {'PF':>6} {'PnL':>10} {'DD%':>6}"
    lines = ["Sensibilidad de parámetros", header, "-" * len(header)]
    for row in rows:
        pf = "—" if row["profit_factor"] is None else f"{row['profit_factor']:.2f}"
        lines.append(
            f"{row['consensus']:>5} {row['macro_min']:>6} {row['trades']:>7} "
            f"{row['win_rate']:>6.1f} {pf:>6} {row['pnl']:>10.2f} {row['max_dd']:>6.2f}"
        )
    return "\n".join(lines)


# ─── CLI ──────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backtest sobre snapshots del collector.")
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--consensus", type=int, default=2)
    parser.add_argument("--window", type=float, default=6.0, help="horas de ventana para el consenso")
    parser.add_argument("--macro-min", type=int, default=0, help="bullish_count mínimo (0 = sin filtro)")
    parser.add_argument("--stop", type=float, default=2.0, help="% de stop")
    parser.add_argument("--take", type=float, default=4.0, help="% de objetivo")
    parser.add_argument("--max-hold", type=float, default=72.0, help="horas máximas en posición")
    parser.add_argument("--cooldown", type=float, default=6.0, help="horas sin reentrar tras cerrar")
    parser.add_argument("--notional", type=float, default=1_000.0)
    parser.add_argument("--fee", type=float, default=0.1, help="% por lado (modo fijo)")
    parser.add_argument(
        "--fees",
        choices=("fixed", "exchange"),
        default="fixed",
        help="usar el fee fijo o leer el taker del exchange",
    )
    parser.add_argument("--slippage", type=float, default=0.05, help="% por lado")
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--sweep", action="store_true", help="probar consenso 1-3 × macro 0/3")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    load_env()
    args = build_parser().parse_args(argv)

    try:
        snapshots = load_snapshots(Path(args.dir))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    params = Params(
        consensus=args.consensus,
        window_hours=args.window,
        macro_min=args.macro_min,
        stop_pct=args.stop,
        take_pct=args.take,
        max_hold_hours=args.max_hold,
        reentry_cooldown_hours=args.cooldown,
        notional=args.notional,
        fee_pct=args.fee,
        slippage_pct=args.slippage,
        start_equity=args.equity,
    )

    if args.fees == "exchange":
        exchange_id = env("CCXT_EXCHANGE", "binance")
        try:
            params.fee_schedule = load_fee_schedule(tracked_symbols(snapshots), exchange_id)
        except Exception as exc:  # noqa: BLE001 - the backtest must run offline
            print(
                f"aviso: no se pudieron leer fees de {exchange_id} ({exc}); se usa --fee",
                file=sys.stderr,
            )
        if not params.fee_schedule:
            print("aviso: sin fees del exchange; se usa el fee fijo", file=sys.stderr)

    if args.sweep:
        rows = sweep_rows(snapshots, params)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(render_sweep(rows))
        return 0

    result = run_backtest(snapshots, params)
    if args.json:
        payload = {
            "params": asdict(params),
            "snapshots": result.snapshots,
            "signals": result.signals,
            "trades": [asdict(trade) for trade in result.trades],
            "metrics": {
                **asdict(result.metrics),
                "equity_curve": None,
            },
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(render_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
