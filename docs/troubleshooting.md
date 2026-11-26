# Troubleshooting

## Diagnóstico rápido

```bash
python scripts/preflight.py            # todo: entorno, claves, conectividad
python scripts/preflight.py --offline  # solo configuración local
```

El script imprime PASS / WARN / FAIL por chequeo y sale con código 1 si algo
bloquea la operación.

---

## OpenCode

| Síntoma | Causa probable | Solución |
| --- | --- | --- |
| `ccxt` o `alpaca` no aparecen en `/mcps` | Config mal formada o comando inexistente | Revisar `opencode.jsonc`; en V2 los servidores van bajo `mcp.servers` |
| El servidor figura como `needs authentication` | Es remoto y usa OAuth | `/mcps` → seleccionar → iniciar sesión |
| Los cambios de configuración no se aplican | Caché de configuración | Guardar el archivo (recarga automática) o `/reload` |
| El agente no lee las reglas | Falta `instructions` o la ruta no existe | Verificar `.opencode/rules/trading-rules.md` |
| Las skills no se cargan | Estructura incorrecta | Debe ser `.opencode/skills/<nombre>/SKILL.md` con frontmatter `name` y `description` |

## CCXT MCP

| Error | Significado | Solución |
| --- | --- | --- |
| `LIVE_SESSION_NOT_ARMED` | Sesión real sin armar | `arm_live_session()` antes de operar |
| `CLOCK_OUT_OF_SYNC` | Desvío de reloj > 5 s | `w32tm /resync /force`; verificar zona horaria |
| `INSUFFICIENT_FUNDS` | Balance libre insuficiente | Bajar tamaño o fondear testnet |
| `LOT_SIZE_VIOLATION` | Notional o step inválido | Ajustar cantidad; mínimo $10 |
| `ORDER_VALUE_EXCEEDED` | Supera `MAX_ORDER_VALUE_USD` | Bajar tamaño o subir el tope conscientemente |
| `SLIPPAGE_EXCEEDED` | El precio se movió > `MAX_SLIPPAGE_PCT` | Usar orden limit |
| `DUPLICATE_ORDER` | Orden idéntica repetida en 30 s | Esperar la ventana o cambiar la orden |
| `RATE_LIMITED` | Órdenes demasiado seguidas | Esperar `RATE_LIMIT_ORDER_SEC` |
| `LOT_SIZE_VIOLATION` con otro exchange | Mínimo o step distintos de Binance | `python scripts/exchange_profile.py check` y normalizar la cantidad |
| `close_failed_permanent` en el motor | El plan no cumple el mínimo del venue | Ver [`multi-exchange.md`](multi-exchange.md) y corregir el plan |
| Timeout de red | Estado de la orden incierto | `reconcile_order(client_order_id, symbol)` antes de reintentar |
| `uvx: command not found` | uv no instalado | `pip install uv` |

Notas del sandbox: las órdenes **limit** son estables; las **market** tuvieron
problemas de redondeo de decimales. Validar compuertas con limit.

## Alpaca MCP

| Síntoma | Causa | Solución |
| --- | --- | --- |
| `403 Forbidden` | Claves live contra endpoint paper (o al revés) | Verificar `ALPACA_PAPER_TRADE` y el par de claves |
| Orden queda `accepted` sin llenarse | Mercado cerrado | `get_clock`; las órdenes se ejecutan en la apertura |
| `insufficient buying power` | Tamaño mayor al disponible | `get_account_info` y recalcular |
| No aparecen noticias | Toolset `news` no habilitado | Revisar `ALPACA_TOOLSETS` |
| Fracción rechazada | El activo no admite fracciones | Usar cantidad entera |

## Scanner

| Síntoma | Causa | Solución |
| --- | --- | --- |
| `CLIPTAP_AI_TOKEN no configurado` | Falta la variable | Exportarla o cargar el secreto en Actions |
| No llegan notificaciones | Topic distinto o app sin suscripción | Probar con `curl -d test https://ntfy.sh/<topic>` |
| Corre pero nunca alerta | Umbrales altos o agentes inactivos | Revisar `MIN_BUY_CONSENSUS` y el feed |
| `401` en todas las llamadas | Token inválido o rotado | Regenerar y actualizar el secreto |
| Alertas repetidas cada 5 min | Estado de dedup perdido o cooldown 0 | Revisar `alert_state.json` y `ALERT_COOLDOWN_MIN` |
| Alerta que no se repite aunque el consenso sigue | Cooldown activo | Esperar; es el comportamiento esperado |
| Actions no se ejecuta | Cron deshabilitado por inactividad del repo | Ejecutar `workflow_dispatch`; en repos inactivos GitHub pausa los schedules |

## Collector

| Síntoma | Causa | Solución |
| --- | --- | --- |
| No abre la ventana/bandeja | Falta `pystray` o `pillow` | `pip install -r requirements.txt` |
| Se cierra al iniciar | Token ausente o inválido | Revisar `.env` y logs |
| No guarda snapshots | Permisos de escritura en `.opencode/knowledge/` | Verificar ruta y espacio en disco |
| Estado corrupto | Cierre abrupto | El collector escribe atómico; si quedó un `.tmp`, borrarlo |

## Windows

| Síntoma | Solución |
| --- | --- |
| Acentos o emojis rotos en consola | `chcp 65001` o `$env:PYTHONIOENCODING="utf-8"` |
| `Activate.ps1` bloqueado | `Set-ExecutionPolicy -Scope Process Bypass` |
| Ruta con espacios falla en un comando | Entrecomillar la ruta completa |
| Reloj desincronizado | Servicio `w32time` activo y `w32tm /resync /force` |

## Git y secretos

| Síntoma | Solución |
| --- | --- |
| `git status` muestra `.env` | Verificar `.gitignore`; si ya se commiteó, rotar las claves |
| Se commiteó un token | Rotarlo primero, después limpiar el historial |
| `node_modules` aparece | Está ignorado; si se coló, `git rm -r --cached .opencode/node_modules` |

Un secreto commiteado se considera comprometido aunque se borre en el commit
siguiente: el historial lo conserva.
