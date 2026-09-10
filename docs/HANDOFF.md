# Tester-Spin — Handoff técnico completo

> Documento principal de continuidad. Si esta conversación se pierde o el proyecto se retoma en otro chat, leer este archivo antes de modificar código.
>
> Estado documentado: 2026-09-08.
>
> Repositorio: `Shisetsu-Code/Tester-Spin`.
>
> Rama estable de trabajo: `main`.

## 1. Objetivo del proyecto

Tester-Spin es una aplicación de escritorio Python/Tkinter para:

1. enumerar el catálogo de juegos de múltiples proveedores;
2. persistir nombre humano, slug, ID interno, URL de ficha/demo y miniatura;
3. probar juegos de forma masiva y concurrente;
4. ejecutar directamente el protocolo remoto observado cuando ya fue reconstruido;
5. conservar evidencia RAW/derivada para depurar estados que todavía no están automatizados;
6. distinguir claramente entre:
   - juego probado y terminal: `OK`;
   - protocolo parcialmente conocido o estado no terminal: `PARCIAL`;
   - fallo de bootstrap/transporte/protocolo: `ERROR`;
   - nunca probado: `PENDIENTE`.

El proyecto NO debe marcar `OK` sólo porque una página o demo devuelve HTTP 200. `OK` significa que la acción de juego solicitada fue efectivamente ejecutada y llegó a un estado terminal según el protocolo observado.

## 2. Proveedores actuales

| Proveedor | key | Catálogo | Runtime de juego | Estado |
|---|---|---|---|---|
| Pragmatic Play | `pragmatic` | AJAX Load More real | HTTP `gameService` endpoint-first | Maduro |
| 1spin4win / D1 | `1spin4win` | Webflow HTML paginado | WebSocket directo | Spin base funcional |
| Belatra Games | `belatra` | Next.js/RSC categoría 2 | HTTP cifrado `POST /game` endpoint-first | Spin base funcional; features/buy/free-spins pendientes |
| BGaming | `bgaming` | HTML inicial + WordPress REST `/wp-json/bg/v1/games/search` | HTTP JSON API v2 `init/spin` | Spin base funcional; features no observadas quedan PARCIAL |

## 3. Comandos habituales

### Ejecutar desde checkout de desarrollo

```powershell
cd C:\Proyectos\Tester-Spin
git pull
py -m pip install -r requirements.txt
.\Abrir-Tester-Spin.cmd
```

También se puede ejecutar:

```powershell
py run.py
```

### Construir launcher

```powershell
cd C:\Proyectos\Tester-Spin
git pull
.\scripts\build-launcher.ps1 -Clean
```

Salida esperada:

```text
dist\Tester-Spin.exe
```

### Tests

El CI de GitHub ejecuta al menos:

- instalación de dependencias;
- compile;
- unit tests.

Nunca afirmar que una rama está validada hasta que el workflow CI correspondiente termine en `success`.

## 4. Arquitectura

### Entry points

- `run.py`
  - entry point de la aplicación;
  - captura excepciones de arranque;
  - escribe `app-startup-error.log`;
  - abre `tester_spin.app_current.main`.

- `launcher.py`
  - entry point del ejecutable autoactualizable;
  - prepara runtime;
  - asegura entorno virtual;
  - lanza `run.py`;
  - detecta cierre inmediato;
  - escribe logs de launcher/arranque.

### GUI

- `tester_spin/app.py`
  - GUI base;
  - registra proveedores;
  - controles de catálogo y testing;
  - vista de juegos y miniaturas.

- `tester_spin/app_live.py`
  - streaming incremental del catálogo hacia Tk;
  - `Máx. cargas (0=todas)`;
  - un crawl completo no interrumpido permite reconciliar filas obsoletas.

- `tester_spin/app_current.py`
  - versión activa;
  - ordenamiento por nombre/ID/estado/fecha;
  - botón `PROBAR NO OK`;
  - drenado de eventos con budget temporal;
  - evita congelar Tk ante productores rápidos;
  - usa `ExecutionBackend`.

### Ejecución

- `tester_spin/execution_backend.py`
  - frontera transport-neutral;
  - `LocalThreadExecutionBackend` ejecuta un pool local;
  - la GUI no debe asumir que el worker siempre vive en el mismo proceso;
  - arquitectura preparada para backend remoto futuro.

- `tester_spin/scheduler.py`
  - orquesta juegos concurrentes;
  - la unidad de paralelismo es un juego/sesión independiente;
  - no paralelizar pasos pertenecientes a la misma máquina de estados de una sesión.

### Persistencia

- `tester_spin/storage.py`
  - SQLite local;
  - tabla de juegos;
  - historial de `test_results`;
  - `reconcile_provider_games()` elimina filas obsoletas del catálogo actual;
  - NO elimina histórico de resultados ni carpetas de artefactos.

### Modelos

- `tester_spin/models.py`
  - `Game`;
  - `SpinAttempt`;
  - `GameTestResult`.

### Proveedores

- `tester_spin/providers/base.py`
  - interfaz `ProviderAdapter`;
  - `crawl_catalog()`;
  - `test_game()`.

- `tester_spin/providers/__init__.py`
  - registry/export de proveedores.

- `tester_spin/providers/pragmatic_hybrid.py`
- `tester_spin/providers/one_spin4win.py`
- `tester_spin/providers/belatra.py`
- `tester_spin/providers/bgaming/adapter.py`
  - adapter público;
  - catálogo y metadata del provider;
  - no contiene protocolo de otros proveedores.
- `tester_spin/providers/bgaming/execution.py`
  - orquestación exclusiva de ejecución BGaming.
- `tester_spin/providers/bgaming/profile.py`
  - clasificación dinámica del runtime;
  - wire profile persistente y revalidable.
