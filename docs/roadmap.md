# Roadmap

El entorno cubre el ciclo completo en paper, pero le faltan las piezas que lo
vuelven seguro para capital real. Este documento lista los huecos detectados,
por qué importan y cómo se cierran.

## Estado actual

| Área | Estado |
| --- | --- |
| Agente con reglas, skills y comandos | Listo |
| Paper trading y copy trading en AI-Trader | Listo |
| Ejecución crypto sandbox | Validado con órdenes limit |
| Ejecución acciones paper (Alpaca) | Listo |
| Scanner 24/7 con push | Listo |
| Collector de snapshots | Listo |
| Cálculo de tamaño de posición | Listo (`scripts/position_size.py`) |
| Preflight de entorno | Listo (`scripts/preflight.py`) |
| Motor de riesgo que ejecute stops | Listo (`risk_engine.py` + tests) |
| Métricas automáticas y journal | Listo (`scripts/metrics.py` + tests) |
| Backtester | **Falta** |
| Reconciliación paper ↔ real | Listo (`scripts/reconcile.py` + tests) |
| Kill switch global | Parcial: `preflight.py` detecta `state/HALT` |

---

## Prioridad 1 — seguridad operativa

### 1.1 Motor de riesgo (stop executor)

**Problema.** Los stops son mentales. Si la sesión está cerrada, nadie cierra
una posición que perforó el stop. Es la diferencia entre una pérdida planificada
y una pérdida arbitraria.

**Estado: implementado** en `risk_engine.py` (ver
[`risk-engine.md`](risk-engine.md)).

- Proceso sin LLM que corre cada 60 s.
- Planes en `state/positions.json`; el stop es obligatorio al registrarlos.
- Cierra a mercado cuando el precio perfora el stop (o el target, si está
  habilitado).
- `client_order_id` estable: un reinicio consulta la orden en lugar de duplicar
  el cierre.
- Venues live bloqueados salvo `RISK_ENGINE_LIVE=true`.
- Log en `state/risk-log.jsonl`; tres rechazos ⇒ `failed` y revisión humana.
- 24 tests unitarios que cubren validación, breaches, idempotencia y huérfanos.

**Pendiente dentro del mismo ítem.**

