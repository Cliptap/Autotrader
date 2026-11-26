"""
Autotrader Collector - System Tray Application
================================================
Recolecta datos de mercado cada 5 min y se minimiza a la bandeja del sistema.

Características:
- Ícono en la barra de tareas (system tray)
- Click derecho: Estado, Forzar snapshot, Pausar/Reanudar, Salir
- Tolerancia a fallos: escrituras atómicas, no se corrompe si se cierra abruptamente
- Auto-resume: al reiniciar, retoma donde estaba
- Log de errores y estado

Configuración: CLIPTAP_AI_TOKEN (obligatoria), CLIPTAP_AGENT_ID,
COLLECT_INTERVAL y HEARTBEAT_INTERVAL. Ver config/collector.env.example.

Ejecutar: python collector.py
"""

import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import requests
import pystray
from PIL import Image, ImageDraw

from env import load_env

load_env()

# ─── CONFIG ───────────────────────────────────────────
BASE_URL = "https://ai4trade.ai/api"
TOKEN = os.getenv("CLIPTAP_AI_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit(
        "CLIPTAP_AI_TOKEN no configurado.\n"
        "Definilo como variable de entorno o en .env (ver config/collector.env.example)."
    )
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
AGENT_ID = int(os.getenv("CLIPTAP_AGENT_ID", "0") or 0)

SCRIPT_DIR = Path(__file__).resolve().parent
KNOWLEDGE_DIR = SCRIPT_DIR / ".opencode" / "knowledge" / "market-data"
STATE_FILE = SCRIPT_DIR / ".opencode" / "knowledge" / "collector_state.json"

COLLECT_INTERVAL = int(os.getenv("COLLECT_INTERVAL", "300"))  # snapshots
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "60"))  # heartbeat
REQUEST_TIMEOUT = 15

TRACKED_SYMBOLS = [
    ("BTC", "crypto"), ("ETH", "crypto"), ("SOL", "crypto"),
    ("AXP", "us-stock"), ("MU", "us-stock"), ("AMD", "us-stock"),
    ("NVDA", "us-stock"),
]

# ─── ESTADO GLOBAL ────────────────────────────────────
class CollectorState:
    def __init__(self):
        self.running = True
        self.paused = False
        self.snapshots_today = 0
        self.heartbeats_today = 0
        self.errors_today = 0
        self.last_collect = None
        self.last_heartbeat = None
        self.last_error = None
        self.icon = None
        self._lock = threading.Lock()

    def snapshot(self):
        return {
            "last_collect": self.last_collect.isoformat() if self.last_collect else None,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "snapshots_today": self.snapshots_today,
            "heartbeats_today": self.heartbeats_today,
            "errors_today": self.errors_today,
            "last_error": self.last_error,
            "paused": self.paused,
            "last_update": datetime.now(timezone.utc).isoformat(),
        }

state = CollectorState()

# ─── PERSISTENCIA ATÓMICA ─────────────────────────────
def atomic_write(filepath, data, is_json=True):
    """Escribe atómicamente: primero a .tmp, luego renombra."""
    tmp = str(filepath) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            if is_json:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            else:
                f.write(data)
        os.replace(tmp, filepath)
    except Exception as e:
        print(f"[ERROR] atomic_write failed: {e}")