- `tester_spin/providers/bgaming/contracts.py`
  - vocabulario de transiciones BGaming con wire-shape conocido;
  - contratos por familia, nunca por título/slug.
- `tester_spin/providers/bgaming/catalog.py`
  - parser del catálogo público.
- `tester_spin/providers/bgaming/runtime.py`
  - transporte/helpers/validación API v2 y legacy line-bets.
- `tester_spin/providers/bgaming/hyperhive.py`
  - familia HyperHive JSON-RPC.
- `tester_spin/providers/bgaming/switchable.py`
  - contenedores con variantes seleccionables.

## 5. Semántica de la GUI

Campos principales:

- **Proveedor**
- **URL catálogo**
- **Máx. cargas (0=todas)**
- **Juegos simultáneos**
- **Repeticiones por modo**
- **Delay entre juegos**
- **Timeout**

Botones:

- `CARGAR / ACTUALIZAR CATÁLOGO`
- `PROBAR SELECCIONADOS`
- `PROBAR TODOS`
- `PROBAR NO OK`
- `DETENER`

`PROBAR NO OK` incluye todo juego cuyo último estado no sea `OK`.

La GUI hace streaming del catálogo sin leer SQLite por cada item. El provider devuelve `Game` directamente; el commit del catálogo se hace por lote al terminar.

## 6. Estados de resultado

### OK

Usar solamente si la acción fue ejecutada y completó la máquina de estados conocida.

Ejemplos:

- Pragmatic: `doSpin`/modo correspondiente + continuaciones + collect hasta terminal.
- D1: init WS válido + spin WS + resultado `type=3` terminal.

### PARCIAL

Usar si:

- hubo respuesta;
- el transporte funciona;
- pero existe un estado pendiente/no automatizado;
- o el provider todavía sólo tiene discovery.

### ERROR

Usar cuando:

- no se puede resolver bootstrap;
- handshake falla;
- timeout;
- respuesta de error del provider;
- parser no puede reconstruir parámetros indispensables.

## 7. Pragmatic Play — estado actual

### Catálogo

El catálogo usa el Load More AJAX real de Pragmatic, no navegación de navegador repetitiva.

Históricamente el crawl completo observado produjo aproximadamente:

- 642 juegos;
- 9 juegos por carga;
- última carga parcial;
- terminación oficial cuando la página trae menos items que el tamaño esperado.

Código principal:

- `pragmatic_catalog_ajax.py`
- `pragmatic_hybrid.py`

### Resolución de symbol

`pragmatic_symbol_resolver.py` no acepta un candidate sólo por regex: lo valida mediante bootstrap real.

Ejemplo relevante:

- Gates of Olympus POP puede sugerir `vs10olymppop`, pero el candidate debe validar.

### Runtime

Transporte principal:

```text
HTTP
→ gameService
→ respuesta de estado
→ transición siguiente
```

El estado base se descubre desde `doInit`.

Modos:

- `SPIN`
- `ANTE_BET_N`
- `PURCHASE_N`

Los multiplicadores/modos se derivan de campos observados como:

- `bls`
- `purInit`
- `purInit_e`

### Continuaciones conocidas

- `na=b` → `doBonus`
- bonus collect → `doCollectBonus`
- collect normal → `doCollect`
- feature activa → siguiente `doSpin`
- terminal → fin

HAR-grounded handlers:

#### Free Spin Option

Estado:

```text
na=fso
```

Acción:

```text
doFSOption&ind=<index>
```

Caso observado: Frozen Charms.

Campos relevantes:

- `fs_opt_mask`
- opciones `ind` válidas;
- sentinels negativos se excluyen.

El tester expande sesiones adicionales para cubrir opciones FSO internas que no aparecieron en las repeticiones normales.

#### Mystery Scatter

Estado:

```text
na=m
```

Acción:

```text
doMysteryScatter
```

Caso observado: Book of Vikings.

Debe conservarse `sInfo` del trigger.

### Artefactos Pragmatic

Ejemplo:

```text
data/providers/pragmatic/<Game Name>/
  thumbnail.*
  game.json
  tests/<timestamp>/
    discovery/
      doInit.request.txt
      doInit.response.raw
      doInit.response.json
      doInit.response.analysis.json
      calibration.response.raw
      modes.json
      protocol.json
    SPIN/
      attempt-0001/
        bootstrap/
        step-000-*.request.txt
        step-000-*.response.raw
        step-000-*.response.json
        step-000-*.analysis.json
        attempt.json
    ANTE_BET_1/
    PURCHASE_1/
    protocol-observations.json
    result.json
```

Regla: conservar RAW siempre que sea posible.

## 8. D1 / 1spin4win — estado actual

### 8.1 Catálogo

La captura de catálogo demostró que el portfolio público usa Webflow CMS.

Root habitual:

```text
https://www.1spin4win.com/games
```

También se observó versión localizada:

```text
https://www.1spin4win.com/es/games
```

Cada tarjeta:

```text
div.item_portfolio
├── img.image_portfolio-game
├── a.link_portfolio-game
│   ├── [fs-list-field="name"]
│   └── [fs-list-field="slug"]
└── enlace demo gs.1spin4win.com:10443
```

Paginación Webflow:

```text
a.w-pagination-next[href]
```

Ejemplo:

```text
?ae0c3ebe_page=2
```

Tester-Spin sigue el `href` directamente; no hace click/scroll.

Los HTML crudos se guardan:

```text
data/providers/1spin4win/catalog-pages/page-001.html
...
```

### 8.2 Demo y Game ID

Hay al menos dos formatos observados:

```text
/gmh5/games.html?game=<GameId>&...
```

y:

```text
/gmh5/<game>.html?...
```

`_demo_symbol()`:

1. usa query `game=` si existe;
2. si no, usa el stem del archivo.

### 8.3 Transporte de juego

El juego es WebSocket.

Caso base observado: `VeryLucky1024`.

Endpoint observado:

```text
wss://gs.1spin4win.com:443/games
```

Prefijo de mensajes de cliente:

```text
A/u2
```

### 8.4 Init

Mensaje:

```text
A/u2{"key":"","type":"0","data":",,freeplay,<GameName>,<version>,<config>,<currency>,test"}
```

Ejemplo:

```text
A/u2{"key":"","type":"0","data":",,freeplay,VeryLucky1024,01,1,EUR,test"}
```

La respuesta `type=1` contiene parámetros de juego/apuesta. Campos observados/relevantes:

- `l` — líneas;
- `b3` — índice de apuesta;
- `bs` — steps/tabla de apuestas;
- balance y otros campos del estado.

### 8.5 Spin

Mensaje:

```text
A/u2{"key":"","type":"1","data":"<lines>,<betIndex>,0"}
```

El último `0` es playmode base observado.

Respuesta de resultado:

```text
type=3
```

Error:

```text
type=2
```

### 8.6 Keepalive

Servidor:

```text
pns
```

Cliente:

```text
A/pns
```

### 8.7 Features

Estados tratados como feature activa:

```text
st ∈ {5, 6, 11, 12}
```

El cliente oficial vuelve por la misma ruta `playGame()` / mensaje tipo `1`.

Tester-Spin continúa con el mismo `lines/betIndex` y tiene wire guard de 128 pasos.

### 8.8 Resolver híbrido de runtime

No todos los juegos cargan `gameURL` igual.

Estrategia:

1. descargar demo;
2. escanear HTML;
3. escanear scripts y loaders;
4. buscar:
   - literal `gameURL = "wss://..."`;
   - cualquier literal final `wss://...`;
   - `gameController.connect(...)`;
5. seguir referencias dinámicas conocidas como `addJSFile(...)`;
6. si falta WSS o connect params:
   - abrir demo headless con Playwright;
   - `page.on("websocket")`;
   - observar socket real;
   - observar primer frame `A/u2 type=0`;
   - extraer `gameName/version/config/currency`;
7. cerrar navegador;
8. abrir `websocket-client` directo;
9. ejecutar init/spin sin UI.

Caso que motivó el fallback:

- **Very Lucky 243**
- Game ID: `VeryLucky243`
- tenía `gameController.connect(...)` pero el resolver estático no encontraba `gameURL`.

Artefacto del fallback:

```text
runtime-bootstrap-observed.json
```

Spec final:

```text
runtime-spec.json
```

Intento WS:

```text
ws-attempt.json
```

## 9. Belatra — estado actual

### 9.1 Corrección importante del catálogo

No asumir el esquema viejo:

```text
/en/games
/en/games/2
<a href="/en/games/game/...">
```

Ese parser sólo encontraba unos pocos juegos en la versión española.

La captura HAR real usa:

```text
https://belatragames.com/es/games/category/2
```

Categoría:

```text
id=2
title=Ranura
```

Paginación observada:

```text
/es/games/category/2
/es/games/category/2/2
/es/games/category/2/3
...
```

En el HAR:

```text
current_page = 1
last_page    = 5
per_page     = 25
total        = 104
```

El backend metadata expone:

```text
https://back.belatra.games/api/games
```

pero el navegador recibe los objetos del catálogo dentro del stream Next.js/RSC.

### 9.2 Formato Next.js/RSC

Los objetos están dentro de `self.__next_f.push([1,"..."])`.

Los chunks pueden estar partidos entre múltiples tags `<script>`.

Por lo tanto:

```text
HTML
→ localizar self.__next_f.push
→ JSON-decode del string de cada chunk
→ concatenar chunks en orden
→ buscar campo "games"
→ json.JSONDecoder.raw_decode()
→ buscar meta de paginación
```

No usar regex plana para intentar balancear objetos JSON anidados.

### 9.3 Objeto de juego observado

Ejemplo conceptual:

```json
{
  "id": 109,
  "title": "Princess Suki",
  "slug": "princess-suki",
  "image": {
    "desktop": {
      "x1": "...",
      "x2": "...",
      "webp_x1": "...",
      "webp_x2": "..."
    },
    "tablet": {},
    "mobile": {}
  },
  "category": {
    "id": 2,
    "title": "Ranura"
  },
  "meta_title": "...",
  "meta_description": "..."
}
```

Tester-Spin debe preferir miniatura:

1. desktop `webp_x2`;
2. desktop `x2`;
3. desktop `webp_x1`;
4. desktop `x1`;
5. luego tablet/mobile como fallback.

El `Game.symbol` de Belatra usa el `id` numérico convertido a string cuando está disponible.

URL de ficha reconstruida:

```text
https://belatragames.com/es/games/game/<slug>
```

### 9.4 Resolución de demo y runtime Belatra

No asumir que el slug corporativo coincide con el slug del sitio free-slot.

Alias observados públicamente:

```text
20-icy-fruits -> /play/icy-fruits
7-fruits      -> /play/seven-fruits
88-golden     -> /play/88-golden-88
```

El resolver debe:

1. aceptar enlaces free-slot explícitos;
2. generar candidatos derivados del slug/título;
3. validar cada candidato por HTTP en vez de abortar ante el primer 404;
4. si siguen fallando, revisar `promotion-packs`;
5. extraer enlaces `/play/...` y el campo `Nickname`;
6. validar los nuevos candidatos;
7. guardar `demo-resolution.json` con intentos, status y URL elegida.

Estado runtime actual:

- catálogo: implementado con Next/RSC;
- demo/bootstrap: implementado;
- spin base: implementado endpoint-first sobre HTTP cifrado `POST /game`;
- flujo validado: `enter → start → finish`;
- terminal validado: `start.phaseNext=toPaid` y `finish: finished → toIdle`;
- `toDoubleDialog` legacy se terminaliza declinando gamble mediante `finish`;
- `max_test_concurrency=1` por límite observado del demo público;
- features, buy bonus, free spins y selecciones `selectId` no observadas siguen en `PARCIAL`.

No inventar continuaciones no observadas: preservar request/response descifrados y añadir handlers únicamente con HAR/runtime suficiente.

## 9.5 BGaming — estado actual

BGaming está integrado como provider autónomo (`key=bgaming`). Un test de arquitectura analiza por AST todos los módulos de `tester_spin/providers/bgaming/` y falla si alguno importa internals de Pragmatic, Belatra o 1spin4win.

### Arquitectura interna

```text
BGamingProvider
    │
    ├── catálogo / metadata ── adapter.py + catalog.py
    │
    └── ejecución ─────────── execution.py
                                │
                                ├── profile.py
                                ├── runtime.py
                                ├── hyperhive.py
                                └── switchable.py
```

La clasificación nunca utiliza nombre humano, slug ni identifier para decidir protocolo. Esos campos pueden aparecer en artefactos/logs, pero no gobiernan ramas de ejecución.

Familias actuales:

```text
api-v2
legacy-lines
hyperhive-jsonrpc
switchable-container
unknown
```

`unknown` es fail-closed: no se inventan comandos; el intento conserva evidencia y queda no validado.

### Catálogo

Fuente pública:

```text
https://bgaming.com/game-type/slots
```

Paginación observada:

```text
GET https://bgaming.com/wp-json/bg/v1/games/search?page=N
```

La respuesta expone `page`, `total`, `hasMore` y HTML. El crawler:

- filtra exclusivamente `Slots`;
- exige coherencia de `page`;
- exige que `total`, observado por HAR como cantidad total de páginas, permanezca estable;
- sólo conserva autoridad al terminar normalmente con `hasMore=false` y con la página terminal igual a `total`;
- límite manual, stop, HTTP error, JSON inválido, página inconsistente o `total` cambiante degradan el crawl a no autoritativo;
- un crawl no autoritativo nunca debe autorizar reconciliación destructiva.

El HAR de referencia confirma la semántica de `total`: en página 2 se observó `total=13` con 25 tarjetas/página y `hasMore=true`, por lo que se trata como total de páginas. No se compara `total` contra la cantidad de juegos.

### Demo y credenciales efímeras

Links de catálogo con `play_token`/`launch_token` no se persisten. Si sólo existe una URL pública, al ejecutar se resuelve una sesión demo fresca y cualquier token resultante vive únicamente en memoria.

Se sanitizan:

- tokens;
- valores CSRF;
- URLs de sesión;
- `state_lock`;
- campos sensibles contenidos en opciones/artefactos.

### Runtime profile

Después de bootstrap/init se construye un `BGamingProfile` usando evidencia del protocolo. El perfil puede guardar:

```text
family
confidence
evidence
spin_options
command_options
purchase_features
rows_required
line_count
variable_layout
allowed_continuations
source
bundle_sha256
discovery_diagnostics
validated
```

Un perfil validado se conserva bajo `provider_protocol` en `game.json` y se reutiliza. También se genera `profile.json` dentro de la ejecución para diagnóstico.

Si un request devuelve 422 o un SPIN base deja de validar:

1. se invalida el perfil;
2. se redescubre desde el runtime actual;
3. se permite un recovery controlado;
4. el nuevo perfil sólo vuelve a ser `validated=true` después de una ejecución base correcta.

### API v2

La secuencia preferida es:

```text
bootstrap
→ init
→ discover_profile
→ primer spin ya con wire options descubiertas
```

No se usa el 422 como flujo normal de aprendizaje. El 422 es únicamente recovery para cambios de contrato.

Los scripts de terceros presentes en el launch (por ejemplo analytics/tag managers) no pueden convertirse en fuente de protocolo. Sólo se inspeccionan orígenes declarados por el runtime o controlados por BGaming, y los scripts se priorizan por firmas de contrato (`additionalSpinOptions`, `purchased_feature`, `round_series_id`, `flow`, etc.).

Si el cliente demuestra `additionalSpinOptions.<campo>`, el perfil puede resolver el valor de ese campo desde `init.options` o `init.options.layout` antes del primer spin. La mera presencia de `layout.rows` sigue sin ser suficiente.

Cada modo API-v2 se ejecuta en una sesión demo fresca. Un 422/400 en una compra o continuación no contamina el siguiente modo con una ronda abierta.

La validación distingue la intención local del resultado remoto. Un `outcome.bet` distinto de la apuesta solicitada no queda `OK` sólo porque el balance cierre. Una traducción de apuesta debe ser explicada por el perfil wire correspondiente.

### Compras

Las compras anunciadas por metadata del proveedor se registran como cobertura.
Cuando el cliente cargado expone vocabulario `purchased_feature`, se distingue
`ADVERTISED_ONLY` de `CLIENT_OBSERVED`: una compra que el init anuncia pero
cuyo wire-shape no aparece en un contrato cliente disponible no se ejecuta a
ciegas y queda pendiente de cobertura.

Si `feature_multipliers` publica un denominador/base, se calcula el costo con esa evidencia.

Si no publica denominador:

```text
cost_multiplier = unknown
```

No se asume base 100 ni ninguna escala histórica. La primera ejecución válida aprende:

```text
observed_debit = previous_balance + win - final_balance
cost_multiplier = observed_debit / requested_bet
```

El valor aprendido se usa para validar repeticiones siguientes.

### Continuaciones

No existe el fallback genérico `state == available_action → ejecutar state`.