- [x] cierre automático y persistente
- [x] idempotencia entre reinicios
- [ ] integración opcional con el journal (cerrar el plan actualiza el dashboard)
- [ ] cierre por cantidad exacta del venue en fills parciales
- [x] prueba manual en testnet documentada con fecha y resultado
      (2026-11-26, ver [`risk-engine.md`](risk-engine.md#validación-en-testnet))

### 1.2 Kill switch

**Problema.** Detener todo requiere recordar comandos en tres sistemas.

**Especificación.**

- Archivo `state/HALT` (o variable `TRADING_HALTED=true`).
- `preflight.py` lo detecta y falla.
- MCP de CCXT y cualquier script de ejecución lo consultan antes de operar.
- Crear el archivo no cierra posiciones: bloquea nuevas entradas y deja que el
  motor de riesgo gestione las abiertas.

**Criterios de aceptación.**

- [x] `preflight.py` detecta `state/HALT` y bloquea la sesión
- [ ] las herramientas de ejecución consultan `HALT` antes de enviar una orden
- [ ] el estado se informa en `/trading-scan`

### 1.3 Reconciliación paper ↔ real

**Problema.** AI-Trader, el exchange y el journal pueden divergir y nadie lo
detecta.

**Estado: implementado** en `scripts/reconcile.py` (ver
[`reconciliation.md`](reconciliation.md)).

- Compara cuatro libros: planes, paper, venue y journal.
- Reporta `plan_missing_at_venue`, `plan_qty_short`, `plan_qty_extra`,
  `plan_qty_unknown`, `venue_unmanaged`, `paper_without_plan`,
  `plan_without_paper` y `journal_missing`.
- Salida en texto, Markdown (para el handoff) y JSON.
- Nunca corrige: propone y espera decisión humana.
- 18 tests unitarios sobre cada tipo de diferencia y el render.

**Pendiente dentro del mismo ítem.**

- [ ] detectar órdenes en vuelo (`new` / `partially_filled`) para evitar
      falsos positivos entre dos corridas
- [ ] comparar también contra `risk-log.jsonl` para auditar cierres
- [ ] programarlo junto al motor (check diario) y publicar el resultado en el
      dashboard

---

## Prioridad 2 — medición

### 2.1 Journal y métricas automáticas

**Problema.** Sin métricas calculadas, la decisión de promover de modo se toma
de memoria.

**Estado: implementado** en `scripts/metrics.py` (ver
[`metrics.md`](metrics.md)).

- Journal canónico en `state/trades.csv` (una fila por operación cerrada).
- Calcula: cerradas, win rate, profit factor, Sharpe, max drawdown, R medio,
  duración media, expectancy.
- Verifica los umbrales de `docs/trading-modes.md` y emite
  `promote` / `hold` / `degrade` / `insufficient_sample`.
- Genera `.opencode/knowledge/DASHBOARD.md` con cuenta, posiciones valorizadas,
  métricas y planes de riesgo activos.
- Modo `--offline` para operar sin red; `--json` para consumo automático.
- 19 tests unitarios sobre el cálculo, los veredictos y el render.

**Pendiente dentro del mismo ítem.**

- [ ] importar el formato markdown histórico de `knowledge/trade-log/`
- [ ] que el motor de riesgo agregue la fila al cerrar (hoy es manual)
- [ ] slippage real y funding como columnas separadas
- [ ] comparación automática contra benchmark (BTC / SPY)

### 2.2 Backtester sobre snapshots

**Problema.** El collector guarda snapshots desde el día uno y no se usan para
validar reglas.

**Especificación.**

- `scripts/backtest.py` reproduce snapshots en orden cronológico.
- Evalúa las reglas declaradas (consenso, macro, F&G) y simula entradas con
  stop, objetivo y fees.
- Reporte: curva de equity, drawdown, trades, sensibilidad a umbrales.

**Criterios de aceptación.**

- [ ] el backtest es determinístico para el mismo rango de fechas
- [ ] incluye fees y slippage configurable
- [ ] permite comparar dos juegos de umbrales

---

## Prioridad 3 — calidad de vida

### 3.1 Dedup y cooldown de alertas

**Problema.** Si el consenso persiste, el scanner alerta cada 5 minutos. El
operador termina silenciando el canal.

**Especificación.** Estado en `alert_state.json` con `(símbolo, lado)` y
timestamp; suprimir repeticiones dentro de `ALERT_COOLDOWN` (default 60 min) y
avisar cuando el consenso se rompe.

### 3.2 Retención de snapshots

**Problema.** Un snapshot cada 5 minutos crece sin límite.

**Especificación.** Script de compactación: mantener snapshots crudos 7 días,
resumen horario 90 días, resumen diario indefinido.

### 3.3 Tests y CI

**Problema.** `scanner.py` y `collector.py` no tienen tests; los cambios se
validan en producción.

**Especificación.**

- Tests de las funciones puras: normalización de señales, umbrales, detección
  de horario de mercado, cálculo de tamaño.
- CI en cada push: `compileall`, `ruff`, `pytest`.
- Fixtures con respuestas de API grabadas (sin red en CI).

### 3.4 Gestión de secretos

**Problema.** Variables de entorno y `.env` funcionan, pero rotar y auditar es
manual.

**Especificación.** Integrar un gestor (1Password CLI, Doppler o SOPS) y hacer
que `preflight.py` valide que los secretos provienen del gestor. Nunca
commitear valores.

---

## Prioridad 4 — cobertura

### 4.1 Polymarket ejecutable

Hoy es solo lectura de mercados. Falta confirmar si la plataforma expone
ejecución y, si no, integrar un cliente on-chain con límites propios.

### 4.2 Multi-exchange

`CCXT_EXCHANGE` ya permite cambiar de exchange, pero los límites y símbolos
están calibrados para Binance. Falta una capa de normalización de mínimos,
steps y fees por exchange.

### 4.3 Alertas de mercado en tiempo real

El collector es por polling (5 min). Para estrategias intradía hace falta
WebSocket (`wss://ai4trade.ai/ws/notify/{client_id}`) y un bus de eventos local.

---

## Cómo retomar

1. Elegir un ítem de Prioridad 1.
2. Escribir los criterios de aceptación como tests primero.
3. Implementar en un branch con CI verde.
4. Actualizar este documento y `docs/architecture.md` en el mismo PR.

La regla del repo: **un cambio de comportamiento siempre trae su documento**.
