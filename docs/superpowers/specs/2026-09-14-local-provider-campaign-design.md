# Local Provider Campaign Runner Design

## Goal

Add a local campaign runner that can validate every enabled provider concurrently from the user's own PC, with an independently configurable session count per provider, bounded request rate, checkpoint/resume, explicit-action coverage, and a natural-spin soak of up to 3,000 spins per game.

## Scope

The runner is local-only. It does not change the normal GUI workflow and does not change provider catalog/protocol ownership. Each provider keeps its own catalog, bootstrap, protocol, continuation, and evidence logic.

The runner coordinates existing provider adapters and adds only the provider-specific natural-spin entry paths required to guarantee that the soak loop never multiplies purchases, ante bets, selector matrices, or other explicit branches by the 3,000-spin budget.

## Configuration

Create `campaign.config.json` with global defaults and per-provider overrides.

Global defaults:

- `natural_spins_per_game`: 3000
- `natural_batch_size`: 100
- `coverage_spins`: 1
- `requests_per_minute`: 2000
- `timeout_seconds`: 60
- `max_pages`: 0, meaning full catalog
- `stop_on_structural_event`: true
- `output_dir`: `campaign-results/current`
- `data_root`: `campaign-data`

Per provider:

- `enabled`: boolean
- `sessions`: integer >= 1
- optional overrides for `natural_spins_per_game`, `natural_batch_size`, `coverage_spins`, `requests_per_minute`, and `timeout_seconds`

Default session counts:

- Pragmatic: 3
- RubyPlay: 3
- Belatra: 3
- 1Spin4Win: 3
- RedTiger: 1 because the adapter currently declares `max_test_concurrency = 1`
- BGaming: 1 by default; the user may raise it after confirming the provider behaves correctly on the local environment

The effective concurrency is always `provider.effective_test_concurrency(configured_sessions)` so a provider cap cannot be overridden accidentally.

## Execution Model

All enabled providers run concurrently. Each provider receives one provider instance and one shared `ProviderRequestRateLimiter`. A provider owns an internal worker pool sized to its effective concurrency.

Each game has two phases:

1. Coverage phase: `provider.test_game(..., spins=coverage_spins)` followed by `finalize_test_result()` and `build_action_audit()`. This is the short exhaustive/explicit-action pass.
2. Natural soak phase: repeated calls to `provider.test_natural_spins()` in batches until either the configured natural-spin budget is accumulated or a structural event is demonstrated.

The natural phase must never execute purchases or explicit selector matrices merely because the natural-spin budget is large.

## Structural Event Stop

A natural batch is considered to contain a structural event when at least one of these evidence-backed conditions holds:

- an attempt has more than one wire step;
- the batch discovers an observed continuation/choice/feature mode beyond the root SPIN;
- `sample-catalog.json` reports an `unclassified_wire_variant`;
- a provider-specific event detector reports a non-baseline state with an evidence artifact.

A structural event row must include at least one artifact/evidence path. A bare boolean is never sufficient.

When `stop_on_structural_event=true`, the game stops after the batch that first demonstrates the event and moves to the next game.

## Campaign Acceptance

Coverage remains fail-closed and uses the existing independent action audit.

Natural coverage is complete when either:

- the game reaches the configured natural-spin target with `status=OK`, zero failed spins, and at least the target number of successful spins; or
- the game stops early because an evidence-backed structural event was observed, with `status=OK`, zero failed spins, and at least one successful natural spin.

The provider campaign can be COMPLETE only when the catalog is authoritative and every catalog slug has exactly one accepted coverage row and one accepted natural row.

## Checkpoint and Resume

Each provider writes atomically:

- `catalog.json`
- `coverage-results.json`
- `natural-results.json`
- `checkpoint.json`
- `provider-summary.json`

`checkpoint.json` records completed coverage slugs, completed natural slugs, accumulated natural counts, event-stop metadata, and failures. Re-running the same output directory resumes completed work and does not repeat accepted games.

## Failure Semantics

- Provider/catalog/bootstrap failures are isolated to that provider/game and recorded; they do not cancel other providers.
- Ctrl+C sets one shared stop event. Workers finish/cancel safely and checkpoints are persisted.
- Unsupported `test_natural_spins()` is a hard provider error, not a fallback to `test_game(spins=3000)`.
- Non-authoritative catalogs can produce diagnostic output but never a COMPLETE provider summary.
- Duplicate slugs/results are campaign errors.

## CLI

Primary command:

```powershell
.\.venv\Scripts\python.exe scripts\provider_campaign.py --config campaign.config.json
```

Useful overrides:

```powershell
.\.venv\Scripts\python.exe scripts\provider_campaign.py --config campaign.config.json --sessions pragmatic=2 --sessions bgaming=1
```

The CLI prints provider/game progress, effective concurrency, rate-limit stats, event stops, checkpoint location, and the final provider/global verdicts.

## Testing

Use TDD. Unit tests must cover config validation, session overrides, effective provider caps, provider-level concurrency isolation, checkpoint resume, event-stop evidence requirements, natural aggregation, and fail-closed unsupported providers.

Provider tests must prove the natural path does not execute purchase modes. CI must compile all sources and execute the full existing unittest/pytest suite plus a fake-provider campaign smoke that performs no external network access.
