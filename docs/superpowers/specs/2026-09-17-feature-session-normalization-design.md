# Feature Session Normalization Design

## Status

Approved design for implementation on `lab/actions-validation`.

## Goal

Represent every purchased or naturally triggered multi-step game event as a first-class, provider-neutral `FEATURE_SESSION` without replacing provider-specific wire executors.

A feature session must preserve the full lifecycle from the root action that enters the event until the provider proves return to its base/terminal state. Logical feature rounds, wire transitions and selectable branches are distinct concepts and must not be collapsed into one `wire_steps` counter.

The design applies to all active providers: Pragmatic Play, RubyPlay, Red Tiger, BGaming, Belatra and 1spin4win/D1.

## Non-goals

- Do not build one universal request serializer for all providers.
- Do not infer picker domains from UI, OCR, canvas/WebGL, game names or array positions.
- Do not infer a logical feature round merely because one HTTP/WS request occurred.
- Do not treat every wire continuation as a wager.
- Do not weaken provider-local terminal validation or existing path coverage.
- Do not invent Belatra buy/gamble payloads while their wire contract remains unresolved.
- Do not treat missing D1 purchase evidence as proof of absence.

## Core invariants

1. Provider adapters remain authoritative for request construction and state transitions.
2. The common layer only normalizes evidence already produced by the provider runtime.
3. `wire_steps` and `logical_rounds` are different metrics.
4. A purchase root is not complete merely because its initial request succeeded.
5. A feature session is complete only when its terminal state is proven and every discovered selectable branch is closed.
6. Picker domains must come from provider/server/client evidence that proves the finite domain. Observing one accepted index never proves that it is the only option.
7. If a wire step cannot be classified as round, choice or known state transition without guessing, the session remains incomplete/unknown.
8. Natural and purchased features use the same normalized session model; only `trigger` differs.

## Normalized schema

Persist one artifact per final result:

`<run_dir>/feature-sessions.json`

Schema identifier:

`tester-spin/feature-sessions/v1`

Top-level form:

```json
{
  "schema": "tester-spin/feature-sessions/v1",
  "provider": "rubyplay",
  "game": "example",
  "complete": false,
  "session_count": 1,
  "sessions": [],
  "by_parent_mode": {}
}
```

### `FEATURE_SESSION`

Each observed session contains:

- `session_id`: deterministic identity inside the run.
- `trigger`: `PURCHASE` or `NATURAL`.
- `parent_mode`: root mode such as `PURCHASE_FREESPIN` or `SPIN`.
- `attempt_number`.
- `artifact_dir`: evidence location relative to the run when possible.
- `entry`: the provider action/message that entered the feature.
- `rounds`: ordered logical feature rounds.
- `choices`: ordered/path-sensitive selectable branches.
- `transitions`: known non-round state transitions when needed.
- `terminal`: provider-specific proof normalized to `proven` and `returned_to_base`.
- `totals`: `logical_rounds`, `wire_steps`, `choices`.
- `round_classification_complete`.
- `choice_coverage_complete`.
- `state`: `COMPLETE`, `INCOMPLETE` or `UNKNOWN`.
- `reasons`: exact blockers.

A one-step root purchase that never exposes a feature state does not automatically become a feature session. It remains a root purchase unless provider evidence proves an embedded/multi-round feature.

### `FEATURE_ROUND`

A logical round is one game outcome inside an active feature, not one network request.

Fields include:

- `ordinal`.
- `provider_action` or provider result-state identifier.
- `wire_step` when one round maps to one request.
- `source`: e.g. `wire_response`, `websocket_result`, `response_tree`.
- provider state identifiers where stable for evidence.
- `win`, `balance_before`, `balance_after` only when explicitly available.
- `stake_charged`: `true`, `false` or `unknown`; never inferred solely from a `bet` field.
- `evidence`: artifact/file/frame reference.

If one response contains multiple independently identifiable round result nodes, each becomes a separate logical round while preserving the same wire step.

