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

# 4. Belatra — protocolo actual confirmado por HAR

## 4.11 Demo oficial actual

El detalle corporativo publica directamente un iframe:

```text
https://demo.bltr-static.com/belatra/demo?game=<nickname>
```

El frontend localizado añade:

```text
language=<locale>
```

Ejemplo observado:

```text
https://demo.bltr-static.com/belatra/demo?game=fortune_mummy&language=es
```

Ese GET responde con redirect a una URL de sesión:

```text
https://demo.bltr-static.com/?modification=<N>&sid=<SESSION>
```

y establece la cookie `connect.sid`.

La página final contiene:

```javascript
var config = {
  request_crypt: true,
  sc: "...",
  modification: 148,
  nickname: "fortune_mummy",
  user: {
    sid: "...",
    userCurrency: "FUN"
  }
}
```

`sc` y `sid` son datos efímeros de sesión: usarlos en memoria y no documentar valores reales.

## 4.12 Transporte de juego

El HAR confirma que el runtime funcional es HTTP, no WebSocket:

```text
POST https://demo.bltr-static.com/game
Content-Type: application/x-www-form-urlencoded
```

El único WebSocket observado en la captura fue telemetría Yandex/WebVisor y no pertenece al protocolo del juego.

Body wire:

```text
d=<encrypted-base64>&sid=<session-id>
```

Respuesta:

```json
{"d":"<encrypted-base64>"}
```

## 4.13 Cifrado

El cliente usa el valor `config.sc`.

Derivación para 128 bits:

1. UTF-8 de `sc`;
2. truncar/pad a 16 bytes;
3. cifrar ese bloque con AES-128 usando el mismo bloque como key;
4. el resultado de 16 bytes es la key de stream.

Payload:

1. JSON UTF-8;
2. prefix de 8 bytes:
   - milliseconds dentro del segundo, uint16 little-endian;
   - random uint16 little-endian;
   - epoch seconds uint32 little-endian;
3. counter block = prefix + uint64 big-endian empezando en 0;
4. AES-ECB(key derivada, counter block);
5. XOR con plaintext por bloques;
6. salida = Base64(prefix + ciphertext).

Es AES-CTR/NIST compatible con la implementación del cliente, con contador en los últimos 8 bytes.

## 4.14 Campos automáticos de AjaxQueue

Antes de cifrar cada request el cliente agrega:

```text
uid
c
modification
```

- `uid`: identificador aleatorio de la instancia;
- `c`: contador secuencial, wrap en 1000;
- `modification`: valor de config.

Después de cifrar agrega `sid` fuera del blob `d`.

Cuando existe un historyId vigente, el cliente añade:

```text
ghistId
```

a requests posteriores.

## 4.15 Máquina de estados base observada

### Enter

Request descifrado:

```json
{
  "q": "enter",
  "curFloor": 1,
  "userAgent": "...",
  "uid": "...",
  "c": 0,
  "modification": 148
}
```

La respuesta expone parámetros autoritativos de apuesta y juego:

```text
gs.betPerLine
gs.nlines
gs.linesAssortment
gs.betAssortment
gs.gdenom
gs.denomAssortment_cents
gs.vipMode
gs.dop.curModeID
gs.phaseCur
gs.phaseNext
```

### Start / spin

Request base observado:

```json
{
  "q": "start",
  "betPerLine": 10,
  "nlines": 5,
  "denom": 1,
  "buyBonus": null,
  "selectId": null,
  "hideInsideInHistory": 0,
  "showingInMoney": 0,
  "vipOn": 1,
  "curModeID": 0
}
```

Más `ghistId` cuando ya existe un histórico anterior, y los campos automáticos.

Respuesta típica:

```text
gs.phaseCur  = basedeal
gs.phaseNext = toPaid
gs.historyId = <new history>
```

También contiene el resultado:

```text
curWin
placedbet
startBox
stopBox
linesInfo
wildMask
wereFeatures
...
```

### Finish

Request:

```json
{
  "q": "finish",
  "ghistId": "<historyId del start>"
}
```

Respuesta terminal observada:

```text
gs.phaseCur  = finished
gs.phaseNext = toIdle
```

Por lo tanto, para un spin base confirmado:

```text
enter
→ start
→ phaseNext=toPaid
→ finish
→ phaseCur=finished + phaseNext=toIdle
→ OK
```

Si `start` devuelve otra transición, NO inventar la continuación. Guardar respuesta descifrada y marcar `PARCIAL` hasta clasificar bonus/free-spins/buy.

## 4.16 HAR de referencia 2026-09-08

Juego:

```text
Fortune Mummy
nickname=fortune_mummy
```

La captura contiene múltiples pares `start/finish` válidos y una variación de apuesta de `betPerLine=10` a `12`, confirmando que los parámetros se transmiten explícitamente y no están hardcodeados en el endpoint.

Este HAR reemplaza la hipótesis anterior de “runtime Belatra todavía desconocido”: el spin base está suficientemente documentado para ejecución HTTP directa.

## 4.17 Concurrencia del demo Belatra

La concurrencia del runner no es equivalente a la capacidad del proveedor.

Evidencia operativa:

- con tres demos simultáneas aparecieron HTTP 500 en títulos que luego devolvieron OK al ejecutarse individualmente;
- por lo tanto Belatra se ejecuta con una sola sesión activa por defecto.

La limitación se modela como capacidad del provider (`max_test_concurrency=1`), no como excepción de GUI.

## 4.18 isMathElf y vipOn

Slattors Battle confirma dos dimensiones de modo en `start`:

```text
isMathElf ∈ {0,1}
vipOn     ∈ {0,1}
```

Ejemplo observado:

```json
{
  "q": "start",
  "betPerLine": 10,
  "nlines": 20,
  "denom": 1,
  "buyBonus": null,
  "selectId": null,
  "vipOn": 1,
  "isMathElf": 0
}
```

El `enter` devuelve el valor actual/default de `isMathElf`; el start base debe reenviarlo.

`vipMode.vipBetK` describe el multiplicador del modo de apuesta VIP. En el HAR observado es 1.2.

`buyBonus.buyTotalBetK` lista tres opciones con id/cost/prefix2/rtp, pero el contrato de compra todavía no está implementado sin un HAR de una compra real.

## 4.19 Legacy toDoubleDialog

Para títulos legacy como Lucky Drink:

```text
start
→ basedeal / toDoubleDialog
→ finish(ghistId)
→ finished / toIdle
```

No es necesario ejecutar la apuesta de double/gamble para completar el spin base; `finish` la declina.

## 4.20 Campos de modelo matemático dinámicos

No asumir que todos los selectores de volatilidad se llaman isMathElf.

Regla actual: copiar al start cualquier campo top-level de gs que coincida con ^isMath[A-Za-z0-9_]*$ y cuyo valor sea bool/int/float.

La regla está fundamentada en Slattors, cuyo cliente agrega isMathElf específicamente al body de start.

No copiar indiscriminadamente otros campos de gs: muchos son estado de respuesta y no parámetros de request.

## 4.21 Respuestas HTTP de error

Los errores del endpoint /game también son evidencia de protocolo.

Artefactos obligatorios incluso con HTTP 500:
- start.request.json
- start.response.raw.txt
- start.wire.json

Si el body es JSON con d, se intenta descifrar y escribir start.response.json.

Esto permite distinguir campo obligatorio ausente, valor inválido, incompatibilidad de modificación o error interno sin detalle.

## 4.22 Line-dependent bets

Some legacy Belatra games expose both:
- linesAssortment
- betDependOnLines

The tuple (nlines, betPerLine) must be treated atomically.

Invalid example observed from the previous adapter:
- nlines=1
- betPerLine=5

while enter advertised for line 1:
- betAssort=[10,20,50,...]

That request produced HTTP 500.

Current rule:
1. use current gs.nlines if valid;
2. use current gs.betPerLine;
3. if betDependOnLines has an entry for that line, validate the bet against its betAssort;
4. repair only with a value explicitly advertised for that line.

## 4.23 mathType selector

Cops vs Robs publishes:
- gs.mathType
- analInfo.mathTypeCops
- analInfo.mathTypeRobs

The base start must preserve gs.mathType. This is a different naming convention from Slattors' isMathElf.

## Pragmatic catalogue authority

Catalogue transport and game transport are separate trust domains.

