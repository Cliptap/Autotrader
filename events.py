#!/usr/bin/env python3
"""Local event bus.

A single append-only JSONL file that realtime sources write to and the agent
reads from. Simple on purpose: no daemon, no broker, no database. If you need
more than this, you need a different project.

Usage:
    python events.py tail --limit 20
    python events.py tail --follow
    python events.py stats

Schema (one JSON object per line):
    {"ts": "...", "source": "binance", "type": "book_ticker",
     "symbol": "BTC/USDT", ...}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_FILE = ROOT / "state" / "events.jsonl"
MAX_BYTES = int(os.getenv("EVENTS_MAX_MB", "20")) * 1024 * 1024
POLL_SECONDS = 0.5


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def append_event(
    source: str,
    type_: str,
    path: Path | None = None,
    max_bytes: int = MAX_BYTES,
    **fields: Any,
) -> dict[str, Any]:
    """Append one event, rotating the file when it grows too large."""
    target = Path(path or DEFAULT_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)

    if max_bytes and target.is_file() and target.stat().st_size > max_bytes:
        rotated = target.with_suffix(target.suffix + ".1")
        try:
            if rotated.exists():
                rotated.unlink()
            target.replace(rotated)
        except OSError:
            pass

    event = {"ts": utcnow().isoformat(timespec="milliseconds"), "source": source, "type": type_, **fields}
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def read_events(path: Path | None = None, limit: int = 50) -> list[dict[str, Any]]:
    target = Path(path or DEFAULT_FILE)
    if not target.is_file():
        return []
    lines: deque[str] = deque(maxlen=max(limit, 1))
    with target.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                lines.append(line)
    events = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def follow_events(path: Path | None = None, poll: float = POLL_SECONDS):
    """Yield new events as they are appended."""
    target = Path(path or DEFAULT_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=True)
    with target.open(encoding="utf-8", errors="replace") as handle:
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if not line:
                time.sleep(poll)
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                yield parsed


def file_stats(path: Path | None = None) -> dict[str, Any]:
    target = Path(path or DEFAULT_FILE)
    if not target.is_file():
        return {"exists": False, "events": 0, "bytes": 0, "last": None}
    count = 0
    with target.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                count += 1
    last = None
    for event in reversed(read_events(target, limit=1)):
        last = event
    return {
        "exists": True,
        "events": count,
        "bytes": target.stat().st_size,
        "last": last,
    }


# ─── CLI ──────────────────────────────────────────────
def cmd_tail(args: argparse.Namespace) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    path = Path(args.file)
    for event in read_events(path, limit=args.limit):
        print(json.dumps(event, ensure_ascii=False))
    if not args.follow:
        return 0
    try:
        for event in follow_events(path):
            print(json.dumps(event, ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        print("\nseguimiento detenido")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    stats = file_stats(Path(args.file))
    if args.json:
        print(json.dumps(stats, indent=2, default=str))
        return 0
    if not stats["exists"]:
        print("todavía no hay eventos")
        return 0
    print(f"eventos   {stats['events']}")
    print(f"tamaño    {stats['bytes'] / 1024:,.1f} KiB")
    if stats["last"]:
        print(f"último    {stats['last'].get('ts')} {stats['last'].get('source')} "
              f"{stats['last'].get('type')} {stats['last'].get('symbol', '')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bus de eventos local (JSONL).")
    parser.add_argument("--file", default=str(DEFAULT_FILE))
    sub = parser.add_subparsers(dest="command", required=True)

    tail = sub.add_parser("tail", help="mostrar los últimos eventos")
    tail.add_argument("--limit", type=int, default=20)
    tail.add_argument("--follow", action="store_true")
    tail.set_defaults(func=cmd_tail)

    stats = sub.add_parser("stats", help="resumen del archivo")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
