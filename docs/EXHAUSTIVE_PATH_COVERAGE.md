# Exhaustive path coverage

Actualización de muestreo: consultar [Muestreo y aprendizaje](SAMPLING_AND_PROTOCOL_LEARNING.md).
Las ramas conocidas incluyen cuotas de muestras terminales; las elecciones se
identifican por su prefijo, no por el número de paso. En BGaming se excluyen
`purchased_feature` y `purchased_feature_level` de la matriz independiente porque
ya están acoplados y enumerados como contratos de compra.

## Invariante

`OK` significa que Tester-Spin completó todas las acciones ejecutables y todas las ramas finitas que el proveedor anunció o que fueron observadas durante la corrida.

Una respuesta HTTP/WS válida no basta. Una rama anunciada que no fue recorrida, un selector cuyo dominio no está resuelto, una continuación sin wire contract, o un estado nuevo que no se puede clasificar mantiene el juego en `PARCIAL`. Tester-Spin no inventa requests para convertir un resultado en `OK`.

El scheduler aplica `ProviderAdapter.finalize_test_result()` a todos los proveedores. El finalizador genera `path-coverage.json` y vuelve a comprobar las ramas declaradas por el adaptador y las ramas explícitas encontradas en los artefactos de la ejecución.

Los adaptadores pueden declarar una rama con:

```text
coverage_required = true
required_options  = [...]
covered_options   = [...]
```

Además, una acción de gameplay con `executable=false` o `observed=false` se considera cobertura pendiente aunque el adaptador no haya añadido explícitamente `coverage_required`. Las filas de metadata pura `DISCOVERED_ONLY` no se convierten automáticamente en acciones.

## Red Tiger

Fuente de verdad actual: settings/runtime de Evolution Games más respuestas `platform/game/spin` y `platform/game/choice`.

Las compras anunciadas por `featureBuy` se ejecutan por separado. Si una respuesta contiene `choices.available` y `selected=null`, cada opción es una rama real. Una elección consume el `roundId`, por lo que las ramas hermanas se prueban recreando una compra/ronda fresca. El expansor es recursivo: si `A` abre `[A1,A2]`, se recorren `A/A1` y `A/A2`; `Random` también cuenta como una rama de protocolo independiente.

Varios `spinMode` dentro de una misma respuesta no se convierten en compras ficticias: son fases observadas de una ejecución. Las ramas sólo se crean cuando el protocolo expone una elección efectiva.

Guardas: profundidad máxima 8 y 128 prefijos por modo. Alcanzar una guarda deja cobertura incompleta; nunca produce un falso `OK`.

## Pragmatic Play

Fuente de verdad: `doInit`, respuestas de juego y transiciones confirmadas por HAR.

Se ejecutan los modos base, ante-bet y purchases descubiertos por `bls`/`purInit`/`purInit_e`. Las continuaciones conocidas incluyen `doBonus`, `doCollectBonus`, `doCollect`, continuation `doSpin`, `doMysteryScatter` y `doFSOption(ind)`.

`fs_opt` se trata como un árbol, no como un conjunto global. Cada aparición de `na=fso` se identifica por su prefijo de selecciones anterior. Si una ruta `0` abre otro selector `[0,1]`, las ramas `0/0` y `0/1` son independientes. Cada rama faltante se reproduce en una sesión fresca con su secuencia `ind` forzada hasta ese punto y se siguen descubriendo ramas hijas.

El `path-coverage.json` también valida cada artefacto `fso-selection-*.json` por ocurrencia para impedir que una selección hecha en un prompt cubra accidentalmente otro prompt distinto.

Un `na` desconocido o una firma de protocolo sin handler queda `PARCIAL` con RAW preservado.

## BGaming

Fuente de verdad: init/runtime API-v2, cliente JS, HAR seleccionado por juego, HyperHive HAR/serializer y perfiles persistidos.

Las purchases y niveles ya se descubren dinámicamente y las continuaciones con wire conocido se siguen hasta estado terminal. Las acciones anunciadas por `flow.available_actions` que no tienen contrato ejecutable se registran como gameplay no cubierto y ya no pueden convivir con `OK`.