Authoritative catalogue evidence:
- successful Pragmatic HTTP root;
- validated AJAX Load More sequence;
- confirmed terminal short page.

Non-authoritative evidence:
- Playwright DOM fallback;
- hidden/preloaded DOM snapshots;
- partial site rendering under WAF/CDN/5xx conditions.

A DOM fallback can recover or add game candidates but cannot establish that missing titles were removed by Pragmatic. Reconciliation therefore requires both an authoritative source and the provider-specific shrink threshold.


---

# 7. BGaming — HAR 2026-09-09

## 7.1 Catálogo

Ruta pública observada:

```text
https://bgaming.com/game-type/slots
```

La primera página contiene 25 tarjetas `[data-catalog-card]`.

Las páginas siguientes se cargan por:

```text
GET https://bgaming.com/wp-json/bg/v1/games/search
```

Parámetros observados:

```text
sort=release_date
order=DESC
posts_per_page=25
format=html
columns_style=1
game_type=1
game_label=1
most_popular=0
ver=105
filter=game
page=N
lang=en
```

Respuesta:

```json
{
  "page": 2,
  "total": 13,
  "hasMore": true,
  "html": "..."
}
```

Terminación autoritativa:

```text
hasMore=false
```

No considerar un crawl limitado manualmente o una página REST fallida como autoritativo.

Campos extraídos de cada tarjeta:

```text
name
slug
public_url
demo_url
identifier
thumbnail
RTP
volatility
game_type
availability
```

En la captura se observaron enlaces demo normales y algunos links con `play_token` efímero. Los links tokenizados no se persisten.

## 7.2 Bootstrap de juego

El demo estable tiene forma observada:

```text
https://demo.bgaming-network.com/play/<Identifier>/FUN?server=demo
```

El navegador termina en una página:

```text
/games/<Identifier>/FUN?...launch_token...
```

El HTML expone:

```javascript
window.__OPTIONS__ = {...}
```

Campos usados por Tester-Spin:

```text
identifier
api
csrfTokenHeaderName
csrfTokenHeaderValue
currency
resources_path
math
rules
```

También puede existir `websocket_url`, pero el HAR de referencia demuestra que el spin base capturado usa HTTP JSON; no se usa el WS para inventar una máquina de estados.

Tokens/CSRF se usan sólo durante la sesión y se redactan de artefactos/logs persistentes.

## 7.3 Init

Request observado:

```json
{
  "command": "init",
  "extra_data": {
    "round_series_id": 1788943009571
  }
}
```

Respuesta relevante:

```text
api_version=2
options.available_bets
options.default_bet
options.paytable/paytables
options.special_symbols
options.lines
options.reels
options.layout
options.currency
options.screen
balance.wallet
balance.game
flow.state
flow.command
flow.available_actions
```

Caso de referencia Treasure of Anubis:

```text
layout.reels = 5
layout.rows  = 3
default_bet  = 90
currency     = FUN
subunits     = 100
flow.state   = ready
flow.command = init
available_actions = [init, spin]
```

## 7.4 Spin

Request observado:

```json
{
  "command": "spin",
  "options": {
    "bet": 90
  },
  "extra_data": {
    "round_series_id": 1788943009571
  }
}
```

Respuesta:

```text
outcome.screen
outcome.special_symbols
outcome.bet
outcome.win
outcome.wins
balance.wallet
balance.game
flow.round_id
flow.last_action_id
flow.state
flow.command
flow.available_actions
```

Terminal base observado:

```text
flow.command = spin
flow.state   = closed
available_actions = [init, spin]
```

## 7.5 Validación contable

BGaming separa saldo en:

```text
balance.wallet
balance.game
```

Para el spin base observado:

```text
balance_total = wallet + game
balance_total_n = balance_total_(n-1) - bet + win
```

No validar únicamente `wallet`, porque una ganancia puede permanecer temporalmente en `balance.game`.

## 7.6 Features no observadas

El bundle contiene referencias a comandos adicionales, pero el HAR suministrado no ejecuta esos flujos. Por lo tanto NO implementar automáticamente todavía:

```text
freespin
respin
gamble
select_bonus
buy_feature / purchased_feature
```

Si una respuesta de runtime expone un `flow.state` o `available_actions` fuera de `init/spin/ready/closed`:

1. guardar request/response;
2. marcar intento como `PARCIAL`;
3. registrar la acción/estado desconocido;
4. añadir handler sólo con evidencia HAR/runtime específica.


## 7.7 Compras observadas — AlienFruits3

HAR 2026-09-09 confirma compras BGaming mediante el mismo command `spin`.

```json
{
  "command": "spin",
  "options": {
    "bet": 200,
    "purchased_feature": "bonus_buy"
  }
}
```

El `init` publica:

```json
{
  "feature_options": {
    "feature_multipliers": {
      "bonus_buy": 2000,
      "bonus_chance": 30,
      "base_bet": 20
    },
    "disabled_features": []
  }
}
```

Los nombres distintos de `base_bet` son candidatos de compra explícitamente publicados.
El débito observado se deriva como:

```text
cost_multiplier = feature_multiplier / base_bet
expected_debit  = requested_bet * cost_multiplier
```

En la captura:

```text
bonus_buy:    2000 / 20 = x100  → bet 200 descuenta 20000
bonus_chance:   30 / 20 = x1.5  → bet 200 descuenta 300
```

La respuesta confirma el modo mediante:

```json
"flow": {
  "state": "closed",
  "command": "spin",
  "purchased_feature": {"name": "bonus_buy"}
}
```

AlienFruits3 usa además resultado seed-driven:

```json
"outcome": {
  "screen": null,
  "storage": {
    "seed": 77018,
    "mode": "1"
  }
}
```

Por tanto `screen=null` no es error si `outcome.storage.seed` está presente.

## 7.8 Free spins observados — TreasureOfAnubis

Un spin normal puede abrir free spins:

```text
spin
→ flow.state=freespins
→ available_actions=[init,freespin]
```

La continuación exacta observada es:

```json
{
  "command": "freespin",
  "extra_data": {
    "round_series_id": "<misma serie>"
  }
}
```

Durante toda la feature se conserva el mismo `round_id` y avanza `last_action_id`:

```text
<round>_2
<round>_3
...
<round>_23
```

`features.freespins_left` decrece y puede aumentar por retrigger. El HAR observado pasó de 11 tiradas emitidas a 22.

Terminal:

```text
flow.state=closed
flow.command=freespin
available_actions=[init,spin]
freespins_left=0
```

Las free spins no vuelven a cobrar `outcome.bet`; para validación contable:

```text
balance_total_n = balance_total_(n-1) + win
```

Tester-Spin sigue automáticamente `freespin` hasta terminal con guard de 256 pasos.


## 7.9 Preselection game — AlwaysUp

HAR 2026-09-09 confirma una continuación adicional después de comprar `bonus_buy`.

La compra:

```json
{
  "command": "spin",
  "options": {
    "bet": 200,
    "purchased_feature": "bonus_buy"
  }
}
```

responde:

```text
flow.state=preselection_game
flow.command=spin
available_actions=[init, preselection_game]
purchased_feature.name=bonus_buy
```

El cliente muestra una selección visual de cohete, pero el índice elegido NO se envía al backend.
El bundle guarda `rocket_index` sólo localmente para UI/animación y al confirmar manda:

```json
{
  "command": "preselection_game",
  "extra_data": {
    "round_series_id": "<misma serie>"
  }
}
```

Sin `options`, sin nombre de cohete y sin índice.

La respuesta conserva el mismo `round_id`, avanza `last_action_id` y termina:

```text
flow.state=closed
flow.command=preselection_game
available_actions=[init, spin]
```

El multiplicador real llega desde servidor:

```json
"features": {
  "bonus_data": {
    "multiplier": 100
  }
}
```

La continuación `preselection_game` tiene débito cero. En la captura:
saldo previo total=83400, win=20000, saldo final total=103400.

Tester-Spin automatiza esta continuación como parte del mismo intento de compra.


## 7.10 HyperHive JSON-RPC

HAR 2026-09-09 de Blackbeard's Bounty confirma una segunda familia runtime BGaming.

El HTML sigue exponiendo `window.__OPTIONS__`, pero aunque `options.api` contiene una URL del estilo:

```text
/api/<Identifier>/<internal-id>/<session>
```

