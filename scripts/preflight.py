#!/usr/bin/env python3
"""Preflight checks for the Autotrader environment.

Validates configuration, credentials and connectivity before a trading
session. Read-only: it never places or cancels orders.

Usage:
    python scripts/preflight.py
    python scripts/preflight.py --offline     # skip network checks
    python scripts/preflight.py --strict      # warnings count as failures

Exit codes:
    0  ready to trade (warnings allowed)
    1  at least one blocking problem
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from env import env, env_float, load_env  # noqa: E402

AI_TRADER_BASE = "https://ai4trade.ai/api"
BINANCE_TIME_URL = "https://api.binance.com/api/v3/time"

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))


def check_python() -> None:
    version = sys.version_info
    label = f"Python {version.major}.{version.minor}.{version.micro}"
    if version < (3, 10):
        record(FAIL, "runtime", f"{label}: se requiere 3.10 o superior")
    else:
        record(PASS, "runtime", label)


def check_files() -> None:
    config = next(
        (p for p in (ROOT / "opencode.jsonc", ROOT / "opencode.json") if p.is_file()),
        None,
    )
    if config:
        record(PASS, "opencode config", config.name)
        text = config.read_text(encoding="utf-8", errors="replace")
        if '"mcp"' in text and '"servers"' not in text:
            record(
                WARN,
                "opencode config",
                "parece usar el formato V1 (servidores bajo mcp); V2 usa mcp.servers",
            )
    else:
        record(FAIL, "opencode config", "falta opencode.jsonc (copiar config/opencode.jsonc.example)")

    server = ROOT / "mcp" / "ccxt" / "server.py"
    if server.is_file():
        record(PASS, "ccxt server", str(server.relative_to(ROOT)))
    else:
        record(FAIL, "ccxt server", "falta mcp/ccxt/server.py")

    ccxt_env = ROOT / "mcp" / "ccxt" / ".env"
    if ccxt_env.is_file():
        record(PASS, "ccxt env", str(ccxt_env.relative_to(ROOT)))
    else:
        record(WARN, "ccxt env", "falta mcp/ccxt/.env (copiar config/ccxt.env.example)")

    halt = ROOT / "state" / "HALT"
    if halt.exists():
        record(FAIL, "kill switch", "state/HALT presente: no operar hasta removerlo")
    else:
        record(PASS, "kill switch", "sin HALT")


def check_dependencies() -> None:
    packages = {
        "requests": "scanner y collector",
        "ccxt": "MCP de crypto",
        "mcp": "MCP de crypto",
        "pystray": "collector (bandeja)",
        "PIL": "collector (icono)",
    }
    missing = [f"{mod} ({why})" for mod, why in packages.items() if importlib.util.find_spec(mod) is None]
    if missing:
        record(WARN, "dependencies", "faltan: " + ", ".join(missing))
    else:
        record(PASS, "dependencies", "todas presentes")


def check_secrets() -> None:
    token = env("CLIPTAP_AI_TOKEN")
    if not token or token.startswith("your_"):
        record(FAIL, "CLIPTAP_AI_TOKEN", "no configurado")
    else:
        record(PASS, "CLIPTAP_AI_TOKEN", f"presente ({len(token)} chars)")

    topic = env("CLIPTAP_NTFY_TOPIC")
    if not topic or topic in {"cliptap-alerts", "change-me-to-a-random-topic"}:
        record(WARN, "CLIPTAP_NTFY_TOPIC", "usa un topic largo y difícil de adivinar")
    elif len(topic) < 8:
        record(WARN, "CLIPTAP_NTFY_TOPIC", "topic demasiado corto")
    else:
        record(PASS, "CLIPTAP_NTFY_TOPIC", "configurado")

    key, secret = env("ALPACA_API_KEY"), env("ALPACA_SECRET_KEY")
    if not key or not secret:
        record(WARN, "Alpaca credentials", "sin claves: ejecución de acciones deshabilitada")
    else:
        record(PASS, "Alpaca credentials", "claves presentes")

    paper = env("ALPACA_PAPER_TRADE", "true").lower()
    if paper == "true":
        record(PASS, "Alpaca mode", "paper")
    else:
        record(WARN, "Alpaca mode", "LIVE (ALPACA_PAPER_TRADE=false): dinero real")

    sandbox = env("CCXT_SANDBOX", "true").lower()
    allow_live = env("ALLOW_LIVE", "false").lower()
    dry_run = env("DRY_RUN", "true").lower()
    if sandbox == "true":
        record(PASS, "CCXT mode", "SANDBOX")
    elif allow_live == "true" and dry_run == "false":
        record(WARN, "CCXT mode", "LIVE habilitado: cada sesión requiere arm_live_session()")
    else:
        record(WARN, "CCXT mode", f"sandbox={sandbox} allow_live={allow_live} dry_run={dry_run}")

    max_order = env_float("MAX_ORDER_VALUE_USD", 100.0)
    if max_order <= 0:
        record(WARN, "MAX_ORDER_VALUE_USD", "sin tope por orden")
    elif max_order > 500:
        record(WARN, "MAX_ORDER_VALUE_USD", f"{max_order:.0f} USD es alto para empezar")
    else:
        record(PASS, "MAX_ORDER_VALUE_USD", f"{max_order:.0f} USD")


def _http_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def check_network() -> None:
    token = env("CLIPTAP_AI_TOKEN")
    if not token:
        record(WARN, "AI-Trader API", "sin token, no se puede verificar")
    else:
        try:
            me = _http_json(
                f"{AI_TRADER_BASE}/claw/agents/me",
                {"Authorization": f"Bearer {token}"},
            )
            agent = me.get("agent", me)
            name = agent.get("name", "?")
            cash = agent.get("cash")
            detail = f"agente={name}" + (f" cash={cash}" if cash is not None else "")
            record(PASS, "AI-Trader API", detail)
        except urllib.error.HTTPError as exc:
            record(FAIL, "AI-Trader API", f"HTTP {exc.code}: token inválido o expirado")
        except Exception as exc:  # noqa: BLE001
            record(WARN, "AI-Trader API", f"sin conexión: {exc}")

    try:
        data = _http_json(BINANCE_TIME_URL, timeout=10)
        drift_ms = abs(int(time.time() * 1000) - int(data["serverTime"]))
        limit = int(env_float("CLOCK_MAX_DRIFT_MS", 5000))
        if drift_ms > limit:
            record(FAIL, "clock drift", f"{drift_ms} ms > {limit} ms (sincronizar el reloj)")
        else:
            record(PASS, "clock drift", f"{drift_ms} ms")
    except Exception as exc:  # noqa: BLE001
        record(WARN, "clock drift", f"no se pudo verificar: {exc}")

    key, secret = env("ALPACA_API_KEY"), env("ALPACA_SECRET_KEY")
    if not key or not secret:
        record(WARN, "Alpaca API", "sin claves")
        return
    host = (
        "https://paper-api.alpaca.markets"
        if env("ALPACA_PAPER_TRADE", "true").lower() == "true"
        else "https://api.alpaca.markets"
    )
    try:
        clock = _http_json(
            f"{host}/v2/clock",
            {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        )
        state = "abierto" if clock.get("is_open") else "cerrado"
        record(PASS, "Alpaca API", f"mercado {state} (next_open={clock.get('next_open', '?')})")
    except urllib.error.HTTPError as exc:
        record(FAIL, "Alpaca API", f"HTTP {exc.code}: revisar claves y ALPACA_PAPER_TRADE")
    except Exception as exc:  # noqa: BLE001
        record(WARN, "Alpaca API", f"sin conexión: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verifica el entorno antes de operar.")
    parser.add_argument("--offline", action="store_true", help="omitir chequeos de red")
    parser.add_argument("--strict", action="store_true", help="tratar warnings como fallos")
    args = parser.parse_args()

    loaded = load_env()
    for path in loaded:
        record(PASS, ".env", str(path.relative_to(ROOT)))

    check_python()
    check_files()
    check_dependencies()
    check_secrets()
    if not args.offline:
        check_network()

    width = max(len(name) for _, name, _ in results)
    print(f"\nAutotrader preflight — {platform.system()} {platform.release()}")
    print("-" * (width + 62))
    for status, name, detail in results:
        print(f"[{status}] {name.ljust(width)}  {detail}")
    print("-" * (width + 62))

    failures = [r for r in results if r[0] == FAIL]
    warnings = [r for r in results if r[0] == WARN]
    print(f"{len(results)} chequeos · {len(failures)} fallos · {len(warnings)} advertencias\n")

    if failures or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
