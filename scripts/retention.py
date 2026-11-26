#!/usr/bin/env python3
"""Retention for collector snapshots.

A snapshot every five minutes is roughly 100k files per year. This script keeps
the resolution you need and discards the rest:

    raw       one file per scan          kept RAW_DAYS (default 7)
    hourly    OHLC per symbol per hour   kept HOURLY_DAYS (default 90)
    daily     OHLC per symbol per day    kept forever

Safe by default: without ``--apply`` it only reports what it would do.

Usage:
    python scripts/retention.py
    python scripts/retention.py --apply
    python scripts/retention.py --dir /ruta/market-data --raw-days 3
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_FMT = "%Y-%m-%dT%H"


# ─── AGGREGATION ──────────────────────────────────────
def merge_series(existing: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Merge two OHLC buckets keeping the earliest open and the latest close."""
    if existing is None:
        return {
            "open": new["open"],
            "high": new["high"],
            "low": new["low"],
            "close": new["close"],
            "samples": int(new.get("samples", 0)),
        }
    return {
        "open": existing["open"],
        "high": max(existing["high"], new["high"]),
        "low": min(existing["low"], new["low"]),
        "close": new["close"],
        "samples": int(existing.get("samples", 0)) + int(new.get("samples", 0)),
    }


def aggregate_prices(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build ``{symbol/market: OHLC}`` from raw snapshots, in order."""
    series: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        for key, item in (snapshot.get("prices") or {}).items():
            if not isinstance(item, dict):
                continue
            price = item.get("price")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            series[key] = merge_series(
                series.get(key),
                {"open": price, "high": price, "low": price, "close": price, "samples": 1},
            )
    return series


def aggregate_signals(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Count buy/sell signals per symbol so the summary keeps some signal history."""
    counts: dict[str, dict[str, int]] = {}
    for snapshot in snapshots:
        feed = snapshot.get("signal_feed") or {}
        for signal in feed.get("signals", []) if isinstance(feed, dict) else []:
            side = str(signal.get("side") or "").lower()
            if side in ("short", "cover"):
                side = "sell"
            symbol = signal.get("symbol")
            if not symbol or side not in ("buy", "sell"):
                continue
            key = f"{symbol}/{signal.get('market', 'crypto')}"
            bucket = counts.setdefault(key, {"buy": 0, "sell": 0})
            bucket[side] += 1
    return counts


# ─── PLANNING ─────────────────────────────────────────
@dataclass
class Action:
    kind: str  # hourly | daily
    period: datetime
    inputs: list[Path]
    output: Path
    sources: int = 0


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    kept_days: list[str] = field(default_factory=list)

    @property
    def hourly(self) -> list[Action]:
        return [action for action in self.actions if action.kind == "hourly"]

    @property
    def daily(self) -> list[Action]:
        return [action for action in self.actions if action.kind == "daily"]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _snapshot_period(snapshot: dict[str, Any], fallback: datetime) -> datetime:
    raw = snapshot.get("collected_at")
    if not raw:
        return fallback
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def plan_retention(
    root: Path,
    now: datetime | None = None,
    raw_days: int = 7,
    hourly_days: int = 90,
) -> Plan:
    now = now or datetime.now(timezone.utc)
    raw_cutoff = now - timedelta(days=raw_days)
    hourly_cutoff = now - timedelta(days=hourly_days)
    plan = Plan()

    if not root.is_dir():
        raise FileNotFoundError(f"no existe el directorio: {root}")

    # 1. Raw snapshots of old days -> hourly files.
    for day_dir in sorted(root.iterdir()):
        if not day_dir.is_dir() or not DAY_RE.match(day_dir.name):
            continue
        try:
            day = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        end_of_day = datetime.combine(day, time.max, tzinfo=timezone.utc)
        if end_of_day >= raw_cutoff:
            plan.kept_days.append(day_dir.name)
            continue

        buckets: dict[datetime, list[Path]] = {}
        for snapshot_path in sorted(day_dir.glob("snapshot_*.json")):
            snapshot = _read_json(snapshot_path)
            if snapshot is None:
                continue
            period = _snapshot_period(snapshot, end_of_day).replace(minute=0, second=0, microsecond=0)
            buckets.setdefault(period, []).append(snapshot_path)

        for period, inputs in sorted(buckets.items()):
            output = root / "hourly" / f"{period.strftime(DATE_FMT)}.json"
            plan.actions.append(
                Action("hourly", period, inputs, output, sources=len(inputs))
            )

    # 2. Old hourly files -> daily files.
    hourly_dir = root / "hourly"
    if hourly_dir.is_dir():
        by_day: dict[date, list[Path]] = {}
        for hourly_path in sorted(hourly_dir.glob("*.json")):
            try:
                period = datetime.strptime(hourly_path.stem, DATE_FMT).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if period >= hourly_cutoff:
                continue
            by_day.setdefault(period.date(), []).append(hourly_path)

        for day, inputs in sorted(by_day.items()):
            output = root / "daily" / f"{day.isoformat()}.json"
            plan.actions.append(
                Action("daily", datetime.combine(day, time.min, tzinfo=timezone.utc), inputs, output, sources=len(inputs))
            )

    return plan


# ─── APPLY ────────────────────────────────────────────
def _build_document(action: Action) -> dict[str, Any]:
    series: dict[str, dict[str, Any]] = {}
    signals: dict[str, dict[str, int]] = {}
    if action.kind == "daily":
        existing = _read_json(action.output)
        if existing:
            series = {
                key: dict(value) for key, value in (existing.get("symbols") or {}).items()
            }
            signals = {
                key: dict(value) for key, value in (existing.get("signals") or {}).items()
            }
            samples = int(existing.get("sources", 0))
        else:
            samples = 0
        for path in action.inputs:
            document = _read_json(path)
            if not document:
                continue
            samples += int(document.get("sources", 1))
            for key, value in (document.get("symbols") or {}).items():
                series[key] = merge_series(series.get(key), value)
            for key, value in (document.get("signals") or {}).items():
                bucket = signals.setdefault(key, {"buy": 0, "sell": 0})
                bucket["buy"] += int(value.get("buy", 0))
                bucket["sell"] += int(value.get("sell", 0))
    else:
        samples = action.sources
        snapshots = [doc for doc in (_read_json(path) for path in action.inputs) if doc]
        series = aggregate_prices(snapshots)
        signals = aggregate_signals(snapshots)

    return {
        "period": action.period.isoformat(timespec="seconds"),
        "kind": action.kind,
        "sources": samples,
        "symbols": series,
        "signals": signals,
    }


def apply_plan(plan: Plan) -> dict[str, int]:
    removed = 0
    written = 0
    for action in plan.actions:
        document = _build_document(action)
        action.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = action.output.with_suffix(".tmp")
        tmp.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(action.output)
        written += 1
        for path in action.inputs:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        if action.kind == "hourly":
            parent = action.inputs[0].parent if action.inputs else None
            if parent and parent.is_dir() and not any(parent.iterdir()):
                try:
                    parent.rmdir()
                except OSError:
                    pass
    return {"written": written, "removed": removed}


# ─── CLI ──────────────────────────────────────────────
def render(plan: Plan, root: Path) -> str:
    hourly = plan.hourly
    daily = plan.daily
    raw_files = sum(action.sources for action in hourly)
    lines = [
        f"Retención en {root}",
        "------------------------------",
        f"Días conservados crudos: {', '.join(plan.kept_days) or 'ninguno'}",
        f"Snapshots a compactar:   {raw_files} ({len(hourly)} horas)",
        f"Archivos horarios a pasar a diario: {sum(action.sources for action in daily)} ({len(daily)} días)",
        "",
    ]
    if not plan.actions:
        lines.append("Nada que hacer.")
        return "\n".join(lines)
    for action in plan.actions[:10]:
        lines.append(
            f"  {action.kind:<6} {action.period:%Y-%m-%d %H:%M} "
            f"{action.sources:>3} origen -> {action.output.relative_to(root)}"
        )
    if len(plan.actions) > 10:
        lines.append(f"  … y {len(plan.actions) - 10} acciones más")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compacta snapshots viejos a resúmenes horarios y diarios.")
    parser.add_argument("--dir", default=str(ROOT / ".opencode" / "knowledge" / "market-data"))
    parser.add_argument("--raw-days", type=int, default=7, help="días de snapshots crudos")
    parser.add_argument("--hourly-days", type=int, default=90, help="días de resúmenes horarios")
    parser.add_argument("--apply", action="store_true", help="ejecutar (sin esto solo informa)")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    root = Path(args.dir)

    try:
        plan = plan_retention(root, raw_days=args.raw_days, hourly_days=args.hourly_days)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "actions": [
                {
                    "kind": action.kind,
                    "period": action.period.isoformat(),
                    "sources": action.sources,
                    "output": str(action.output),
                }
                for action in plan.actions
            ],
            "kept_days": plan.kept_days,
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(render(plan, root))
    if not args.apply:
        if plan.actions:
            print("\n(dry-run) Agregá --apply para ejecutar.")
        return 0

    result = apply_plan(plan)
    print(f"\nAplicado: {result['written']} archivos escritos, {result['removed']} crudos eliminados.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
