#!/usr/bin/env python3
"""Trade journal metrics and dashboard generation.

The journal is a CSV so the arithmetic is reproducible:

    state/trades.csv

Required columns: id, closed_at, side, qty, entry, exit.
Optional: opened_at, symbol, market, fees, stop, target, risk_pct, thesis, notes.

Usage:
    python scripts/metrics.py report
    python scripts/metrics.py report --json
    python scripts/metrics.py dashboard --out .opencode/knowledge/DASHBOARD.md
    python scripts/metrics.py dashboard --offline

Verdicts follow docs/trading-modes.md:

    insufficient_sample  fewer than 50 closed trades
    promote              every threshold met
    degrade              drawdown >= 15% or profit factor < 1.0 or win rate < 45%
    hold                 anything else

Exit codes for ``report``:
    0  metrics computed
    1  the journal is missing or malformed
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from env import env, load_env  # noqa: E402

DEFAULT_JOURNAL = ROOT / "state" / "trades.csv"
DEFAULT_DASHBOARD = ROOT / ".opencode" / "knowledge" / "DASHBOARD.md"
AI_TRADER_BASE = "https://ai4trade.ai/api"

REQUIRED_COLUMNS = ("id", "closed_at", "side", "qty", "entry", "exit")

THRESHOLDS = {
    "closed": 50,
    "win_rate": 55.0,
    "profit_factor": 1.5,
    "sharpe": 1.0,
    "max_drawdown": 10.0,
    "degrade_drawdown": 15.0,
    "degrade_profit_factor": 1.0,
    "degrade_win_rate": 45.0,
    "min_active_days": 20,
}


# ─── JOURNAL ──────────────────────────────────────────
@dataclass
class Trade:
    id: str
    closed_at: datetime
    side: str
    qty: float
    entry: float
    exit: float
    opened_at: datetime | None = None
    symbol: str = ""
    market: str = ""
    fees: float = 0.0
    stop: float | None = None
    target: float | None = None
    risk_pct: float | None = None
    thesis: str = ""
    notes: str = ""

    @property
    def pnl(self) -> float:
        gross = (self.exit - self.entry) * self.qty
        if self.side.lower() == "short":
            gross = -gross
        return gross - self.fees

    @property
    def notional(self) -> float:
        return abs(self.entry * self.qty)

    @property
    def return_pct(self) -> float:
        return (self.pnl / self.notional * 100) if self.notional else 0.0

    @property
    def r_multiple(self) -> float | None:
        if self.stop is None or self.qty == 0:
            return None
        risk = abs(self.entry - self.stop) * self.qty
        return self.pnl / risk if risk else None

    @property
    def duration_hours(self) -> float | None:
        if self.opened_at is None:
            return None
        return (self.closed_at - self.opened_at).total_seconds() / 3600


def _parse_dt(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _num(row: dict[str, str], key: str, required: bool = False) -> float | None:
    raw = (row.get(key) or "").strip()
    if not raw:
        if required:
            raise ValueError(f"columna '{key}' vacía en la fila {row.get('id', '?')}")
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"'{key}' no es numérico en la fila {row.get('id', '?')}: {raw}") from exc


def load_trades(path: Path) -> list[Trade]:
    if not path.is_file():
        raise FileNotFoundError(f"no existe el journal: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"faltan columnas: {', '.join(missing)}")
        trades: list[Trade] = []
        for row in reader:
            if not (row.get("id") or "").strip():
                continue
            opened = (row.get("opened_at") or "").strip()
            trades.append(
                Trade(
                    id=row["id"].strip(),
                    closed_at=_parse_dt(row["closed_at"]),
                    side=(row.get("side") or "long").strip(),
                    qty=_num(row, "qty", required=True) or 0.0,
                    entry=_num(row, "entry", required=True) or 0.0,
                    exit=_num(row, "exit", required=True) or 0.0,
                    opened_at=_parse_dt(opened) if opened else None,
                    symbol=(row.get("symbol") or "").strip(),
                    market=(row.get("market") or "").strip(),
                    fees=_num(row, "fees") or 0.0,
                    stop=_num(row, "stop"),
                    target=_num(row, "target"),
                    risk_pct=_num(row, "risk_pct"),
                    thesis=(row.get("thesis") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return sorted(trades, key=lambda t: t.closed_at)


# ─── METRICS ──────────────────────────────────────────
@dataclass
class Metrics:
    closed: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    profit_factor: float | None
    total_pnl: float
    avg_win: float
    avg_loss: float
    expectancy: float
    avg_r: float | None
    avg_duration_hours: float | None
    max_drawdown_pct: float
    sharpe: float | None
    active_days: int
    start_equity: float
    end_equity: float
    equity_curve: list[float] = field(default_factory=list)


def compute_metrics(trades: list[Trade], start_equity: float = 100_000.0) -> Metrics:
    closed = [t for t in trades]
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl < 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    total_pnl = sum(t.pnl for t in closed)

    equity = start_equity
    curve = [equity]
    peak = equity
    max_dd = 0.0
    daily_pnl: dict[str, float] = {}
    daily_start: dict[str, float] = {}
    for trade in closed:
        day = trade.closed_at.date().isoformat()
        daily_start.setdefault(day, equity)
        daily_pnl[day] = daily_pnl.get(day, 0.0) + trade.pnl
        equity += trade.pnl
        curve.append(equity)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)

    returns: list[float] = []
    for day in sorted(daily_pnl):
        base = daily_start[day]
        if base > 0:
            returns.append(daily_pnl[day] / base)

    sharpe: float | None = None
    if len(returns) >= 2:
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(variance)
        if std > 0:
            sharpe = mean / std * math.sqrt(252)

    r_values = [r for r in (t.r_multiple for t in closed) if r is not None]
    durations = [d for d in (t.duration_hours for t in closed) if d is not None]

    return Metrics(
        closed=len(closed),
        wins=len(wins),
        losses=len(losses),
        win_rate=(len(wins) / len(closed) * 100) if closed else 0.0,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        total_pnl=total_pnl,
        avg_win=(gross_profit / len(wins)) if wins else 0.0,
        avg_loss=(gross_loss / len(losses)) if losses else 0.0,
        expectancy=(total_pnl / len(closed)) if closed else 0.0,
        avg_r=(sum(r_values) / len(r_values)) if r_values else None,
        avg_duration_hours=(sum(durations) / len(durations)) if durations else None,
        max_drawdown_pct=max_dd,
        sharpe=sharpe,
        active_days=len(daily_pnl),
        start_equity=start_equity,
        end_equity=equity,
        equity_curve=curve,
    )


def verdict(metrics: Metrics) -> tuple[str, list[str]]:
    """Return (verdict, reasons) following the promotion thresholds."""
    reasons: list[str] = []
    if metrics.closed < THRESHOLDS["closed"]:
        return "insufficient_sample", [
            f"{metrics.closed}/{THRESHOLDS['closed']} operaciones cerradas"
        ]

    promote_ok = True
    if metrics.win_rate <= THRESHOLDS["win_rate"]:
        promote_ok = False
        reasons.append(f"win rate {metrics.win_rate:.1f}% <= {THRESHOLDS['win_rate']}%")
    if metrics.profit_factor is not None and metrics.profit_factor <= THRESHOLDS["profit_factor"]:
        promote_ok = False
        reasons.append(f"profit factor {metrics.profit_factor:.2f} <= {THRESHOLDS['profit_factor']}")
    if metrics.max_drawdown_pct >= THRESHOLDS["max_drawdown"]:
        promote_ok = False
        reasons.append(f"drawdown {metrics.max_drawdown_pct:.1f}% >= {THRESHOLDS['max_drawdown']}%")
    if metrics.sharpe is None:
        promote_ok = False
        reasons.append("Sharpe no calculable todavía (faltan días con actividad)")
    elif metrics.sharpe <= THRESHOLDS["sharpe"]:
        promote_ok = False
        reasons.append(f"Sharpe {metrics.sharpe:.2f} <= {THRESHOLDS['sharpe']}")
    if metrics.active_days < THRESHOLDS["min_active_days"]:
        promote_ok = False
        reasons.append(
            f"{metrics.active_days} días con actividad < {THRESHOLDS['min_active_days']}"
        )

    if promote_ok:
        return "promote", ["todas las métricas cumplen los umbrales"]

    if (
        metrics.max_drawdown_pct >= THRESHOLDS["degrade_drawdown"]
        or (metrics.profit_factor is not None and metrics.profit_factor < THRESHOLDS["degrade_profit_factor"])
        or metrics.win_rate < THRESHOLDS["degrade_win_rate"]
    ):
        return "degrade", reasons
    return "hold", reasons


# ─── RENDERING ────────────────────────────────────────
def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def fmt_optional(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def render_report(metrics: Metrics, decision: str, reasons: list[str]) -> str:
    lines = [
        "Métricas del journal",
        "--------------------",
        f"Operaciones cerradas   {metrics.closed}",
        f"Ganadoras / perdedoras {metrics.wins} / {metrics.losses}",
        f"Win rate               {metrics.win_rate:.1f}%",
        f"Profit factor          {fmt_optional(metrics.profit_factor)}",
        f"PnL total              {fmt_money(metrics.total_pnl)}",
        f"PnL medio (expectancy) {fmt_money(metrics.expectancy)}",
        f"R medio                {fmt_optional(metrics.avg_r)}",
        f"Duración media         {fmt_optional(metrics.avg_duration_hours, 1)} h",
        f"Max drawdown           {metrics.max_drawdown_pct:.2f}%",
        f"Sharpe (anualizado)    {fmt_optional(metrics.sharpe)}",
        f"Días con actividad     {metrics.active_days}",
        f"Equity                 {fmt_money(metrics.start_equity)} -> {fmt_money(metrics.end_equity)}",
        "",
        f"Veredicto: {decision.upper()}",
    ]
    lines.extend(f"  - {reason}" for reason in reasons)
    return "\n".join(lines)


def _http_json(path: str, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(f"{AI_TRADER_BASE}{path}", headers=headers or {})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def fetch_account() -> dict[str, Any] | None:
    token = env("CLIPTAP_AI_TOKEN")
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        me = _http_json("/claw/agents/me", headers)
        positions = _http_json("/positions", headers)
    except Exception:  # noqa: BLE001 - dashboard must render even offline
        return None
    return {"me": me, "positions": positions}


def fetch_price(symbol: str, market: str) -> float | None:
    token = env("CLIPTAP_AI_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        data = _http_json(f"/price?symbol={symbol}&market={market}", headers)
    except Exception:  # noqa: BLE001
        return None
    price = data.get("price") if isinstance(data, dict) else None
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


def render_dashboard(
    metrics: Metrics,
    decision: str,
    reasons: list[str],
    account: dict[str, Any] | None,
    plans: list[dict[str, Any]],
    prices: dict[tuple[str, str], float | None] | None = None,
    now: datetime | None = None,
) -> str:
    prices = prices or {}
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "<!-- generated by scripts/metrics.py — do not edit by hand -->",
        "# Dashboard",
        "",
        f"Última actualización: {stamp}",
        "",
        "## Cuenta",
        "",
    ]

    if account:
        me = account["me"]
        positions = account["positions"].get("positions", [])
        cash = float(account["positions"].get("cash") or me.get("cash") or 0)
        unrealized = 0.0
        unpriced = 0
        for position in positions:
            price = prices.get((position["symbol"], position["market"]))
            if price is None:
                unpriced += 1
                continue
            qty = float(position.get("quantity") or 0)
            entry = float(position.get("entry_price") or 0)
            unrealized += (price - entry) * qty if position.get("side") != "short" else (entry - price) * qty
        equity = cash + unrealized

        lines += [
            "| Campo | Valor |",
            "| --- | --- |",
            f"| Agente | {me.get('name', '?')} ({me.get('id', '?')}) |",
            f"| Cash | {fmt_money(cash)} |",
            f"| PnL no realizado | {fmt_money(unrealized)} |",
            f"| Equity estimada | {fmt_money(equity)} |",
            f"| Puntos | {me.get('points', '?')} |",
            "",
            "## Posiciones abiertas",
            "",
        ]
        if positions:
            lines += [
                "| Activo | Mercado | Lado | Cantidad | Entrada | Precio | PnL | Origen |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            for position in positions:
                price = prices.get((position["symbol"], position["market"]))
                qty = float(position.get("quantity") or 0)
                entry = float(position.get("entry_price") or 0)
                if price is None:
                    pnl_text = "sin precio"
                else:
                    pnl = (price - entry) * qty if position.get("side") != "short" else (entry - price) * qty
                    pnl_text = f"{fmt_money(pnl)} ({pnl / (entry * qty) * 100:+.1f}%)" if entry * qty else fmt_money(pnl)
                lines.append(
                    f"| {position['symbol']} | {position['market']} | {position.get('side', 'long')} | "
                    f"{qty:g} | ${entry:,.6g} | {'—' if price is None else f'${price:,.6g}'} | "
                    f"{pnl_text} | {position.get('source', 'self')} |"
                )
            if unpriced:
                lines.append("")
                lines.append(f"_{unpriced} posiciones sin precio disponible: no se incluyen en la equity._")
        else:
            lines.append("Sin posiciones abiertas.")
    else:
        lines += [
            "_Sin acceso a AI-Trader en esta ejecución (modo offline o token ausente)._",
            "",
        ]

    lines += [
        "",
        "## Métricas (journal)",
        "",
        "| Métrica | Valor | Umbral | Estado |",
        "| --- | --- | --- | --- |",
        f"| Operaciones cerradas | {metrics.closed} | ≥ {THRESHOLDS['closed']} | "
        f"{'OK' if metrics.closed >= THRESHOLDS['closed'] else 'falta'} |",
        f"| Win rate | {metrics.win_rate:.1f}% | > {THRESHOLDS['win_rate']:.0f}% | "
        f"{'OK' if metrics.win_rate > THRESHOLDS['win_rate'] else 'no'} |",
        f"| Profit factor | {fmt_optional(metrics.profit_factor)} | > {THRESHOLDS['profit_factor']} | "
        f"{'OK' if (metrics.profit_factor or 0) > THRESHOLDS['profit_factor'] else 'no'} |",
        f"| Sharpe | {fmt_optional(metrics.sharpe)} | > {THRESHOLDS['sharpe']} | "
        f"{'OK' if (metrics.sharpe or 0) > THRESHOLDS['sharpe'] else 'no'} |",
        f"| Max drawdown | {metrics.max_drawdown_pct:.2f}% | < {THRESHOLDS['max_drawdown']:.0f}% | "
        f"{'OK' if metrics.max_drawdown_pct < THRESHOLDS['max_drawdown'] else 'no'} |",
        f"| PnL total | {fmt_money(metrics.total_pnl)} | — | — |",
        f"| Equity del journal | {fmt_money(metrics.start_equity)} → {fmt_money(metrics.end_equity)} | — | — |",
        "",
        f"**Veredicto: `{decision}`**",
        "",
    ]
    lines.extend(f"- {reason}" for reason in reasons)

    lines += ["", "## Planes de riesgo", ""]
    active = [p for p in plans if p.get("status") in ("open", "closing")]
    if active:
        lines += [
            "| Plan | Activo | Lado | Cantidad | Stop | Estado |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for plan in active:
            lines.append(
                f"| {plan['id']} | {plan['symbol']} | {plan['side']} | {plan['qty']:g} | "
                f"${plan['stop']:,.6g} | {plan['status']} |"
            )
    else:
        lines.append("Sin posiciones registradas en el motor de riesgo.")

    lines += [
        "",
        "## Cómo se genera",
        "",
        "```bash",
        "python scripts/metrics.py dashboard --out .opencode/knowledge/DASHBOARD.md",
        "```",
        "",
    ]
    return "\n".join(lines)


def load_open_plans() -> list[dict[str, Any]]:
    try:
        import risk_engine  # local import: the engine is optional for metrics
    except Exception:  # noqa: BLE001
        return []
    try:
        return [asdict(plan) for plan in risk_engine.load_plans()]
    except Exception:  # noqa: BLE001
        return []


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if path.exists():
        print(f"ya existe: {path}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "id",
        "opened_at",
        "closed_at",
        "symbol",
        "market",
        "side",
        "qty",
        "entry",
        "exit",
        "fees",
        "stop",
        "target",
        "risk_pct",
        "thesis",
        "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(columns)
    print(f"journal creado: {path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    try:
        trades = load_trades(Path(args.file))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    metrics = compute_metrics(trades, start_equity=args.equity)
    decision, reasons = verdict(metrics)
    if args.json:
        payload = asdict(metrics)
        payload["verdict"] = decision
        payload["reasons"] = reasons
        print(json.dumps(payload, indent=2))
    else:
        print(render_report(metrics, decision, reasons))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    try:
        trades = load_trades(Path(args.file))
        metrics = compute_metrics(trades, start_equity=args.equity)
    except (OSError, ValueError) as exc:
        print(f"aviso: sin journal utilizable ({exc}); se genera solo la cuenta", file=sys.stderr)
        metrics = compute_metrics([], start_equity=args.equity)
    decision, reasons = verdict(metrics)

    account = None if args.offline else fetch_account()
    prices: dict[tuple[str, str], float | None] = {}
    if account:
        for position in account["positions"].get("positions", []):
            key = (position["symbol"], position["market"])
            prices[key] = fetch_price(*key)

    content = render_dashboard(
        metrics,
        decision,
        reasons,
        account,
        load_open_plans(),
        prices,
    )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(content + "\n", encoding="utf-8")
        tmp.replace(out)
        print(f"dashboard escrito en {out}")
    else:
        print(content)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Métricas del journal y dashboard.")
    sub = parser.add_subparsers(dest="command", required=True)

    report = sub.add_parser("report", help="métricas y veredicto")
    report.add_argument("--file", default=str(DEFAULT_JOURNAL))
    report.add_argument("--equity", type=float, default=100_000.0, help="equity inicial del journal")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=cmd_report)

    init = sub.add_parser("init", help="crear el journal vacío con sus columnas")
    init.add_argument("--file", default=str(DEFAULT_JOURNAL))
    init.set_defaults(func=cmd_init)

    dash = sub.add_parser("dashboard", help="generar DASHBOARD.md")
    dash.add_argument("--file", default=str(DEFAULT_JOURNAL))
    dash.add_argument("--out", default=None, help="ruta de salida (default: stdout)")
    dash.add_argument("--equity", type=float, default=100_000.0)
    dash.add_argument("--offline", action="store_true", help="no consultar AI-Trader")
    dash.set_defaults(func=cmd_dashboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; never crash on a symbol we print.
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
