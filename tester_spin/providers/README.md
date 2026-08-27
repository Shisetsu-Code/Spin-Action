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

Luego registrar una instancia en `tester_spin/app.py`:

```python
self.registry.register(NewProvider(self.data_root))
```

Reglas del proyecto para todos los proveedores:

- una carpeta por juego bajo `data/providers/<provider_key>/<Nombre humano>/`;
- miniatura original/nativa guardada sin resize ni re-encode;
- `game.json` con URL pública, link directo al cliente cuando se conozca, ID interno del proveedor y metadata de protocolo reutilizable;
- respuestas de red originales guardadas en `.raw` antes o junto al parseo;
- modos del proveedor detectados dinámicamente cuando sea posible;
- estados todavía desconocidos se conservan como evidencia RAW, no se inventa una transición;
- GUI, scheduler y SQLite permanecen neutrales respecto del protocolo.
