# Scanner

Vigilante 24/7. No ejecuta, no decide: detecta consenso y avisa. Corre en GitHub
Actions cada 5 minutos o localmente en loop.

## Qué detecta

| Señal | Condición | Default |
| --- | --- | --- |
| Consenso de compra | ≥ N agentes seguidos compran el mismo activo | `MIN_BUY_CONSENSUS=2` |
| Frenesí del feed | ≥ N compras del mismo activo en el feed global | `GLOBAL_BUY_FRENZY=5` |
| Macro alcista | `bullish_count` del macro intel ≥ N | `MACRO_BULLISH_THRESHOLD=3` |
| Acción de alta convicción | Score y comunidad de un agente de referencia | `STOCK_MIN_SCORE=8`, `STOCK_MIN_COMMUNITY=5` |
| Horario de acciones | Solo 9:30–16:00 ET, lunes a viernes | — |

Los umbrales son constantes al inicio de `scanner.py`. Cambiarlos es una decisión
de estrategia: si se sube el consenso requerido, hay menos alertas y más
calidad; si se baja, pasa lo contrario.

## Qué no hace

- No abre posiciones.
- No calcula tamaño ni stops.
- No reemplaza el análisis: una alerta es una invitación a mirar, no una orden.

## Configuración

| Variable | Obligatoria | Default |
| --- | --- | --- |
| `CLIPTAP_AI_TOKEN` | Sí | — |
| `CLIPTAP_NTFY_TOPIC` | No | `cliptap-alerts` |

El scanner falla al arrancar si falta el token. No hay token embebido: un token
en el código es un token filtrado.

## Despliegue en GitHub Actions

`.github/workflows/scanner.yml`:

```yaml
on:
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch:
```

Secretos necesarios en Settings → Secrets → Actions:

- `CLIPTAP_AI_TOKEN`
- `CLIPTAP_NTFY_TOPIC`

Cada ejecución dura menos de un minuto. GitHub puede demorar el cron algunos
minutos en horas pico; para trading de consenso no es crítico, para ejecución
sí, y por eso la ejecución no vive acá.

## Notificaciones

ntfy.sh es push sin cuenta. El topic es la única credencial: cualquiera que lo
conozca recibe (y puede publicar en) las alertas. Usar un topic largo y difícil
de adivinar.

```bash
# probar el canal
curl -d "test" https://ntfy.sh/mi-topic-largo-y-aleatorio
```

Cada alerta se escribe también en `alerts.md` (ignorado por git), con timestamp
UTC.

## Ajuste fino

- **Demasiadas alertas**: subir `MIN_BUY_CONSENSUS` a 3.
- **Alertas tardías**: el feed de la plataforma tiene latencia; el scanner no
  puede adelantarse a ella. Para señales propias, usar el collector.
- **Pocas acciones**: revisar `TRACKED_STOCKS` y el score mínimo. La detección
  de acciones depende del formato del contenido publicado por el agente de
  referencia; si cambia, hay que ajustar los regex.
- **Falsos positivos de frenesí**: subir `GLOBAL_BUY_FRENZY` o filtrar por
  liquidez del activo.

## Costo

En repo público, Actions es gratis. En repo privado, 288 ejecuciones diarias de
~1 minuto superan los 2000 minutos mensuales del plan gratuito: espaciar el cron
a `*/15` o mover el loop a una máquina propia.

## Local

```bash
python scanner.py
```

Imprime el estado al iniciar, envía una notificación de prueba y entra en loop
cada 5 minutos. `Ctrl+C` para detener.

## Extender

1. Agregar la fuente en `scan()`.
2. Definir el umbral como constante de módulo, no como número mágico.
3. Documentar la señal en la tabla de arriba.
4. Si la señal es nueva, agregar el caso al checklist de pruebas manuales.
