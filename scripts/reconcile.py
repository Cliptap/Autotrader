#!/usr/bin/env python3
"""Reconcile paper positions, venue positions and the journal.

Read-only by design: it reports differences and never "fixes" anything. A
discrepancy is a decision for the operator, not for a script.

Sources:
    plans     state/positions.json      what the risk engine protects
    paper     AI-Trader /api/positions  the simulated book
    venue     CCXT balances / Alpaca positions  the real book
    journal   state/trades.csv          what actually got recorded

Usage:
    python scripts/reconcile.py
    python scripts/reconcile.py --markdown
    python scripts/reconcile.py --out .opencode/knowledge/reconciliation.md
    python scripts/reconcile.py --json

Exit codes:
    0  no differences worth acting on
    2  warnings or errors found
    1  could not read the local state
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from env import env, load_env  # noqa: E402

AI_TRADER_BASE = "https://ai4trade.ai/api"
DEFAULT_PLANS = ROOT / "state" / "positions.json"
DEFAULT_JOURNAL = ROOT / "state" / "trades.csv"
JOURNAL_WINDOW_HOURS = 24

ACTIVE_STATUSES = ("open", "closing")


@dataclass
class Finding:
    kind: str
    severity: str  # "error" | "warn" | "info"
    subject: str
    detail: str


def base_symbol(symbol: str) -> str:
    return symbol.split("/")[0].upper()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ─── CORE ─────────────────────────────────────────────
def reconcile(
    plans: list[dict[str, Any]],
    paper_positions: list[dict[str, Any]],
    venue_positions: dict[str, list[dict[str, Any]]],
    journal_trades: list[Any],
    tolerance: float = 0.005,
) -> list[Finding]:
    """Compare the four books. ``venue_positions`` maps venue -> [{symbol, qty}]."""
    findings: list[Finding] = []
    active = [plan for plan in plans if plan.get("status") in ACTIVE_STATUSES]

    venue_lookup: dict[tuple[str, str], float] = {}
    for venue, positions in venue_positions.items():
        for position in positions:
            venue_lookup[(venue, str(position["symbol"]).upper())] = float(position["qty"])

    for plan in active:
        symbol = base_symbol(plan["symbol"])
        key = (plan["venue"], symbol)
        expected = float(plan["qty"])
        actual = venue_lookup.get(key)

        if actual is None:
            findings.append(
                Finding(
                    "plan_qty_unknown",
                    "warn",
                    f"{plan['venue']}:{symbol}",
                    f"plan {plan['id']} activo pero no se pudo leer la posición en el venue",
                )
            )
        elif actual <= 0:
            findings.append(
                Finding(
                    "plan_missing_at_venue",
                    "error",
                    f"{plan['venue']}:{symbol}",
                    f"plan {plan['id']} espera {expected:g} y el venue no tiene posición",
                )
            )
        elif actual < expected * (1 - tolerance):
            findings.append(
                Finding(
                    "plan_qty_short",
                    "warn",
                    f"{plan['venue']}:{symbol}",
                    f"plan {plan['id']} espera {expected:g}, el venue tiene {actual:g} "
                    f"({(actual / expected - 1) * 100:+.1f}%)",
                )
            )
        elif actual > expected * (1 + tolerance):
            findings.append(
                Finding(
                    "plan_qty_extra",
                    "info",
                    f"{plan['venue']}:{symbol}",
                    f"plan {plan['id']} espera {expected:g}, el venue tiene {actual:g} "
                    "(posición mayor a la registrada)",
                )
            )

    # Venue positions nobody protects: only meaningful where the venue reports
    # individual positions (Alpaca). CCXT balances are not position objects.
    protected = {(plan["venue"], base_symbol(plan["symbol"])) for plan in active}
    for venue, positions in venue_positions.items():
        for position in positions:
            symbol = str(position["symbol"]).upper()
            if position["qty"] <= 0 or (venue, symbol) in protected:
                continue
            if venue == "ccxt":
                continue
            findings.append(
                Finding(
                    "venue_unmanaged",
                    "warn",
                    f"{venue}:{symbol}",
                    f"posición en el venue sin plan de riesgo (qty {position['qty']:g})",
                )
            )

    # Paper: only self-sourced positions should have a plan.
    plan_keys = {(plan["market"], base_symbol(plan["symbol"])) for plan in active}
    for position in paper_positions:
        if position.get("source") != "self":
            continue
        key = (position.get("market"), base_symbol(position.get("symbol", "")))
        if key not in plan_keys:
            findings.append(
                Finding(
                    "paper_without_plan",
                    "info",
                    f"{position.get('market')}:{position.get('symbol')}",
                    f"posición paper sin plan de riesgo (qty {float(position.get('quantity') or 0):g})",
                )
            )

    paper_keys = {
        (position.get("market"), base_symbol(position.get("symbol", "")))
        for position in paper_positions
        if position.get("source") == "self"
    }
    for plan in active:
        key = (plan["market"], base_symbol(plan["symbol"]))
        if paper_keys and key not in paper_keys:
            findings.append(
                Finding(
                    "plan_without_paper",
                    "warn",
                    f"{plan['market']}:{base_symbol(plan['symbol'])}",
                    f"plan {plan['id']} sin posición paper 'self' equivalente",
                )
            )

    # Journal: a plan the engine closed must appear in the journal.
    for plan in plans:
        if plan.get("status") != "closed":
            continue
        closed_at = _parse_dt(plan.get("closed_at"))
        if closed_at is None:
            continue
        symbol = base_symbol(plan["symbol"])
        matched = False
        for trade in journal_trades:
            trade_symbol = base_symbol(getattr(trade, "symbol", "") or "")
            trade_closed = getattr(trade, "closed_at", None)
            if trade_symbol != symbol or trade_closed is None:
                continue
            delta = abs((trade_closed - closed_at).total_seconds()) / 3600
            if delta <= JOURNAL_WINDOW_HOURS:
                matched = True
                break
        if not matched:
            findings.append(
                Finding(
                    "journal_missing",
                    "warn",
                    f"{symbol}",
                    f"plan {plan['id']} cerrado el {closed_at.isoformat(timespec='minutes')} "
                    "sin fila equivalente en el journal",
                )
            )

    return findings


# ─── GATHER ───────────────────────────────────────────
def fetch_paper_positions() -> list[dict[str, Any]] | None:
    token = env("CLIPTAP_AI_TOKEN")
    if not token:
        return None
    request = urllib.request.Request(
        f"{AI_TRADER_BASE}/positions", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data.get("positions", [])


def load_plans(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("positions", raw) if isinstance(raw, dict) else raw
    return list(items)


def load_journal(path: Path) -> list[Any]:
    import metrics

    return metrics.load_trades(path)


def gather_venue_positions(plans: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], list[Finding]]:
    import risk_engine

    findings: list[Finding] = []
    result: dict[str, list[dict[str, Any]]] = {}
    venues = {plan["venue"] for plan in plans if plan.get("status") in ACTIVE_STATUSES}

    for venue_name in venues:
        try:
            venue = risk_engine.build_venue(venue_name)
        except Exception as exc:  # noqa: BLE001
            findings.append(Finding("venue_unavailable", "warn", venue_name, str(exc)))
            continue
        positions: list[dict[str, Any]] = []
        try:
            if venue_name == "ccxt":
                seen: set[str] = set()
                for plan in plans:
                    if plan.get("status") not in ACTIVE_STATUSES or plan.get("venue") != "ccxt":
                        continue
                    symbol = base_symbol(plan["symbol"])
                    if symbol in seen:
                        continue
                    seen.add(symbol)
                    positions.append({"symbol": symbol, "qty": venue.base_balance(plan["symbol"])})
            else:
                positions.extend(venue.positions())
        except Exception as exc:  # noqa: BLE001
            findings.append(Finding("venue_unavailable", "warn", venue_name, str(exc)))
            continue
        result[venue_name] = positions

    return result, findings


# ─── RENDER ───────────────────────────────────────────
def counts(findings: list[Finding]) -> dict[str, int]:
    summary = {"error": 0, "warn": 0, "info": 0}
    for finding in findings:
        summary[finding.severity] = summary.get(finding.severity, 0) + 1
    return summary


def render_text(findings: list[Finding]) -> str:
    summary = counts(findings)
    if not findings:
        return "Reconciliación sin diferencias: planes, venue, paper y journal coinciden."
    lines = [
        f"Reconciliación: {summary['error']} errores · {summary['warn']} advertencias · "
        f"{summary['info']} informativos",
        "",
    ]
    for severity in ("error", "warn", "info"):
        group = [f for f in findings if f.severity == severity]
        if not group:
            continue
        lines.append(f"[{severity.upper()}]")
        for finding in group:
            lines.append(f"  {finding.subject:<18} {finding.kind:<22} {finding.detail}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_markdown(findings: list[Finding], generated_at: datetime | None = None) -> str:
    stamp = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    summary = counts(findings)
    lines = [
        "# Reconciliación",
        "",
        f"Generado: {stamp}",
        "",
        "| Errores | Advertencias | Informativos |",
        "| --- | --- | --- |",
        f"| {summary['error']} | {summary['warn']} | {summary['info']} |",
        "",
    ]
    if not findings:
        lines.append("Sin diferencias entre planes, venue, paper y journal.")
        return "\n".join(lines)

    lines += ["| Severidad | Sujeto | Tipo | Detalle |", "| --- | --- | --- | --- |"]
    for finding in findings:
        lines.append(
            f"| {finding.severity} | {finding.subject} | {finding.kind} | {finding.detail} |"
        )
    return "\n".join(lines)


# ─── CLI ──────────────────────────────────────────────
def cmd_run(args: argparse.Namespace) -> int:
    try:
        plans = load_plans(Path(args.plans))
    except (OSError, ValueError) as exc:
        print(f"error: no se pudo leer {args.plans}: {exc}", file=sys.stderr)
        return 1

    findings: list[Finding] = []

    paper = fetch_paper_positions()
    if paper is None:
        findings.append(
            Finding("paper_unavailable", "warn", "ai-trader", "sin token o sin conexión")
        )
        paper = []

    venue_positions, venue_findings = gather_venue_positions(plans)
    findings.extend(venue_findings)

    journal: list[Any] = []
    journal_path = Path(args.journal)
    if journal_path.is_file():
        try:
            journal = load_journal(journal_path)
        except (OSError, ValueError, ImportError) as exc:
            findings.append(
                Finding("journal_unreadable", "warn", "journal", f"no se pudo leer {journal_path}: {exc}")
            )

    findings.extend(
        reconcile(plans, paper, venue_positions, journal, tolerance=args.tolerance)
    )

    findings.sort(key=lambda f: {"error": 0, "warn": 1, "info": 2}.get(f.severity, 3))

    if args.json:
        print(json.dumps([asdict(finding) for finding in findings], indent=2))
    elif args.markdown:
        print(render_markdown(findings))
    else:
        print(render_text(findings))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(findings) + "\n", encoding="utf-8")
        print(f"\nreporte escrito en {out}", file=sys.stderr)

    return 2 if any(f.severity in ("error", "warn") for f in findings) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcilia paper, venue y journal.")
    parser.add_argument("--plans", default=str(DEFAULT_PLANS))
    parser.add_argument("--journal", default=str(DEFAULT_JOURNAL))
    parser.add_argument("--tolerance", type=float, default=0.005, help="tolerancia relativa de cantidad")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--out", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    load_env()
    args = build_parser().parse_args(argv)
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
