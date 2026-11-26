# Política de riesgo

Estas reglas son el contrato entre el operador y el agente. Están duplicadas en
`.opencode/rules/trading-rules.md` (para que el agente las lea en cada sesión) y
en `scripts/position_size.py` (para que el cálculo sea determinístico). Si hay
conflicto, manda el script.

## Límites duros

| Regla | Valor | Se mide |
| --- | --- | --- |
| Riesgo por operación | ≤ 2% del equity | Distancia al stop × cantidad |
| Posición individual | ≤ 10% del capital | Notional |
| Drawdown máximo (paper) | 15% | Equity vs máximo previo |
| Drawdown máximo (real) | 10% | Equity vs máximo previo |
| Operaciones por día | ≤ 5 | Journal |
| Activos distintos | ≥ 3 | Cartera abierta |
| Relación riesgo/beneficio | ≥ 1:2 | Distancia a stop vs objetivo |
| Stop obligatorio | Sí | Toda posición abierta |
| Pausa tras 3 pérdidas consecutivas | Resto del día | Journal |

## Cálculo del tamaño

```text
riesgo_usd   = equity * riesgo_pct
distancia    = |entrada - stop|
cantidad     = riesgo_usd / distancia
notional     = cantidad * entrada
```

Si `notional` supera el 10% del capital, se recorta la cantidad hasta el límite
(la operación deja de arriesgar el 2% y pasa a arriesgar menos). Nunca al revés.

Uso:

```bash
python scripts/position_size.py --equity 10000 --risk-pct 0.5 \
  --entry 100 --stop 95 --target 112
```

Salida: riesgo en USD, cantidad, notional, porcentaje del equity y R:R. Si el
R:R es menor a 2, el script advierte y sale con código 2.

### Ejemplo

Equity $10.000, riesgo 0.5% = $50. Entrada $100, stop $95 (distancia $5).

```text
cantidad = 50 / 5 = 10 unidades
notional = 10 * 100 = $1.000  (10% del equity, justo en el límite)
R:R      = (112 - 100) / (100 - 95) = 2.4
```

Si el stop estuviera en $97,5 (distancia $2,5), la cantidad sería 20 unidades y
el notional $2.000 (20%): se recorta a 10 unidades y el riesgo real baja a $25.

## Stops

- Se definen **antes** de entrar, junto con el objetivo.
- Se colocan como orden real cuando el broker lo permite (bracket en Alpaca,
  OCO en CCXT).
- Solo se mueven a favor: a break-even y luego trailing.
- Un stop mental no es un stop. Si el mercado se mueve rápido y no hay orden
  colocada, la pérdida puede duplicar el plan.

## Concentración y correlación

Tres posiciones en BTC, ETH y SOL no son diversificación: es la misma apuesta
con tres tickers. Reglas:

- máximo 2 activos del mismo sector o narrativa;
- correlación alta ⇒ reducir el tamaño total, no sumar;
- el riesgo agregado de posiciones correlacionadas no debe superar 3% del equity.

## Régimen de mercado

El tamaño se ajusta al régimen, no solo al activo:

| Régimen | Ajuste |
| --- | --- |
| Alcista con macro a favor | Riesgo planificado (hasta el límite) |
| Lateral / mixto | 50% del riesgo planificado |
| Bajista o alta volatilidad | Solo paper, o ninguna operación |
| Evento macro de alto impacto | Sin posiciones nuevas hasta después del evento |

## Anti-sesgo

El modelo puede enamorarse de una tesis. Controles:

1. Antes de operar, escribir explícitamente **la evidencia contraria** y qué la
   invalidaría.
2. Si no se encuentra evidencia contraria sólida, reducir el tamaño a la mitad.
3. Revisar el consenso de agentes como señal de multitud, no como verdad.
4. Registrar las predicciones y compararlas después. El sesgo sobrevive en la
   memoria; no en los números.

## Kill switch

Condiciones para detener toda operación y pasar a paper:

- drawdown de 10% en real;
- dos fallas de ejecución consecutivas (orden rechazada, estado incierto);
- token o claves potencialmente expuestas;
- pérdida de sincronización entre posiciones reales y el journal.

Procedimiento:

```text
1. disarm_live_session()            (crypto)
2. cancel_all_orders() + close_all_positions() solo si hay órdenes huérfanas
3. Alpaca: cancel_all_orders() y revisar posiciones abiertas
4. marcar el día como incidente en .opencode/knowledge/
5. no reanudar hasta encontrar la causa raíz y escribirla
```

## Qué no es negociable

- Operar sin stop.
- Aumentar el riesgo después de una pérdida para "recuperar".
- Ejecutar en real sin la señal paper equivalente.
- Saltarse el journal.
- Compartir el token o las claves en el repositorio, en un chat o en una captura.