el cliente HyperHive NO juega contra esa URL. El transporte efectivo observado es:

```text
POST <origin>/api
Content-Type: application/json
```

con JSON-RPC 2.0.

Detección actual:

- launch final `/hyperhive`; o
- `game_bundle_source` presente + `game=slots/...` + `version=1.0.0`.

Init:

```json
{
  "id": "<uuid>",
  "jsonrpc": "2.0",
  "method": "init",
  "params": {
    "token": "<play_token>"
  }
}
```

El resultado expone:

```text
config.bet_limits
config.default_bet
config.purchased_features
balance
currency_attributes
state_lock
```

Spin normal:

```json
{
  "method": "play",
  "params": {
    "token": "<play_token>",
    "req": {
      "bet": 100,
      "bet_type": "bet"
    }
  }
}
```

La respuesta usa:

```text
result.final
result.balance
result.resp.commonGame
result.resp.freespins
result.resp.freespinsGame
result.resp.totalWin
result.resp.roundStep
result.resp.bet
```

Si `result.final=false`, el HAR confirma que la ronda continúa mediante otro `method=play` con `bet` + `bet_type=bet`. El runner repite hasta `final=true` con guard de 256 pasos.

Blackbeard's Bounty confirma:

```text
buy_chance:
  purchased_feature=buy_chance
  bet_type=bet
  coste observado x1.5

buy_bonus/freeSpin:
  purchased_feature=buy_bonus
  bonus_multiplier_type=freeSpin
  coste observado x100

buy_bonus/freeSpinRandom:
  purchased_feature=buy_bonus
  bonus_multiplier_type=freeSpinRandom
  coste observado x200
```

Los modos HyperHive se descubren desde literales realmente presentes en `game_bundle_source`, no desde la lista genérica `config.purchased_features`.

Validación de balance HyperHive:

```text
final_balance = balance_previo - debito_observado + totalWin
```

El débito observado se deriva del primer estado de la ronda y el saldo final; el balance del servidor es autoridad.

## 7.11 Geometría dinámica y free spins

No tratar `layout.rows` como altura fija para Megaways/Trueways. En esos motores la cantidad de símbolos por reel puede variar. Para layouts dinámicos se valida que cada reel sea no vacío, pero no una altura exacta.

Durante `command=freespin`, `outcome.bet` tampoco es un invariante universal entre juegos BGaming. La autoridad es débito cero + evolución correcta del balance. Por ello no se compara `outcome.bet` contra la apuesta base en continuaciones.


## 7.12 API v2 con `rows` — BigAtlantisFrenzy

HAR manual 2026-09-09 confirma una variante API v2 donde `layout.rows`
también forma parte del request wire.

Init relevante:

```text
options.default_bet = 30
options.layout.reels = 5
options.layout.rows  = 5
feature_options.feature_multipliers:
  freespin_chance = 200
  freespin_buy    = 8000
```

Spin normal observado:

```json
{
  "command": "spin",
  "options": {
    "bet": 30,
    "rows": 5
  }
}
```

Compra de free spins:

```json
{
  "command": "spin",
  "options": {
    "bet": 30,
    "rows": 5,
    "purchased_feature": "freespin_buy"
  }
}
```

Compra de mayor chance:

```json
{
  "command": "spin",
  "options": {
    "bet": 30,
    "rows": 5,
    "purchased_feature": "freespin_chance"
  }
}
```

Durante la feature:

```json
{
  "command": "freespin",
  "options": {
    "rows": 5
  }
}
```

Tester-Spin no aplica `rows` indiscriminadamente a todos los juegos.
Si un comando API v2 falla con HTTP 422, existe `layout.rows` y el request
todavía no contiene `rows`, se reintenta una vez usando ese valor. Si el
retry funciona, el perfil `rows-required` queda aprendido para el resto de
la sesión y se aplica a spins, compras y continuaciones.

Este HAR también confirma una segunda escala de
`feature_multipliers`. Cuando no existe `base_bet` explícito, esta familia
usa base porcentual 100:

```text
freespin_buy:
8000 / 100 = x80
bet 30 → débito 2400

freespin_chance:
200 / 100 = x2
bet 30 → débito 60
```

Los débitos son confirmados por la evolución del balance del HAR.
