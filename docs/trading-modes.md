# Modos de ejecución y promoción

El entorno tiene seis modos. Cada uno habilita más capacidad y exige más
evidencia que el anterior. La regla es simple: **se sube un escalón cuando los
números lo justifican, nunca cuando la tesis suena convincente**.

## Resumen

| Modo | Herramienta | Qué mueve | Se habilita con |
| --- | --- | --- | --- |
| `research` | AI-Trader | Nada | Token de AI-Trader |
| `paper` | AI-Trader | $100K simulados | Token de AI-Trader |
| `crypto-sandbox` | CCXT MCP | Fondos de testnet | `CCXT_SANDBOX=true` |
| `crypto-live` | CCXT MCP | Dinero real | `ALLOW_LIVE=true`, `DRY_RUN=false`, armado |
| `stocks-paper` | Alpaca MCP | Cuenta paper | `ALPACA_PAPER_TRADE=true` |
| `stocks-live` | Alpaca MCP | Dinero real | `ALPACA_PAPER_TRADE=false` |

`.` Los modos son independientes por mercado. Se puede estar en `paper` para
crypto y `stocks-paper` para acciones al mismo tiempo.

---

## 1. Research

Sin operaciones. Sirve para reconocer el terreno:

- leer `/api/market-intel/overview`, `/news`, `/macro-signals`;
- revisar el feed y los perfiles de agentes;
- mapear símbolos, horarios y liquidez;
- escribir la tesis inicial en `.opencode/knowledge/`.

Criterio de salida: entender qué señales existen, con qué frecuencia y con qué
calidad.

## 2. Paper (AI-Trader)

Todo se registra como señal y se liquida contra el capital simulado.

| Acción | Endpoint |
| --- | --- |
| Operar | `POST /api/signals/realtime` |
| Documentar tesis | `POST /api/signals/strategy` |
| Reflexionar | `POST /api/signals/discussion` |
| Ver posiciones | `GET /api/positions` |
| Seguir agentes | `POST /api/signals/follow` |

Reglas:

- cada operación con stop y objetivo documentados antes de publicar;
- máximo 2% de riesgo por operación, máximo 5 operaciones por día;
- toda entrada tiene tesis escrita; sin tesis, no hay señal.

Es el único modo que puede durar meses. La mayoría de las estrategias mueren
acá, y está bien: es el lugar barato para morir.

## 3. Crypto sandbox (testnet)

Objetivo: comprobar que la ejecución funciona, no que la estrategia gana.

Configuración:

```dotenv
CCXT_EXCHANGE=binance
CCXT_SANDBOX=true
ALLOW_LIVE=false
DRY_RUN=false          # ejecuta contra testnet
MAX_ORDER_VALUE_USD=50
```

Notas operativas:

- Los símbolos usan formato unificado: `BTC/USDT`, `ETH/USDT`. `BTCUSDT` es error.
- Las órdenes **limit** funcionan de forma estable. Las **market** presentaron
  problemas de decimales en el sandbox: usarlas solo para validar compuertas.
- El sandbox no necesita depósito ni claves reales.
- El reloj también importa acá: si el desvío supera 5 s, la orden se bloquea.

Checklist de salida del sandbox:

- [ ] orden limit de compra ejecutada y visible en `get_open_orders`
- [ ] orden cancelada correctamente
- [ ] una orden deliberadamente inválida es rechazada con el error esperado
- [ ] `reconcile_order()` resuelve un timeout simulado
- [ ] `get_balance()` refleja el movimiento

## 4. Crypto live

Requisitos acumulativos:

1. Métricas de paper y sandbox que cumplan la tabla de promoción (abajo).
2. Claves live con permiso de **trade** y sin permiso de retiro.
3. `ALLOW_LIVE=true` y `DRY_RUN=false` en `mcp/ccxt/.env`.
4. Reloj sincronizado (`w32tm /resync /force` en Windows).
5. `MAX_ORDER_VALUE_USD` bajo al inicio ($20–50).
6. `arm_live_session()` al comenzar cada sesión y `disarm_live_session()` al
   terminarla.

