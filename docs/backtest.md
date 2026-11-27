# Backtester

`scripts/backtest.py` reproduce los snapshots del collector en orden
cronológico y simula la estrategia de consenso: qué habría comprado, cuándo
habría salido y cuánto habría costado.

No predice el futuro. Sirve para dos cosas concretas: descartar reglas que no
funcionan con los datos que ya tenés, y comparar variantes antes de arriesgar
capital.

## Entrada

Los snapshots que escribe `collector.py` en
`.opencode/knowledge/market-data/AAAA-MM-DD/snapshot_HHMMSS.json`:

| Campo | Uso |
| --- | --- |
| `collected_at` | Orden cronológico |
| `prices` | Precio por símbolo y mercado |
| `signal_feed.signals` | Agentes, lado y símbolo |
| `macro_signals.bullish_count` | Filtro de régimen |

Se deduplican señales por `id`: una señal vista en cinco snapshots cuenta una
vez. Y se usa el **momento de observación**, no el de creación, para que no haya
lookahead.

## Reglas simuladas

| Regla | Default |
| --- | --- |
| Entrada | ≥ N agentes distintos comprando el mismo símbolo en la ventana |
| Ventana de consenso | 6 h |
| Filtro macro | `bullish_count` ≥ M (0 = sin filtro) |
| Stop | 2% |
| Objetivo | 4% |
| Tiempo máximo en posición | 72 h |
| Cooldown tras cerrar | 6 h (evita reentrar en el mismo snapshot) |
| Tamaño | notional fijo (USD 1.000) |
| Fees | Fijas (`--fee`, default 0.1% por lado) o del exchange (`--fees exchange`) |
| Slippage | 0.05% por lado |

### Fees reales

Con `--fees exchange` el backtest lee el `taker` de cada mercado desde ccxt en
vez de usar un valor único:

```bash
python scripts/backtest.py --fees exchange
```

```text
fees por exchange: BTC 0.100%, ETH 0.100%, SOL 0.100%
```

Los símbolos de los snapshots vienen sin quote (`BTC`), así que se asume el par
contra USDT en el exchange configurado (`CCXT_EXCHANGE`). Si el exchange no
responde o el símbolo no existe, cae al fee fijo y lo avisa. Las acciones no
tienen fee en Alpaca y quedan en cero.

El fee es el `taker` público: no modela descuentos por BNB, nivel de volumen ni
maker/taker distinto por orden.

Una posición por símbolo. Las salidas se procesan antes que las entradas: si el
precio perfora el stop en el mismo snapshot en que aparece una señal nueva, la
posición vieja se cierra primero.

## Uso

```bash
# configuración por defecto, sobre el directorio del collector
python scripts/backtest.py

# otro directorio (por ejemplo, datos de otra máquina)
python scripts/backtest.py --dir /ruta/market-data

# variante concreta
python scripts/backtest.py --consensus 3 --macro-min 3 --stop 1.5 --take 3

# sensibilidad: consenso 1-3 × macro 0/3
python scripts/backtest.py --sweep

# salida para otro script
python scripts/backtest.py --json
```

Salida típica:

```text
Backtest de consenso
--------------------
Snapshots        76
Rango            2026-07-02 16:31 → 2026-07-04 02:46
Señales de compra 229

Entradas         3
Win rate         0.0%
Profit factor    0.00
PnL total        $-9.00
Max drawdown     0.09%

Advertencias
  - el rango cubre 1.4 días: muestra insuficiente para concluir
  - 3/3 operaciones se cerraron al final del rango, sin tiempo para resolverse
```

Las advertencias no son decorativas: si el rango es corto o la mayoría de las
posiciones queda abierta al final, el resultado **no dice nada** sobre la
estrategia.

## Cómo leer el resultado

| Señal | Interpretación |
| --- | --- |
| Muchas entradas y PF < 1 | La regla de consenso no tiene edge con estos datos |
| Pocas entradas | Umbral alto o feed con pocas operaciones |
| Todo cerrado `open_at_end` | El rango es demasiado corto: no concluir |
| Drawdown alto con PF > 1 | El retorno depende de pocas operaciones |
| `--sweep` plano | La regla es insensible al parámetro: sospechoso, no robusto |

Un backtest con 3 operaciones no valida nada. El mínimo útil ronda las 30–50
operaciones simuladas, y eso exige semanas de snapshots.

## Límites

- **Fill optimista en gaps.** El stop se ejecuta al precio del stop; en un gap
  real se ejecuta peor. El resultado es un techo, no un piso.
- **Solo ve lo que el collector vio.** Si el collector estaba apagado, esos
  minutos no existen. El feed además viene limitado a 15 señales por snapshot.
- **Sin libro de órdenes.** No hay spread ni profundidad: solo precio de última
  operación.
- **Sin intrabar.** Los extremos se evalúan con el precio del snapshot (5 min).
  Un stop tocado y recuperado dentro del intervalo no se detecta.
- **Sesgo de supervivencia de señales.** Solo se simulan las señales que
  llegaron al feed, no las que existieron y no se publicaron.
- **Fees fijos.** No modela maker/taker distinto ni descuentos por volumen.

## Cuándo correrlo

| Momento | Para qué |
| --- | --- |
| Antes de cambiar un umbral en el scanner | Ver si el cambio tiene sustento |
| Revisión mensual | Detectar si el edge se está agotando |
| Después de sumar un agente al seguimiento | Medir su aporte real |

Nunca para "confirmar" una decisión ya tomada. Un backtest que solo se corre
cuando querés que diga que sí es una forma elegante de autoengaño.
