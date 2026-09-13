# Muestreo y aprendizaje del protocolo

## Objetivo y significado de cobertura

Se recolectan rondas completas con sus decisiones intermedias para estudiar la
semántica y reconstruir después la lógica local. El modo de entrada no equivale
al tipo de resultado: una compra puede abrir selectores, respins y otros eventos.
Una request enviada, un HTTP 200 o haber elegido una opción no prueban por sí solos
que esa ruta haya terminado correctamente.

`path-coverage.json` comprueba dominios anunciados/observados. `required_samples`
y `sample_counts` permiten expresar la cuota de muestras válidas por rama.
Pragmatic FSO, BGaming flow choices y Red Tiger publican estos contadores. Las
repeticiones configuradas se aplican también a las ramas descubiertas al expandir.
Los intentos fallidos quedan como evidencia pero no satisfacen la cuota.

La cobertura nunca demuestra que un RNG haya producido todos sus resultados.
Un evento natural que no reaparece puede dejar una ruta pendiente. Un dominio
desconocido, un límite de expansión o una acción sin contrato siguen siendo
pendientes; no se inventan payloads para convertirlos en `OK`.

## Catálogo de muestras

El finalizador común genera `sample-catalog.json` para todos los proveedores.

- `groups`: modo de entrada y secuencia completa de elecciones reconocidas,
  muestras válidas, cuota faltante y lista de intentos.
- `observed_state_types`: etiquetas de estado observadas en JSON (`command`,
  `na`, `flow.state`, `phaseNext`, `spinMode`, etc.) y referencias a las capturas.
- Cada muestra enlaza los archivos de evidencia mediante rutas relativas y
  SHA-256. Las secuencias conservan el orden de pasos y omiten bootstrap.
- `diagnostics`: archivos ausentes/externos, lecturas fallidas o referencias
  repetidas al mismo directorio. Duplicar una referencia no suma una muestra.
- `scope=observed_paths_only`: el catálogo inventaría evidencia, no demuestra
  exhaustividad del juego ni asigna semántica a campos desconocidos.

Los tags son literales del protocolo, no nombres inferidos de features. Los WS
sin JSON de respuesta desglosado conservan su evidencia original para análisis.
Los contratos especializados de emulación BGaming siguen siendo complementarios.
El catálogo no reemplaza los contadores históricos de intentos de la GUI. Si el
adaptador informa OK pero faltan muestras/evidencia para las rutas observadas,
el finalizador lo degrada a PARCIAL. No eleva un resultado parcial a OK.

## Cómo se usa GitHub para aprender casos desconocidos

La rama `tester-spin-runs` contiene documentos
`run-results/<provider>/<slug>.json`, con `source_commit`, resultado y artefactos.
La publicación reemplaza el documento del juego; las versiones anteriores quedan
en el historial Git. El corpus local conserva sus carpetas de corridas.

Para un caso desconocido:

1. Abrir el documento y comprobar `source_commit` antes de atribuir un fallo al
   código actual.
2. Encontrar el modo, la última respuesta válida, la acción anunciada y el error.
3. Reconstruir la request exacta y contrastarla con el HAR/serializer del cliente
   oficial. Una lista `available_actions` demuestra acciones, no sus argumentos.
4. Extraer un fixture mínimo sin datos de sesión y añadir una regresión.
5. Incorporar un contrato de proveedor y recorrer todas las opciones demostradas
   en rondas frescas, guardando las muestras terminales.

Desde esta revisión, la evidencia HTTP BGaming incluye `request.method`, URL
sin sesión y cuerpo JSON sanitizado de la request preparada, después de fusionar
opciones de perfil/elección. El error de un retry se guarda en un archivo separado
`http-<command>-<status>-retry.json`. No se reemplaza el primer error.

La publicación prioriza los índices de cobertura/muestras/perfil y luego los
artefactos de intentos fallidos. Sigue teniendo límites de 2 MiB por archivo y
16 MiB de fuentes por documento; `skipped_artifacts` indica lo que no se embebió.
Publicar el índice no significa que todas sus capturas hayan cabido en GitHub.

## Hallazgos del corpus del 13 de septiembre de 2026

Se revisaron 59 documentos BGaming: 45 PARCIAL, 12 ERROR y 2 OK. De ellos, 47
proceden de `e6fb9803a301fa12d1eda2d3b8d4f2a8b881ba2a` y 12 de
`7ec75b8f3e5b7e6692dde9e0aa5020d5fc2f582e`. Son resultados previos a esta corrección.

| Evidencia | Conclusión y tratamiento |
|---|---|
| Fortune Trio / Secret Bar: matriz de `purchased_feature[_level]` | Son parámetros acoplados de compra, ya enumerados por el executor. Se excluyen de la matriz de opciones independientes. |
| Cats Soup: `play_bonus`, Divine Queen: `freespin`, Ultras: `respin`, todos HTTP 422 `invalid_options` | El corpus preservaba la respuesta pero no el cuerpo exacto de la request fallida. Se corrige la captura; ese error genérico no demuestra por sí solo qué argumento falta. |
| Adventures: `state=select_bonus`, acción `play_bonus_game` | No es suficiente para asumir que usa el contrato `select_bonus(name)`. Falta request del cliente oficial para esa acción. |
| Alice Wonderluck / Frenzy Clusters: `gamble_bonus`, `pick_cards` | Falta contrato y dominio completo de opciones; permanecen pendientes. |
| HyperHive `51100` y `CONTRACT_UNRESOLVED` | Hay que separar errores de sesión/transporte de contratos incompletos. No se resuelven mediante aliases por título. |

## Correcciones estructurales

- FSO se identifica por el prefijo de elecciones, no por el número de paso
  (que varía con cascadas) ni por un conjunto global por modo.
- Los requests sueltos no cuentan como cobertura: falta asociarlos a una ruta
  validada. Red Tiger aporta su grafo explícito de rutas; los casos sin contrato
  de trazado permanecen pendientes.
- Un `coverage_required` sin dominio explícito queda `DOMAIN_UNRESOLVED`.
- La matriz BGaming se limita antes de materializar un producto cartesiano grande.
  Se publica el tamaño total y no se declara completa una matriz truncada.
- Los nombres de carpetas BGaming incluyen un hash para evitar colisiones. Mover
  una corrida nunca borra una carpeta destino existente.
- Un stop durante expansión conserva los resultados y termina CANCELADO.
- Las conexiones SQLite se cierran explícitamente al terminar cada operación,
  conservando commit/rollback y evitando bloqueos de archivos en Windows.

La validación local usa fixtures/simulaciones y los contratos observados; no
equivale a haber repetido en vivo todo el catálogo después de los cambios.