def load_state():
    """Carga el estado previo al iniciar."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            state.snapshots_today = data.get("snapshots_today", 0)
            state.heartbeats_today = data.get("heartbeats_today", 0)
            state.errors_today = data.get("errors_today", 0)
            state.last_error = data.get("last_error")
            print(f"[INIT] Resumed: {state.snapshots_today} snapshots, "
                  f"{state.heartbeats_today} heartbeats today, "
                  f"{state.errors_today} errors")
        except Exception as e:
            print(f"[INIT] Could not load previous state: {e}")
    else:
        print("[INIT] Fresh start - no previous state found")

def save_state():
    """Guarda estado actual de forma atómica."""
    atomic_write(STATE_FILE, state.snapshot())

# ─── API FETCH ────────────────────────────────────────
def fetch(url, method="GET", body=None, timeout=None):
    """Wrapper con manejo de errores."""
    try:
        t = timeout or REQUEST_TIMEOUT
        if method == "GET":
            resp = requests.get(url, headers=HEADERS, timeout=t)
        else:
            resp = requests.post(url, headers=HEADERS, json=body, timeout=t)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        return {"error": "timeout", "url": url}
    except requests.exceptions.ConnectionError as e:
        return {"error": f"connection: {e}", "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}

def collect_snapshot():
    """Recolecta un snapshot completo del mercado."""
    ts = datetime.now(timezone.utc)
    snapshot = {
        "collected_at": ts.isoformat(),
        "market_overview": fetch(f"{BASE_URL}/market-intel/overview"),
        "macro_signals": fetch(f"{BASE_URL}/market-intel/macro-signals"),
        "signal_feed": fetch(f"{BASE_URL}/signals/feed?limit=15&sort=new"),
        "my_positions": fetch(f"{BASE_URL}/positions"),
        "agent_info": fetch(f"{BASE_URL}/claw/agents/me"),
        "prices": {},
    }
    for symbol, market in TRACKED_SYMBOLS:
        snapshot["prices"][f"{symbol}/{market}"] = fetch(
            f"{BASE_URL}/price?symbol={symbol}&market={market}"
        )
    return snapshot

def do_heartbeat():
    """Poll heartbeat endpoint."""
    return fetch(f"{BASE_URL}/claw/agents/heartbeat", "POST",
                 {"agent_id": AGENT_ID, "status": "alive"})

# ─── SAVE SNAPSHOT ────────────────────────────────────
def save_snapshot(snapshot):
    """Guarda snapshot en archivo JSON atómico."""
    ts = datetime.now(timezone.utc)
    date_dir = KNOWLEDGE_DIR / ts.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    filepath = date_dir / f"snapshot_{ts.strftime('%H%M%S')}.json"
    atomic_write(filepath, snapshot)

# ─── DAILY SUMMARY ────────────────────────────────────
def update_daily_summary(snapshot):
    """Actualiza resumen diario legible."""
    ts = datetime.now(timezone.utc)
    date_dir = KNOWLEDGE_DIR / ts.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    filepath = date_dir / "daily_summary.md"

    ov = snapshot.get("market_overview", {})
    macro = snapshot.get("macro_signals", {})
    prices = snapshot.get("prices", {})
    agent = snapshot.get("agent_info", {})

    lines = [
        f"# Daily Summary - {ts.strftime('%Y-%m-%d')}",
        f"",
        f"## Snapshot: {ts.strftime('%H:%M UTC')}",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Macro Verdict | {ov.get('macro_verdict', 'N/A')} |",
        f"| News Status | {ov.get('news_status', 'N/A')} |",
        f"| Headlines | {ov.get('headline_count', 0)} |",
        f"| Macro Bullish | {macro.get('bullish_count', 0)}/{macro.get('total_count', 0)} |",
        f"| ETF Direction | {ov.get('etf_direction', 'N/A')} |",
        f"| Cash | ${agent.get('cash', 0):,.2f} |",
        f"| Points | {agent.get('points', 0)} |",
        f"",
        f"## Prices",
        f"",
    ]
    for key, data in prices.items():
        price = data.get("price", "N/A") if isinstance(data, dict) else "N/A"
        lines.append(f"| {key} | {price} |")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

# ─── COLLECTOR LOOP (thread) ──────────────────────────
def collector_loop():
    """Loop principal de recolección. Corre en un thread."""
    last_collect_time = 0
    last_heartbeat_time = 0

    while state.running:
        try:
            now = time.time()

            if not state.paused:
                # Heartbeat
                if now - last_heartbeat_time >= HEARTBEAT_INTERVAL:
                    hb = do_heartbeat()
                    state.last_heartbeat = datetime.now(timezone.utc)
                    state.heartbeats_today += 1
                    if hb.get("messages"):
                        msg_count = len(hb["messages"])
                        # Guardar mensajes
                        ts = datetime.now(timezone.utc)
                        msg_dir = KNOWLEDGE_DIR / ts.strftime("%Y-%m-%d")
                        msg_dir.mkdir(parents=True, exist_ok=True)
                        msg_file = msg_dir / "heartbeat_messages.md"
                        lines = []
                        for msg in hb["messages"]:
                            lines.append(
                                f"- [{ts.strftime('%H:%M')}] "
                                f"**{msg.get('type')}**: {msg.get('content', '')}"
                            )
                        if lines:
                            with open(msg_file, "a", encoding="utf-8") as f:
                                f.write("\n".join(lines) + "\n")
                    last_heartbeat_time = now

                # Snapshot completo
                if now - last_collect_time >= COLLECT_INTERVAL:
                    snap = collect_snapshot()
                    save_snapshot(snap)
                    update_daily_summary(snap)
                    state.last_collect = datetime.now(timezone.utc)
                    state.snapshots_today += 1
                    last_collect_time = now
                    save_state()

            time.sleep(5)

        except Exception as e:
            state.errors_today += 1
            state.last_error = f"{datetime.now(timezone.utc).strftime('%H:%M')} - {e}"
            save_state()
            time.sleep(30)  # esperar antes de reintentar

# ─── SYSTEM TRAY ICON ─────────────────────────────────
def create_icon_image():
    """Crea un ícono simple: círculo verde (activo) o gris (pausado)."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    color = (150, 150, 150, 255) if state.paused else (76, 175, 80, 255)
    draw.ellipse([12, 12, 52, 52], fill=color, outline=(255, 255, 255, 200), width=2)

    # Pequeña 'C' en el centro
    draw.text((26, 18), "C", fill=(255, 255, 255, 255))

    return img