Sólo se automatizan transiciones cuyo wire-shape está modelado a nivel del protocolo BGaming. Las acciones desconocidas quedan como cobertura pendiente y no se ejecutan.

Si una continuación conocida por nombre devuelve HTTP 422 y no hay parámetros adicionales demostrables, se marca `CONTRACT_UNRESOLVED`, se conserva la respuesta inicial y el error HTTP sanitizado, y no se repite el mismo wire-shape a ciegas en esa corrida.

Para `preselection_game` existen dos formas observadas a nivel del proveedor: `preselection_game` y `play_preselection_game`. Se usa exclusivamente la que aparezca en `available_actions`.

Esto no es hardcode por juego: es una allowlist de contratos observados del proveedor.

### HyperHive

HyperHive se clasifica por firma fuerte del transporte (launch final `/hyperhive`).

El bootstrap conserva los `<script src>` realmente cargados. HyperHive y API v2
usan esas URLs —incluidas URLs CDN sin sufijo `.js`— para reconstruir el
wire-shape antes de ejecutar. Los nombres fijos de bundles son sólo fallback.

Si no se demuestra el contrato base de `play` dentro del objeto `req` (o mediante asignaciones explícitas a `.req.*`), HyperHive devuelve `PARCIAL / CONTRACT_UNRESOLVED` sin enviar un request adivinado. Literales sueltos como `action:"spin"` ya no bastan para declarar el modo ejecutable.

Si un contrato aparentemente válido recibe JSON-RPC `51100`, queda `REJECTED_51100` para esa corrida y se guardan `contract-diagnostic.json`, request/response RPC sanitizados y hashes del bundle/engine.

Los modos distinguen:

```text
DISCOVERED
EXECUTABLE
VALIDATED
```

Encontrar un literal `purchased_feature:"..."` en JavaScript no basta para ejecutarlo. Un literal no clasificado se registra como `DISCOVERED_LITERAL_ONLY`.

Si una respuesta no terminal entrega `nextAction`, sólo se reenvía cuando esa acción también existe en el vocabulario `action:"..."` extraído del bundle/engine contract realmente cargado. En caso contrario se detiene la continuación y se conserva diagnóstico.

### Switchable

Una respuesta balance-only no basta para clasificar un contenedor. La clasificación exige además evidencia de `lobby_launch_url`. La enumeración de variantes se hace desde el cliente cargado y el cambio de runtime se valida contra el identifier devuelto por el servidor.

### Semántica de resultados

Para BGaming:

```text
SpinAttempt.ok = validación completa
```

No significa simplemente “hubo HTTP response”.

Por tanto:

- todos los modos ejecutables/requeridos validados y sin errores → `OK`;
- una acción opcional no clasificada no degrada por sí sola un run validado;
- una compra `ADVERTISED_ONLY` o continuación `CONTRACT_UNRESOLVED` sí crea un `coverage_gap` y mantiene `PARCIAL`;
- respondió pero contrato/wire/state no valida → `PARCIAL`;
- no pudo ejecutar/obtener respuesta válida → `ERROR`;
- no existe demo resoluble → `SIN_DEMO`.

Para estabilidad del demo público, BGaming aplica actualmente
`max_test_concurrency=1`. El límite es exclusivo del provider y no modifica
Pragmatic, Belatra ni 1spin4win. Los HTTP 502 de comandos con estado no se
reintentan ciegamente porque una respuesta de gateway no prueba que el backend
no haya procesado la operación.

Los launches demo persistidos que devuelven 404/410 se invalidan en memoria y se vuelven a resolver desde `public_url`. Un candidato nuevo sólo se acepta si tiene estructura completa `/play|games/<identifier>/<currency>`, responde HTTP correctamente y contiene un `window.__OPTIONS__` parseable; URLs truncadas como `/play/Prince` o `/play/Trea` ya no pueden ganar por heurística. Si no existe reemplazo validable, el resultado es `SIN_DEMO`.

Una respuesta de `init` cuyo envelope sólo contiene `errors` se trata como fallo de sesión/provider, no como una nueva familia de runtime. Se renueva launch+sesión una vez antes de fallar explícitamente.

En legacy line-bets, un estado opcional de card/gamble con `finish` anunciado
se cierra por `finish`; Tester-Spin nunca entra automáticamente en la apuesta
`check_card`.

### Tests de regresión relevantes

Existen tests para:

- parser y seguridad de catálogo;
- paginación real simulada y autoridad;
- aislamiento entre providers;
- clasificación de familias;
- persistencia/reutilización/invalidation del profile;
- wire options presentes en el primer spin;
- rechazo de traducciones de apuesta inexplicadas;
- aprendizaje de costo de compras desde balance;
- continuaciones desconocidas fail-closed;
- HyperHive discovery vs ejecución;
- vocabulario dinámico de `nextAction`;
- switchable;
- sanitización de tokens y URLs.


## 10. Evidencia HAR y reglas de trabajo

Principio central:

> El código del provider debe representar el protocolo observado, no una inferencia basada en cómo suelen funcionar otros proveedores.

Cuando llega un HAR nuevo:

1. identificar URL inicial;
2. inventariar hosts;
3. identificar XHR/fetch;
4. identificar WebSockets;
5. extraer request/response bodies;
6. revisar scripts;
7. relacionar interacción de UI con tráfico;
8. identificar estado inicial;
9. identificar entrada;
10. identificar respuesta;
11. identificar continuaciones;
12. identificar terminal;
13. guardar evidencia como test.

No mezclar protocolos entre proveedores.

Ejemplos de errores históricos que no deben repetirse:

