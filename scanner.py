"""
Autotrader scanner — 24/7 signal watcher

Only SCANS and NOTIFIES. Never executes trades.
Runs on GitHub Actions every 5 minutes (or locally in a loop) and pushes
alerts to your phone through ntfy.sh.

Pipeline:
  Scanner detects >= N followed agents buying -> ntfy.sh push
  -> open OpenCode -> research together -> execute (paper first)

Alerts are deduplicated: the same symbol does not notify again for
ALERT_COOLDOWN_MIN minutes, and a notification is sent when the consensus
breaks. State lives in alert_state.json (cached between workflow runs).

Setup:
  1. Install the ntfy app (https://ntfy.sh) and subscribe to a topic
  2. Set CLIPTAP_AI_TOKEN and CLIPTAP_NTFY_TOPIC
  3. python scanner.py
"""

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from env import load_env

load_env()

# ─── CONFIG ───────────────────────────────────────────
BASE_URL = "https://ai4trade.ai/api"
TOKEN = os.getenv("CLIPTAP_AI_TOKEN", "").strip()
HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def require_token():
    if not TOKEN:
        raise SystemExit(
            "CLIPTAP_AI_TOKEN no configurado.\n"
            "Definilo como variable de entorno o en .env (ver config/collector.env.example).\n"
            "Nunca embebas el token en el codigo."
        )

SCRIPT_DIR = Path(__file__).resolve().parent
ALERTS_FILE = SCRIPT_DIR / "alerts.md"
STATE_FILE = SCRIPT_DIR / "alert_state.json"

FOLLOWED_AGENTS = {
    14773: "Hermes Agent v2",
    834: "Ava_Commander",
    1563: "raftapart",
    59: "ClawTrader-minimax",
    3120: "VoltSignalsAI4",
}

TRACKED_CRYPTO = ["BTC", "ETH", "SOL", "LINK", "ADA", "DOGE", "FET", "AAVE", "XRP", "AVAX", "SUI", "RENDER"]
TRACKED_STOCKS = ["AAPL", "AXP", "NVDA", "MSFT", "AMD", "MU", "GOOGL", "META", "CAT", "MRK", "JPM"]
STOCK_MIN_SCORE = 8     # solo alertar si raftapart score >= 8
STOCK_MIN_COMMUNITY = 5  # minimo 5 traders comprando

SCAN_INTERVAL = 300
MIN_BUY_CONSENSUS = 2
GLOBAL_BUY_FRENZY = 5
MACRO_BULLISH_THRESHOLD = 3
ALERT_COOLDOWN_MIN = int(os.getenv("ALERT_COOLDOWN_MIN", "60"))

# ─── NTFY.SH (notificaciones push al celu) ────────────
NTFY_TOPIC = os.getenv("CLIPTAP_NTFY_TOPIC", "cliptap-alerts")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"


def push(title, message, priority="default"):
    """Envía push notification al celu vía ntfy.sh."""
    try:
        requests.post(NTFY_URL, json={
            "topic": NTFY_TOPIC,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": "chart_with_upwards_trend",
        }, timeout=10)
    except Exception as e:
        print(f"[ntfy] Error: {e}")


# ─── HELPERS ──────────────────────────────────────────
def fetch(url, method="GET", body=None):
    try:
        r = (requests.get(url, headers=HEADERS, timeout=15) if method == "GET"
             else requests.post(url, headers=HEADERS, json=body, timeout=15))
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def log_alert(entry):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    line = f"[{ts}] {entry}"
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_FILE, "a", encoding="utf-8") as f:
        f.write(f"- {line}\n")
    print(line)


def price_lookup(symbol, market="crypto"):
    data = fetch(f"{BASE_URL}/price?symbol={symbol}&market={market}")
    value = data.get("price") if data else None
    return value if value is not None else "?"


# ─── DECISION (pure) ──────────────────────────────────
@dataclass
class Alert:
    key: str
    symbol: str
    kind: str  # consensus | frenzy | stock | break
    title: str
    message: str
    priority: str = "default"


def normalize_side(side):
    if side in ("short", "cover"):
        return "sell"
    return side


def collect_agent_actions(signals_payload, agent_name, tracked):
    """Agrupa compras y ventas por símbolo para un agente."""
    actions = {}
    for signal in (signals_payload or {}).get("signals", []):
        symbol = signal.get("symbol")
        side = normalize_side(signal.get("side"))
        if not symbol or side not in ("buy", "sell") or symbol not in tracked:
            continue
        bucket = actions.setdefault(symbol, {"buy": [], "sell": []})
        bucket[side].append(agent_name)
    return actions


