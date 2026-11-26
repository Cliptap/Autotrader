# Metodología: collect → predict → validate

El agente solo existe mientras hay una sesión abierta. La recolección y la
vigilancia, en cambio, corren sin LLM. Esa separación es lo que permite aprender
en vez de improvisar cada vez.

## Fases

### 1. Collect (script)

`collector.py` guarda cada 5 minutos un snapshot con:

- precios de los símbolos seguidos,
- señales del feed,
- régimen macro y sentimiento,
- posiciones y balances.

Cada 60 segundos hace heartbeat para recibir replies, menciones y tareas.
Todo queda en `.opencode/knowledge/market-data/AAAA-MM-DD/`.

### 2. Analyze / Predict (sesión)

Al abrir sesión:

1. Leer los snapshots desde la última vez.
2. Comparar las predicciones anteriores con lo que pasó.
3. Escribir predicciones nuevas, con condiciones verificables: "si BTC cierra
   sobre X con volumen, espero Y en Z días".
4. Guardarlas en `.opencode/knowledge/predictions.md`.

Una predicción sin condición de verificación no es una predicción: es una
opinión.

### 3. Validate (sesión siguiente)

- ¿Acertó la dirección?
- ¿Acertó la magnitud?
- ¿Acertó el timing?
- ¿Qué señal fue más confiable: técnica, macro, consenso o sentimiento?

Con eso se ajustan los pesos de la estrategia. Ajustar sin haber validado es
cambiar reglas por aburrimiento.

## Reglas del ciclo

- No se opera sin haber leído los snapshots de la sesión anterior.
- No se cambia la estrategia a mitad de una racha: se espera al cierre de la
  semana.
- Toda predicción se registra antes de operar. Si se escribe después, se está
  justificando, no prediciendo.
- Los snapshots son datos, no narrativa: si contradicen la tesis, gana el dato.

## Heartbeat vs snapshots

| Heartbeat | Snapshots |
| --- | --- |
| Replies y menciones | Precios |
| Nuevos seguidores | Señales del feed |
| Tareas asignadas | Régimen macro |
| Actividad social | Cambios de posiciones |

Uno es el componente social; el otro, el de mercado. Los dos hacen falta.
