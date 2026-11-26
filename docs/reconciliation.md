# Reconciliación paper ↔ real

Cuatro registros cuentan la misma historia y, tarde o temprano, dejan de
coincidir: los planes del motor de riesgo, la cuenta paper de AI-Trader, la
cuenta real en el venue y el journal. La reconciliación encuentra la diferencia
antes de que la diferencia encuentre al operador.

`scripts/reconcile.py` es **read-only**. No corrige nada: reporta. Corregir un
descuadre es una decisión, no un script.

## Las cuatro fuentes

| Fuente | Dónde vive | Qué representa |
| --- | --- | --- |
| Planes | `state/positions.json` | Lo que el motor de riesgo protege |
| Paper | `GET /api/positions` | El libro simulado |
| Venue | CCXT (saldos) / Alpaca (posiciones) | El libro real |
| Journal | `state/trades.csv` | Lo que quedó registrado |

## Qué compara

### Plan contra venue

Para cada plan activo (`open` o `closing`):

| Hallazgo | Severidad | Significado |
| --- | --- | --- |
| `plan_missing_at_venue` | error | El plan espera posición y el venue no la tiene |
| `plan_qty_short` | warn | Hay menos cantidad que la registrada |
| `plan_qty_extra` | info | Hay más cantidad que la registrada |
| `plan_qty_unknown` | warn | No se pudo leer el venue (sin claves, sin red) |

En CCXT spot no existen "posiciones": se compara contra el saldo libre del
activo base (`BTC/USDT` → `BTC`). En Alpaca se compara contra la posición real.

### Venue contra planes

Una posición en Alpaca sin plan es una posición **sin stop protegido**:
`venue_unmanaged`. En CCXT no se reportan saldos sin plan porque un balance no
es una posición (el faucet de testnet, por ejemplo, tiene cientos de activos).

### Paper contra planes

- `paper_without_plan` (info): posición paper propia (`source: self`) sin plan.
- `plan_without_paper` (warn): plan activo sin posición paper equivalente.

Las posiciones **copiadas** (`source: copied:<id>`) se excluyen a propósito: no
las abriste vos y no llevan plan propio.

### Journal contra planes

Un plan cerrado por el motor debe tener su fila en `state/trades.csv`
(`journal_missing`). Si no la tiene, las métricas no lo van a contar y el
win rate queda incompleto.

La ventana de coincidencia es de 24 horas desde `closed_at`, comparando el
activo base.

## Uso

```bash
python scripts/reconcile.py                 # tabla para la consola
python scripts/reconcile.py --markdown      # bloque para el handoff
python scripts/reconcile.py --out .opencode/knowledge/reconciliation.md
python scripts/reconcile.py --json
python scripts/reconcile.py --tolerance 0.01
```

Códigos de salida:

| Código | Significado |
| --- | --- |
| `0` | Sin diferencias accionables (puede haber informativos) |
| `2` | Hay advertencias o errores |
| `1` | No se pudo leer el estado local |

`--tolerance` es relativa (default 0.5%): absorbe redondeos de cantidad sin
esconder un descuadre real.

## Cómo resolver cada hallazgo

Nunca en automático. Siempre con evidencia del venue.

| Hallazgo | Acción |
| --- | --- |
| `plan_missing_at_venue` | Verificar en el exchange/broker; si la posición no existe, `plan remove` con motivo |
| `plan_qty_short` | Revisar fills parciales; ajustar el plan a la cantidad real |
| `plan_qty_extra` | Confirmar si hubo una compra manual; si es intencional, registrar un plan nuevo |
| `venue_unmanaged` | Abrir un plan con stop o cerrar la posición |
| `paper_without_plan` | Registrar el plan si la posición es propia y sigue viva |
| `plan_without_paper` | Verificar si la señal paper se publicó; si no, publicarla |
| `journal_missing` | Agregar la fila al journal con el precio de salida real |

## Cuándo correrlo

| Momento | Por qué |
| --- | --- |
| Al abrir sesión | Antes de decidir nada |
| Después de un cierre del motor | Confirmar que el journal quedó completo |
| Antes de operar en real | Un descuadre en paper se vuelve caro en real |
| Revisión semanal | Detectar deriva silenciosa |

## Límites

- **No ve intenciones.** Si hay una posición real sin plan porque decidiste
  salir del sistema, va a seguir figurando hasta que la cierres o la registres.
- **No conoce órdenes en vuelo.** Una orden `new` en Alpaca cambia la posición
  cuando se llena; correr el reporte dos veces puede dar resultados distintos.
- **Depende de la API.** Si AI-Trader no responde, `paper_unavailable` y el
  resto del reporte sigue siendo válido.
- **No corrige.** Esa es la característica, no la limitación.
