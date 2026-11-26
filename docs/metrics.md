# Métricas y journal

La decisión de promover de modo no se toma de memoria: se toma con números
calculados sobre operaciones cerradas. `scripts/metrics.py` hace esa cuenta y
genera el dashboard.

## El journal es un CSV

El registro canónico vive en `state/trades.csv` (ignorado por git). Un CSV y no
un markdown porque la aritmética tiene que ser reproducible, no interpretada.

Columnas obligatorias:

| Columna | Ejemplo | Nota |
| --- | --- | --- |
| `id` | `t-0001` | Único |
| `closed_at` | `2026-11-05T16:40:00+00:00` | ISO 8601 con zona |
| `side` | `long` / `short` | |
| `qty` | `0.5` | Positivo |
| `entry` | `3000` | Precio de entrada |
| `exit` | `3150` | Precio de salida |

Opcionales:

| Columna | Uso |
| --- | --- |
| `opened_at` | Duración media |
| `symbol`, `market` | Contexto |
| `fees` | Se resta del PnL |
| `stop`, `target` | Cálculo de R y R:R |
| `risk_pct` | Auditoría del sizing |
| `thesis`, `notes` | Por qué se entró y cómo salió |

Plantilla: [`config/trades.example.csv`](../config/trades.example.csv).

Una fila por operación cerrada. La tesis larga vive en
`.opencode/knowledge/trade-log/`; el CSV guarda los números.

## Cálculos

```text
pnl        = (exit - entry) * qty - fees        (short: invertido)
retorno    = pnl / (entry * qty)
R          = pnl / (|entry - stop| * qty)
win_rate   = ganadoras / cerradas
PF         = suma(ganancias) / |suma(pérdidas)|
drawdown   = max(1 - equity / peak)
sharpe     = (media(retorno_diario) / desvío(retorno_diario)) * sqrt(252)
```

El equity arranca en `--equity` (default 100 000) y se mueve con cada operación
cerrada. El drawdown es sobre esa curva, no sobre el mark-to-market.

## Veredicto

`report` compara contra los umbrales de
[`trading-modes.md`](trading-modes.md):

| Veredicto | Condición |
| --- | --- |
| `insufficient_sample` | menos de 50 operaciones cerradas |
| `promote` | WR > 55%, PF > 1.5, Sharpe > 1, DD < 10%, ≥ 20 días con actividad |
| `degrade` | DD ≥ 15% o PF < 1.0 o WR < 45% |
| `hold` | todo lo demás |

`degrade` no es una opinión: es la señal de volver a paper.

## Uso

```bash
# métricas y veredicto
python scripts/metrics.py report

# para consumir desde otro script
python scripts/metrics.py report --json

# dashboard (consulta AI-Trader: posiciones, cash, precios)
python scripts/metrics.py dashboard --out .opencode/knowledge/DASHBOARD.md

# dashboard sin red
python scripts/metrics.py dashboard --offline
```

El dashboard incluye cuenta, posiciones con PnL, métricas con su estado frente
al umbral, veredicto y los planes activos del motor de riesgo. Es un artefacto
generado: no se edita a mano y está en `.gitignore`.

## Qué mide y qué no

**Mide** resultados de operaciones cerradas, con fees si están cargadas.

**No mide** (todavía):

- slippage real distinto del precio registrado;
- posiciones abiertas en las métricas (solo se valorizan en el dashboard);
- funding, intereses ni comisiones de financiación;
- calidad de la tesis: dos trades iguales pueden venir de un análisis brillante
  o de una moneda al aire.

## Límites estadísticos

- **50 operaciones es el mínimo, no la meta.** Con 50 trades el intervalo de
  confianza del win rate sigue siendo ancho.
- **Sharpe desde PnL diario aproxima.** Con pocos días con actividad el valor es
  inestable; por eso el veredicto exige 20 días y no promueve sin él.
- **Un mes bueno no es una estrategia.** El drawdown y el PF importan más que el
  retorno.
- **El CSV puede mentir** si se cargan precios teóricos en lugar de fills. El
  journal vale lo que vale su disciplina.

## Cuándo correrlo

| Momento | Comando |
| --- | --- |
| Al cerrar una operación | agregar fila al CSV |
| Al cerrar la sesión | `report` + `dashboard` |
| Revisión semanal | `report --json` y comparar contra la semana previa |
| Antes de subir de modo | `report` y leer el veredicto |
| Después de un incidente | `dashboard` para ver el estado real |