- interpretar `D1` como Cloudflare D1;
- asumir que 1spin4win catalogaba por WS sin revisar el HAR de catálogo;
- asumir que todos los D1 exponen `gameURL` igual;
- usar el parser HTML inglés de Belatra sobre la ruta española Next.js;
- marcar `OK` porque una demo devuelve 200.

## 11. Artefactos y directorios

Raíz:

```text
data/
  tester-spin.sqlite3
  providers/
    pragmatic/
    1spin4win/
    belatra/
```

Cada provider debe guardar evidencia suficiente para poder diagnosticar una falla sin repetir toda la captura manual.

Nunca guardar secrets deliberadamente.

Si una captura contiene tokens efímeros/cookies:

- utilizarlos sólo en memoria si son necesarios;
- evitar copiarlos a documentación;
- redacción en logs persistentes cuando corresponda.

## 12. Concurrencia

La GUI permite N juegos simultáneos.

Regla:

- paralelizar juegos/sesiones independientes;
- NO paralelizar pasos dependientes dentro de una misma sesión de protocolo.

D1 usa sesiones WS independientes.

Pragmatic usa sesiones HTTP aisladas por worker.

Belatra ya sigue el mismo principio y además limita el provider a una sola sesión activa (`max_test_concurrency=1`) por los HTTP 500 observados bajo concurrencia.

## 13. Updater y distribución

El launcher estable puede usar modo git.

Rutas habituales:

```text
%LOCALAPPDATA%\Tester-Spin\
  updater.json
  runtime\repo
  venv
  playwright
  app-startup.log
  app-startup-error.log
  launcher-error.log
```

El updater de runtime gestionado puede hacer reset/clean de SU copia administrada.

No hacer reset destructivo del checkout de desarrollo del usuario.

También existe infraestructura para modo manifest con:

- URL;
- versión;
- SHA-256;
- zip seguro;
- últimas versiones como fallback.

## 14. Diagnóstico rápido

### La GUI no abre

Revisar:

```text
%LOCALAPPDATA%\Tester-Spin\app-startup.log
%LOCALAPPDATA%\Tester-Spin\app-startup-error.log
%LOCALAPPDATA%\Tester-Spin\launcher-error.log
```

### Catálogo devuelve pocos juegos

1. verificar URL del provider;
2. revisar si cambió la tecnología/paginación;
3. no asumir que los juegos siguen siendo anchors;
4. guardar la respuesta cruda;
5. buscar objetos de catálogo en JSON/RSC;
6. comparar total remoto vs total local.

### D1: no resuelve gameURL

El resolver debe caer a observación Playwright. Revisar:

```text
runtime-bootstrap-observed.json
runtime-spec.json
```

### D1: init funciona pero spin falla

Revisar `ws-attempt.json`:

- frames enviados;
- frames recibidos;
- `type=2`;
- falta de `type=3`;
- `st`;
- keepalive;
- timeout.

### Pragmatic PARCIAL

Revisar:

```text
protocol-observations.json
*.analysis.json
*.response.raw
```

Buscar `na`/firma no automatizada.

### Belatra PARCIAL

Es esperado mientras no exista cliente directo de spin. Si el catálogo falla, revisar primero el parser Next/RSC, no el runtime.

## 15. Pruebas que deben existir

### D1

- catálogo Webflow;
- query `game=`;
- filename game ID;
- parse de `gameURL`;
- parse de `connect(...)`;
- fallback runtime;
- parse de init `A/u2 type=0`;
- keepalive `pns/A/pns`;
- init `type=1`;
- result `type=3`;
- error `type=2`;
- feature states;
- status `OK` sólo con terminal.

### Belatra

- decodificación de chunks Next;
- concatenación de chunks partidos;
- extracción de `games[]`;
- metadata de paginación;
- rutas `/es/games/category/2/N`;
- selección de imagen;
- provider ID;
- demo explícita;
- fallback free-slot;
- bootstrap sigue `PARCIAL`.

### Pragmatic

Mantener tests existentes de:

- catálogo AJAX;
- símbolos;
- modos;
- continuaciones;
- FSO;
- mystery;
- parsing de respuesta.

## 16. Evolución reciente / PRs relevantes

- PR #13: incorporación inicial de 1spin4win y Belatra.
- PR #14: D1 modelado con WS para apuestas.
- PR #15: intento de catálogo D1 WS; posteriormente superseded por evidencia HAR.
- PR #16: catálogo D1 corregido desde HAR Webflow.
- PR #17: ejecución directa de spin D1 sobre WS.
- PR #18: fallback runtime para D1 cuando assets no exponen `gameURL`.
- Belatra Next/RSC y el spin base HTTP cifrado ya están implementados; los siguientes cambios deben concentrarse en estados/features aún no observados y en robustez de catálogo.

La documentación debe actualizarse cada vez que un HAR contradiga una hipótesis previa.

## 17. Próximos trabajos prioritarios

1. validar en vivo la recuperación del catálogo Pragmatic bajo 502/WAF y, cuando el AJAX oficial vuelva a responder, confirmar un crawl autoritativo completo sin reconciliación destructiva;
2. ampliar Belatra sólo con evidencia nueva:
   - features cuyo `phaseNext` no sea `toPaid`;
   - buy bonus;
   - free spins;
   - selecciones `selectId`;
3. ampliar D1:
   - buy bonus;
   - side/ante modes;
   - features específicas no cubiertas por `st`;
4. reforzar clasificación automática de frames D1 y generar firmas reutilizables para estados todavía desconocidos;
5. añadir fixtures sanitizados de HAR/protocolo para regresiones reproducibles;
6. futuro backend remoto usando `ExecutionBackend`.

## 18. Invariantes del proyecto

