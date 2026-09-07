# Tester-Spin

GUI extensible en Python para catalogar juegos por proveedor y probar automáticamente todos los modos de entrada detectables de cada juego.

## Primera versión

Proveedores disponibles: **Pragmatic Play** y **Belatra Games**.

- Recorre `https://www.pragmaticplay.com/en/games/` y su paginación.
- Guarda nombre humano, URL de ficha, slug e ID interno del proveedor cuando se resuelve.
- Descarga la miniatura exactamente desde la URL de mayor resolución/original expuesta por el catálogo y conserva sus bytes sin reescalar ni re-encodear.
- Cada juego vive en su propia carpeta con el mismo nombre humano del juego.
- Cada carpeta contiene `game.json` con links, IDs, endpoint, cver, campos `doInit`, modos detectados y metadata reutilizable.
- Conserva respuestas `.raw` además de JSON derivados para diagnóstico.
- Persistencia seleccionable: SQLite local o Cloudflare D1.
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

### Cloudflare D1

La GUI permite elegir `SQLite local` o `Cloudflare D1`. Para D1 se usan las mismas tablas lógicas (`games` y `test_results`) y la API REST parametrizada de Cloudflare. El token no se guarda en archivos.

Variables necesarias:

```powershell
$env:TESTER_SPIN_D1_ACCOUNT_ID = \"<account-id>\"
$env:TESTER_SPIN_D1_DATABASE_ID = \"<database-uuid>\"
$env:TESTER_SPIN_D1_API_TOKEN = \"<token con D1 Read/Write>\"
```

También se acepta `CLOUDFLARE_API_TOKEN` como fallback. Los artefactos grandes (RAW, HTML, capturas) continúan en disco local; D1 almacena catálogo, estado y resultados. Para despliegues distribuidos de alto volumen se recomienda interponer un Worker con binding D1 en lugar de usar la REST administrativa directamente.

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

La GUI, los backends de persistencia y el scheduler no conocen `openGame`, `doInit`, `doSpin`, `bl`, `pur` ni ninguna particularidad de Pragmatic. Para BGaming/RubyPlay se agrega otro adaptador y se registra en `ProviderRegistry`. Belatra ya sigue este mismo contrato.
