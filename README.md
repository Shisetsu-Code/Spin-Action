# Tester-Spin

GUI extensible en Python para catalogar juegos por proveedor y ejecutar pruebas de spin de validación sobre los enlaces descubiertos.

## Primera versión

Proveedor implementado: **Pragmatic Play**.

- Recorre `https://www.pragmaticplay.com/en/games/` y su paginación.
- Guarda nombre, URL, slug, miniatura y símbolo cuando puede resolverlo.
- Descarga/cachea miniaturas localmente para mostrarlas en la GUI.
- Catálogo persistente en SQLite.
- Selección múltiple de juegos.
- Prueba todos los juegos o sólo los seleccionados.
- `Juegos simultáneos` configurable.
- `Tiradas/juego` configurable.
- `Delay entre juegos` configurable.
- Timeout configurable.
- Estado y error por juego, con historial persistente.
- Arquitectura por adaptadores: agregar RubyPlay, BGaming u otros proveedores no requiere modificar el scheduler ni la GUI.

## Instalación rápida en Windows

Ejecutar:

```powershell
.\Abrir-Tester-Spin.cmd
```

El launcher crea `.venv`, instala dependencias y abre la aplicación. La primera ejecución también instala Chromium para el fallback de descubrimiento de protocolo.

Alternativa manual:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe run.py
```

## Diseño

```text
tester_spin/
  app.py                 GUI y cola de eventos
  models.py              modelos neutrales por proveedor
  storage.py             SQLite
  image_cache.py         cache de miniaturas
  scheduler.py           concurrencia/delay/cancelación
  providers/
    base.py               contrato ProviderAdapter
    pragmatic.py          catálogo + protocolo de prueba Pragmatic
```

Cada proveedor implementa dos operaciones principales:

1. `crawl_catalog(...)` -> lista de `Game`.
2. `test_game(...)` -> `GameTestResult`.

El resto del programa trabaja exclusivamente contra ese contrato.

## Semántica de la prueba

`Tiradas/juego` representa **spins de validación independientes**. La prueba no intenta completar bonus/free-spins ni interpretar toda la máquina de estados del juego: su objetivo es validar que el enlace pueda abrir sesión, resolver protocolo y obtener una respuesta válida a un `doSpin`. Esto hace que una mecánica particular de un juego no bloquee el barrido de todo el catálogo.

Los datos locales se guardan bajo `data/` y no se versionan.