def build_menu():
    """Construye el menú del system tray."""
    status_text = (
        f"Snapshots hoy: {state.snapshots_today}\n"
        f"Heartbeats: {state.heartbeats_today}\n"
        f"Errores: {state.errors_today}\n"
        f"{'PAUSADO' if state.paused else 'Activo'}\n"
        f"Último: {state.last_collect.strftime('%H:%M') if state.last_collect else 'N/A'}"
    )

    return pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            f"{'Reanudar' if state.paused else 'Pausar'} recolección",
            toggle_pause,
            checked=lambda item: state.paused,
        ),
        pystray.MenuItem("Forzar snapshot ahora", force_collect),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Salir", exit_app),
    )

def toggle_pause(icon, item):
    state.paused = not state.paused
    icon.icon = create_icon_image()
    save_state()

def force_collect(icon, item):
    threading.Thread(target=_force_collect_thread, daemon=True).start()

def _force_collect_thread():
    try:
        snap = collect_snapshot()
        save_snapshot(snap)
        update_daily_summary(snap)
        state.last_collect = datetime.now(timezone.utc)
        state.snapshots_today += 1
        save_state()
    except Exception as e:
        state.errors_today += 1
        state.last_error = f"force: {e}"
        save_state()

def exit_app(icon, item):
    state.running = False
    save_state()
    icon.stop()

def setup_icon():
    """Configura y ejecuta el system tray icon."""
    icon = pystray.Icon(
        "autotrader-collector",
        create_icon_image(),
        "Autotrader Collector",
        build_menu(),
    )
    state.icon = icon

    # Iniciar collector en un thread
    collector_thread = threading.Thread(target=collector_loop, daemon=True)
    collector_thread.start()

    icon.run()

# ─── ENTRY POINT ──────────────────────────────────────
def main():
    print("=" * 50)
    print("  Autotrader Collector")
    print("  Minimizado en la bandeja del sistema")
    print("  Click derecho para opciones")
    print("=" * 50)

    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    load_state()

    # Manejar Ctrl+C graceful shutdown
    def handler(sig, frame):
        print("\n[SHUTDOWN] Saving state...")
        state.running = False
        save_state()
        if state.icon:
            state.icon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)

    setup_icon()

if __name__ == "__main__":
    main()
