# BGaming emulation contract

Tester-Spin can now turn a validated BGaming run into backend-facing protocol artifacts. The objective is not to infer the slot RNG. The objective is to describe, from captured runtime evidence, which requests are valid, which response families have been observed, and how each response moves the session state.

The exporter runs after the provider-neutral completeness gate, so `wire_replay_complete=true` is only possible when the final Tester-Spin result is `OK`, there is persisted request/response evidence, and no executable mode is still unresolved.

## Generated files

Each BGaming test run can contain:

```text
emulation-contract.json
protocol-contract.json
state-machine.json
outcome-catalog.json
backend-response-plan.json
backend-conformance.json
response-schemas/
  <outcome>.json
```

`emulation-contract.json` and `protocol-contract.json` currently contain the same top-level provider contract. The duplicate name is intentional while the cross-provider representation is still being introduced.

## protocol-contract.json

Contains:

- provider/game identity;
- runtime family discovered from BGaming evidence;
- observed request contracts;
- dynamic request paths;
- selector domains and selector vectors actually observed;
- modes discovered by Tester-Spin;
- unresolved executable modes;
- dispatch table from state + request contract to observed outcome families;
- `wire_replay_complete`;
- explicit `math_model_complete=false` and `rng_probabilities_known=false`.

Example shape:

```json
{
  "schema": "tester-spin/bgaming-emulation-contract/v1",
  "provider": "bgaming",
  "wire_replay_complete": true,
  "math_model_complete": false,
  "rng_probabilities_known": false,
  "dispatch": {
    "READY": {
      "SPIN": ["SPIN__TO__READY"],
      "SPIN__PURCHASE_BONUS_BUY": [
        "SPIN__PURCHASE_BONUS_BUY__TO__FLOW_PRESELECTION_GAME"
      ]
    }
  }
}
```

## state-machine.json

Represents observed transitions as:

```text
source state + request contract -> outcome -> target state
```

API-v2 states are normalized conservatively:

```text
flow.state=ready                       -> READY
flow.state=closed + spin available     -> READY
flow.state=<feature>                   -> FLOW:<feature>
```

Examples:

```text
READY
  + SPIN
  -> SPIN__TO__READY
  -> READY

READY
  + SPIN__PURCHASE_BONUS_BUY
  -> ...TO__FLOW_PRESELECTION_GAME
  -> FLOW:preselection_game

FLOW:preselection_game
  + PRESELECTION_GAME
  -> ...TO__READY
  -> READY
```

For HyperHive:

```text
result.final=false -> ROUND_ACTIVE
result.final=true  -> READY
```

## outcome-catalog.json

Lists response families demonstrated by the test run. Every outcome records:

- request contract;
- source/target states;
- raw provider states;
- observed available actions;
- terminal/non-terminal evidence;
- response schema reference;
- evidence paths back to the saved request/response pair;
- `math_probability_known=false`.

`observed_count` is diagnostic evidence only. It must never be used as an RNG weight.

## backend-response-plan.json

This is the artifact intended to answer the backend question:

```text
Given the current state and this received request, what kinds of responses am I allowed to return?
```

It maps:

```text
state
  -> request contract
     -> selector domains/vectors
     -> allowed observed outcomes
        -> target state
        -> response schema
```

The game engine/RNG can eventually choose one of those outcomes. This file deliberately does not tell the RNG how likely each outcome is.

## response-schemas/

A structural schema is generated for every observed outcome family. It contains:

- required keys observed across samples;
- nested object/array structure;
- observed string enums when the domain is small;
- paths identified as dynamic.

Typical dynamic paths include round/action ids, balance, win, seeds, state locks and session-related identifiers.

The schema is evidence-derived. It is not a claim that fields absent from the captured samples can never exist.

## backend-conformance.json

Turns every observed transition into a conformance case. A future emulator can be tested against the same cases:

```text
request contract accepted in source state
response matches expected outcome schema
session reaches expected target state
```

Unknown or unresolved contracts fail closed. Tester-Spin never invents a request shape merely to complete the state machine.

## additionalSpinOptions

The BGaming exhaustive layer already executes every client-proven combination of `additionalSpinOptions`. The contract exporter aggregates those runs recursively, including the branch-run folders, and records:

- selector domains observed;
- complete selector vectors observed;
- transition evidence for each vector.

Therefore volatility/level/mode selectors remain part of the backend request contract instead of being flattened into one arbitrary default.

## API-v2 and HyperHive

The exporter is transport-aware:

```text
API-v2    -> HTTP_JSON request contracts (`command`)
HyperHive -> HTTP_JSONRPC_2.0 request contracts (`method=play` etc.)
```

It does not translate one BGaming family into the other. The contract records the family actually demonstrated by the run.

## Mathematical model boundary

These artifacts are sufficient to build a wire-compatible/state-compatible backend skeleton, but not a mathematically faithful slot engine by themselves.

Still unknown unless separately reconstructed:

- symbol probabilities;
- reel/ways generation probabilities;
- feature trigger probabilities;
- multiplier distributions;
- RTP/volatility math;
- correlations between states and outcomes.

For that reason the generated contract explicitly keeps:

```text
math_model_complete=false
rng_probabilities_known=false
```

The later math/RNG layer should produce a normalized outcome. The BGaming serializer/state layer should then build a response conforming to the selected outcome schema and transition.
