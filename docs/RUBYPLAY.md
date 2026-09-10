# RubyPlay provider

## Estado

RubyPlay está integrado como proveedor aislado bajo `tester_spin/providers/rubyplay/`.
La implementación se deriva de los HAR observados y evita routing por nombre, slug o
ID de juego.

## Catálogo

El catálogo usa WordPress/Bricks sin Playwright. La página inicial se inspecciona
para localizar estructuralmente el query cuyo `data-query-vars.post_type` contiene
`games`. De ese mismo documento se obtienen `bricksData.restApiUrl`, nonces,
`postId`, idioma, `queryElementId`, página y límites. Ningún ID Bricks generado se
hardcodea.

Las páginas posteriores usan `load_query_page`; `updated_query.count`,
`max_num_pages`, `start` y `end` son la autoridad para validar continuidad y
completitud. Un crawl limitado, detenido o inconsistente se marca no autoritativo y
no puede reconciliar destructivamente el catálogo existente.

Los juegos se extraen por el namespace `/games/<slug>/`; el slug proviene de la URL,
no de transformar el nombre humano.

## Bootstrap y runtime

La ficha pública aporta un iframe `/launcher`. Sus parámetros son dinámicos:
`gamename`, `operator`, `server_url`, `currency`, `mode` y `lang`.

El flujo observado es:

```text
public game page
  -> launcher
  -> GET server_url/init-session/demo
  -> POST server_url/gameserver/demo action=init
  -> acciones posteriores dirigidas por response.data.next_action
```

`sessionKey` nunca se persiste en claro dentro de los artefactos de request.
`funModeData` se trata como state carrier opaco: se conserva completo y se devuelve
en la siguiente acción sin modificar su estado interno.

El cursor `an` también es dirigido por servidor: el request usa el `data.an` de la
respuesta previa y la validación exige, para el contrato observado, un incremento de
uno en la respuesta siguiente.

## Modelo de apuestas

No hay listas de apuestas por juego en el código. Después de `init`:

- `data.game_config.bets` define el dominio válido de apuestas;
- `data.game_config.def_bet_index` selecciona la apuesta nominal por defecto;
- `data.player.currency` y `data.player.subunit` describen la unidad monetaria;
- el cliente del proveedor puede demostrar `WAGER`, usado para calcular el stake
  efectivo como `bet * wager`.

La política inicial del tester es `default`: `Repeticiones por modo` repite el modo
con la apuesta anunciada por `def_bet_index`. No multiplica automáticamente las
repeticiones por todos los niveles de `bets`.

Por tanto `10000` significa diez mil iteraciones del modo, no diez mil configuraciones
hardcodeadas ni `len(bets) * 10000` ejecuciones.

## Descubrimiento del contrato cliente

Sólo se inspeccionan scripts del origen launcher y de los orígenes declarados por
`RP_CONFIG.cdn/root`. Analytics y terceros no son autoridad de protocolo.

Cuando las señales son unívocas, el perfil puede aprender:

- `BinarySerializer.VERSION` -> `v_protocol`;
- `MATH_VERSION` y `RTP` -> `v_math`;
- `WAGER` -> stake efectivo;
- tabla de wrappers de acciones -> nombres de `init`, `spin`, `respin`, etc.;
- `getBuyFeatureType` -> tipo de compra;
- `BUY_FEATURE_*_COST_IN_TIMES_BET` -> multiplicador de costo.

No se acepta una relación por mera coincidencia numérica. Si falta evidencia cliente
suficiente, la capacidad queda no ejecutable/PARCIAL.

## Modos observados

`SPIN` se ejecuta con la apuesta por defecto anunciada por `init`.

Si `init` anuncia `buy_feature_available=true` y el cliente demuestra tipo, wager y
multiplicador, se crea dinámicamente `PURCHASE_<TIPO>` y el precio se calcula desde
el contrato observado:

```text
buy_feature_price = nominal_bet * wager * feature_multiplier
```

Una compra seguida por múltiples continuaciones cuenta como una sola repetición del
modo. El executor sigue `response.data.next_action` hasta regresar a `spin`.

En la evidencia actual sólo está demostrado el wire-shape de la continuación
`respin`. Cualquier `next_action` diferente se registra como cobertura pendiente y
produce PARCIAL; no se inventan parámetros para `freespin`, `pick`, `select`,
`minispin` u otras acciones hasta disponer de evidencia.

## Artefactos

Cada juego mantiene `game.json` con launcher sanitizado, identificador interno,
perfil cliente reutilizable y perfil de apuestas. Cada ejecución crea una carpeta
`tests/<timestamp>/` con bootstrap, requests sanitizados, responses y pasos de
continuación.
