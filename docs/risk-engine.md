# Motor de riesgo

`risk_engine.py` cierra posiciones cuyo stop fue perforado, aunque no haya una
sesión de OpenCode abierta. No abre posiciones, no busca oportunidades y no
interpreta noticias: solo reduce riesgo.

## Qué es y qué no es

| Es | No es |
| --- | --- |
| Un ejecutor de stops por polling | Una orden stop nativa del exchange |
| Un proceso sin LLM | Un bot de trading |
| Idempotente entre reinicios | Un sistema de alta frecuencia |
| Una red de seguridad | Un reemplazo del bracket/OCO |

Jerarquía de protección, de mejor a peor:

1. **Bracket / OCO en el exchange**: la orden vive en el libro, se ejecuta sin
   depender de tu máquina.
2. **Motor de riesgo**: cierra a mercado cuando detecta la perforación. Depende
   de que el proceso esté vivo y de la latencia del polling.
3. **Stop mental**: no es un stop.

El motor existe para el caso 2, cuando el venue no permite bracket o la orden
protectora se perdió.

## Modelo del plan

Cada posición abierta se registra en `state/positions.json`:

```json
{
  "updated_at": "2026-11-26T19:00:00+00:00",
  "positions": [
    {
      "id": "p-8dbfde11b0",
      "venue": "ccxt",
      "symbol": "BTC/USDT",
      "market": "crypto",
      "side": "long",
      "qty": 0.01,
      "entry": 60000.0,
      "stop": 58000.0,
      "target": 65000.0,
      "status": "open",
      "opened_at": "2026-11-26T18:55:00+00:00",
      "close_client_id": null,
      "attempts": 0,
      "note": ""
    }
  ]
}
```

Estados:

| Estado | Significado |
| --- | --- |
| `open` | Vigilada; el motor puede cerrarla |
| `closing` | Orden de cierre enviada, esperando confirmación |
| `closed` | Cerrada y confirmada |
| `orphan` | El venue ya no tiene la posición |
| `failed` | Tres intentos fallidos; requiere revisión humana |
| `canceled` | Cerrada manualmente en el registro |

El plan **exige** stop. Un plan sin stop se rechaza al registrarlo: si no se
puede definir el riesgo, no se puede proteger la posición.

## Ciclo

```text
cada RISK_POLL_INTERVAL (60 s):
  1. leer state/positions.json
  2. para cada plan activo:
     a. si status=closing  -> consultar la orden por client_order_id y finalizar
     b. si el venue ya no tiene la posición -> marcar orphan
     c. obtener precio
     d. si precio <= stop (long) o >= stop (short) -> cerrar
        si target alcanzado y el take-profit está habilitado -> cerrar
  3. persistir planes y escribir state/risk-log.jsonl
```

## Idempotencia

Antes de enviar la orden, el plan pasa a `closing` y guarda
`close_client_id = rk-<hash(plan_id)>`. Si el proceso muere entre el envío y la
confirmación, el siguiente ciclo **consulta** esa orden en lugar de enviar otra.

- Si la orden existe y está llena → `closed`.
- Si existe y sigue abierta → espera.
- Si no existe después de 15 segundos → reintenta con el mismo id.
- Si el venue la rechaza → `open` (hasta 3 intentos) y luego `failed`.

Esto es lo que evita el peor error posible: vender dos veces por un reinicio.

## Modos y compuertas

| Situación | Comportamiento |
| --- | --- |
| `CCXT_SANDBOX=true` | Cierra en testnet (seguro) |
| `ALPACA_PAPER_TRADE=true` | Cierra en la cuenta paper |
| Venue live | **Bloquea** salvo `RISK_ENGINE_LIVE=true` |
| `--dry-run` | Nunca envía; solo registra lo que haría |
| `state/HALT` presente | No bloquea los stops; solo impide entradas nuevas |

`RISK_ENGINE_LIVE=true` es un armado explícito, equivalente al
`arm_live_session()` del MCP. Un motor mal configurado no debe poder liquidar
una cuenta por accidente.

## Comandos

```bash
# registrar la posición al abrir el trade
python risk_engine.py plan add \
  --symbol BTC/USDT --venue ccxt --market crypto --side long \
  --qty 0.01 --entry 60000 --stop 58000 --target 65000 \
  --equity 10000

# ver y validar
python risk_engine.py plan list
python risk_engine.py check

# cancelar el plan cuando se cierra a mano
python risk_engine.py plan remove --id p-8dbfde11b0 --reason "closed by OCO"

# un ciclo (prueba) o el loop completo
python risk_engine.py run --once --dry-run
python risk_engine.py run --interval 60
```

`plan add` rechaza:

- stop del lado equivocado o ausente,
- qty ≤ 0,
- más de 2% de riesgo cuando se pasa `--equity`,
- dos planes abiertos para el mismo símbolo y venue (salvo `--force`).

## Programarlo

### Windows (Task Scheduler)

```powershell
$action  = New-ScheduledTaskAction -Execute "python" `
  -Argument "risk_engine.py run" -WorkingDirectory "C:\ruta\Autotrader"
$trigger = New-ScheduledTaskTrigger -AtStartup
$set     = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "AutotraderRiskEngine" -Action $action -Trigger $trigger -Settings $set
```

### Linux (systemd)

```ini
# /etc/systemd/system/autotrader-risk.service
[Unit]
Description=Autotrader risk engine
After=network-online.target

[Service]
WorkingDirectory=/opt/Autotrader
EnvironmentFile=/opt/Autotrader/.env
ExecStart=/usr/bin/python3 risk_engine.py run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Prueba en testnet

1. Abrir una posición de prueba en Binance testnet.
2. Registrar el plan con un stop apenas por debajo del precio.
3. `python risk_engine.py run --once` → debe cerrar y loguear `close_sent`.
4. `python risk_engine.py plan list` → estado `closing` y luego `closed`.
5. Reiniciar el proceso a mitad del cierre → no debe enviar una segunda orden.

## Límites conocidos

- **Gap risk**: el cierre es a mercado. Si el precio salta el stop, se ejecuta
  al precio disponible, no al stop.
- **Spot**: CCXT no expone "posiciones" en spot; el motor confía en el plan y
  el exchange rechaza una venta sin saldo.
- **Fills parciales**: se cierra `plan.qty`, no el saldo exacto del venue. En
  Alpaca se podría cerrar la posición completa; está en el roadmap.
- **Un plan por símbolo y venue**: dos estrategias sobre el mismo activo
  requieren consolidar el plan a mano.
- **Sin journal automático**: el motor escribe `risk-log.jsonl`, pero no
  actualiza el dashboard. Eso llega con `scripts/metrics.py`.

## Tests

```bash
python -m pytest -q
```

Cubren validación de planes, detección de breach, idempotencia entre reinicios,
bloqueo de venues live, dry-run, huérfanos y persistencia.

## Cuando algo falla

| Síntoma | Causa | Acción |
| --- | --- | --- |
| `blocked: live venue sin RISK_ENGINE_LIVE=true` | Falta el armado | Revisar la posición y armar conscientemente |
| `close order not found, retrying` | Timeout o id inválido | Esperar el ciclo siguiente; verificar en el exchange |
| `failed` | Tres rechazos | Cerrar a mano y revisar el motivo en `risk-log.jsonl` |
| `orphan` | La posición ya no existe | Confirmar en el broker y limpiar el plan |
| `price_error` | Red o símbolo inválido | Verificar conectividad y formato `BTC/USDT` |
