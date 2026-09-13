# Structural Map v1

`tester-spin/game-structure/v1` describes observed control flow, not an emulator
and not a claim of exhaustive discovery. The provider-neutral implementation is
in `tester_spin/structure/`; the BGaming adapter is
`tester_spin/providers/bgaming/structural_map.py`.

## Artifacts and integration

BGaming JSON-flow transport calls feed `Observation` objects directly into a
run-scoped `Capture`. No HAR or text-log reprocessing is required. Failed HTTP,
non-JSON, and provider-error requests retain attempts without inventing edges.
Successful JSON with an accepted `flow.state` establishes a wire-observed
transition; this is explicitly **not** financial/reward validation.

The exhaustive wrapper writes `<game>/analysis/game-structure.json`, merging
previous runs, and copies a snapshot into `<run>/analysis/game-structure.json`.
`GameTestResult.structural_map` supplies its schema, path, coverage, and diagnostic
summary. The existing GitHub result publisher prioritizes the snapshot.
Other catalogs and their execution policies remain intact.

The JSON root contains `metadata`, `states`, `decision_points`, `choices`,
`transitions`, `request_contracts`, `response_contracts`, `field_semantics`,
`path_context`, `coverage`, `unknowns`, `evidence`, and `relations`. Entity tables
are dictionaries keyed by ID. `schema/game-structure-v1.schema.json` provides
the interchange envelope; `StructuralMap.load` additionally checks transition
references and rejects unsupported versions.

## Identity and merge

SHA-256-derived IDs include provider, protocol family, game, and structural
identity. State identity uses server state and an optional provider-proven
structural signature. Actions and available-action-list changes do not create
duplicate states. Decisions belong to a state and command. Choice identity uses
the option payload, not its display label. Transitions reference both contracts,
states, choice and inherited context. Neither balances nor response RNG values
participate in control-state identity.

Known sensitive/session/round fields are replaced with structural placeholders;
UUID values and UUID mapping keys are normalized. URLs in examples are redacted,
not persisted as credential-bearing references. Unknown opaque fields cannot be
assigned a session role without provider evidence; adapters must extend the
classification for additional protocol-specific credentials before ingestion.

An equal scope merges counts, observed types, options, actions, edges and
evidence. Absence never deletes knowledge. `merged_runs` provides retry
idempotency for finalization. A different client fingerprint archives the old
revision and starts a separate map, with distinct IDs in BGaming snapshots.
Provider/game/protocol mismatches fail rather than merge. Missing fingerprint
is explicitly unverified. Multiple fingerprints inside one capture fail closed.
Archived revisions accompany the run snapshot.

Writes use a same-directory temporary file, flush/fsync, then `os.replace`.
An OS file lock serializes per-game read/merge/write across threads/processes;
locks release when a process exits. A `ContextVar` isolates active captures.
Generation errors leave the game result available, mark it partial when
appropriate, and include a sanitized diagnostic.

## Contracts, decisions and context

Each recursive contract schema preserves observed type counts (including null),
sample counts, observed presence, optionality, nested properties, array element
schemas, observed array length bounds, and bounded scalar values. `required`
remains null: presence in all samples is not proof of a required protocol field.
Enums are observed values, never asserted exhaustive. UUID-keyed mappings use
`mapping_values` instead of one field per session key.

Request fields distinguish proven static command, selected option fields,
runtime fields, session fields and unknown fields. Response examples retain
sanitized real structures; there are at most three examples, each at most 8 KiB.
Schemas continue accumulating when a response exceeds that example bound.
Conditional requirements and unproven semantics remain unresolved. This first
version does not infer reward equations or create counterfactual outcomes.

The BGaming adapter consumes finite flow choices and client/runtime index-domain
evidence. A client-proven collection/index relationship becomes a `relations`
entry and a cardinality semantic mapping. No title-specific logic or fixed card
count exists. RNG arrays only contribute response schemas, never choices.

Coverage distinguishes discovered, executed, outcome observed and outcome derived.
Attempts and successful wire samples are counted separately. Decision metadata
does not assume mutual exclusion, repeatability or Cartesian products when
those properties are unproven. The map always reports `complete: false`.

`path_context` preserves prior selected options without interpreting them as
reward multipliers. Decision replay prefixes reference structural choices.
`StructuralMap.pending_replays()` resolves those references to ordered request
templates for a future scheduler consumer; runtime binding and current-state
validation are mandatory. It filters out heuristic/server-only targets. The
existing BGaming flow-choice scheduler still controls execution and performs
its own finite-domain/prefix checks. The map never dispatches requests.

## Explicit limits

- This iteration captures BGaming JSON `flow` transport. HyperHive and the other
  providers do not yet have structural-map adapters; unsupported observations
  produce an explicit unknown, not guessed states or cross-protocol contracts.
- Terminal status is known only where the existing BGaming closed/new-spin
  contract establishes it. Otherwise it is null.
- Dynamic non-UUID mapping keys, additional opaque runtime fields, response
  field semantics, conditional fields and complete UI cardinality may need more
  provider evidence. Schema node roles and `unknowns` retain this uncertainty.
- Replay prefixes are bounded at 128 steps and 32 representative prefixes per
  decision during capture. Overflow is explicitly unresolved.
- Real-money/live discovery was not run for development; generic Alice-shaped
  transport fixtures exercise purchase → gamble → pick → freespins, including
  the client-proven runtime index domain.

## Validation

`tests/test_structural_map.py` covers the graph, contracts, sanitization,
incremental/idempotent merge, revisions and atomic/concurrent writes.
`tests/test_bgaming_structural_map.py` exercises the real JSON transport function,
indexed choices, isolation, error handling and published snapshot embedding.
CI runs both unittest and pytest so function-style discovery regressions are
not silently omitted.
