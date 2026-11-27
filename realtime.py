#!/usr/bin/env python3
"""Realtime relay: WebSocket sources -> local event bus.

The collector polls every five minutes, which is fine for swing decisions and
useless for intradía. This process keeps a socket open and appends normalized
events to state/events.jsonl.

Sources:
    binance    public bookTicker stream (no keys, no account)
    ai-trader  notification socket (requires CLIPTAP_AI_TOKEN)

Usage:
    python realtime.py --source binance --symbols BTC/USDT,ETH/USDT
    python realtime.py --source binance --testnet --once
    python realtime.py --source ai-trader --duration 300

Security: the AI-Trader socket authenticates with the token in the query
string. That means the token can appear in proxy or server logs; run it from
your machine, not a shared host, and never paste the URL anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from env import env, load_env  # noqa: E402
from events import append_event  # noqa: E402
from sanitize import scrub_secrets  # noqa: E402

BINANCE_LIVE = "wss://stream.binance.com:9443"
BINANCE_TESTNET = "wss://stream.testnet.binance.vision"
AI_TRADER_WS = "wss://ai4trade.ai/ws/notify/{client_id}"
QUOTES = ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "EUR")


# ─── PARSING (pure) ───────────────────────────────────
def binance_native(symbol: str) -> str:
    """BTC/USDT -> btcusdt (lowercase, as Binance streams expect)."""
    return symbol.replace("/", "").replace("-", "").lower()


def split_native(native: str) -> str:
    """BTCUSDT -> BTC/USDT, using known quote assets. Unknown -> unchanged."""
    upper = native.upper()
    for quote in QUOTES:
        if upper.endswith(quote) and len(upper) > len(quote):
            return f"{upper[: -len(quote)]}/{quote}"
    return upper


def stream_url(symbols: list[str], testnet: bool = True) -> str:
    host = BINANCE_TESTNET if testnet else BINANCE_LIVE
    streams = "/".join(f"{binance_native(symbol)}@bookTicker" for symbol in symbols)
    return f"{host}/stream?streams={streams}"


def parse_book_ticker(payload: Any) -> dict[str, Any] | None:
    """Normalize a Binance bookTicker message (raw or combined-stream wrapper)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict) or "s" not in data:
        return None
    try:
        return {
            "source": "binance",
            "type": "book_ticker",
            "symbol": split_native(str(data["s"])),
            "native": str(data["s"]),
            "bid": float(data["b"]),
            "ask": float(data["a"]),
            "bid_qty": float(data.get("B") or 0),
            "ask_qty": float(data.get("A") or 0),
            "exchange_ts": data.get("u"),
        }
    except (KeyError, TypeError, ValueError):
        return None


def parse_ai_trader(raw: str) -> dict[str, Any] | None:
    """Normalize an AI-Trader notification. Unknown shapes are forwarded as-is."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return {"source": "ai-trader", "type": "text", "payload": raw[:500]}
    if not isinstance(payload, dict):
        return {"source": "ai-trader", "type": "unknown", "payload": payload}

    payload = scrub_secrets(payload)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "source": "ai-trader",
        "type": str(payload.get("type") or "message"),
        "symbol": data.get("symbol"),
        "payload": payload,
    }


def ai_trader_url() -> str:
    token = env("CLIPTAP_AI_TOKEN")
    agent_id = env("CLIPTAP_AGENT_ID", "0")
    if not token:
        raise SystemExit("CLIPTAP_AI_TOKEN no configurado: el socket de AI-Trader lo requiere")
    return AI_TRADER_WS.format(client_id=agent_id) + f"?token={token}"


# ─── RELAY ────────────────────────────────────────────
async def relay(
    url: str,
    parse: Callable[[Any], dict[str, Any] | None],
    *,
    once: bool = False,
    duration: float = 0,
    dry_run: bool = False,
    events_file: Path | None = None,
    reconnect: bool = True,
) -> int:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit("websockets no está instalado: pip install websockets") from exc

    received = 0
    started = time.monotonic()
    backoff = 1.0

    while True:
        try:
            async with websockets.connect(url, open_timeout=15, ping_interval=20) as socket:
                backoff = 1.0
                while True:
                    if duration and (time.monotonic() - started) >= duration:
                        return received
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    event = parse(raw)
                    if event is None:
                        continue
                    if dry_run:
                        print(json.dumps(event, ensure_ascii=False), flush=True)
                    else:
                        append_event(
                            str(event.pop("source", "unknown")),
                            str(event.pop("type", "event")),
                            path=events_file,
                            **event,
                        )
                    received += 1
                    if once:
                        return received
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect on any socket failure
            if not reconnect or once:
                raise
            print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
                  f"socket error: {exc}; reintento en {backoff:.0f}s", file=sys.stderr)
            if duration and (time.monotonic() - started + backoff) >= duration:
                return received
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ─── CLI ──────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Relay WebSocket -> bus de eventos local.")
    parser.add_argument("--source", choices=("binance", "ai-trader"), default="binance")
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT")
    parser.add_argument("--live", action="store_true", help="Binance mainnet (default: testnet)")
    parser.add_argument("--once", action="store_true", help="salir tras el primer evento")
    parser.add_argument("--duration", type=float, default=0, help="segundos a correr (0 = infinito)")
    parser.add_argument("--dry-run", action="store_true", help="imprimir sin escribir el bus")
    parser.add_argument("--file", default=None)
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
        if args.source == "binance":
            symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
            if not symbols:
                raise SystemExit("--symbols no puede estar vacío")
            url = stream_url(symbols, testnet=not args.live)
            parse = parse_book_ticker
        else:
            url = ai_trader_url()
            parse = parse_ai_trader
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1

    events_file = Path(args.file) if args.file else None
    try:
        received = asyncio.run(
            relay(
                url,
                parse,
                once=args.once,
                duration=args.duration,
                dry_run=args.dry_run,
                events_file=events_file,
            )
        )
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"{received} eventos recibidos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