def collect_feed_actions(feed_payload, tracked):
    """Cuenta compras y ventas por símbolo en el feed global."""
    actions = {}
    for signal in (feed_payload or {}).get("signals", []):
        symbol = signal.get("symbol")
        side = normalize_side(signal.get("side"))
        if not symbol or side not in ("buy", "sell") or symbol not in tracked:
            continue
        bucket = actions.setdefault(symbol, {"buy": 0, "sell": 0})
        bucket[side] += 1
    return actions


def evaluate_agent_alerts(actions, macro_bullish, min_consensus, prices=None):
    """Convierte el consenso de agentes seguidos en alertas."""
    alerts = []
    for symbol, sides in actions.items():
        buyers = sides["buy"]
        if len(buyers) < min_consensus:
            continue
        price = prices(symbol) if prices else "?"
        agents = ", ".join(buyers[:4])
        message = f"{symbol} @ ${price} — {len(buyers)} agentes comprando: {agents}"
        if macro_bullish:
            message += " | MACRO BULLISH"
        alerts.append(
            Alert(
                key=f"consensus:{symbol}",
                symbol=symbol,
                kind="consensus",
                title=f"🟢 {symbol} — {len(buyers)} agentes comprando",
                message=message,
                priority="high" if len(buyers) >= 3 else "default",
            )
        )
    return alerts


def evaluate_feed_alerts(feed_actions, frenzy_threshold):
    """Detecta frenesí de compra en el feed global."""
    alerts = []
    for symbol, counts in feed_actions.items():
        if counts["buy"] < frenzy_threshold:
            continue
        alerts.append(
            Alert(
                key=f"frenzy:{symbol}",
                symbol=symbol,
                kind="frenzy",
                title=f"FRENESI COMPRA {symbol}",
                message=f"{counts['buy']} agentes comprando en el feed global",
                priority="high",
            )
        )
    return alerts


STOCK_BUY_RE = r"{symbol}:\s+\*\*BUY\*\*\s+\(Score:\s+([\d.]+)"
STOCK_COMMUNITY_RE = r"Community:\s+(\d+)B"


def parse_stock_signals(content, tracked, min_score, min_community):
    """Extrae compras de alta convicción del texto publicado por un agente."""
    found = []
    for symbol in tracked:
        if f"{symbol}: **BUY**" not in content:
            continue
        match = re.search(STOCK_BUY_RE.format(symbol=symbol), content)
        score = float(match.group(1)) if match else 0.0
        community_match = re.search(STOCK_COMMUNITY_RE, content)
        community = int(community_match.group(1)) if community_match else 0
        if score >= min_score and community >= min_community:
            found.append((symbol, score, community))
    return found


def evaluate_stock_alerts(signals_payload, tracked, min_score, min_community, prices=None):
    alerts = []
    for signal in (signals_payload or {}).get("signals", []):
        if signal.get("market") != "us-stock":
            continue
        content = signal.get("content") or ""
        for symbol, score, community in parse_stock_signals(content, tracked, min_score, min_community):
            price = prices(symbol, "us-stock") if prices else "?"
            alerts.append(
                Alert(
                    key=f"stock:{symbol}",
                    symbol=symbol,
                    kind="stock",
                    title=f"📈 {symbol} BUY",
                    message=f"Score: {score}\nComunidad: {community}B\nPrecio: ${price}",
                    priority="high",
                )
            )
    return alerts


NY_TZ = ZoneInfo("America/New_York")


def is_us_market_open(now=None):
    """NYSE: Mon-Fri 9:30-16:00 hora de Nueva York (DST incluido)."""
    moment = (now or datetime.now(timezone.utc)).astimezone(NY_TZ)
    if moment.weekday() >= 5:
        return False
    minutes = moment.hour * 60 + moment.minute
    return 570 <= minutes < 960


# ─── STATE / DEDUP ────────────────────────────────────
def load_alert_state(path=None):
    path = Path(path or STATE_FILE)
    if not path.is_file():
        return {"active": [], "alerts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": [], "alerts": {}}
    return {"active": list(data.get("active", [])), "alerts": dict(data.get("alerts", {}))}


