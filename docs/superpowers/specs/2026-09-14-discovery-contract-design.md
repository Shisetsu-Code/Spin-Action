# Discovery Contract Design

## Objetivo

Al terminar el descubrimiento de un juego, cada proveedor debe generar un archivo estable y legible por scripts que describa únicamente lo que ya quedó resuelto y validado para ese juego.

El archivo se llamará `farm-contract.json`. Su función en esta fase es exportar conocimiento; no ejecuta juegos ni vuelve a descubrir nada.

## Ubicación

```text
data/providers/<provider>/<juego>/
    game.json
    farm-contract.json
    analysis/
        farm-contract-candidate.json
```

`farm-contract-candidate.json` representa el último discovery aunque sea parcial. `farm-contract.json` representa el último contrato completo y validado. Un discovery parcial nunca pisa un contrato bueno anterior.

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
- `unresolved`: motivos exactos que impiden usar el contrato como completo

El core valida sólo el sobre común. Cada proveedor construye y valida su bloque `protocol`.

## Evidencia

Cada modo conserva uno de estos estados:

- `DEMOSTRADO`: ejecutado contra el servidor y validado hasta terminal conocido.
- `NO_VALIDADO`: se intentó pero no terminó correctamente.
- `CANDIDATO_WIRE`: el formato parece conocido pero no fue demostrado en esta corrida.
- `SOLO_ANUNCIADO`: el proveedor anuncia que existe pero el wire no está demostrado.

Sólo los modos `DEMOSTRADO` cuentan como resueltos.

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

## Interfaz de proveedor

`ProviderAdapter` incorporará hooks neutrales para exportación:

```python
def build_farm_contract(self, game: Game, result: GameTestResult) -> dict[str, Any]:
    ...

def validate_farm_contract(self, contract: dict[str, Any]) -> list[str]:
    ...
```

Los proveedores que todavía no implementen un contrato específico podrán generar un candidate `ready=false` indicando `PROVIDER_CONTRACT_UNSUPPORTED`; no se rompe su discovery actual.

## Integración

La generación ocurre después de `provider.finalize_test_result()` para trabajar sobre el estado final real:

```text
provider.test_game()
→ provider.finalize_test_result()
→ build_farm_contract()
→ validar
→ guardar candidate
→ promover si ready
```

Un fallo al escribir estos artefactos es diagnóstico y no cambia un `OK/PARCIAL/ERROR` ya determinado por el protocolo.

## Aislamiento

No se crea un DSL universal de HTTP/WS. El bloque `protocol` es autónomo por proveedor. El core no contiene lógica del tipo `if provider == "bgaming"`.

## Primera implementación

BGaming será el primer proveedor con bloque `protocol` completo porque ya tiene evidencia por modo (`DEMOSTRADO`, `NO_VALIDADO`, `CANDIDATO_WIRE`, `SOLO_ANUNCIADO`).

Pragmatic, RubyPlay, Red Tiger, Belatra y D1/OneSpin4Win usarán el mismo sobre y añadirán su bloque específico sin cambiar el core.

## Tests mínimos

- contrato común válido;
- schema inválido;
- unresolved impide promoción;
- modo obligatorio no demostrado impide promoción;
- todos los modos obligatorios demostrados permiten promoción;
- candidate parcial no pisa contrato previo;
- sanitización impide secretos;
- BGaming genera contrato estable desde resultado final;
- proveedor no soportado genera candidate explícito y no rompe discovery;
- concurrencia de juegos no mezcla contratos.

## No objetivos de esta fase

Esta fase no implementa el farm masivo, no ejecuta contratos headless, no añade UI analysis, no añade HAR analysis y no redescubre protocolo fuera del flujo actual.

## Criterio de aceptación

Después de probar un juego, existe un `analysis/farm-contract-candidate.json` fiel al resultado final. Si el juego quedó completamente resuelto, también existe `farm-contract.json` listo para lectura futura. Si quedó parcial, el contrato publicado anterior permanece intacto y el candidate explica exactamente qué falta.
