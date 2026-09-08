# Operations, debugging and development

Este documento explica cómo operar Tester-Spin, dónde buscar evidencia y cómo modificarlo sin romper los invariantes del proyecto.

# 1. Entorno de desarrollo

Objetivo principal:

- Windows 10/11;
- PowerShell 5.1 compatible;
- Python 3.11+;
- Git;
- Playwright Chromium;
- acceso a GitHub.

Instalación:

```powershell
cd C:\Proyectos\Tester-Spin
py -m pip install -r requirements.txt
py -m playwright install chromium
```

Ejecución:

```powershell
.\Abrir-Tester-Spin.cmd
```

o:

```powershell
py run.py
```

# 2. Actualización

Checkout normal:

```powershell
cd C:\Proyectos\Tester-Spin
git pull
```

El launcher autoactualizable mantiene un runtime separado.

No confundir:

```text
checkout de desarrollo
!=
runtime gestionado por updater
```

El updater puede resetear/clean únicamente su runtime administrado.

# 3. Rutas del launcher

Habitualmente:

```text
%LOCALAPPDATA%\Tester-Spin\
```

Subdirectorios/archivos:

```text
runtime\repo
venv
playwright
updater.json
current.json
app-startup.log
app-startup-error.log
launcher-error.log
```

Los browsers Playwright se mantienen persistentes para no reinstalarlos en cada lanzamiento.

# 4. Build del launcher

```powershell
cd C:\Proyectos\Tester-Spin
.\scripts\build-launcher.ps1 -Clean
```

Resultado:

```text
dist\Tester-Spin.exe
```

Sólo es necesario reconstruir el launcher si cambia código incluido en el ejecutable bootstrap/packaging. Cambios normales de providers se obtienen mediante el updater del runtime.

# 5. Data root

Desde un checkout normal:

```text
data/
```

Contenido:

```text
tester-spin.sqlite3
providers/
```

No borrar `data/` para solucionar un parser salvo que exista una razón concreta. El histórico es valioso para regresiones.

# 6. SQLite

El catálogo actual vive en la tabla `games`.

Los resultados quedan en `test_results`.

`reconcile_provider_games()`:

- se ejecuta sólo tras un crawl completo solicitado con `0=todas`;
- elimina filas de `games` que ya no están en el catálogo;
- preserva `test_results`;
- preserva carpetas de artefactos.

Esto evita perder evidencia histórica cuando un proveedor retira un juego.

# 7. Catálogo completo vs limitado

GUI:

```text
Máx. cargas (0=todas)
```

`0` significa:

- continuar hasta condición oficial de fin del provider;
- internamente existe un guard alto.

Un número positivo sirve para debugging parcial.

No reconciliar catálogo contra una carga parcial.

# 8. Concurrencia

Parámetro:

```text
Juegos simultáneos
```

La ejecución local usa `LocalThreadExecutionBackend`.

Cada worker debe tener estado de red independiente cuando el protocolo lo exige.

No compartir una sesión mutable entre workers sin sincronización.

D1:

- un WebSocket por intento/sesión.

Pragmatic:

- sesiones HTTP aisladas por thread.

# 9. Responsividad Tk

Nunca ejecutar en el main thread:

- requests;
- waits de futures;
- WebSocket recv;
- Playwright;
- escritura pesada;
- espera de procesos.

Los workers publican eventos en una queue.

`app_current.py` drena con:

- máximo de eventos por tick;
- budget temporal;
- batching de log;
- ordenamiento una vez por slice.

Si la ventana vuelve a “No responde”, revisar primero si algún I/O fue reintroducido en Tk.

# 10. Estados visuales

La tabla muestra:

- miniatura;
- nombre;
- ID provider;
- estado;
- última prueba;
- URL.

Estados:

```text
PENDIENTE
OK
PARCIAL
ERROR
```

`PROBAR NO OK` debe seleccionar:

```text
status != OK
```

# 11. Debug de catálogo Pragmatic

Síntoma:

```text
faltan juegos
```

Revisar:

1. log de páginas AJAX;
2. cantidad por carga;
3. página final;
4. duplicados;
5. parser de card;
6. cambios de URL del sitio.

No volver a browser-click si el endpoint AJAX sigue disponible.

# 12. Debug de catálogo D1

Síntoma:

```text
catálogo vacío o incompleto
```

Revisar:

```text
data/providers/1spin4win/catalog-pages/
```

Validar selectores:

```text
div.item_portfolio
[fs-list-field="name"]
[fs-list-field="slug"]
img.image_portfolio-game
a.w-pagination-next[href]
```

Validar que el enlace demo siga usando host:

```text
gs.1spin4win.com
```

# 13. Debug de runtime D1

Orden recomendado:

## 13.1 runtime-spec.json

Debe contener:

```text
ws_url
origin
game_name
version
wallet
currency
freeplay
scripts_scanned
discovery
```

Si `ws_url` falta, revisar fallback.

## 13.2 runtime-bootstrap-observed.json

Se crea si el resolver necesita Playwright.

Buscar:

- URL WS;
- frame init;
- error de browser;
- game_name/version.

## 13.3 ws-attempt.json

Fuente principal de diagnóstico después de abrir socket.

Buscar:

