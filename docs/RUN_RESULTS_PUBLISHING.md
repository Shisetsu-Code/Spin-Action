# Resultados de ejecución en GitHub

Tester-Spin publicará evidencia de corridas terminadas en una rama separada `tester-spin-runs`, dentro de `run-results/<provider>/<slug>/latest/`. La rama de resultados no se mezcla con `main` ni dispara el CI de `main`.

El objetivo es conservar `result.json`, contratos de cobertura/emulación y los artefactos textuales de request/response necesarios para depurar estados `PARCIAL` sin copiar logs manualmente.

Una falla de publicación nunca debe cambiar el estado de una prueba local ni borrar sus artefactos.