1. Evidencia antes que hipótesis.
2. RAW antes que pérdida de información.
3. Nunca falso `OK`.
4. Provider-specific state machines.
5. GUI no debe bloquearse por I/O.
6. Una sesión = una máquina de estados secuencial.
7. Concurrencia entre sesiones, no dentro de una transición.
8. Historial de resultados no se borra al reconciliar catálogo.
9. El updater no debe destruir el checkout de desarrollo.
10. No documentar credenciales/tokens efímeros.
11. No extrapolar un provider desde otro.
12. Cuando una captura contradice documentación, corregir código + tests + docs.

## 19. Qué debe hacer un nuevo chat al retomar

Pedirle que:

1. lea `docs/HANDOFF.md`;
2. lea `docs/PROTOCOLS.md`;
3. lea `docs/OPERATIONS.md`;
4. revise `README.md`;
5. inspeccione el último commit de `main`;
6. no reimplemente protocolos ya resueltos;
7. use nuevos HAR/logs para añadir únicamente las transiciones faltantes;
8. corra/espere CI antes de mergear.

Este archivo es la fuente de continuidad humana del proyecto; el código y los tests siguen siendo la autoridad final.

## 9.5 Runtime action capture

Belatra ya no se limita a escanear strings JS. Después de resolver la demo:

1. abre la demo con Playwright;
2. registra requests/responses y WebSockets;
3. espera que termine el bootstrap;
4. cambia de fase a `action`;
5. intenta una entrada de diagnóstico:
   - control DOM visible con texto exacto Spin/Start/Girar/Tirar;
   - si no existe, enfoca el canvas visible más grande y envía Space;
   - último fallback: Space a nivel de página;
6. captura el delta de red posterior;
7. guarda `runtime-activity.json`.

La captura clasifica como señales de acción:

- requests no-GET;
- XHR/fetch;
- frames WebSocket enviados.

Analytics conocidos (Yandex, Google Analytics, GTM, DoubleClick) se marcan como ruido.

IMPORTANTE: una señal posterior al input todavía NO equivale a una tirada validada. Hasta identificar el request/frame y la respuesta terminal, el resultado permanece `PARCIAL`.

Artefactos Belatra por intento:

```text
demo-resolution.json
detail-page.html
demo-page.html
bootstrap-discovery.json
runtime-activity.json
```

El archivo `runtime-activity.json` es el siguiente artefacto prioritario para reconstruir el protocolo directo.

## 9.6 Belatra: spin base confirmado por HAR 2026-09-08

Nuevo estado autoritativo:

```text
catálogo: Next.js/RSC
demo: demo.bltr-static.com
runtime: encrypted HTTP POST /game
base spin: enter → start → finish
```

El iframe actual se obtiene de la ficha corporativa, por ejemplo:

```text
https://demo.bltr-static.com/belatra/demo?game=fortune_mummy
```

La demo crea una sesión, redirige a `/?modification=...&sid=...` y expone `var config` con:

- `request_crypt`;
- `sc`;
- `modification`;
- `nickname`;
- `user.sid`;
- moneda demo.

El protocolo de juego es `POST /game` cifrado. El HAR permitió descifrar y validar repetidamente:

```text
q=enter
q=start
q=finish
```

Terminal base:

```text
start:  basedeal → toPaid
finish: finished → toIdle
```

Tester-Spin debe ejecutar ese flujo directamente por requests y sólo conservar Playwright/runtime-action-capture como herramienta diagnóstica/fallback para formatos no cubiertos.

No persistir los valores reales de `sc`, `sid` o cookies. Guardar respuestas descifradas y metadata sanitizada.

Pendiente Belatra después de este HAR:

1. features cuyo `phaseNext` no sea `toPaid`;
2. buy bonus;
3. free spins;
4. selecciones `selectId`;
5. cualquier otra q específica observada en futuros HAR.

El spin base ya no debe quedar `PARCIAL` si termina en `finished→toIdle`.

## 9.7 Belatra: límite de sesión y variantes confirmadas

Observación operativa importante:

- el demo público de Belatra puede invalidar/rechazar sesiones cuando se abren varios juegos simultáneamente;
- al repetir los mismos títulos uno a uno aparecieron nuevos `OK`;
- Tester-Spin limita por defecto Belatra a `max_test_concurrency=1`;
- el scheduler impone ese límite incluso si la GUI o un backend futuro solicita más workers.

Esto evita clasificar como incompatibilidad del juego un `HTTP 500` inducido por concurrencia.

### Selector matemático / volatilidad

HAR de Slattors Battle - Orcs vs Elves:

```text
nickname=battle
modification=151
```

El `enter` expone:

```text
isMathElf = 1
vipMode.vipBetK = 1.2
buyBonus.buyTotalBetK = 3 opciones
```

El cliente oficial envía `isMathElf` en cada `start` y el HAR confirma HTTP 200 para:

```text
isMathElf=0 / vipOn=0
isMathElf=0 / vipOn=1
isMathElf=1 / vipOn=0
isMathElf=1 / vipOn=1
```

Por eso el start base debe preservar `isMathElf` cuando aparece en `gs`.

Las opciones de buy bonus se detectan pero no deben ejecutarse automáticamente hasta capturar su request exacto.

### Legacy double dialog

HAR de Lucky Drink:

```text
nickname=lucky_old
modification=6
```

Un spin ganador puede devolver:

```text
phaseCur=basedeal
phaseNext=toDoubleDialog
```

El cliente oficial puede declinar el gamble/double enviando directamente:

```json
{"q":"finish","ghistId":<historyId>}
```

y termina en:

```text
finished → toIdle
```

Por lo tanto `toDoubleDialog` es una continuación conocida y terminalizable; ya no debe quedar PARCIAL sólo por aparecer esa fase.

## 9.8 Belatra: diagnóstico de HTTP 500 y selectores isMath*

Después de serializar Belatra quedaron cuatro títulos con HTTP 500 persistente en start:

Book of Doom; BuyBonus of Maya; Cops vs Robs; Legacy of Doom.

Slattors Battle dejó de fallar al preservar isMathElf, confirmando que algunos juegos añaden campos específicos mediante su override cliente de addToRequestBody("start", ...).

El adapter ahora:
1. propaga automáticamente cualquier campo top-level de gs cuyo nombre cumpla isMath* y cuyo valor sea bool/int/float;
2. muestra esos campos como capacidades en el log ENTER;
3. escribe <label>.request.json antes de realizar el POST;
4. conserva <label>.response.raw.txt incluso en HTTP >= 400;
5. intenta parsear/descifrar el envelope aunque el status sea 500;
6. guarda siempre <label>.wire.json con status, content-type, tamaños y preview;
7. sólo después genera RuntimeError.

Esto es deliberado: no agregar campos inventados por nombre de juego. El próximo 500 debe producir evidencia suficiente para comparar el request rechazado con un HAR del mismo título o con su código cliente.

## 9.9 Belatra: line/bet compatibility and mathType

Four persistent start HTTP 500s were traced to request construction rather than transport.

For Book of Doom, BuyBonus of Maya and Legacy of Doom, enter returned current state with nlines=10 and betPerLine=5, while Tester-Spin forced nlines=min(linesAssortment)=1 and kept betPerLine=5. Their betDependOnLines tables explicitly reject that combination for line 1, where the minimum advertised bet is 10.

Rule now:
- preserve server-authoritative current gs.nlines when it is present and allowed;
- preserve gs.betPerLine;
- if the chosen line count has an explicit betDependOnLines row and the current bet is not allowed there, choose the first advertised allowed bet for that line;
- do not minimize lines just to reduce wager size.

Cops vs Robs exposes gs.mathType directly:
- mathType=0 corresponds to the Cops math;
- analInfo.mathTypeCops=0;
- analInfo.mathTypeRobs=1.

mathType is now propagated to start alongside existing isMath* selectors.

## Pragmatic catalog fail-closed

Incident observed 2026-09-08:
- official HTTP catalog returned 502;
- crawler fell back to preloaded DOM;
- fallback saw 96 image/card structures but only 61 game-like records;
- one false positive was named 日本語 and used a data:image thumbnail;
- the app treated that partial fallback as authoritative and deleted 641 existing Pragmatic rows.

Current invariants:
1. AJAX official crawl starts non-authoritative and becomes authoritative only after reaching a confirmed terminal page.
2. DOM/preloaded fallback is always non-authoritative.
3. Non-authoritative crawls may add/update validated games but never reconcile deletions.
4. App blocks reconciliation on catastrophic shrink: previous >=100 and current <60% of previous.
5. Pragmatic thumbnail-only slug recovery requires a normal HTTP(S) wp-content/uploads asset with an explicit WxH filename token.
6. data: images cannot create games.
7. external hosts cannot create Pragmatic /games/ records.
8. thumbnail-only recovery does not borrow broad ancestor/navigation text as the game name.
9. During non-authoritative fallback, preserved per-game game.json artifacts are scanned and merged back into the result so temporary site failures do not erase known catalog state.

## Pragmatic catalog hardening after fallback contamination

A real failure mode was observed when `https://www.pragmaticplay.com/en/games/` returned HTTP 502:

- the crawler fell back to DOM/preloaded enumeration;
- site chrome leaked into the DOM candidate set, including a Japanese language label with a data-URI image;
- only ~61 items were seen instead of the previously known ~700;
- an older local build reconciled that partial set and removed hundreds of catalogue rows.

Current invariants:

1. Pragmatic fallback DOM/preloaded crawls are always non-authoritative.
2. Non-authoritative crawls may add/update validated rows but must never delete existing rows.
3. Pragmatic uses `min_catalog_reconcile_ratio=0.90`; even an authoritative crawl cannot reconcile if it retains less than 90% of the previous catalogue when the catalogue is large.
4. Structural invalid-row detection is diagnostic before the crawl. No row is deleted before the new crawl proves authoritative.
5. Preloaded fallback only treats images with known native game-thumbnail signatures such as 339x180/338x180/340x180/300x160/600x320 as game images.
6. Data-URI placeholders are never persisted as thumbnails. If a valid lazy `data-src` exists it is preferred.
7. A navigation/language image next to a valid game URL cannot supply the game name; the slug-derived name is used instead.
8. Previously discovered per-game artifacts are preserved and may be used to recover known rows during a non-authoritative fallback.
9. Hidden/preloaded DOM cards do not prove catalogue exhaustion: if a click only reveals existing nodes but Load More remains active, the fallback keeps probing. It stops only when the control disappears or after 8 consecutive no-growth clicks.

The safe failure mode is under-enumeration with no deletion, never a destructive partial catalogue.


### BGaming HAR adicional: compras y free spins

Evidencia 2026-09-09:

- `AlienFruits3` confirma `options.feature_options.feature_multipliers` y ejecución de compras por `spin.options.purchased_feature`.
- compras observadas: `bonus_buy` y `bonus_chance`;
- costo real se valida con `feature_multiplier/base_bet`, no con `outcome.bet` solamente;
- `AlienFruits3` es seed-driven: `outcome.screen=null` + `outcome.storage.seed` es un resultado válido;
- `TreasureOfAnubis` confirma transición natural `spin → freespins → freespin...`;
- los freespins mantienen `round_id`, avanzan `last_action_id`, soportan retrigger y terminan en `state=closed` con `available_actions=[init,spin]`;
- las continuaciones `freespin` tienen débito cero y sus wins se acumulan en `balance.game`;
- el runner BGaming prueba SPIN base y cada compra publicada, y sigue automáticamente free spins hasta terminal.
