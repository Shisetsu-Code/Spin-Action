# Tester-Spin

GUI extensible en Python para catalogar juegos por proveedor y probar automáticamente todos los modos de entrada detectables de cada juego.

## Primera versión

Proveedores disponibles: **Pragmatic Play**, **1spin4win (D1)** y **Belatra Games**.

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

- Recorre el catálogo público oficial de Belatra por páginas.
- Guarda nombre, slug, ficha, miniatura y URL demo.
- Usa `https://free-slot.belatragames.com/play/<slug>` como fallback de demo cuando la ficha no expone el enlace directamente.
- En cada prueba realiza bootstrap HTTP de ficha+demo, guarda HTML, scripts y candidatos de endpoints en `bootstrap-discovery.json`.
- Hasta disponer de una captura HAR/runtime que demuestre el contrato real de tirada/bonus/buy, Belatra se marca `PARCIAL` y nunca `OK` por un simple HTTP 200.

### 1spin4win (D1)

- D1 se trata como un proveedor **WebSocket-native**: catálogo, sesión y estado de juego se obtienen de frames WS.
- La URL configurable de catálogo es sólo una URL de entrada/lobby para que el navegador cargue el shell y establezca los sockets. El HTML/HTTP no se usa como fuente autoritativa de datos D1.
- El catálogo se construye desde objetos observados en frames WS y se guarda junto con `catalog-websocket-capture.jsonl` y `catalog-websocket-summary.json`.
- Se filtran sockets/payloads de telemetría. En particular, frames WebVisor/Yandex con `wv-type`, `wv-check`, `wv-hit`, `wstoken` o `sessionStart` no se consideran datos de catálogo.
- Las miniaturas pueden descargarse como assets HTTP/CDN una vez que su URL fue obtenida del catálogo WS; esto no convierte HTTP en fuente de datos del proveedor.
- Las pruebas de juego observan exclusivamente los WS funcionales y guardan `runtime-websocket.json`. Hasta clasificar handshake y frames reales de spin/bet/bonus/buy, el resultado queda `PARCIAL`.

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
