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
| Backtester | Listo (`scripts/backtest.py` + tests) |
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

**Estado: implementado** en `scripts/backtest.py` (ver
[`backtest.md`](backtest.md)).

- Replay cronológico y determinístico de los snapshots del collector.
- Deduplica señales por id y usa el momento de observación (sin lookahead).
- Reglas configurables: consenso, ventana, filtro macro, stop, objetivo, tiempo
  máximo, cooldown de reentrada, notional, fees y slippage.
- Reutiliza `metrics.compute_metrics`, así el backtest y el journal se miden
  igual.
- `--sweep` compara consenso 1-3 × macro 0/3.
- Advierte cuando el rango es corto o las posiciones quedan abiertas al final.
- 18 tests unitarios, incluidos determinismo y no-lookahead.

**Pendiente dentro del mismo ítem.**

- [ ] curva de equity exportable (CSV/PNG)
- [ ] variantes de estrategia más allá del consenso (F&G, RSI)
- [ ] walk-forward: calibrar en una ventana y validar en la siguiente

---

## Prioridad 3 — calidad de vida

### 3.1 Dedup y cooldown de alertas

**Problema.** Si el consenso persiste, el scanner alerta cada 5 minutos. El
operador termina silenciando el canal.

**Estado: implementado** (ver [`scanner.md`](scanner.md#deduplicación-y-cooldown)).

- Estado por clave en `alert_state.json`, persistido con escritura atómica.
- Cooldown configurable con `ALERT_COOLDOWN_MIN` (default 60).
- Aviso único de "consenso terminado" cuando la señal desaparece.
- En GitHub Actions el estado viaja por `actions/cache` y hay `concurrency`
  para evitar scans solapados.
- 21 tests: parsing de señales, umbrales, horario de mercado con DST y dedup.

**Pendiente.**

- [ ] agrupar varias alertas del mismo scan en una sola notificación
- [ ] permitir silenciar un símbolo por más tiempo (`snooze`)

### 3.2 Retención de snapshots

**Problema.** Un snapshot cada 5 minutos crece sin límite.

**Estado: implementado** en `scripts/retention.py` (ver
[`retention.md`](retention.md)).

- Crudos 7 días, horarios 90 días, diarios para siempre.
- Cada resumen conserva OHLC por símbolo y conteo de señales buy/sell.
- Dry-run por defecto; `--apply` para ejecutar.
- Idempotente y con escritura atómica; 11 tests.

**Pendiente.**

- [ ] que el backtester pueda leer resúmenes horarios (hoy necesita crudos)
- [ ] comprimir los diarios en un único parquet/CSV anual

### 3.3 Tests y CI

**Problema.** `scanner.py` y `collector.py` no tienen tests; los cambios se
validan en producción.

**Estado: implementado.**

- 112 tests en total (riesgo, métricas, reconciliación, backtest, scanner,
  retención y sanitización).
- `scanner.py` se dividió en funciones puras (`collect_*`, `evaluate_*`,
  `dispatch`) para poder probarlas sin red.
- CI en cada push: `compileall`, `ruff` (errores críticos) y `pytest`.
- Los tests no tocan red ni exchanges.

**Pendiente.**

- [ ] fixtures con respuestas reales de API grabadas
- [ ] cobertura mínima exigida en CI

### 3.4 Gestión de secretos

**Problema.** Variables de entorno y `.env` funcionan, pero rotar y auditar es
manual.

**Estado: parcial.**

- `preflight.py` verifica que `.env` no esté versionado (`git ls-files`).
- `sanitize.py` redacta credenciales antes de persistir respuestas de API.
- Falta integrar un gestor (1Password CLI, Doppler o SOPS) y validar en
  `preflight` que los valores provienen de ahí.

**Pendiente.**

- [ ] elegir gestor e integrarlo en el arranque
- [ ] rotación documentada con fecha y responsable
- [ ] alerta si una credencial aparece en un archivo versionado

---

## Prioridad 4 — cobertura

### 4.1 Polymarket

**Estado: research + paper implementados; ejecución real fuera de alcance por
diseño** (ver [`polymarket.md`](polymarket.md)).

- Discovery y filtros contra Gamma (liquidez, volumen, cierre, búsqueda).
- Order book del CLOB con mejor bid/ask, spread y profundidad.
- Edge, Kelly fraccional y tope del 2% del equity.
- Registro paper en AI-Trader (`market: polymarket`), con `--publish` explícito.
- 19 tests de parsing, filtros, libro, edge, sizing y payload.

**Por qué no ejecuta.** Polymarket expone ejecución vía CLOB, pero requiere
custodiar una clave privada de Polygon, allowances de USDC y firma EIP-712 de
cada orden. Eso es un riesgo de custodia que no se valida en paper.

**Pendiente.**

- [ ] diseño de custodia con firmante separado (hardware wallet / KMS)
- [ ] allowances y credenciales L2 documentadas paso a paso
- [ ] prueba end-to-end en Polygon testnet
- [ ] límites propios por mercado y por evento

### 4.2 Multi-exchange

**Estado: implementado** (ver [`multi-exchange.md`](multi-exchange.md)).

- `exchange_rules.py` deriva límites, step, tick y fees del mercado que devuelve
  CCXT, sin tablas hardcodeadas.
- `scripts/exchange_profile.py` inspecciona un exchange y valida una orden antes
  de enviarla.
- El motor de riesgo normaliza la cantidad antes de cerrar y falla con un error
  permanente (sin reintentos inútiles) si el venue no acepta el tamaño.
- `preflight.py` verifica el reloj contra el exchange configurado, no contra
  Binance fijo.
- 19 tests de la capa de normalización + 1 del camino de error permanente.

**Pendiente.**

- [ ] inyectar `taker_fee`/`maker_fee` del mercado en el backtester
- [ ] soportar swap/futuros (cierre con orden opuesta, margen, liquidación)
- [ ] matriz de humo por exchange contra su testnet

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
