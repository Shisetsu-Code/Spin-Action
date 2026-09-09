# Tester-Spin

GUI extensible en Python para catalogar juegos por proveedor y probar automáticamente todos los modos de entrada detectables de cada juego.

## Primera versión

Proveedores disponibles: **Pragmatic Play**, **1spin4win (D1)** y **Belatra Games**.

> Para retomar el proyecto en otro chat o después de perder contexto, leer primero:
> - [docs/HANDOFF.md](docs/HANDOFF.md) — estado completo, arquitectura, decisiones y próximos pasos.
> - [docs/PROTOCOLS.md](docs/PROTOCOLS.md) — contratos observados por proveedor.
> - [docs/OPERATIONS.md](docs/OPERATIONS.md) — ejecución, updater, debugging y checklist de desarrollo.

- Recorre `https://www.pragmaticplay.com/en/games/` y su paginación.
- Guarda nombre humano, URL de ficha, slug e ID interno del proveedor cuando se resuelve.
- Descarga la miniatura exactamente desde la URL de mayor resolución/original expuesta por el catálogo y conserva sus bytes sin reescalar ni re-encodear.
- Cada juego vive en su propia carpeta con el mismo nombre humano del juego.
- Cada carpeta contiene `game.json` con links, IDs, endpoint, cver, campos `doInit`, modos detectados y metadata reutilizable.
- Conserva respuestas `.raw` además de JSON derivados para diagnóstico.
- Catálogo y resultados persistentes en SQLite local.
- Selección múltiple de juegos.
- Prueba todos los juegos o sólo los seleccionados.
- `Juegos simultáneos` configurable.
- `Repeticiones por modo` configurable.
- `Delay entre juegos` configurable.
- Timeout configurable.
- Arquitectura por adaptadores: agregar RubyPlay, BGaming u otros proveedores no requiere modificar el scheduler ni la GUI.

### Belatra Games

- El catálogo de slots usa la ruta observada `https://belatragames.com/es/games/category/2`.
- La aplicación oficial es Next.js: los juegos se extraen de `games[]` dentro del stream RSC de `self.__next_f.push(...)`; la paginación usa `current_page`, `last_page`, `per_page` y `total`.
- En el HAR de referencia se observaron 25 juegos/página, 5 páginas y 104 slots.
- La ficha corporativa actual publica un iframe `https://demo.bltr-static.com/belatra/demo?game=<nickname>`; aliases `free-slot` se conservan como fallback histórico.
- La demo crea una sesión y expone `var config` con `request_crypt`, `sc`, `sid`, `modification` y `nickname`.
- El runtime funcional confirmado es HTTP cifrado: `POST https://demo.bltr-static.com/game`. El único WebSocket del HAR era Yandex/WebVisor.
- El demo público se prueba con una sola sesión Belatra activa (`max_test_concurrency=1`): ejecutar varios juegos simultáneos produjo 500 intermitentes que desaparecieron al repetirlos individualmente.
- Algunos títulos exponen campos adicionales de modo en `enter`, por ejemplo `isMathElf` y `vipMode`; el start base preserva el selector matemático cuando existe.
- Los títulos legacy pueden pasar por `basedeal → toDoubleDialog`; Tester-Spin declina ese gamble con `finish` y valida `finished → toIdle`.
- Tester-Spin reproduce el cifrado AES-CTR del cliente y ejecuta el spin base endpoint-first: `enter → start → finish`.
- Un spin queda `OK` sólo cuando `start.phaseNext=toPaid` y `finish` termina en `phaseCur=finished`, `phaseNext=toIdle`.
- Features/buy/free-spins no observados todavía quedan `PARCIAL` con la respuesta descifrada preservada.

### 1spin4win (D1)