Protocolo de sesión:

```text
1. exchange_info()        → confirmar modo LIVE, límites y clock_ok
2. get_balance()          → saldo disponible real
3. get_ticker(symbol)     → precio fresco
4. calcular qty con scripts/position_size.py
5. publicar la señal paper equivalente
6. create_order()         → limit preferentemente
7. get_open_orders()      → confirmar que quedó viva
8. disarm_live_session()  → al cerrar la sesión
```

Nunca:

- reintentar a ciegas tras un timeout (usar `reconcile_order`);
- operar sin `client_order_id` registrado en el journal;
- subir límites en caliente durante una racha perdedora;
- dejar la sesión armada sin supervisión.

## 5. Stocks paper (Alpaca)

- `ALPACA_PAPER_TRADE=true`.
- Solo 7 toolsets habilitados: `trading`, `account`, `stock-data`, `positions`,
  `assets`, `watchlists`, `news`.
- Fracciones de acción permitidas.
- Órdenes fuera de horario quedan en cola hasta la apertura.

Buenas prácticas:

- usar `get_clock` antes de operar para no asumir mercado abierto;
- preferir órdenes bracket (stop + take profit) en lugar de stops mentales;
- para ETF líquidos (SPY, QQQ) el slippage es despreciable; en small caps no.

## 6. Stocks live (Alpaca)

- `ALPACA_PAPER_TRADE=false` con claves live.
- Misma tabla de promoción que crypto, medida sobre operaciones de acciones.
- Empezar con 1–2 posiciones y montos mínimos.
- Revisar el calendario de earnings: una posición abierta en earnings no es una
  estrategia, es una apuesta.

---

## Criterios de promoción

Se evalúan sobre **operaciones cerradas**, no sobre posiciones abiertas, y solo
con journal completo (entrada, stop, objetivo, tesis, salida).

| Métrica | Umbral | Por qué |
| --- | --- | --- |
| Operaciones cerradas | ≥ 50 | Menos que eso es ruido |
| Win rate | > 55% | Consistencia direccional |
| Profit factor | > 1.5 | Ganancias brutas / pérdidas brutas |
| Sharpe ratio | > 1.0 | Retorno ajustado por volatilidad |
| Max drawdown | < 10% | Supervivencia |
| Semanas en paper | ≥ 4 | Cubre distintos regímenes |
| Estrategia documentada | Sí | Reproducible por otra persona |

Cálculo:

```text
profit_factor = suma(ganancias) / |suma(pérdidas)|
win_rate      = ganadoras / cerradas
sharpe        ≈ (retorno_medio_diario / desvío_retorno_diario) * sqrt(252)
max_drawdown  = max(1 - equity / máximo_equity_previo)
```

Si falta cualquier fila, no se promueve. No hay excepciones por "esta vez es
distinto".

## Reglas de degradación

Se baja de modo, sin discusión, cuando:

- el drawdown supera 10% en real o 15% en paper;
- aparecen tres pérdidas consecutivas por encima del riesgo planificado;
- una orden real queda en estado desconocido más de una sesión;
- el journal tiene operaciones sin tesis o sin stop.

Degradar no es fracasar: es el mecanismo que evita que un mal mes se convierta
en la pérdida de la cuenta.

---

## Errores frecuentes

| Error | Consecuencia | Corrección |
| --- | --- | --- |
| Operar en real "para probar" | Pérdida sin datos | Usar paper; el sandbox existe para eso |
| Medir con 5 operaciones | Métricas sin significado | Esperar 50 cierres |
| Cambiar la estrategia en cada pérdida | Nunca se valida nada | Congelar reglas por 4 semanas |
| Mover el stop en contra | Pérdida mayor a la planificada | El stop solo se mueve a favor |
| Copiar a un agente sin muestra | Se copia suerte, no habilidad | Ver [`copytrading.md`](copytrading.md) |
| Dejar `ALLOW_LIVE=true` siempre | Una sesión olvidada opera sola | Armar y desarmar por sesión |
