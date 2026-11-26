# Retención de snapshots

Un snapshot cada 5 minutos son unos 105.000 archivos por año. `scripts/retention.py`
reduce eso a algo manejable sin perder la información que se usa para decidir.

## Escalera de resolución

| Nivel | Contenido | Retención | Dónde |
| --- | --- | --- | --- |
| Crudo | Un archivo por scan | `--raw-days` (7) | `AAAA-MM-DD/snapshot_*.json` |
| Horario | OHLC por símbolo + conteo de señales | `--hourly-days` (90) | `hourly/AAAA-MM-DDTHH.json` |
| Diario | OHLC por símbolo + conteo de señales | para siempre | `daily/AAAA-MM-DD.json` |

El resumen horario conserva:

```json
{
  "period": "2026-07-02T16:00:00+00:00",
  "kind": "hourly",
  "sources": 12,
  "symbols": {
    "BTC/crypto": {"open": 64400.0, "high": 64510.0, "low": 64380.0, "close": 64496.0, "samples": 12}
  },
  "signals": {"BTC/crypto": {"buy": 3, "sell": 1}}
}
```

## Uso

```bash
# ver qué haría (no toca nada)
python scripts/retention.py

# ejecutar
python scripts/retention.py --apply

# otra política
python scripts/retention.py --dir /ruta/market-data --raw-days 3 --hourly-days 180 --apply
```

Sin `--apply` es **dry-run**: informa acciones y no borra ni escribe. El
programa es idempotente: correrlo dos veces seguidas no duplica resúmenes.

## Qué se pierde

- **Señales crudas.** El resumen guarda conteos por símbolo, no el detalle por
  agente. El backtester necesita los archivos crudos: solo puede reproducir la
  ventana de `--raw-days`.
- **Timing fino.** Un OHLC horario no distingue si el máximo ocurrió antes o
  después del mínimo. Para decisiones intradía, no alcanza.
- **Contexto macro y noticias.** El resumen conserva precios y conteos de
  señales, no el texto de las señales ni el snapshot macro completo.

Si vas a hacer investigación histórica, subí `--raw-days` o copiá los snapshots
antes de compactar. La retención es para operar, no para archivar.

## Cuándo correrlo

| Frecuencia | Comando |
| --- | --- |
| Semanal | `python scripts/retention.py` (mirar el plan) |
| Mensual | `python scripts/retention.py --apply` |
| Antes de un análisis histórico | ajustar `--raw-days` o no ejecutar |

Programarlo junto al motor de riesgo (Task Scheduler / cron) es razonable: es
barato y no depende del mercado.

## Seguridad

- Nunca toca días dentro de `raw-days`.
- Nunca toca el día en curso: el corte es por fecha de `collected_at`.
- Si un JSON está corrupto, lo saltea y lo deja en su lugar.
- Escribe con `tmp` + `replace`: un corte de luz no deja un resumen a medias.
