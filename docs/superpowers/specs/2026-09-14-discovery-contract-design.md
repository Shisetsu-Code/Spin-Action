# Discovery Contract Design

## Objetivo

Al terminar el descubrimiento de un juego, cada proveedor debe generar un archivo estable y legible por scripts que describa únicamente lo que ya quedó resuelto y validado para ese juego.

El archivo se llama `farm-contract.json`. Tester-Spin **no ejecuta el farm masivo**: su responsabilidad termina al descubrir, validar y persistir la estructura. El farm será otro programa que consuma estos archivos sin GUI y sin volver a descubrir apuestas, compras o elecciones.

## Ubicación

```text
data/providers/<provider>/<juego>/
    game.json
    farm-contract.json
    analysis/
        farm-contract-candidate.json
```

`farm-contract-candidate.json` representa el último discovery aunque sea parcial. `farm-contract.json` representa el último contrato completo y validado. Un discovery parcial nunca pisa un contrato bueno anterior.

## Proveedores activos

Los seis proveedores activos generan este formato:

- Pragmatic Play
- BGaming
- RubyPlay
- Red Tiger
- Belatra
- 1spin4win / D1

Cada uno conserva su protocolo autónomo. El core no inventa un DSL universal de HTTP/WS ni mezcla reglas entre proveedores.

## Sobre común

Todos los proveedores exportan:

- `schema`: `tester-spin/farm-contract/v1`
- `provider`
- `game`: slug, nombre, symbol/identifier estable cuando exista
- `ready`
- `source`: run, familia de protocolo y fingerprint estable si existe
- `bootstrap`: estrategia estable e inputs públicos/estables
- `modes`: modos descubiertos y su evidencia
- `continuations`: conocidas y no resueltas
- `terminal_contract`: semántica terminal identificada por el proveedor
- `protocol`: bloque específico del proveedor
- `execution_structure`: vista de lectura para el futuro farm
- `unresolved`: motivos exactos que impiden considerar completo el contrato

## `execution_structure`

Esta sección es deliberadamente una **vista de lectura**, no un lenguaje de requests. Resume lo ya descubierto para evitar resolver otra vez la superficie de apuestas y elecciones.

```json
{
  "execution_structure": {
    "wagers": [
      {
        "mode_id": "SPIN",
        "kind": "SPIN",
        "evidence": "DEMOSTRADO",
        "executor": "spin",
        "parameters": {}
      },
      {
        "mode_id": "PURCHASE_BONUS",
        "kind": "PURCHASE",
        "evidence": "DEMOSTRADO",
        "executor": "spin",
        "cost_multiplier": 100,
        "parameters": {
          "purchased_feature": "bonus_buy"
        }
      }
    ],
    "choices": [
      {
        "mode_id": "PURCHASE_BONUS__CHOICE_ROOT",
        "kind": "FSO_BRANCH",
        "evidence": "DEMOSTRADO",
        "executor": "choose",
        "parent": "PURCHASE_BONUS",
        "prefix": [],
        "domain": ["0", "1"],
        "covered": ["0", "1"],
        "coverage_complete": true,
        "parameters": {}
      }
    ],
    "provider_domains": {}
  }
}
```

### `wagers`

Incluye únicamente modos de apuesta conocidos: `SPIN`, `ANTE_BET`, `PURCHASE` y sus variantes ya descubiertas. Cada entrada conserva los parámetros estables que el módulo del proveedor ya conoce, por ejemplo:

- índices `bl/pur` y costos en Pragmatic;
- `allowed_bets`, `default_bet`, `effective_stake`, tipos y multiplicadores de compra en RubyPlay;
- `stake`, `feature_buy`, multiplicadores y costos en Red Tiger;
- selectores, `mathType`, `vipOn`, `line_bet` y `bet` en Belatra;
- estados conocidos en D1 cuando forman parte del contrato;
- opciones, features comprables, niveles y multiplicadores en BGaming.

No se inventa un valor ausente. Si el discovery no lo conoce, el archivo no lo fabrica.

### `choices`

Incluye elecciones y ramas ya descubiertas, por ejemplo `CONTINUATION`, `CHOICE_CONTINUATION`, `FSO_BRANCH` e `INDEXED_CHOICE`.

