# Multi-exchange

CCXT unifica símbolos, no reglas. Cambiar `CCXT_EXCHANGE` de `binance` a `bybit`
puede fallar por un mínimo distinto, un step más grueso o una passphrase que
falta. Esta capa deriva las reglas del propio exchange y normaliza la orden
antes de enviarla.

## Qué se normaliza

`exchange_rules.py` lee el mercado que devuelve CCXT y construye:

| Dato | De dónde | Para qué |
| --- | --- | --- |
| `min_amount` | `limits.amount.min` | Rechazar órdenes que el venue no acepta |
| `max_amount` | `limits.amount.max` | Recortar cantidades absurdas |
| `amount_step` | `limits.amount.step` o `precision.amount` | Redondear hacia abajo sin inventar decimales |
| `min_cost` | `limits.cost.min` | Notional mínimo del par |
| `price_tick` | `precision.price` | Redondear precios |
| `taker_fee` / `maker_fee` | Mercado | Estimar costos |

La cantidad se redondea **hacia abajo** con `Decimal` (nunca hacia arriba: pedir
más de lo que hay es peor que pedir de menos). El precio se redondea al tick más
cercano.

## Uso

```bash
# qué exchanges hay y qué requieren
python scripts/exchange_profile.py list

# reglas reales del par
python scripts/exchange_profile.py profile --symbol BTC/USDT
python scripts/exchange_profile.py profile --symbol ETH/USDT --exchange bybit --sandbox

# validar una orden antes de mandarla
python scripts/exchange_profile.py check --symbol BTC/USDT --qty 0.0002 --price 60440
```

Ejemplo de `profile` en Binance testnet:

```text
binance (sandbox)
BTC/USDT (spot) · min amount=1e-05 · step=1e-05 · min cost=5.0 · tick=0.01 · taker=0.001
precio actual: 60440.07
mínimo operativo: 0.00017 unidades (≈ $10.27)
fee taker estimado: 0.100% por lado
```

`mínimo operativo` combina el mínimo del exchange con tu piso en USD
(`MIN_NOTIONAL_USD`, default 10). Es la cantidad más chica que podés operar sin
que te rechacen la orden.

Ejemplo de `check` con una orden inválida:

```text
binance · BTC/USDT · qty 1e-05 -> 1e-05
  - notional $0.60 por debajo del mínimo $10.00 (mínimo operativo: 0.00017 unidades)
```

Código de salida 1: sirve para frenar un script antes de llegar al exchange.

## Integración con el motor de riesgo

Antes de cerrar una posición, el motor:

1. normaliza `plan.qty` al step del exchange;
2. verifica el mínimo operativo contra el precio actual;
3. si no se puede, marca el plan como `failed` con un error **permanente**
   (no reintenta tres veces algo que nunca va a funcionar).

El mensaje queda en `state/risk-log.jsonl` como `close_failed_permanent`. Es una
alerta real: significa que el plan protegía una cantidad que el venue no acepta,
y hay que corregir el plan a mano.

## Exchanges

`python scripts/exchange_profile.py list` consulta ccxt en runtime (versión
4.5.63 al escribir esto):

| Exchange | Password | Sandbox |
| --- | --- | --- |
| binance | no | sí |
| bybit | no | sí |
| okx | sí (passphrase) | sí |
| kucoin | sí (passphrase) | no |
| kraken | no | no |
| coinbase | no | no |

`password: sí` significa que la API key necesita una passphrase además de key y
secret: se configura en `CCXT_PASSWORD`.

**Sandbox no es igual en todos.** Binance y Bybit tienen testnet usable. OKX
requiere claves de demo trading. KuCoin y Kraken no tienen sandbox: para
probarlos se usa una cuenta real con montos mínimos, o directamente no se
prueban.

## Cambiar de exchange

Checklist antes de tocar `CCXT_EXCHANGE`:

- [ ] `python scripts/exchange_profile.py list` y confirmar password/sandbox
- [ ] claves del exchange nuevo en `mcp/ccxt/.env` (`CCXT_API_KEY`, `CCXT_SECRET`, `CCXT_PASSWORD`)
- [ ] `python mcp/ccxt/server.py --healthcheck` sin errores
- [ ] `python scripts/exchange_profile.py profile --symbol BTC/USDT` muestra límites
- [ ] `python scripts/exchange_profile.py check` con la cantidad que pensás usar
- [ ] una orden limit mínima en sandbox (o real mínima si no hay sandbox)
- [ ] `python scripts/preflight.py` con drift de reloj OK para el exchange nuevo

## Símbolos

Formato unificado de CCXT:

| Mercado | Formato | Ejemplo |
| --- | --- | --- |
| Spot | `BASE/QUOTE` | `BTC/USDT` |
| Futuros perpetuos | `BASE/QUOTE:SETTLE` | `BTC/USDT:USDT` |
| Opciones | `BASE/QUOTE:SETTLE-YYMMDD-STRIKE-C/P` | — |

Nunca usar `BTCUSDT` ni `btc/usdt`. Si el símbolo no existe en el exchange, el
`profile` lo dice antes de que falle una orden.

## Límites que siguen siendo globales

Estos topes son del entorno, no del exchange, y aplican en cualquier venue:

- `MAX_ORDER_VALUE_USD` — tope por orden.
- `MAX_SESSION_VALUE_USD` — tope acumulado por sesión.
- `MIN_NOTIONAL_USD` — piso propio, por encima del mínimo del exchange.
- `MAX_SLIPPAGE_PCT` — solo órdenes market.

Un exchange con mínimo de $20 hace que `MIN_NOTIONAL_USD=10` no alcance: el
efectivo es el mayor de los dos.

## Pendiente

- **Fees por exchange en el backtester.** Hoy `--fee` es un valor fijo. La capa
  ya expone `taker_fee`/`maker_fee` por mercado; falta inyectarlos en el replay.
- **Swap y futuros.** La normalización aplica igual, pero el motor asume spot
  (cierre vendiendo el activo base). Para futuros hay que cerrar con la orden
  opuesta y considerar liquidación.
- **Matriz de pruebas por exchange.** Hay tests de las reglas, no de cada venue
  contra su testnet.
