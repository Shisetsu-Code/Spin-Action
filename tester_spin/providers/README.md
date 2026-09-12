# Agregar un proveedor

Crear `tester_spin/providers/<proveedor>.py` con una clase que implemente `ProviderAdapter`.

Contrato mínimo:

```python
class NewProvider(ProviderAdapter):
    key = "new-provider"
    display_name = "New Provider"
    catalog_url = "https://..."

    def crawl_catalog(self, *, stop_event, progress, max_pages=100, on_game=None):
        # devolver list[Game]
        ...

    def test_game(self, game, *, spins, timeout_s, stop_event, progress):
        # descubrir todos los modos propios del proveedor y devolver GameTestResult
        ...
```

Luego registrar una instancia en `tester_spin/app.py`.

## Invariante de cobertura exhaustiva

`OK` significa que Tester-Spin recorrió todos los modos y todas las ramificaciones ejecutables que el proveedor anunció u observó durante la prueba. No alcanza con que una ruta representativa responda correctamente.

Reglas obligatorias para todos los adaptadores:

- ejecutar `SPIN` y cada modo de apuesta/ante/purchase anunciado por el contrato activo;
- cuando una respuesta exponga alternativas seleccionables, probar **cada alternativa**;
- si seleccionar una alternativa consume la ronda, crear una ronda/sesión fresca para cada rama hermana en vez de reutilizar un `roundId` ya consumido;
- repetir la expansión recursivamente si una alternativa abre otro selector; el objetivo es recorrer hojas del árbol, no sólo el primer nivel;
- varios estados internos devueltos por una misma acción (`spinMode`, respins, free spins, cascadas, etc.) no son ramas por sí solos: sólo se expanden cuando el protocolo exige una elección/request diferente;
- una opción aleatoria (`Random`, cuando el proveedor la anuncia) se prueba como rama de protocolo, sin exigir un resultado semántico determinista;
- nunca inventar el wire contract de una rama. Si la rama existe pero su request todavía no está demostrado, conservar la evidencia y devolver `PARCIAL`;
- un límite defensivo de profundidad/cantidad de ramas puede detener una expansión patológica, pero alcanzar ese límite también deja la prueba `PARCIAL`;
- todo selector descubierto debe quedar representado en `discovered_modes` con `coverage_required=True`, `required_options` y `covered_options`, o mediante un contrato equivalente que el gate neutral pueda verificar.

El scheduler aplica `ProviderAdapter.finalize_test_result()` a **todos** los proveedores. Ese gate genera `path-coverage.json` y evita que un adaptador reporte `OK` si la metadata o los artefactos JSON muestran opciones descubiertas que no fueron recorridas. El gate es deliberadamente conservador: verifica cobertura, pero no fabrica requests específicos del proveedor.

Reglas generales del proyecto para todos los proveedores:

- una carpeta por juego bajo `data/providers/<provider_key>/<Nombre humano>/`;
- miniatura original/nativa guardada sin resize ni re-encode;
- `game.json` con URL pública, link directo al cliente cuando se conozca, ID interno del proveedor y metadata de protocolo reutilizable;
- respuestas de red originales guardadas en `.raw` antes o junto al parseo;
- modos del proveedor detectados dinámicamente cuando sea posible;
- estados todavía desconocidos se conservan como evidencia RAW, no se inventa una transición;
- GUI, scheduler y SQLite permanecen neutrales respecto del protocolo.
