# Resultados de ejecución en GitHub

Tester-Spin publica evidencia de corridas terminadas en una rama separada `tester-spin-runs`, en `run-results/<provider>/<slug>.json`. La rama de resultados no se mezcla con `main` ni dispara el CI de `main`. Cada documento incluye `source_commit`, resultado y artefactos; las corridas anteriores del juego se consultan mediante el historial Git.

El objetivo es conservar `result.json`, contratos de cobertura/emulación y los artefactos textuales de request/response necesarios para depurar estados `PARCIAL` sin copiar logs manualmente.

Una falla de publicación nunca debe cambiar el estado de una prueba local ni borrar sus artefactos.

Los índices de cobertura/muestras se priorizan, seguidos por la evidencia de
intentos fallidos. Los límites y el procedimiento para transformar capturas en
contratos y regresiones están en [Muestreo y aprendizaje](SAMPLING_AND_PROTOCOL_LEARNING.md).