def save_alert_state(state, path=None):
    path = Path(path or STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _parse_ts(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def cooldown_active(state, key, now, cooldown_min):
    entry = state.get("alerts", {}).get(key)
    if not entry or cooldown_min <= 0:
        return False
    last = _parse_ts(entry.get("last_alert"))
    if last is None:
        return False
    return (now - last) < timedelta(minutes=cooldown_min)


def dispatch(alerts, state, now=None, cooldown_min=ALERT_COOLDOWN_MIN, notify=push, log=log_alert):
    """Envía solo lo que corresponde y devuelve un resumen.

    - Un símbolo alertado no vuelve a sonar hasta que expire el cooldown.
    - Cuando el consenso desaparece se envía un aviso de cierre.
    """
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    previous = set(state.get("active", []))
    seen = {alert.key for alert in alerts}
    summary = {"sent": 0, "suppressed": 0, "breaks": 0}

    for alert in alerts:
        if cooldown_active(state, alert.key, now, cooldown_min):
            summary["suppressed"] += 1
            continue
        notify(alert.title, alert.message, alert.priority)
        log(f"{alert.title} — {alert.message.splitlines()[0]}")
        previous_count = int(state.get("alerts", {}).get(alert.key, {}).get("count", 0))
        state.setdefault("alerts", {})[alert.key] = {
            "last_alert": stamp,
            "count": previous_count + 1,
        }
        summary["sent"] += 1

    for key in sorted(previous - seen):
        if not key.startswith("consensus:"):
            continue
        symbol = key.split(":", 1)[1]
        notify(
            f"Consenso terminado: {symbol}",
            "Ya no hay agentes comprando este activo.",
            "low",
        )
        log(f"Consenso terminado — {symbol}")
        state.get("alerts", {}).pop(key, None)
        summary["breaks"] += 1

    state["active"] = sorted(seen)
    return summary


# ─── ESCANEO ──────────────────────────────────────────
def scan():
    require_token()
    alerts = []

    macro = fetch(f"{BASE_URL}/market-intel/macro-signals")
    macro_bullish = bool(macro) and macro.get("bullish_count", 0) >= MACRO_BULLISH_THRESHOLD

    tracked_set = set(TRACKED_CRYPTO)
    for agent_id, name in FOLLOWED_AGENTS.items():
        payload = fetch(f"{BASE_URL}/signals/{agent_id}?limit=3")
        actions = collect_agent_actions(payload, name, tracked_set)
        alerts.extend(evaluate_agent_alerts(actions, macro_bullish, MIN_BUY_CONSENSUS, price_lookup))

    feed = fetch(f"{BASE_URL}/signals/feed?limit=20&sort=new&message_type=operation")
    feed_actions = collect_feed_actions(feed, tracked_set)
    alerts.extend(evaluate_feed_alerts(feed_actions, GLOBAL_BUY_FRENZY))

    # ── Stocks (solo durante market hours + alta convicción) ──
    if is_us_market_open():
        stock_signals = fetch(f"{BASE_URL}/signals/1563?limit=1")  # raftapart
        alerts.extend(
            evaluate_stock_alerts(
                stock_signals,
                TRACKED_STOCKS,
                STOCK_MIN_SCORE,
                STOCK_MIN_COMMUNITY,
                price_lookup,
            )
        )

    state = load_alert_state()
    summary = dispatch(alerts, state)
    save_alert_state(state)

    if not alerts:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M')}] Sin señales claras")
    elif summary["suppressed"]:
        print(f"[dedup] {summary['suppressed']} alertas dentro del cooldown")
    return summary


# ─── MAIN ─────────────────────────────────────────────
def main():
    in_github = os.getenv("GITHUB_ACTIONS") == "true"

    if not in_github:
        print("=" * 55)
        print("  Autotrader scanner — ntfy.sh")
        print(f"  Topic: {NTFY_TOPIC}")
        print(f"  {len(FOLLOWED_AGENTS)} agentes | crypto: {len(TRACKED_CRYPTO)} stocks: {len(TRACKED_STOCKS)} | cada {SCAN_INTERVAL}s")
        print(f"  Compra: >= {MIN_BUY_CONSENSUS} agentes | Stocks: score >= {STOCK_MIN_SCORE}")
        print(f"  Cooldown: {ALERT_COOLDOWN_MIN} min")
        print("=" * 55)
        print()
        push("Autotrader scanner iniciado", "Vigilando oportunidades de trading...")

    if in_github:
        # GitHub Actions: ejecutar una vez y salir
        scan()
        print("Scan completo. Próxima ejecución en ~5 min.")
        return

    # Local: loop infinito
    while True:
        try:
            scan()
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nScanner detenido.")
