# Protocol reference

Este documento registra únicamente protocolos observados o inferencias explícitamente marcadas como tales. Si un HAR/runtime nuevo contradice algo de aquí, el nuevo HAR tiene prioridad y deben actualizarse código, tests y documentación.

## 1. Principios

- No reutilizar contratos de un proveedor en otro.
- No marcar `OK` por disponibilidad de página/demo.
- Conservar request/response RAW cuando el protocolo lo permita.
- Toda transición automatizada debe tener evidencia observable.
- Las transiciones no comprendidas deben quedar como `PARCIAL`, no descartarse.

---

# 2. Pragmatic Play

## 2.1 Catálogo

El catálogo usa el Load More AJAX real de Pragmatic.

El crawler no necesita abrir un navegador por cada página.

Terminación:

- tamaño de página esperado conocido;
- una página final con menos elementos implica fin oficial;
- existe guard adicional para evitar loops.

## 2.2 Bootstrap

El resolver obtiene:

- symbol;
- URL runtime;
- session/bootstrap;
- `mgckey`;
- versión/configuración necesaria;
- `doInit`.

Un symbol encontrado por HTML/regex no es autoritativo hasta validar el bootstrap.

## 2.3 Entrada de juego

El endpoint principal observado es `gameService`.

Modos construidos desde `doInit`:

```text
SPIN
ANTE_BET_N
PURCHASE_N
```

Los datos exactos dependen del juego.

## 2.4 State machine

Estados observados:

### Base/continuation

```text
na=s
```

Puede ser terminal o requerir otra tirada de feature según campos de estado.

### Bonus

```text
na=b
→ doBonus
```

### Collect bonus

```text
→ doCollectBonus
```

### Collect normal

```text
→ doCollect
```

### Free Spin Option

```text
na=fso
→ doFSOption&ind=<index>
```

Caso de referencia:

- Frozen Charms;
- `fs_opt_mask` describe opciones;
- índices negativos/sentinel no son opciones ejecutables.

### Mystery Scatter

```text
na=m
→ doMysteryScatter
```

Caso de referencia:

- Book of Vikings;
- conservar `sInfo` del trigger.

## 2.5 Clasificación

`pragmatic_har_protocol.py` analiza respuestas y genera firmas.

Artefactos:

```text
*.analysis.json
protocol-observations.json
```

Si aparece un `na` nuevo:

1. guardar RAW;
2. clasificar;
3. no inventar acción;
4. implementar handler sólo con evidencia.

---

# 3. 1spin4win / D1

## 3.1 Separación catálogo/runtime

Catálogo y juego son capas distintas.

### Catálogo

Webflow HTML.

### Runtime

WebSocket.

No volver a modelar el catálogo como WS sólo porque el runtime sea WS.

## 3.2 Catálogo Webflow

Card:

```text
div.item_portfolio
```

Campos:

```text
[fs-list-field="name"]
[fs-list-field="slug"]
img.image_portfolio-game
```

Demo:

```text
https://gs.1spin4win.com:10443/...
```

Paginación:

```text
a.w-pagination-next[href]
```

Ejemplo:

```text
?ae0c3ebe_page=2
```

## 3.3 Formatos de demo observados

### Router genérico

```text
https://gs.1spin4win.com:10443/gmh5/games.html?game=<GameId>&currency=EUR&config=1&freeplay=true...
```

### HTML por juego

```text
https://gs.1spin4win.com:10443/gmh5/<game>.html?currency=EUR&config=1&freeplay=true...
```

## 3.4 Descubrimiento de runtime

Orden:

```text
demo HTML
→ scripts directos
→ loaders/referencias dinámicas
→ buscar WSS + gameController.connect
→ fallback Playwright si falta información
```

Patrones estáticos:

```javascript
gameURL = "wss://..."
```

o cualquier literal final:

```text
wss://...
```

Parámetros:

```javascript
gameController.connect(
  gameName,
  ...,
  version,
  wallet,
  currency
)
```

No asumir que todos los bundles tienen el mismo formato.

## 3.5 Fallback runtime

Se activa si:

- no se encuentra WSS;
- o no se resuelve `connect(...)`.

Playwright:

```text
page.on("websocket")
```

Se registra el WSS real y los frames iniciales.

El frame de init oficial puede recuperar parámetros aunque el JS sea opaco.

## 3.6 Frame wire

Prefijo cliente:

```text
A/u2
```

La parte posterior es JSON.

### Init

```json
{
  "key": "",
  "type": "0",
  "data": ",,freeplay,<GameName>,<version>,<config>,<currency>,test"
}
```

Wire:

```text
A/u2{"key":"","type":"0","data":",,freeplay,<GameName>,<version>,<config>,<currency>,test"}
```

Caso observado:

```text
VeryLucky1024
version=01
config=1
currency=EUR
```

## 3.7 Respuesta init

El cliente espera respuesta de inicialización.

Respuesta útil:

```text
type=1
```

Campos usados para construir una tirada base:

```text
l  = lines
b3 = bet index
```

Otros campos observados:

```text
bs
b
w
cp
...
```

## 3.8 Spin

Wire:

```text
A/u2{"key":"","type":"1","data":"<lines>,<betIndex>,0"}
```

Interpretación actual:

- `lines`: valor de `l`;
- `betIndex`: valor de `b3`;
- `0`: playmode base observado.

## 3.9 Resultado

```text
type=3
```

Un `type=3` recibido no necesariamente significa terminal si el estado indica feature activa.

## 3.10 Error

```text
type=2
```

Tratar como error de protocolo/provider.

## 3.11 Keepalive

Servidor:

```text
pns
```

Respuesta:

```text
A/pns
```

No guardar el keepalive como resultado de juego.

## 3.12 Features

Estados tratados como activos:

```text
st=5
st=6
st=11
st=12
```

El flujo observado vuelve a la acción de juego tipo `1`.

Tester-Spin:

1. conserva `lines`;
2. conserva/actualiza `b3`;
3. envía continuación;
4. espera otro `type=3`;
5. repite;
6. corta por terminal o wire guard.

Wire guard:

```text
128
```

## 3.13 Very Lucky 243

Problema observado:

```text
D1: no se resolvió gameURL WebSocket desde los assets.
```

Causa:

- el juego no exponía `gameURL` con el mismo patrón estático del caso `VeryLucky1024`.

Solución:

- mantener resolución de `connect(...)` desde assets si está disponible;
- observar WSS real con Playwright;
- combinar ambas fuentes;
- si también falta connect, leer init `A/u2 type=0` observado.

Esto es un patrón de diseño importante: **componer evidencia parcial**, no exigir que todo venga del mismo archivo JS.

---

# 4. Belatra

## 4.1 Catálogo actual

Ruta observada:

```text
https://belatragames.com/es/games/category/2
```

La categoría 2 es:

```text
Ranura
```

Rutas paginadas observadas:

```text
/es/games/category/2
/es/games/category/2/2
/es/games/category/2/3
...
```

## 4.2 Backend metadata

El stream expone metadata cuyo `path` es:

```text
https://back.belatra.games/api/games
```

Ejemplo de metadata de la captura:

```json
{
  "current_page": 1,
  "from": 1,
  "last_page": 5,
  "per_page": 25,
  "to": 25,
  "total": 104
}
```

No se depende de llamar directamente a ese backend: el crawler usa la ruta pública de Belatra y parsea el stream que la aplicación entrega.

## 4.3 Next.js / RSC

El HTML contiene chunks:

```javascript
self.__next_f.push([1,"..."])
```

Problema:

- un JSON grande puede partirse entre dos o más pushes;
- hacer `raw_decode` sobre un solo chunk puede fallar con string truncado.

Algoritmo correcto:

```text
for script in page:
  if self.__next_f.push:
    extraer string JSON
    json.loads() del string
    append chunk decodificado

stream = "".join(chunks)
```

Después:

```text
buscar "games":
raw_decode(array)
```

y:

```text
buscar meta paginación
raw_decode(object)
validar current_page + last_page + per_page
```

## 4.4 Game object

Campos observados:

```text
id
title
slug
image
tag
category
meta_title
meta_description
```