- init enviado;
- mensajes recibidos;
- `type=1`;
- spin enviado;
- `type=3`;
- `type=2`;
- `st`;
- keepalive;
- wire_steps;
- terminal.

## 13.4 Fallas típicas

### No se encontró WSS estático

No es fatal.

Debe activar fallback Playwright.

### No se resolvió gameName/version

Usar init observado.

### Handshake rechazado

Revisar:

- origin;
- cookie;
- URL WS;
- demo freeplay;
- cambios de host/puerto.

### Timeout esperando type=1

Guardar frames previos. Puede haber:

- mensaje previo no contemplado;
- cambio de init;
- error envelope;
- auth/session.

### Timeout esperando type=3

Revisar el frame tipo 1 enviado y parámetros `l/b3/playmode`.

# 14. Debug de catálogo Belatra

Root actual:

```text
https://belatragames.com/es/games/category/2
```

Esperar objetos Next/RSC, no anchors.

Log esperado por página:

```text
juegos=25
current=1
last=5
total_remoto=104
```

Los valores exactos pueden cambiar con el catálogo.

Si devuelve muy pocos juegos:

1. guardar response;
2. buscar `self.__next_f.push`;
3. verificar que los chunks se puedan JSON-decode;
4. concatenar chunks;
5. buscar `"games":`;
6. verificar `meta.current_page`;
7. comparar `len(local)` vs `meta.total`.

No asumir que la clase CSS de las cards es API estable.

# 15. Debug de runtime Belatra

Antes del bootstrap se genera:

```text
demo-resolution.json
```

Si aparece un 404 en free-slot, revisar allí todos los aliases intentados y la URL finalmente seleccionada. El resolver no debe abortar por el primer 404.

Estado actual:

```text
DISCOVERY
PARCIAL
```

Artefacto:

```text
bootstrap-discovery.json
```

También:

```text
detail-page.html
demo-page.html
scripts
endpoint_candidates
```

El siguiente paso es capturar tráfico de juego real.

No convertir candidatos de strings JS en “spin endpoint” sin observar una acción real.

# 16. Cómo capturar un HAR útil

Para catálogo:

1. limpiar Network;
2. abrir catálogo;
3. cambiar páginas/cargar más;
4. recorrer hasta varias páginas;
5. exportar HAR con contenido.

Para juego:

1. abrir demo desde cero;
2. limpiar Network justo antes si es necesario;
3. esperar init completo;
4. hacer spin base;
5. hacer varios spins;
6. activar ante si existe;
7. comprar bonus si existe;
8. recorrer free spins;
9. collect;
10. exportar.

Problema: Firefox HAR puede omitir frames WebSocket.

Para WS, preferir además un capturador CDP/Playwright con:

```text
WebSocketCreated
WebSocketWillSendHandshakeRequest
WebSocketHandshakeResponseReceived
WebSocketFrameSent
WebSocketFrameReceived
```

# 17. Cómo agregar un proveedor

Implementar:

```python
class Provider(ProviderAdapter):
    key = "..."
    display_name = "..."
    catalog_url = "..."

    def crawl_catalog(...):
        ...

    def test_game(...):
        ...
```

Registrar en:

```text
tester_spin/providers/__init__.py
tester_spin/app.py
```

Reglas:

- provider key estable;
- slug estable;
- folder safe;
- miniaturas persistidas;
- logs claros;
- tests;
- no falsos OK.

# 18. Cómo agregar una transición

Proceso mínimo:

1. obtener evidencia;
2. escribir fixture/test;
3. implementar parser;
4. implementar acción;
5. guardar request/frame;
6. guardar respuesta;
7. decidir terminal;
8. ejecutar CI.

No hacer primero el código y después buscar evidencia que lo justifique.

# 19. Branch/PR workflow

Recomendado:

```text
main
→ branch descriptiva
→ commits atómicos
→ PR
→ CI
→ merge sólo si verde
```

No mergear una rama que falle compile/tests.

# 20. Logging

Los mensajes deben incluir:

- nombre del juego;
- modo;
- repetición;
- status;
- duración;
- cantidad de pasos/frames;
- error concreto.

Evitar mensajes ambiguos como:

```text
falló
```

Preferir:

```text
SPIN 1/1: ERROR RuntimeError: no llegó type=3
```

# 21. Seguridad de datos

No commitear:

- PAT;
- cookies reales;
- tokens de sesión;
- wstoken de analytics;
- credenciales.

Los HAR pueden contener datos temporales. Antes de convertirlos en fixtures permanentes, sanitizar.

# 22. Checklist antes de mergear un cambio de provider

- [ ] parser basado en evidencia;
- [ ] test unitario;
- [ ] no rompe proveedores existentes;
- [ ] status OK/PARCIAL/ERROR correcto;
- [ ] artefactos útiles;
- [ ] timeout;
- [ ] stop_event;
- [ ] no bloquea Tk;
- [ ] docs actualizadas;
- [ ] CI verde.

# 23. Checklist para retomar en un chat nuevo

Compartir el repo y pedir:

```text
Lee primero:
docs/HANDOFF.md
docs/PROTOCOLS.md
docs/OPERATIONS.md

Después inspecciona main y el CI reciente.
No cambies protocolos ya resueltos sin nueva evidencia.
```

Después adjuntar únicamente el HAR/log que corresponda al siguiente problema.
