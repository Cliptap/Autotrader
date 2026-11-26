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
| Motor de riesgo que ejecute stops | **Falta** |
| Métricas automáticas y journal | **Falta** |
| Backtester | **Falta** |
| Reconciliación paper ↔ real | **Falta** |
| Kill switch global | Parcial: `preflight.py` detecta `state/HALT` |

---

## Prioridad 1 — seguridad operativa

### 1.1 Motor de riesgo (stop executor)

**Problema.** Los stops son mentales. Si la sesión está cerrada, nadie cierra
una posición que perforó el stop. Es la diferencia entre una pérdida planificada
y una pérdida arbitraria.

**Especificación.**

- Proceso sin LLM (`risk_engine.py`) que corre cada 60 s.
- Lee posiciones reales (CCXT `get_open_orders`, Alpaca `get_all_positions`) y
  el plan de cada operación desde `state/positions.json`.
- Si `precio <= stop` en long o `precio >= stop` en short, coloca la orden de
  cierre con `client_order_id` derivado de la posición.
- Si el broker soporta bracket/OCO, verifica que la orden protectora exista; si
  no existe, la crea.
- Escribe cada acción en `state/risk-log.jsonl`.
- Rechaza operar si el plan no tiene stop.

**Criterios de aceptación.**

- [ ] una posición con stop violado se cierra en menos de 90 s
- [ ] no se duplica la orden de cierre si el proceso reinicia
- [ ] funciona con el mercado cerrado (deja la orden en cola)
- [ ] testeado con un stop simulado en testnet

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

**Especificación.**

- Script `scripts/reconcile.py` que compara tres fuentes: posiciones paper,
  posiciones reales y journal.
- Reporta faltantes, sobrantes y cantidades distintas.
- Salida en Markdown para pegar en el handoff.
- Nunca corrige automáticamente: propone y espera confirmación.

**Criterios de aceptación.**

- [ ] una posición abierta solo en el exchange aparece como `unmanaged`
- [ ] una posición cerrada sin registro en el journal aparece como `missing_exit`

---

## Prioridad 2 — medición

### 2.1 Journal y métricas automáticas

**Problema.** Sin métricas calculadas, la decisión de promover de modo se toma
de memoria.

**Especificación.**

- `scripts/metrics.py` lee el journal (`knowledge/trade-log/*.md`) o un CSV.
- Calcula: operaciones cerradas, win rate, profit factor, Sharpe, max drawdown,
  R medio, duración media.
- Compara contra los umbrales de `docs/trading-modes.md` y emite un veredicto:
  `paper`, `promote`, `degrade`.
- Genera `DASHBOARD.md` a partir de las posiciones de la API.

**Criterios de aceptación.**

- [ ] el veredicto coincide con el cálculo manual en un set de prueba
- [ ] el dashboard se regenera sin editar a mano

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
