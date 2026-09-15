# Purchase Coverage Campaign Design

## Status

Approved design for implementation on `lab/actions-validation`.

## Goal

Build an exhaustive, provider-isolated purchase-validation campaign that discovers and executes every authoritative purchase option exposed by every supported game, while preventing false positives by failing closed whenever purchase semantics are not proven by runtime wire evidence.

The campaign covers Pragmatic Play, 1Spin4Win, Belatra, RubyPlay, and Red Tiger. BGaming remains excluded until its runtime module is reliable enough to produce trustworthy purchase evidence.

## Non-goals

- Do not infer purchase support from UI labels, game names, DOM text, screenshots, OCR, or visual button detection.
- Do not mark a purchase complete because metadata merely contains a buy/bonus field.
- Do not mark a purchase complete solely because an HTTP/WebSocket request returned successfully.
- Do not invent provider wire fields or request shapes from naming similarities with another provider.
- Do not weaken existing fail-closed action-inventory rules.
- Do not use HAR capture unless a provider-specific purchase wire contract cannot be established from existing authoritative protocol/runtime sources and an explicit future task enables that fallback.
- Do not include BGaming in the default campaign.

## Core principle

A purchase is `PURCHASE_COMPLETE` only when one concrete provider option is proven end to end:

1. An authoritative runtime source announces or exposes the option.
2. The option has a stable provider-local identifier or selector.
3. The executor issues a request whose wire semantics demonstrably select that exact purchase option.
4. The remote server accepts the request and returns a protocol-valid response.
5. The execution follows all required demonstrated continuations.
6. The purchased round reaches a proven terminal state.
7. The stored evidence ties the announced option, sent selector, returned response, and terminal result together.

Anything weaker remains fail-closed.

## Result states

Every game receives a purchase coverage result independent from ordinary spin coverage.

### `PURCHASE_COMPLETE`

All authoritative purchase options discovered for the game were individually executed and reached a proven terminal state with no unresolved purchase branch.

### `PURCHASE_FAILED`

At least one authoritative purchase option was correctly identified and attempted with a demonstrated wire contract, but the runtime execution failed, returned a provider/server error, contradicted the expected option, or failed to terminate.

### `PURCHASE_UNKNOWN`

Purchase coverage cannot be closed. Examples include:

- metadata advertises purchase capability but the exact request selector is not proven;
- client/runtime evidence suggests a purchase but its type, price, identifier, or wire contract is ambiguous;
- the provider launcher/runtime is unavailable before authoritative purchase inventory can be obtained;
- an advertised purchase option exists but cannot be mapped uniquely to an executable request;
- no authoritative source proves either presence or absence of purchase support.

`PURCHASE_UNKNOWN` must never be promoted to success by heuristics.

### `NO_PURCHASE_PROVEN`

An authoritative provider source proves that the current game exposes no purchase actions. Mere failure to detect a purchase does not qualify.

## Evidence model

Each discovered purchase option is represented as a provider-neutral record while preserving provider-local semantics.

Required conceptual fields:

- `provider`
- `game_slug`
- `game_identifier`
- `purchase_id` — stable campaign identifier
- `provider_selector` — exact provider-local selector when known
- `display_name` — optional diagnostic label, never authoritative by itself
- `source_kind` — source that proved the option exists
- `source_evidence` — compact sanitized evidence reference
- `price` — when authoritatively known
- `price_multiplier` — when authoritatively known
- `currency_or_stake_basis` — when applicable
- `executable`
- `wire_contract_state` — `PROVEN`, `UNKNOWN`, or `CONTRADICTED`
- `execution_state`
- `terminal`
- `artifact_dir`
- `reason`

Provider secrets, transient auth tokens, cookies, and raw sensitive launch credentials must remain sanitized under the existing artifact policies.

## Promotion rules

The campaign must use stricter semantics than general mode discovery.

A purchase candidate may be listed for diagnostics without being executable. A candidate is executable only if its provider module can construct the request from evidence-backed runtime state without guessing fields.

A purchase option cannot be marked `PURCHASE_COMPLETE` when any of these are true:

