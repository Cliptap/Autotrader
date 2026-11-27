# Tiempo real

El collector hace polling cada 5 minutos. Para decisiones de swing alcanza; para
intradía, no. Un stop que se ejecuta 4 minutos tarde no es un stop: es una
donación.

`realtime.py` mantiene sockets abiertos y escribe eventos normalizados en
`state/events.jsonl`. El agente los lee cuando los necesita; no hay daemon
mágico ni broker de mensajes.

## Fuentes

| Fuente | Auth | Contenido |
| --- | --- | --- |
| `binance` | Ninguna | `bookTicker`: mejor bid/ask y tamaño en cada nivel |
| `ai-trader` | `CLIPTAP_AI_TOKEN` | Notificaciones del agente: replies, menciones, tareas |

Binance arranca en **testnet** por defecto (`--live` para mainnet). El socket de
AI-Trader se autentica con el token en la query string; ver la nota de seguridad
más abajo.

## Uso

```bash
# precio en vivo (una sola lectura, para probar)
python realtime.py --source binance --once --dry-run

# BTC y ETH a mainnet, escribiendo al bus
python realtime.py --source binance --symbols BTC/USDT,ETH/USDT --live

# notificaciones del agente por 5 minutos
python realtime.py --source ai-trader --duration 300
```

Salida de una lectura en testnet:

```json
{
  "source": "binance",
  "type": "book_ticker",
  "symbol": "BTC/USDT",
  "native": "BTCUSDT",
  "bid": 60590.2,
  "ask": 60590.21,
  "bid_qty": 39.02,
  "ask_qty": 5.13,
  "exchange_ts": 22010576
}
```

## El bus

Un archivo JSONL, una línea por evento:

```json
{"ts": "2026-11-27T00:17:52.996+00:00", "source": "binance", "type": "book_ticker",
 "symbol": "BTC/USDT", "bid": 60590.2, "ask": 60590.21, "bid_qty": 39.02, "ask_qty": 5.13}
{"ts": "2026-11-27T00:18:10.120+00:00", "source": "ai-trader", "type": "reply",
 "symbol": "BTC", "payload": {"type": "reply", "data": {"content": "..."}}}
```

Herramientas:

```bash
python events.py tail --limit 20     # últimos eventos
python events.py tail --follow       # seguir en vivo (como tail -f)
python events.py stats               # cantidad, tamaño y último evento
```

Rotación automática a `events.jsonl.1` al pasar `EVENTS_MAX_MB` (default 20 MB).
El bus está en `.gitignore`: es estado local, no se versiona.

## Cómo lo usa el agente

Antes de decidir, el agente puede leer el estado más reciente del mercado sin
depender del snapshot de 5 minutos:

```text
python events.py tail --limit 30
```

Y si necesita esperar un movimiento puntual:

```text
python realtime.py --source binance --symbols BTC/USDT --duration 600
python events.py tail --limit 50
```

El scanner sigue siendo el canal de consenso (cada 5 minutos, GitHub Actions).
El tiempo real es el canal de precio y de notificaciones: son complementarios,
no reemplazos.

## Correrlo como servicio

### Windows

```powershell
$action  = New-ScheduledTaskAction -Execute "python" `
  -Argument "realtime.py --source binance --live" -WorkingDirectory "C:\ruta\Autotrader"
$trigger = New-ScheduledTaskTrigger -AtStartup
$set     = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "AutotraderRealtime" -Action $action -Trigger $trigger -Settings $set
```

### Linux

```ini
# /etc/systemd/system/autotrader-realtime.service
[Unit]
Description=Autotrader realtime relay
After=network-online.target

[Service]
WorkingDirectory=/opt/Autotrader
EnvironmentFile=/opt/Autotrader/.env
ExecStart=/usr/bin/python3 realtime.py --source binance --live
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

El relay reconecta con backoff exponencial (1 s hasta 60 s) ante cualquier
corte de socket.

## Seguridad

El socket de AI-Trader autentica con `?token=...`. Es el mecanismo que expone la
plataforma, pero significa que el token viaja en la URL y puede quedar en logs
de proxy o de servidor.

- Correr el relay desde tu máquina, no desde un host compartido.
- No pegar la URL en issues, chats ni capturas.
- Si el token se filtra, rotarlo: el socket no es más seguro que el resto de la
  API.

Los payloads de AI-Trader pasan por `sanitize.scrub_secrets` antes de guardarse:
si la plataforma alguna vez devuelve un token en un mensaje, no termina en el
bus.

## Límites

- **Solo Binance** como fuente de precio. Agregar otro exchange es sumar un
  parser y una URL; la estructura ya está.
- **Sin ejecución.** El relay no ordena nada: observa.
- **Sin lógica de señales.** No decide cuándo alertar; eso lo hace el agente o
  el scanner. Un relay que "interpreta" es un bot escondido.
- **Geografía.** Binance puede bloquear el stream según el país; si no conecta,
  probar testnet o usar otro exchange.
- **Un proceso por fuente.** `binance` y `ai-trader` corren como dos servicios
  separados, o en dos terminales.

## Pendiente

- WebSocket del CLOB de Polymarket (precio de outcomes en vivo).
- Que el relay alerte por umbral de precio directo (hoy el scanner lee el bus y
  evalúa cada 5 minutos).
- Compresión/rotación más fina del bus (hoy rota un solo archivo).