`additionalSpinOptions` era un hueco importante: el perfil descubría dominios finitos desde el cliente pero utilizaba sólo el primer valor. Ahora se construye el producto cartesiano de todos los valores demostrados en `spin_option_choices`; cada tupla se ejecuta en una sesión fresca y, dentro de esa tupla, vuelve a ejecutarse el conjunto normal de SPIN/purchases/continuaciones del proveedor.

No se prueban valores que el cliente no haya anunciado. Guarda: 128 combinaciones; excederla deja la matriz incompleta y por tanto `PARCIAL`.

HyperHive conserva su aislamiento y su descubrimiento por HAR/bundle. Una purchase o acción descubierta pero no ejecutable queda pendiente en lugar de degradarse a metadata opcional silenciosa.

## Belatra

Fuente de verdad: `enter`, contrato cifrado `/game`, y campos/start/finish demostrados por HAR.

El start conserva la configuración válida de líneas, apuesta y denominación. Para selectores de modo con dominio explícito se genera una matriz de sesiones frescas:

- `vipOn` se recorre como `0/1` cuando `vipMode.vipBetK > 1` demuestra que VIP es un modo real;
- cada `isMath*` escalar observado se recorre como `0/1`;
- `mathType` se recorre cuando `analInfo.mathType*` demuestra más de un valor explícito.

No se interpreta cada importe/denominación como un modo de juego: son parámetros de apuesta, no ramas del state machine.

`buyBonus.buyTotalBetK` anuncia compras, pero los HAR/documentación actuales del proyecto no contienen todavía un request de compra autoritativo. Esas opciones se registran como requeridas y no cubiertas: Tester-Spin queda `PARCIAL` en vez de inventar el wire.

En `toDoubleDialog`, `finish`/decline está demostrado. La rama gamble no tiene todavía contrato demostrado en el proyecto, por lo que un título que exponga ese diálogo queda `PARCIAL` hasta incorporar evidencia real de esa rama.

## RubyPlay

Fuente de verdad: launcher/init, gameserver, client bundle y action wrappers.

Las continuaciones demostradas son `respin`, `freespin`, `minispin`, `select` y `pick`. `select` y `pick` usan un campo `index`; el executor actual puede enviar esos índices y mantiene índices distintos en cadenas `pick`.

El proyecto todavía no demuestra un dominio finito completo para `select`/`pick` en todos los títulos. Por eso ya no se acepta que `index=0` signifique cobertura exhaustiva. Si una corrida entra en `select` o `pick`, se conservan los índices observados y se declara `DOMAIN_UNRESOLVED`; el juego queda `PARCIAL` hasta que un HAR o el cliente permita derivar de forma autoritativa todos los índices legales.

Las continuaciones sin wire contract siguen la misma regla: evidencia preservada, sin request inventado y sin `OK`.

## 1Spin4Win / D1

Fuente de verdad: WebSocket funcional `A/u2` y estados observados por HAR/cliente.

Los estados activos conocidos `st={5,6,11,12}` son continuaciones deterministas del mismo wire `type=1`, por lo que se siguen hasta `st=0` terminal. No hay en la evidencia actual del proyecto un selector de usuario equivalente a `choices.available`.

La auditoría ahora inspecciona los frames guardados. Un `type=3` con un `st` distinto de `{0,5,6,11,12}` se convierte en estado no resuelto y bloquea `OK`; no se lo trata accidentalmente como terminal.

## Política para nueva evidencia

Cuando aparezca un HAR nuevo, la prioridad es extraer el dominio y el wire exactos y convertir el gap en un executor provider-local. Hasta entonces el comportamiento correcto es `PARCIAL`, no elegir una opción arbitraria y no compartir heurísticas de wire entre proveedores.

El orden de autoridad es: request/response real del proveedor > código cliente que construye ese request > metadata runtime explícita > inferencia. Una inferencia nunca autoriza por sí sola a enviar una transición destructiva o de apuesta.