- El HAR de catálogo aportado muestra que el portfolio público de `1spin4win.com/games` es Webflow CMS renderizado en HTML; cada página se parsea desde `div.item_portfolio` y el “cargar más” se sigue directamente mediante `a.w-pagination-next[href]`.
- La URL demo publicada en `gs.1spin4win.com:10443` se usa para resolver los assets reales del juego. El resolver intenta primero extraer `gameURL`, nombre interno y versión desde HTML/JS.
- Algunos títulos no exponen `gameURL` como literal estático. En ese caso Tester-Spin abre sólo el bootstrap de la demo con Playwright, observa el WebSocket real mediante `page.on("websocket")` y recupera `gameName/version/config/currency` del primer frame oficial `A/u2 type=0`. La tirada posterior sigue ejecutándose con el cliente WS directo.
- En la captura de `VeryLucky1024`, el protocolo observado es `wss://gs.1spin4win.com:443/games` con prefijo de salida `A/u2`.
- Al abrir el socket se envía un mensaje tipo `0`: `A/u2{"key":"","type":"0","data":",,freeplay,<GameName>,<version>,<config>,<currency>,test"}`.
- La respuesta `type=1` inicializa líneas y apuesta (`l`, `b3`, `bs`, etc.). La tirada se envía como tipo `1` con `data="<lines>,<betIndex>,0"`; una respuesta `type=3` valida el resultado. `type=2` se trata como error.
- El keepalive observado por el cliente es `pns`; Tester-Spin responde `A/pns`.
- Estados de bonus/free-spins con `st in {5,6,11,12}` se continúan por el mismo mensaje de juego tipo `1`, con guard de 128 pasos para evitar loops.
- Las pruebas D1 ya son endpoint-first sobre WebSocket: no necesitan hacer clic en la interfaz para una tirada normal. Cada intento guarda `runtime-spec.json`, `ws-attempt.json` y `result.json`.
- Un juego pasa a `OK` sólo si cada tirada solicitada recibe un resultado terminal `type=3`; si responde pero queda en un estado no terminal se conserva como `PARCIAL`.

## Modos Pragmatic

A partir de `doInit`, el adaptador crea automáticamente:

- `SPIN`: tirada base.
- `ANTE_BET_N`: todas las variantes/ante-bet declaradas en `bls` (`bl != 0`).
- `PURCHASE_N`: todas las compras declaradas en `purInit` / `purInit_e`.
- `mode_evidence`: conserva cualquier otro campo de `doInit` relacionado con bet/buy/pur/ante/mode/feature/bonus para poder añadir nuevos handlers sin volver a descubrir el juego.

Para cada modo y repetición se abre un contexto de protocolo limpio, se ejecuta la entrada y se guardan request/response originales. Se siguen automáticamente las continuaciones conocidas (`doSpin` de feature y `doCollect`). Si aparece un estado todavía no implementado, por ejemplo un `na` nuevo, la respuesta no se descarta: se guarda RAW y el intento queda marcado con warning para implementar esa transición después.

## Carpeta de cada juego

Ejemplo:

```text
data/providers/pragmatic/Harvest Moon – Grave Profits/
  thumbnail.webp
  game.json
  tests/
    2026-08-27_14-30-00/
      discovery/
        doInit.request.txt
        doInit.response.raw
        doInit.response.json
        calibration.response.raw
        modes.json
        protocol.json
      SPIN/
        attempt-0001/
          bootstrap/
          step-000-entry.request.txt
          step-000-entry.response.raw
          step-000-entry.response.json
          step-000-entry.http.json
          attempt.json
      ANTE_BET_1/
      PURCHASE_1/
      result.json
```

`game.json` incluye como mínimo:

```json
{
  "provider": "Pragmatic Play",
  "provider_key": "pragmatic",
  "human_name": "Harvest Moon – Grave Profits",
  "provider_slug": "harvest-moon-grave-profits",
  "provider_internal_id": "vs15grprofits",
  "page_url": "https://www.pragmaticplay.com/en/games/harvest-moon-grave-profits/",
  "direct_game_link": "...",
  "thumbnail": {
    "source_url": "...",
    "local_file": "thumbnail.webp"
  },
  "provider_protocol": {
    "symbol": "vs15grprofits",
    "cver": null,
    "endpoint": "...",
    "mode_catalog": {},
    "doInit_fields": {},
    "calibration_fields": {}
  }
}
```

## Instalación rápida en Windows

Ejecutar:

```powershell
.\Abrir-Tester-Spin.cmd
```

El launcher crea `.venv`, instala dependencias y Chromium de Playwright la primera vez, y abre la aplicación.

Alternativa manual:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe run.py
```

## Diseño extensible

```text
tester_spin/
  app.py
  models.py
  storage.py
  scheduler.py
  providers/
    base.py
    pragmatic.py
    pragmatic_modes.py
```

Cada proveedor implementa el contrato `ProviderAdapter`:

1. `crawl_catalog(...)` -> catálogo neutral `Game`.
2. `test_game(...)` -> `GameTestResult` con todos los modos propios del proveedor.

La GUI, SQLite y el scheduler no conocen `openGame`, `doInit`, `doSpin`, `bl`, `pur` ni ninguna particularidad de Pragmatic. Para BGaming/RubyPlay se agrega otro adaptador y se registra en `ProviderRegistry`. 1spin4win y Belatra ya siguen este mismo contrato.