Cuando existe un dominio explícito se conserva:

- `domain`: opciones que el proveedor anunció o que el discovery probó como necesarias;
- `covered`: opciones realmente cubiertas;
- `coverage_complete`: `true/false` sólo cuando hay evidencia explícita suficiente; `null` cuando no corresponde afirmar cobertura;
- `parent` y `prefix`: posición de la elección dentro del flujo cuando están disponibles;
- `parameters`: metadata específica del proveedor.

### `provider_domains`

Conserva dominios estables que no pertenecen a un único modo pero son necesarios para interpretar la superficie del juego.

Ejemplos actuales:

- BGaming: `spin_option_choices`, `effective_bet_selector`, `effective_bet_multipliers`, `purchase_features`;
- RubyPlay: `bet_profile`, `client_profile`;
- Red Tiger: `stakes`, `default_stake`, `feature_buys`, `game_modes`, `math_modes`.

Pragmatic, Belatra y D1 pueden dejar este objeto vacío cuando toda su estructura ya está expresada en `wagers`/`choices`. Vacío significa “no hay dominio adicional persistido”, no “inventar uno”.

## Evidencia

Cada modo conserva uno de estos estados:

- `DEMOSTRADO`: ejecutado contra el servidor y validado hasta terminal conocido.
- `NO_VALIDADO`: se intentó pero no terminó correctamente.
- `CANDIDATO_WIRE`: el formato parece conocido pero no fue demostrado en esta corrida.
- `SOLO_ANUNCIADO`: el proveedor anuncia que existe pero el wire no está demostrado.

Sólo `DEMOSTRADO` cuenta como resuelto. Los otros estados pueden aparecer en el candidate para diagnóstico, pero nunca deben presentarse como estructura ejecutable confirmada.

## Promoción

Un candidate puede reemplazar `farm-contract.json` únicamente si:

1. el resultado final del juego es `OK`;
2. todos los modos obligatorios están `DEMOSTRADO`;
3. las continuaciones obligatorias están resueltas;
4. no quedan elementos en `unresolved`;
5. bootstrap y protocolo contienen sólo datos estables;
6. no se persisten secretos o valores efímeros;
7. validación común y validación del proveedor pasan.

Si no cumple, se guarda candidate con `ready=false` y no se toca el contrato publicado anterior.

## Datos prohibidos

No se persisten cookies, tokens de sesión, CSRF, authorization, credenciales, round IDs efímeros, signed URLs temporales ni secretos equivalentes.

## Integración

La generación ocurre después de `provider.finalize_test_result()` para trabajar sobre el estado final real:

```text
provider.test_game()
→ provider.finalize_test_result()
→ provider.build_farm_contract()
→ construir execution_structure
→ validar
→ guardar candidate
→ promover si ready
```

Un fallo al escribir estos artefactos es diagnóstico y no cambia un `OK/PARCIAL/ERROR` ya determinado por el protocolo.

## Aislamiento

No se crea un DSL universal de HTTP/WS. El bloque `protocol` sigue siendo autónomo por proveedor. `execution_structure` sólo normaliza la lectura de apuestas y elecciones descubiertas; no serializa requests ni decide transiciones.

## No objetivos de esta fase

Tester-Spin no implementa aquí:

- farm masivo;
- ejecución headless de contratos;
- workers distribuidos;
- scripts de explotación/farm;
- redescubrimiento durante el farm;
- UI analysis, OCR o canvas analysis nuevos.

Todo eso, si se necesita, pertenece al programa de farm futuro que consumirá `farm-contract.json`.

## Criterio de aceptación

Después de probar un juego existe `analysis/farm-contract-candidate.json` fiel al resultado final. Si el juego quedó completamente resuelto, también existe `farm-contract.json`.

Para los seis proveedores activos, el contrato contiene `execution_structure` con:

- `wagers`: apuestas/modos conocidos y sus parámetros estables;
- `choices`: elecciones/ramas conocidas y sus dominios/cobertura;
- `provider_domains`: dominios globales específicos del proveedor cuando existen.

El programa futuro debe poder leer esa estructura sin volver a analizar la GUI ni redescubrir qué apuestas o elecciones existen.