Ejemplo:

```json
{
  "id": 109,
  "title": "Princess Suki",
  "slug": "princess-suki",
  "category": {
    "id": 2,
    "title": "Ranura"
  }
}
```

## 4.5 Imágenes

Estructura:

```text
image.desktop
image.tablet
image.mobile
```

Variantes:

```text
x1
x2
webp_x1
webp_x2
```

Preferencia actual:

```text
desktop.webp_x2
desktop.x2
desktop.webp_x1
desktop.x1
tablet...
mobile...
```

## 4.6 URL de detalle

A partir de slug:

```text
https://belatragames.com/<lang>/games/game/<slug>
```

El language se deriva del catálogo.

## 4.7 ID interno

`Game.symbol` usa:

```text
str(game.id)
```

si `id` está disponible.

No confundir slug con provider ID.

## 4.8 Demo

Host:

```text
https://free-slot.belatragames.com/
```

El path `/play/<slug>` no usa siempre el mismo slug del catálogo corporativo.

Ejemplos observados:

```text
20-icy-fruits -> icy-fruits
7-fruits      -> seven-fruits
88-golden     -> 88-golden-88
```

Resolución:

1. enlace explícito `/play/...`;
2. slug corporativo;
3. variantes derivadas (quitar prefijo numérico, duplicarlo al final, convertir número inicial a palabra cuando aplica);
4. páginas `promotion-packs`;
5. enlaces play y `Nickname` extraídos de promoción;
6. GET de validación para cada candidato;
7. sólo una respuesta válida se acepta como demo.

Un 404 de un candidato no es error final: sólo descarta ese alias.

## 4.9 Runtime

Estado actual:

```text
bootstrap/discovery
```

Todavía NO existe contrato observado suficiente para ejecutar spin/bonus/buy de forma autoritativa.

Resultado:

```text
PARCIAL
```

aunque la demo sea accesible.

El siguiente HAR útil para Belatra debe incluir:

- carga completa de un juego;
- una o más tiradas;
- bonus/free spins si aparecen;
- buy bonus si el juego lo tiene;
- cualquier WS;
- fetch/XHR;
- mensajes enviados/recibidos;
- timestamps.

---

# 5. Ruido de telemetría

No todo WebSocket pertenece al provider.

Patrón observado de Yandex/WebVisor:

```text
wv-type
wv-check
wv-hit
wstoken
resource=events/...
sessionStart
```

Clasificar como:

```text
telemetry_ignored
```

Otros hosts típicos a filtrar:

- Yandex;
- Google Analytics;
- Google Tag Manager;
- DoubleClick.

Nunca construir una state machine de juego a partir de un socket de analytics.

---

# 6. Criterio para añadir una transición

Antes de añadir un handler nuevo, responder:

1. ¿Qué interacción la dispara?
2. ¿Qué request/frame exacto sale?
3. ¿Qué campos son variables?
4. ¿Qué respuesta confirma éxito?
5. ¿Qué estado indica continuación?
6. ¿Qué estado indica terminal?
7. ¿Hay collect?
8. ¿Hay una selección humana?
9. ¿Hay variantes por juego?
10. ¿Existe un fixture/test que reproduzca la evidencia?

Si cualquiera de los puntos críticos no está resuelto, preservar como `PARCIAL`.

## 4.10 Runtime action correlation

El discovery Belatra tiene ahora dos fases temporales:

```text
bootstrap
→ diagnostic input
→ action
```

Se registran:

- request URL/método/resource type/post data;
- response status;
- WebSocket open;
- WebSocket frames sent/received;
- timestamp relativo;
- fase;
- flag de telemetría.

La entrada de diagnóstico sólo intenta provocar actividad reproducible en la demo. No se usa como criterio de `OK`.

Se consideran señales candidatas de protocolo:

```text
POST/PUT/PATCH/DELETE después de action
XHR/fetch después de action
WebSocket frame sent después de action
```

La siguiente etapa de ingeniería es comparar varias capturas y encontrar una firma estable:

```text
input
→ request/frame
→ response/frame
→ actualización de estado
→ terminal/continuation
```
