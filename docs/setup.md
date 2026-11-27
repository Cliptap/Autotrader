# Instalación

Guía completa para levantar el entorno desde cero. Al final hay una checklist
para verificar que nada quedó a medias.

## Requisitos

| Componente | Versión | Para qué |
| --- | --- | --- |
| Python | 3.10+ | Scanner, collector, MCP de CCXT, scripts |
| [OpenCode](https://opencode.ai/docs/) | última | Agente |
| [uv](https://docs.astral.sh/uv/) | última | Ejecutar el MCP de Alpaca con `uvx` |
| Git | cualquiera | Clonar y desplegar el scanner |
| [ntfy](https://ntfy.sh) (app móvil) | — | Notificaciones push |
| Cuenta AI-Trader | — | Paper trading |
| Cuenta Alpaca paper | gratis | Acciones y ETF |
| Cuenta Binance testnet | gratis | Sandbox crypto |

---

## 1. Clonar e instalar dependencias

```bash
git clone https://github.com/Cliptap/Autotrader.git
cd Autotrader

python -m venv .venv
```

Activar el entorno:

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

Instalar dependencias del entorno local:

```bash
pip install -r requirements.txt
```

El MCP de CCXT tiene sus propias dependencias (no se instalan acá):

```bash
pip install -r mcp/ccxt/requirements.txt
```

---

## 2. Crear la configuración base

```bash
cp config/opencode.jsonc.example opencode.jsonc
cp config/ccxt.env.example      mcp/ccxt/.env
cp config/collector.env.example .env
```

`opencode.jsonc` no contiene secretos: usa `{env:VARIABLE}`. Los valores reales
van en variables de entorno o en los archivos `.env`, que están en `.gitignore`.

---

## 3. Registrar el agente en AI-Trader

Cada agente tiene identidad propia y $100K simulados. El registro devuelve un
token que es la única credencial:

```bash
curl -X POST https://ai4trade.ai/api/claw/agents/selfRegister \
  -H "Content-Type: application/json" \
  -d '{"name":"MyTradingBot","email":"bot@example.com","password":"cambiar-esto"}'
```

Respuesta:

```json
{ "success": true, "token": "...", "agent_id": 123, "name": "MyTradingBot" }
```

Si la cuenta ya existe, se usa `POST /api/claw/agents/login` con email y
password. Verificar la identidad:

```bash
curl https://ai4trade.ai/api/claw/agents/me \
  -H "Authorization: Bearer $CLIPTAP_AI_TOKEN"
```

Guardar el token como `CLIPTAP_AI_TOKEN` y el `agent_id` como
`CLIPTAP_AGENT_ID`. **Nunca** commitear el token: quien lo tiene puede operar la
cuenta.

---

## 4. Variables de entorno

| Variable | Obligatoria | Default | Usada por |
| --- | --- | --- | --- |
| `CLIPTAP_AI_TOKEN` | Sí | — | scanner, collector, scripts |
| `CLIPTAP_AGENT_ID` | No | `0` | collector |
| `CLIPTAP_NTFY_TOPIC` | Sí | `cliptap-alerts` | scanner |
| `ALERT_COOLDOWN_MIN` | No | `60` | scanner |
| `ALPACA_API_KEY` | Para acciones | — | MCP de Alpaca |
| `ALPACA_SECRET_KEY` | Para acciones | — | MCP de Alpaca |
| `ALPACA_PAPER_TRADE` | No | `true` | MCP de Alpaca |
| `ALPACA_TOOLSETS` | No | 7 toolsets | MCP de Alpaca |
| `CCXT_*` | Para crypto | ver abajo | MCP de CCXT (lee `mcp/ccxt/.env`) |
| `RISK_ENGINE_LIVE` | No | `false` | `risk_engine.py` (venues live) |
| `RISK_POLL_INTERVAL` | No | `60` | `risk_engine.py` |

Configurarlas de forma persistente:

```powershell
# Windows: aplica a procesos nuevos
setx CLIPTAP_AI_TOKEN "tu_token"
setx CLIPTAP_NTFY_TOPIC "cliptap-alerts"
setx ALPACA_API_KEY "PK..."
setx ALPACA_SECRET_KEY "..."
```

```bash
# macOS / Linux (~/.zshrc o ~/.bashrc)
export CLIPTAP_AI_TOKEN="tu_token"
export CLIPTAP_NTFY_TOPIC="cliptap-alerts"
export ALPACA_API_KEY="PK..."
export ALPACA_SECRET_KEY="..."
```

Alternativa sin variables globales: reemplazar `{env:ALPACA_API_KEY}` por
`{file:secrets/alpaca_api_key}` en `opencode.jsonc` y guardar cada valor en un
archivo dentro de `secrets/` (ignorado por git).

---

## 5. Configurar el MCP de crypto (CCXT)

### Sandbox (recomendado para empezar)

1. Crear claves en <https://testnet.binance.vision> (gratis, sin depósito).
2. Editar `mcp/ccxt/.env`:

```dotenv
CCXT_EXCHANGE=binance
CCXT_SANDBOX=true
CCXT_API_KEY=clave_testnet
CCXT_SECRET=secreto_testnet

ALLOW_LIVE=false
DRY_RUN=true
MAX_ORDER_VALUE_USD=100
MIN_NOTIONAL_USD=10
MAX_SLIPPAGE_PCT=1.0
CLOCK_MAX_DRIFT_MS=5000
DEDUP_WINDOW_SECONDS=30
RATE_LIMIT_ORDER_SEC=2.0
```

3. Verificar la conexión:

```bash
python mcp/ccxt/server.py --healthcheck
```

4. Ver las reglas reales del exchange y validar la cantidad:

```bash
python scripts/exchange_profile.py profile --symbol BTC/USDT
python scripts/exchange_profile.py check --symbol BTC/USDT --qty 0.0002 --price 60000
```

Los límites completos y las 8 compuertas están en
[`docs/trading-modes.md`](trading-modes.md). Para operar en otro exchange, ver
[`docs/multi-exchange.md`](multi-exchange.md).

### Live

No se configura hasta cumplir los criterios de promoción. Cuando llegue el
momento, se cambian las claves y se habilitan `ALLOW_LIVE=true` y
`DRY_RUN=false`. Aun así, cada sesión real requiere `arm_live_session()`.

---

## 6. Configurar el MCP de acciones (Alpaca)

1. Crear cuenta paper en <https://app.alpaca.markets/paper/dashboard/overview>.
2. Generar claves (paper).
3. Exportarlas como `ALPACA_API_KEY` y `ALPACA_SECRET_KEY`.
4. Dejar `ALPACA_PAPER_TRADE=true`.

`opencode.jsonc` lanza el servidor con:

```jsonc
"alpaca": {
  "type": "local",
  "command": ["uvx", "alpaca-mcp-server"],
  "environment": {
    "ALPACA_API_KEY": "{env:ALPACA_API_KEY}",
    "ALPACA_SECRET_KEY": "{env:ALPACA_SECRET_KEY}",
    "ALPACA_PAPER_TRADE": "{env:ALPACA_PAPER_TRADE}",
    "ALPACA_TOOLSETS": "trading,account,stock-data,positions,assets,watchlists,news"
  }
}
```

Si `uvx` no está instalado: `pip install uv` o seguir
<https://docs.astral.sh/uv/getting-started/installation/>.

---

## 7. Configurar el scanner y las notificaciones

1. Instalar la app de ntfy en el teléfono y suscribirse al topic
   (por defecto `cliptap-alerts`). Usar un topic difícil de adivinar: cualquiera
   que lo conozca puede leer las alertas.
2. Subir el repo a GitHub (público o privado).
3. Settings → Secrets and variables → Actions:

| Secreto | Valor |
| --- | --- |
| `CLIPTAP_AI_TOKEN` | token del agente |
| `CLIPTAP_NTFY_TOPIC` | topic de ntfy |

4. El workflow `.github/workflows/scanner.yml` corre cada 5 minutos. Se puede
   disparar a mano desde la pestaña Actions.

> **Costo**: en un repo privado, 288 ejecuciones diarias (~1 min cada una)
> superan los 2000 minutos gratis de GitHub. En un repo público los minutos son
> ilimitados. Si el repo es privado, espaciar el cron a `*/15`.

Correr el scanner localmente:

```bash
python scanner.py
```

---

## 8. Configurar el collector (opcional pero recomendado)

```bash
python collector.py
```

Se abre en la bandeja del sistema. Menú: estado, forzar snapshot, pausar,
salir. Guarda en `.opencode/knowledge/market-data/` y persiste el estado en
`collector_state.json` con escrituras atómicas.

Variables: `CLIPTAP_AI_TOKEN` (obligatoria), `CLIPTAP_AGENT_ID`,
`COLLECT_INTERVAL` y `HEARTBEAT_INTERVAL` (segundos).

---

## 9. Motor de riesgo

El motor cierra posiciones cuando el stop se perfora, aunque no haya sesión
abierta. Se instala como servicio para que se reinicie solo.

```bash
python risk_engine.py check
python risk_engine.py run --once --dry-run
```

Programarlo al iniciar el sistema (Task Scheduler en Windows, systemd en Linux)
está detallado en [`risk-engine.md`](risk-engine.md#programarlo).

Abrir posiciones reales sin el motor corriendo es operar sin stop.

---

## 10. Journal y métricas

Crear el journal y generar el primer dashboard:

```bash
python scripts/metrics.py init
python scripts/metrics.py report
python scripts/metrics.py dashboard --out .opencode/knowledge/DASHBOARD.md
```

A partir de ahí, cada operación cerrada agrega una fila a `state/trades.csv`
(formato en `config/trades.example.csv`). El dashboard es un artefacto generado
y está en `.gitignore`.

Detalle: [`docs/metrics.md`](metrics.md).

---

## 11. Retención de snapshots

Los snapshots crudos crecen sin límite. Compactarlos conserva precios y conteos
de señales en resolución horaria y diaria:

```bash
python scripts/retention.py           # dry-run: muestra el plan
python scripts/retention.py --apply   # ejecuta
```

Programalo mensualmente. Detalle: [`docs/retention.md`](retention.md).

---

## 12. Verificar

```bash
python scripts/preflight.py
```

Revisa versión de Python, dependencias, configuración, secretos y conectividad
con AI-Trader, Binance y Alpaca. Sale con código 1 si algo bloquea la operación.

Luego, dentro de OpenCode:

```text
/mcps          # ccxt y alpaca deben figurar como conectados
```

```bash
opencode mcp list
```

Y un primer ciclo de lectura:

```text
/trading-scan
```

Debe devolver posiciones, régimen de mercado y candidatos, sin ejecutar nada.

---

## Checklist

- [ ] `python scripts/preflight.py` sin errores
- [ ] `opencode mcp list` muestra `ccxt` y `alpaca` conectados
- [ ] `GET /api/claw/agents/me` devuelve el agente correcto
- [ ] `python scanner.py` una vez y llega la notificación al teléfono
- [ ] Alpaca responde `get_clock` y el mercado está abierto o en cola
- [ ] `python mcp/ccxt/server.py --healthcheck` reporta modo SANDBOX
- [ ] `python -m pytest -q` pasa
- [ ] `python risk_engine.py check` sin planes inválidos
- [ ] el motor de riesgo está programado y corriendo
- [ ] `python scripts/metrics.py report` calcula métricas sin errores
- [ ] el dashboard se regenera y muestra posiciones reales
- [ ] `python scripts/reconcile.py` no reporta errores
- [ ] `python scripts/backtest.py` corre sobre los snapshots disponibles
- [ ] `python scripts/retention.py` informa un plan coherente
- [ ] `alert_state.json` no aparece en `git status`
- [ ] `python polymarket.py markets --limit 3` devuelve mercados
- [ ] `.env`, `mcp/ccxt/.env` y `secrets/` no aparecen en `git status`
- [ ] `/trading-scan` corre de punta a punta sin ejecutar órdenes
