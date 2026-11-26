# Operación

## Rutina diaria

### Al abrir la sesión (10 minutos)

1. `python scripts/preflight.py` — entorno y credenciales.
2. Leer `.opencode/knowledge/DASHBOARD.md` y el último handoff.
3. Comparar las predicciones de la sesión anterior con los snapshots del
   collector. ¿Acertamos dirección, magnitud y timing?
4. `/trading-scan` — posiciones, régimen, feed y candidatos.
5. Revisar `alerts.md` del scanner: qué disparó y si la tesis original sigue en
   pie.
6. Decidir. Si no hay señal clara, no hay operación. No operar también es una
   decisión válida.

### Durante el día

| Momento | Acción |
| --- | --- |
| Cada 30–60 min | Heartbeat: replies, menciones, tareas |
| Cada operación | Publicar señal paper antes de cualquier ejecución |
| Cambio de régimen | Ajustar tamaño o cerrar (ver `risk-policy.md`) |
| Cierre de posición | Registrar resultado y nota de salida |

### Al cerrar la sesión (5 minutos)

1. Actualizar `.opencode/knowledge/DASHBOARD.md` con posiciones y P&L.
2. Escribir el handoff: qué se hizo, qué quedó abierto, qué mirar mañana.
3. `disarm_live_session()` si se operó en real.
4. Si hubo una lección no obvia, guardarla en `knowledge/`.

## Revisión semanal

- Win rate, profit factor, drawdown y cantidad de operaciones cerradas.
- Comparación contra el benchmark (BTC para crypto, SPY para acciones).
- Qué regla se rompió y por qué.
- Costos: fees, slippage, spread.
- ¿La estrategia sigue teniendo el mismo edge o el mercado cambió?

## Revisión mensual

- Recalibrar pesos (técnico, macro, consenso, sentimiento).
- Revisar la lista de agentes seguidos: dar de baja a los inactivos y a los que
  cambiaron de estilo.
- Actualizar el roadmap si algo quedó obsoleto.
- Decidir si se promueve o degrada de modo (ver `trading-modes.md`).

## Journal

Formato mínimo por operación:

```md
### 2026-07-15 · ETH/USDT · long
- Tesis: ruptura de rango con volumen, macro a favor, 3 agentes comprando.
- Evidencia contraria: F&G en 78 (sobrecompra), resistencia en 1.920.
- Entrada: 1.735 · Stop: 1.680 (-3.2%) · Objetivo: 1.880 (+8.4%)
- Tamaño: 0.5% del equity · R:R 2.6
- Resultado: cerrada en 1.882 (+8.5%) el 2026-07-22
- Lección: el F&G alto no invalidó la tendencia, pero la salida fue antes del techo.
```

Sin tesis, stop, objetivo y lección no es un journal: es una lista de precios.

## Runbook de incidentes

### Drawdown supera el límite

```text
1. Detener nuevas entradas.
2. Cerrar posiciones con stop violado o tesis invalidada.
3. Contar la pérdida real contra el plan.
4. Volver a paper hasta identificar la causa.
```

### Orden en estado desconocido

```text
1. NO reintentar.
2. ccxt: reconcile_order(client_order_id, symbol)
3. Si no aparece, es seguro reintentar una vez.
4. Si el exchange devuelve un error que no es de red: abortar y revisar a mano.
```

### Exchange o broker caído

```text
1. Verificar estado del servicio antes de asumir que el problema es local.
2. No cancelar órdenes a ciegas: pueden estar ejecutadas.
3. Esperar confirmación y reconciliar posiciones al volver.
```

### Token o claves expuestas

```text
1. Rotar el token en AI-Trader y las claves en el exchange/broker.
2. Revisar el feed del agente por operaciones que no reconocemos.
3. Buscar el origen de la filtración (commit, captura, chat, log).
4. Agregar el patrón a .gitignore si faltaba.
```

### Reloj desincronizado

```powershell
w32tm /resync /force        # requiere admin
Start-Service w32time
w32tm /stripchart /computer:time.windows.com
```

Linux: `sudo timedatectl set-ntp true`.

## Observabilidad

| Señal | Dónde mirar | Frecuencia |
| --- | --- | --- |
| Alertas de consenso | ntfy + `alerts.md` | Cada 5 min |
| Salud del collector | `collector_state.json` + logs | Cada 5 min |
| Posiciones paper | `GET /api/positions` | Cada sesión |
| Posiciones reales | CCXT `get_balance()` / Alpaca `get_all_positions()` | Antes de operar |
| Snapshots | `.opencode/knowledge/market-data/` | Continuo |
| Ejecuciones del scanner | GitHub Actions → Actions | Diario |

Si algo no se puede observar, no se puede confiar en ello. Un sistema que
"parece estar funcionando" es un sistema sin monitoreo.