- the selector was guessed from array position without authoritative mapping;
- the request reused another provider's convention;
- only a client constant or metadata multiplier was observed;
- the response was HTTP 2xx but the provider protocol reported an error or mismatched action;
- the response did not prove the requested purchase option;
- the flow stopped at a non-terminal feature state;
- a required continuation was observed but its wire shape is unresolved;
- multiple purchase variants were advertised and only a subset was executed;
- the purchase inventory itself is incomplete or ambiguous.

## Provider-specific authority

Provider modules remain isolated. There is no shared guessed purchase wire format.

### Pragmatic Play

Authoritative discovery comes from the current `doInit` purchase fields already parsed by `pragmatic_modes.py`, including `purInit` and `purInit_e`.

Each enabled purchase option must retain its exact `provider_pur` mapping and be executed through the existing Pragmatic `doSpin` flow with the corresponding `pur` selector.

Promotion requires the request artifact to contain the expected `pur`, a protocol-valid response, all required continuations resolved by demonstrated Pragmatic contracts, and terminal completion.

If `purInit`/`purInit_e` are inconsistent, incomplete, or ambiguous, the affected option remains `PURCHASE_UNKNOWN`.

### RubyPlay

Authoritative discovery requires agreement between the active client capability and current init session. `buy_feature_available=true` alone is insufficient when the client purchase contract is incomplete.

The executable contract requires a resolved `buy_feature_type`, positive wager basis, positive purchase multiplier, calculable price, and the demonstrated `buy_feature` wire action.

Promotion requires the request to carry the exact feature type and expected price, the response to remain consistent with those values when returned, and the flow to reach the normal `spin` state after all demonstrated continuations.

### Red Tiger

Authoritative discovery comes from current runtime SETTINGS and parsed `feature_buys`.

Each `FeatureBuy` entry is a distinct purchase option. The executor must send that exact `FeatureBuy` through the existing `platform/game/spin` payload builder.

Promotion requires a valid response, any required choice continuations to be resolved through the demonstrated `platform/game/choice` contract, and a proven terminal result.

If SETTINGS advertises `hasFeatureBuy` while no concrete `feature_buys` can be parsed, coverage is `PURCHASE_UNKNOWN`, not no-purchase.

### Belatra

Current metadata can expose `buyBonus.buyTotalBetK`, but the existing base request still sends `buyBonus: None`. Therefore current Belatra purchase metadata is discovery evidence only and is not an executable purchase contract.

Until an authoritative mapping proves exactly how a `buyTotalBetK` option maps to the request's `buyBonus` field and response semantics, every advertised Belatra buy option remains `PURCHASE_UNKNOWN`.

No guessed index, multiplier, enum, object shape, or copied convention may be used to promote it.

If future runtime evidence establishes the mapping, it must be implemented inside the Belatra module and covered by provider-specific tests before campaign promotion is allowed.

### 1Spin4Win

The current D1 executor proves base play and feature continuations over WebSocket, but it does not currently prove a root purchase action.

Client-action evidence may identify purchase-related methods or candidates, but such evidence alone must not create an executable purchase action.

A D1 purchase becomes executable only when official client/runtime evidence establishes the exact command/message type, arguments, and option mapping and the executor can demonstrate the corresponding remote response.

Until then, purchase presence/absence remains `PURCHASE_UNKNOWN` unless an authoritative client/runtime source explicitly proves that a game has no purchase action.

## Campaign architecture

Introduce a dedicated purchase campaign rather than overloading ordinary spin validation.

The architecture has three layers:

1. Provider purchase inventory adapter — converts authoritative provider-local evidence into normalized purchase records without erasing provider-specific selectors.
2. Provider purchase executor — executes only records whose wire contract is `PROVEN`.
3. Campaign aggregator — scans selected catalog games, records every option independently, and computes game/provider/global coverage without promoting unresolved candidates.

The campaign should reuse existing provider bootstrap, rate limiting, artifact sanitization, and terminal-state logic rather than duplicate transports.

## Session and traffic policy

