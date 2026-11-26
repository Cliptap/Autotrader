#!/usr/bin/env python3
"""Position sizing from a fixed risk budget.

Implements the rules in docs/risk-policy.md:

    risk_usd   = equity * risk_pct / 100
    distance   = abs(entry - stop)
    qty        = risk_usd / distance
    notional   = qty * entry          (capped at max_position_pct of equity)

Usage:
    python scripts/position_size.py --equity 10000 --risk-pct 0.5 \
        --entry 100 --stop 95 --target 112

    python scripts/position_size.py --equity 10000 --risk-pct 0.5 \
        --entry 100 --stop 105 --side short --json

Exit codes:
    0  valid setup
    2  risk/reward below the 1:2 minimum
    1  invalid input (stop on the wrong side, non-positive distance, ...)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass

MIN_RR = 2.0


@dataclass
class Sizing:
    equity: float
    risk_pct: float
    risk_usd: float
    entry: float
    stop: float
    target: float | None
    side: str
    distance: float
    distance_pct: float
    qty: float
    notional: float
    notional_pct: float
    capped: bool
    rr: float | None


def size_position(
    equity: float,
    risk_pct: float,
    entry: float,
    stop: float,
    target: float | None = None,
    side: str = "long",
    max_position_pct: float = 10.0,
) -> Sizing:
    """Return a position size honoring both the risk budget and the position cap."""
    if equity <= 0:
        raise ValueError("equity debe ser mayor que cero")
    if not 0 < risk_pct <= 2:
        raise ValueError("risk_pct debe estar entre 0 y 2 (regla dura: 2% máximo)")
    if entry <= 0 or stop <= 0:
        raise ValueError("entry y stop deben ser positivos")

    distance = entry - stop if side == "long" else stop - entry
    if distance <= 0:
        raise ValueError(f"stop inválido para {side}: el stop debe estar {'debajo' if side == 'long' else 'encima'} de la entrada")

    risk_usd = equity * risk_pct / 100
    qty = risk_usd / distance
    cap = equity * max_position_pct / 100
    notional = qty * entry
    capped = notional > cap
    if capped:
        qty = cap / entry
        notional = cap

    rr = None
    if target is not None:
        reward = (target - entry) if side == "long" else (entry - target)
        if reward <= 0:
            raise ValueError(f"target inválido para {side}")
        rr = reward / distance

    return Sizing(
        equity=equity,
        risk_pct=risk_pct,
        risk_usd=round(risk_usd, 2),
        entry=entry,
        stop=stop,
        target=target,
        side=side,
        distance=round(distance, 8),
        distance_pct=round(distance / entry * 100, 2),
        qty=round(qty, 8),
        notional=round(notional, 2),
        notional_pct=round(notional / equity * 100, 2),
        capped=capped,
        rr=round(rr, 2) if rr is not None else None,
    )


def render(result: Sizing) -> str:
    lines = [
        f"Equity            ${result.equity:,.2f}",
        f"Riesgo            {result.risk_pct}% = ${result.risk_usd:,.2f}",
        f"Entrada / stop    ${result.entry:,.4f} / ${result.stop:,.4f}  ({result.distance_pct}%)",
        f"Cantidad          {result.qty:.8f}",
        f"Notional          ${result.notional:,.2f} ({result.notional_pct}% del equity)",
    ]
    if result.capped:
        lines.append("                   recortado por el límite de posición del 10%")
        effective_risk = (result.qty * result.distance)
        lines.append(f"                   riesgo efectivo: ${effective_risk:,.2f} ({effective_risk / result.equity * 100:.2f}%)")
    if result.target is not None:
        lines.append(f"Target            ${result.target:,.4f}  ·  R:R {result.rr}:1")
        if result.rr is not None and result.rr < MIN_RR:
            lines.append(f"                   ADVERTENCIA: por debajo del mínimo 1:{MIN_RR:.0f}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Calcula tamaño de posición según riesgo.")
    parser.add_argument("--equity", type=float, required=True, help="capital total en USD")
    parser.add_argument("--risk-pct", type=float, default=0.5, help="riesgo por operación (máx 2)")
    parser.add_argument("--entry", type=float, required=True)
    parser.add_argument("--stop", type=float, required=True)
    parser.add_argument("--target", type=float, default=None)
    parser.add_argument("--side", choices=("long", "short"), default="long")
    parser.add_argument("--max-position-pct", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", help="salida JSON")
    args = parser.parse_args()

    try:
        result = size_position(
            equity=args.equity,
            risk_pct=args.risk_pct,
            entry=args.entry,
            stop=args.stop,
            target=args.target,
            side=args.side,
            max_position_pct=args.max_position_pct,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(asdict(result), indent=2) if args.json else render(result))
    if result.rr is not None and result.rr < MIN_RR:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
