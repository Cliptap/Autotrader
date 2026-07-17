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

TRACKED_CRYPTO = ["BTC", "ETH", "SOL", "LINK", "ADA", "DOGE", "FET", "AAVE", "XRP", "AVAX", "SUI", "RENDER"]
TRACKED_STOCKS = ["AAPL", "AXP", "NVDA", "MSFT", "AMD", "MU", "GOOGL", "META", "CAT", "MRK", "JPM"]
STOCK_MIN_SCORE = 8     # solo alertar si raftapart score >= 8
STOCK_MIN_COMMUNITY = 5  # minimo 5 traders comprando

SCAN_INTERVAL = 300
MIN_BUY_CONSENSUS = 2
GLOBAL_BUY_FRENZY = 5
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

# ─── STOCKS ───────────────────────────────────────────
def is_us_market_open():
    """NYSE: Mon-Fri 9:30-16:00 ET (13:30-20:00 UTC / 14:30-21:00 UTC DST)."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    # US Eastern is UTC-4 in July (EDT)
    hour_et = (now.hour - 4) % 24
    minute = now.minute
    minutes_et = hour_et * 60 + minute
    return 570 <= minutes_et < 960  # 9:30-16:00

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
            if not sym or not side or sym not in TRACKED_CRYPTO:
                continue
            # Normalizar: short/cover = sell
            if side in ("short", "cover"):
                side = "sell"
            if sym not in agent_actions:
                agent_actions[sym] = {"buy": [], "sell": []}
            agent_actions[sym][side].append(name)

    # Analizar crypto
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

    # Feed global: detectar frenesíes de compra
    feed = fetch(f"{BASE_URL}/signals/feed?limit=20&sort=new&message_type=operation")
    if feed:
        feed_actions = {}
        for s in feed.get("signals", []):
            sym = s.get("symbol")
            side = s.get("side")
            if not sym or not side or sym not in TRACKED_CRYPTO:
                continue
            if side in ("short", "cover"):
                side = "sell"
            if sym not in feed_actions:
                feed_actions[sym] = {"buy": 0, "sell": 0}
            feed_actions[sym][side] += 1

        for sym, counts in feed_actions.items():
            if counts["buy"] >= GLOBAL_BUY_FRENZY:
                log_alert(f"[FRENESI] {sym} — {counts['buy']} agentes del feed comprando!")
                push(f"FRENESI COMPRA {sym}", f"{counts['buy']} agentes comprando en el feed global", "high")

    if not opportunities:
        print(f"[{datetime.now(timezone.utc).strftime('%H:%M')}] Sin señales crypto claras")

    # ── Stocks (solo durante market hours + alta convicción) ──
    if is_us_market_open():
        stock_signals = fetch(f"{BASE_URL}/signals/1563?limit=1")  # raftapart
        if stock_signals:
            for s in stock_signals.get("signals", []):
                if s.get("market") != "us-stock":
                    continue
                content = s.get("content", "")
                for sym in TRACKED_STOCKS:
                    if f"{sym}: **BUY**" in content:
                        # Extraer score
                        import re
                        m = re.search(rf"{sym}:\s+\*\*BUY\*\*\s+\(Score:\s+([\d.]+)", content)
                        score = float(m.group(1)) if m else 0
                        # Extraer comunidad
                        cm = re.search(rf"Community:\s+(\d+)B", content)
                        community = int(cm.group(1)) if cm else 0
                        if score >= STOCK_MIN_SCORE and community >= STOCK_MIN_COMMUNITY:
                            # Precio via Alpaca no disponible aqui, usamos API price
                            price_data = fetch(f"{BASE_URL}/price?symbol={sym}&market=us-stock")
                            price = price_data.get("price") if price_data else "?"
                            msg = f"{sym} @ ${price} — raftapart score {score}, {community}B comunidad"
                            log_alert(f"📈 STOCK {msg}")
                            push(f"📈 {sym} BUY", f"Score: {score}\nComunidad: {community}B\nPrecio: ${price}", "high")

# ─── MAIN ─────────────────────────────────────────────
def main():
    in_github = os.getenv("GITHUB_ACTIONS") == "true"

    if not in_github:
        print("=" * 55)
        print("  cliptap Scanner — ntfy.sh → tu celular")
        print(f"  Topic: {NTFY_TOPIC}")
        print(f"  {len(FOLLOWED_AGENTS)} agentes | crypto: {len(TRACKED_CRYPTO)} stocks: {len(TRACKED_STOCKS)} | cada {SCAN_INTERVAL}s")
        print(f"  Compra: >= {MIN_BUY_CONSENSUS} agentes | Stocks: score >= {STOCK_MIN_SCORE}")
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