### `FEATURE_CHOICE`

Fields include:

- `command`/provider action.
- `prefix`: path already selected before this branch.
- `required_options`.
- `covered_options`.
- `selected` when describing one execution.
- `domain_state`: `PROVEN`, `UNRESOLVED` or `CONTRADICTED`.
- `source`.
- `evidence`.

`choice_coverage_complete=true` only when every required option has the required number of terminal samples.

## Completion semantics

A normalized session is `COMPLETE` only when all are true:

1. entry into the feature is evidenced;
2. every observed wire transition is classified or explicitly known as non-round state plumbing;
3. all logical rounds visible in provider evidence are captured;
4. the feature reaches the provider's proven terminal/base state;
5. every discovered picker/select branch has a finite proven domain;
6. every required branch has been covered with the required samples;
7. no provider-specific unresolved state remains for that session.

A session is `INCOMPLETE` when the feature is definitely observed but one of those requirements is missing. `UNKNOWN` is reserved for evidence that indicates a possible feature but is insufficient to identify the lifecycle safely.

## Purchase closure

`PURCHASE_COMPLETE` requires more than a valid root request.

For an option that enters a normalized feature session:

```text
root purchase proven
AND feature session COMPLETE
AND feature terminal proven
AND choice graph complete
=> purchase may be PURCHASE_COMPLETE
```

If the root purchase is proven but its observed feature session is incomplete, the purchase option becomes `PURCHASE_UNKNOWN`, not `PURCHASE_FAILED`, unless the wire execution itself failed.

A terminal one-step purchase with no observed feature session may still be complete under the existing provider purchase contract.

## Natural feature closure

A base spin that enters an event creates `FEATURE_SESSION(trigger=NATURAL, parent_mode=SPIN)`.

A natural sample may remain a successful terminal spin only when its observed feature session is also complete. The absence of a feature in repeated natural spins is evidence of non-observation, not proof that the game cannot trigger one.

## Provider mappings

### Pragmatic Play

Pragmatic is the reference implementation.

- Root `doSpin` enters the event.
- Active free/respin state is already derived from provider feature fields.
- Repeated `doSpin` while the feature is active produces logical feature rounds.
- `doBonus`, `doCollectBonus`, `doCollect` and `doMysteryScatter` are state transitions unless their response evidence independently contains logical round outcomes.
- `doFSOption` is `FEATURE_CHOICE`.
- `fs_opt` + `fs_opt_mask` is authoritative picker-domain evidence.
- `pragmatic_exhaustive.py` already replays missing prefix-sensitive branches and remains the executor of record.

The purchase campaign must no longer bypass exhaustive FSO traversal when a purchased feature exposes FSO branches.

### Red Tiger

Red Tiger is the second reference implementation.

- `platform/game/spin` is the root action.
- `result_tree` may contain one or multiple independently identifiable result nodes; these are normalized as logical rounds using response-tree evidence.
- `game.choices.available` plus round id is authoritative picker-domain evidence.
- `branch_coverage.py` remains responsible for replaying every sibling/prefix on fresh sessions.
- A purchased round with any uncovered choice branch cannot close purchase coverage.

### BGaming

BGaming already has the required runtime primitives.

- Root `spin` enters the flow.
- `flow.round_id`, `flow.state` and `available_actions` carry lifecycle state.
- Proven round-producing continuation commands such as `freespin`/`respin` become logical rounds.
- Known state/setup continuations such as `preselection_game` remain transitions unless evidence proves a round outcome.
- An unclassified continuation inside an observed feature makes `round_classification_complete=false`.
- `flow_choices.py` supplies finite server/client-proven domains and prefix-sensitive paths.
- `bgaming_exhaustive.py` remains responsible for replaying branches.

BGaming stays outside the default purchase campaign until its campaign exclusion is explicitly changed; it still exports the common feature-session contract for ordinary validation.

### RubyPlay