- Use one minimal execution per purchase option by default.
- Use a fresh logical runtime/session for each purchase option unless the provider protocol itself proves that session reuse is required.
- Respect each provider's existing concurrency limits and rate limits.
- Do not download thumbnails or unrelated assets for purchase validation.
- Do not recrawl catalog pages when an authoritative cached/current catalog manifest is available.
- Do not enable browser bootstrap for providers that can prove purchase execution directly over HTTP/protocol.
- Do not retry external launcher/Cloudflare/demo failures aggressively; record them as unknown/failed according to where evidence stopped.

## Aggregation rules

Per game:

- zero purchases + authoritative no-purchase proof => `NO_PURCHASE_PROVEN`;
- one or more authoritative options and every option complete => `PURCHASE_COMPLETE`;
- any proven option attempted and failed => `PURCHASE_FAILED` unless inventory is also unresolved, in which case the report must preserve both failure and unresolved inventory details;
- any unresolved advertised/candidate option => `PURCHASE_UNKNOWN` for coverage closure.

Per provider:

- report total games scanned;
- games with authoritative no-purchase proof;
- games with purchase options;
- total authoritative purchase options;
- complete options;
- failed options;
- unknown options/candidates;
- games blocked before authoritative inventory;
- provider coverage closes only when every game is either `PURCHASE_COMPLETE` or `NO_PURCHASE_PROVEN`.

Global campaign success requires every included provider to close under those rules. External availability failures remain visible and prevent a false global complete result.

## Artifacts

Each game run should produce a compact purchase summary plus option-specific artifacts. The campaign-level output should include machine-readable JSON and a human-readable summary.

Suggested artifacts:

- `purchase-inventory.json`
- `purchase-options/<purchase_id>/request.json` or sanitized wire equivalent
- `purchase-options/<purchase_id>/response.json` or sanitized wire equivalent
- `purchase-options/<purchase_id>/summary.json`
- `purchase-coverage.json`
- campaign aggregate `purchase-campaign.json`

Existing provider-native artifacts can be referenced instead of duplicated when they already contain the required evidence.

## False-positive defenses

The following tests are mandatory at the design level:

1. Metadata-only buy capability must not promote a purchase.
2. HTTP 200 with mismatched provider error/action must not promote a purchase.
3. Advertised multiple purchase options with only one executed must not close coverage.
4. Ambiguous option-to-wire mapping must remain unknown.
5. Non-terminal purchased feature flow must not promote a purchase.
6. Missing authoritative no-purchase evidence must not become `NO_PURCHASE_PROVEN`.
7. Provider A's purchase convention must never make Provider B executable.
8. A discovered candidate with `executable=false` must survive aggregation as unresolved evidence.
9. External launcher/bootstrap failure before inventory must remain unknown rather than no-purchase.
10. Exact provider selector sent on wire must match the option promoted as complete.

## Testing strategy

Implementation follows TDD.

Tests should cover:

- normalized purchase evidence records and state transitions;
- fail-closed aggregation;
- provider-specific extraction/mapping for Pragmatic, RubyPlay, and Red Tiger;
- explicit metadata-only unknown behavior for Belatra;
- explicit unresolved-root behavior for 1Spin4Win;
- request/response selector fidelity;
- terminal-state requirements;
- campaign summary counts;
- exclusion of BGaming from default campaign;
- preservation of existing provider tests and CI.

Live validation follows unit/integration verification. It should start with representative known games per provider, then expand through authoritative catalogs in bounded batches. Individual external/demo failures are recorded and skipped without weakening coverage semantics.

## Acceptance criteria

The feature is acceptable only when:

- the campaign can enumerate purchase coverage for all included provider games;
- every promoted purchase has stored evidence tying discovery, exact selector, request, response, and terminal completion together;
- unresolved Belatra and 1Spin4Win purchase contracts cannot become false positives;
- multiple purchase variants are tracked individually;
- no-purchase is promoted only from authoritative absence evidence;
- BGaming is excluded by default;
- existing spin validation behavior remains unchanged;
- the new tests explicitly prove the false-positive defenses above;
- full CI passes before any implementation-complete claim;
- live campaign reports distinguish runtime/provider failures from incomplete purchase knowledge without fabricating success.
