# Local Replay Soak Design

## Goal

Exercise the protocol analyzers at scale without sending additional requests to provider servers. The soak replays only evidence already present in local artifacts and `sample-catalog.json` files.

## Safety boundary

This runner is strictly offline. It must not import provider adapters, `requests`, Playwright, WebSocket clients, or any live execution backend. It reads JSON files from disk and writes JSON reports to disk. A 3,000-iteration result therefore means 3,000 local replays of captured evidence, not 3,000 new RNG outcomes.

## Input

The authoritative input is `tester-spin/sample-catalog/v2`. Each game is identified by `provider` + `game`. The runner consumes only validated samples from groups whose `mode_kind` is `SPIN`; PURCHASE, ANTE_BET and other explicit entry modes are reported but are not multiplied by the natural-spin budget.

## Replay policy

- Default budget: 3,000 local iterations per game.
- The replay corpus is the ordered list of validated SPIN samples already captured for that game.
- Iterations cycle deterministically through those samples so runs are reproducible.
- The dominant state sequence comes from `dominant_state_sequence_id` in the sample catalog.
- If a validated replay uses a different non-empty sequence id, the run stops early and records `REPLAYED_STRUCTURAL_VARIANT`.
- The event report preserves the original attempt number, state sequence and evidence references.
- No semantic label such as free spins, superspin or bonus is invented unless that label already exists literally in the captured wire evidence.
- If the corpus has only the dominant sequence, the full budget is consumed and the result is `BUDGET_EXHAUSTED_NO_CORPUS_VARIANT`.
- If there is no valid SPIN corpus, the result is `NO_VALID_SPIN_CORPUS`.

## Concurrency

Provider groups may run in parallel because all work is local. Within each provider at most 3 games are replayed concurrently. Providers do not share mutable state. The concurrency limit is configurable but clamped to 1..3.

## Output

Each game produces `replay-soak.json` with:

- schema and offline marker;
- provider/game;
- requested and executed iterations;
- corpus sample count and unique sequence count;
- stop reason;
- dominant sequence id;
- first replayed structural event, when present;
- source attempt/evidence references;
- an explicit note that the run cannot discover unseen RNG outcomes.

A corpus-level `summary.json` lists every provider/game and its result. Missing or malformed catalogs are fail-closed and reported, never silently skipped.

## Acceptance criteria

1. A corpus containing two ordinary SPIN samples and one distinct validated sequence stops on the first replay of that distinct sequence and records its original evidence.
2. A corpus containing only the dominant SPIN sequence executes exactly 3,000 local iterations.
3. PURCHASE-only variants cannot stop the natural-spin soak.
4. Per-provider game concurrency never exceeds 3.
5. Running the soak while `socket.socket.connect` is forbidden still succeeds, proving the runner performs no network access.
6. Existing tests remain green.
