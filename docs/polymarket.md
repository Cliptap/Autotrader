# Polymarket

Mercados binarios sobre eventos: política, deportes, cripto, macro. La
probabilidad implícita es el precio, y ahí está la diferencia con un exchange
tradicional: no se apuesta a que el precio suba, se apuesta a que el mercado
está equivocado sobre un evento.

## Qué hace este entorno

| Capacidad | Estado |
| --- | --- |
| Descubrir y filtrar mercados (Gamma API) | Listo |
| Order book y probabilidad implícita (CLOB) | Listo |
| Edge, Kelly y tamaño de apuesta | Listo |
| Registro paper en AI-Trader | Listo |
| Ejecución real (CLOB con wallet) | **No incluida** (ver abajo) |

## Descubrimiento

```bash
python polymarket.py markets --query "fed" --min-liquidity 5000
python polymarket.py markets --max-hours 24 --limit 5
```

Salida:

```text
    liquidez    vol 24h cierra         últ.  pregunta
   3,035,829    186,174 2026-11-27 01:20  40.0%  Chiefs vs. Bills
```

`--min-liquidity` es el filtro más importante: un mercado con poca liquidez
tiene spread enorme y no se puede salir sin mover el precio.

## Order book

```bash
python polymarket.py book --slug nfl-kc-buf-2026-11-27 --outcome Chiefs
```

```text
best bid 0.39 · best ask 0.4 · mid 0.395
spread 0.0100 (2.53%)
profundidad top 5: bids $844,550 · asks $1,362,608
```

La probabilidad implícita es el mid (o el ask si vas a comprar). El spread es el
costo implícito de entrar y salir.

## Edge y tamaño

```bash
python polymarket.py bet --slug nfl-kc-buf-2026-11-27 --outcome Chiefs \
  --prob 0.45 --equity 1000
```

```text
Chiefs vs. Bills
  outcome Chiefs @ 0.400 (implícita 40.0%) · modelo 45.0%
  EV por share +0.0500 · ROI +12.50% · Kelly 8.33%
  stake $20.00 (2.00% del equity) [tope de riesgo]
  shares 50.0
(paper) Agregá --publish para registrarlo en AI-Trader.
```

Matemática:

```text
EV por share = prob_modelo - precio - fee
ROI          = EV / precio
Kelly        = (prob_modelo - precio - fee) / (1 - precio)
stake        = min(kelly * fracción * equity, max_pct% del equity)
```

Se usa **medio Kelly** por defecto (`--kelly-fraction 0.5`): el Kelly completo
asume que tu probabilidad es exacta, y no lo es. El tope del 2% del equity
(`--max-pct`) manda sobre Kelly: si el modelo dice 8% y el límite 2%, se apuesta
2%.

Con `--size` se fija el stake y se saltea el cálculo.

## Registro paper

```bash
python polymarket.py bet ... --publish
```

Publica en AI-Trader:

```json
{
  "market": "polymarket",
  "action": "buy",
  "symbol": "nfl-kc-buf-2026-11-27",
  "outcome": "Chiefs",
  "token_id": "111",
  "price": 0.4,
  "quantity": 50,
  "executed_at": "now"
}
```

AI-Trader liquida la posición simulada; el dashboard y las métricas la levantan
como cualquier otra. Sin `--publish` el comando solo calcula: es la opción
segura para explorar.

## Por qué no hay ejecución real

Operar en Polymarket requiere, en este orden:

1. Una wallet en Polygon con USDC.
2. Aprobar (`approve`) los contratos de exchange y negocio risk para que muevan
   ese USDC.
3. Credenciales L2 del CLOB (API key, secret y passphrase) derivadas de la
   wallet.
4. Firmar cada orden (EIP-712) con la clave privada; el cliente oficial es
   `py-clob-client`.
5. Cumplir las restricciones geográficas de la plataforma.

Eso significa que el sistema tendría que custodiar una clave privada con fondos
y firmar transacciones irreversibles. Ninguna de esas piezas se puede validar en
paper, y una clave privada en una variable de entorno es un riesgo distinto —
más grave— que una API key de exchange.

**Decisión:** la ejecución real queda fuera de este repositorio hasta que exista
un diseño de custodia explícito (hardware wallet o firmante separado), límites
por mercado y una prueba end-to-end en la testnet de Polygon. Mientras tanto, el
flujo es: investigar acá, registrar paper en AI-Trader, y ejecutar a mano si el
operador lo decide.

## Riesgos propios de Polymarket

| Riesgo | Detalle |
| --- | --- |
| Resolución | La resuelve UMA; una mala redacción puede resolverse en contra aunque el evento "obviamente" pasó |
| Liquidez | Muchos mercados tienen bids de $50: entrar es fácil, salir no |
| Longshot bias | Los precios chicos (2-5%) suelen estar sobrevaluados por compradores de lotería |
| Tiempo | El capital queda inmovilizado hasta la resolución |
| Fees | Hoy 0 en mercados estándar; el modelo deja el parámetro abierto |
| Regulación | Acceso restringido en varias jurisdicciones |

## Flujo recomendado

```text
1. polymarket.py markets --min-liquidity 5000
2. polymarket.py book --slug ... --outcome ...
3. Escribir la tesis y la probabilidad propia (con evidencia, no con corazonada)
4. polymarket.py bet ... --prob P     (ver el edge y el stake)
5. polymarket.py bet ... --publish    (paper)
6. Registrar la tesis en .opencode/knowledge/
7. Al resolverse, anotar el resultado en el journal
```

## Cuándo NO apostar

- No hay precio en el libro o el spread supera 5%.
- La probabilidad propia está a menos de 3 puntos del precio: es ruido.
- La resolución depende de una fuente ambigua o de una decisión discrecional.
- El mercado se resuelve en horas y no hay tiempo de reaccionar.
- No podés explicar en una frase por qué el mercado está equivocado.