- Root `buy_feature` or a natural `spin` can enter a session.
- `next_action` is state authority.
- `freespin`, `respin` and `minispin` are logical feature rounds.
- `select` and `pick` are choices/transitions, not rounds.
- A session terminates only when `next_action` returns to `spin`.
- Current `select/pick` wire evidence proves use of an `index` but not a finite domain. Those branches remain `DOMAIN_UNRESOLVED` until client/server evidence proves the domain.
- Branch evidence must be scoped to the concrete parent mode/attempt; one purchase must not inherit picker coverage observed in another mode.

### 1spin4win / D1

- One outbound type=1 play plus the following received type=3 result is one logical round.
- The first type=1 is the root spin. Subsequent type=1 messages caused by active states `{5,6,11,12}` are feature rounds.
- A session begins when the root result enters an active state and ends when a proven terminal result state is reached.
- Unknown type=3 states remain fail-closed.
- No picker domain is inferred from client method names alone. If a future WS state requires a choice, it remains unresolved until message type, payload and domain are proven.

### Belatra

- Current base `start/finish` behavior remains authoritative.
- A non-terminal phase after `start` is normalized as an observed but incomplete feature/state session if it is outside the known paid/idle terminal path.
- `toDoubleDialog` is a choice state with required `DECLINE` and `GAMBLE`; only the demonstrated branch counts as covered.
- `buyBonus.buyTotalBetK` remains advertised purchase evidence only until the request mapping is proven.
- No guessed `buyBonus`, `selectId` or gamble payload is permitted.

## Common integration points

### Provider finalization

`ProviderAdapter` gains a provider-local feature-session builder hook and two finalizers:

- `finalize_test_result`: normal sampling + feature-session gate + path gate.
- `finalize_purchase_result`: feature-session gate + path gate only; it must not run natural sampling logic.

This keeps purchase campaigns from bypassing feature/branch closure while avoiding unrelated soak sampling.

### Purchase campaign

`scripts/purchase_campaign.py` calls `provider.finalize_purchase_result(...)` after `test_purchase_paths()` and before `build_purchase_coverage()`.

The campaign therefore cannot promote a root purchase before feature and picker normalization has run.

### Purchase coverage

The common purchase coverage layer reads normalized state by `parent_mode`.

If an option would otherwise be `COMPLETE` but its observed feature session state is `INCOMPLETE` or `UNKNOWN`, the option is downgraded to `UNKNOWN` while preserving that the root request itself reached terminal.

### Farm contract

`execution_structure` gains a read-only `feature_sessions` summary. It is not a universal request DSL. Provider-local protocol blocks continue to own actual execution semantics.

A farm contract cannot be `ready=true` when an observed feature session required by a demonstrated mode remains incomplete.

## Failure behavior

- Artifact read/parsing ambiguity: do not crash the provider result; create an incomplete/unknown session with reason when the feature is definitely observed.
- Missing picker domain: `INCOMPLETE`, never guessed.
- Defensive guard reached: `INCOMPLETE`.
- Provider runtime failure before feature entry: ordinary runtime error; no fabricated feature session.
- Feature root succeeds but branch replay fails: root evidence remains, session/purchase coverage stays incomplete.

## Acceptance criteria

1. Every active provider can emit `feature-sessions.json` from its normal evidence format.
2. Pragmatic purchased/natural FSO flows retain exhaustive prefix-sensitive coverage.
3. Red Tiger purchased/natural choice flows retain exhaustive prefix-sensitive coverage.
4. BGaming flow choices and feature rounds map into the common schema without replacing its executor.
5. RubyPlay records every freespin/respin/minispin round and keeps unknown picker domains open, scoped to the correct parent mode.
6. D1 records every continuation type=1/type=3 pair as a logical feature round.
7. Belatra reports unresolved feature/buy/gamble states without invented wire.
8. Purchase coverage cannot be `PURCHASE_COMPLETE` for an observed incomplete feature session.
9. Farm contracts expose the normalized session summary and do not promote unresolved sessions.
10. Existing provider-specific wire contracts and path coverage remain isolated and fail closed.
