"""
cliptap Scanner — Vigilante ligero 24/7

Solo ESCANEA y NOTIFICA. No ejecuta trades.
Corre en Railway 24/7, notifica al celular vía ntfy.sh (push nativo).

Pipeline:
  Scanner detecta ≥2 agentes comprando → ntfy.sh → push al celu
  → Abrís OpenCode → Analizamos juntos → Ejecutamos trade

Setup:
  1. Instalá ntfy app en el celu (https://ntfy.sh)
  2. Suscribite al topic: cliptap-alerts
  3. python scanner.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── CONFIG ───────────────────────────────────────────
BASE_URL = "https://ai4trade.ai/api"
TOKEN = os.getenv("CLIPTAP_AI_TOKEN", "JPdLH26cs4tg-7n1NSZBkzIpe-sI0d0MFa0HYjNXpt8")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

SCRIPT_DIR = Path(__file__).resolve().parent
ALERTS_FILE = SCRIPT_DIR / "alerts.md"

FOLLOWED_AGENTS = {
    14773: "Hermes Agent v2",
    834: "Ava_Commander",
    1563: "raftapart",
    59: "ClawTrader-minimax",
    3120: "VoltSignalsAI4",
}

TRACKED = ["BTC", "ETH", "SOL", "LINK", "ADA", "DOGE", "FET", "AAVE", "XRP", "AVAX", "SUI", "RENDER"]

SCAN_INTERVAL = 300
MIN_BUY_CONSENSUS = 2
MIN_SELL_CONSENSUS = 2
GLOBAL_SELL_PANIC = 4    # >=4 agentes del feed vendiendo = alerta
GLOBAL_BUY_FRENZY = 5    # >=5 agentes del feed comprando = senal fuerte
MACRO_BULLISH_THRESHOLD = 3

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
    except:
        return None

def log_alert(entry):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    line = f"[{ts}] {entry}"
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_FILE, "a", encoding="utf-8") as f:
        f.write(f"- {line}\n")
    print(line)

# ─── ESCANEO ──────────────────────────────────────────
def scan():
    opportunities = []

    # Macro
    macro = fetch(f"{BASE_URL}/market-intel/macro-signals")
    macro_bullish = macro.get("bullish_count", 0) >= MACRO_BULLISH_THRESHOLD if macro else False

    # Señales de agentes
    agent_actions = {}
    for aid, name in FOLLOWED_AGENTS.items():
        signals = fetch(f"{BASE_URL}/signals/{aid}?limit=3")
        if not signals:
            continue
        for s in signals.get("signals", []):
            sym = s.get("symbol")
            side = s.get("side")
            if not sym or not side or sym not in TRACKED:
                continue
            # Normalizar: short/cover = sell
            if side in ("short", "cover"):
                side = "sell"
            if sym not in agent_actions:
                agent_actions[sym] = {"buy": [], "sell": []}
            agent_actions[sym][side].append(name)

    # Analizar
    for sym, actions in agent_actions.items():
        buyers = actions["buy"]
        sellers = actions["sell"]

        if len(buyers) >= MIN_BUY_CONSENSUS:
            price_data = fetch(f"{BASE_URL}/price?symbol={sym}&market=crypto")
            price = price_data.get("price") if price_data else "?"

            agents_str = ", ".join(buyers[:4])
            msg = f"{sym} @ ${price} — {len(buyers)} agentes comprando: {agents_str}"
            if macro_bullish:
                msg += " | MACRO BULLISH"

            log_alert(f"🟢 BUY {msg}")

            priority = "high" if len(buyers) >= 3 else "default"
            push(
                f"🟢 {sym} — {len(buyers)} agentes comprando",
                f"Precio: ${price}\nAgentes: {agents_str}\n{'📊 Macro bullish' if macro_bullish else ''}",
                priority,
            )

        if len(sellers) >= MIN_SELL_CONSENSUS:
            agents_str = ", ".join(sellers[:4])
            msg = f"{sym} — {len(sellers)} agentes vendiendo: {agents_str}"
            log_alert(f"🔴 SELL {msg}")
            push(
                f"🔴 {sym} — {len(sellers)} agentes vendiendo",
                f"Agentes: {agents_str}",
                "high",
            )

    # Feed global: detectar pánicos de venta y frenesíes de compra
    feed = fetch(f"{BASE_URL}/signals/feed?limit=20&sort=new&message_type=operation")
    if feed:
        feed_actions = {}
        for s in feed.get("signals", []):
            sym = s.get("symbol")
            side = s.get("side")
            if not sym or not side or sym not in TRACKED:
                continue
            if side in ("short", "cover"):
                side = "sell"
            if sym not in feed_actions:
                feed_actions[sym] = {"buy": 0, "sell": 0}
            feed_actions[sym][side] += 1

        for sym, counts in feed_actions.items():
            if counts["sell"] >= GLOBAL_SELL_PANIC:
                log_alert(f"[PANICO] {sym} — {counts['sell']} agentes del feed vendiendo!")
                push(f"🚨 PANICO VENTA {sym}", f"{counts['sell']} agentes vendiendo en el feed global", "high")
            if counts["buy"] >= GLOBAL_BUY_FRENZY:
                log_alert(f"[FRENESI] {sym} — {counts['buy']} agentes del feed comprando!")
                push(f"🚀 FRENESI COMPRA {sym}", f"{counts['buy']} agentes comprando en el feed global", "high")

    if not opportunities:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M')}] Sin señales claras")

# ─── MAIN ─────────────────────────────────────────────
def main():
    in_github = os.getenv("GITHUB_ACTIONS") == "true"

    if not in_github:
        print("=" * 55)
        print("  cliptap Scanner — ntfy.sh → tu celular")
        print(f"  Topic: {NTFY_TOPIC}")
        print(f"  {len(FOLLOWED_AGENTS)} agentes | {len(TRACKED)} activos | cada {SCAN_INTERVAL}s")
        print(f"  Compra: >= {MIN_BUY_CONSENSUS} agentes | Venta: >= {MIN_SELL_CONSENSUS} agentes")
        print("=" * 55)
        print()
        push("cliptap Scanner iniciado", "Vigilando oportunidades de trading...")

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
