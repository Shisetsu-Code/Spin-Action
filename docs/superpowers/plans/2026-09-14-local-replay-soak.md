# Local Replay Soak Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a strictly offline replay soak that exercises captured SPIN evidence up to 3,000 iterations per game and stops early on a replayed structural variant.

**Architecture:** `tester_spin/local_replay_soak.py` contains the pure offline engine and bounded per-provider scheduler. `scripts/local_replay_soak.py` is a thin CLI that discovers `sample-catalog.json` files and writes reports. No provider adapter or network transport is imported.

**Tech Stack:** Python 3.12, stdlib JSON/pathlib/concurrent.futures, unittest/pytest.

**Spec:** `docs/superpowers/specs/2026-09-14-local-replay-soak-design.md`

## Global Constraints

- No live HTTP, WebSocket, Playwright or provider adapter imports.
- Default budget is exactly 3,000 local iterations per game.
- Natural-spin soak consumes only validated `mode_kind=SPIN` samples.
- Per-provider concurrency is clamped to 1..3.
- A replay never claims to discover RNG outcomes absent from the source corpus.

---

### Task 1: Pure replay engine

**Files:**
- Create: `tester_spin/local_replay_soak.py`
- Test: `tests/test_local_replay_soak.py`

**Interfaces:**
- `replay_game_catalog(catalog: dict, *, iterations: int = 3000) -> dict`
- `run_provider_replays(items: list[tuple[str, dict]], *, iterations: int = 3000, concurrency: int = 3, replay_fn=None) -> list[dict]`

- [ ] Write failing tests for early structural-variant stop, 3,000-iteration dominant-only replay, PURCHASE exclusion and no valid SPIN corpus.
- [ ] Run CI and verify the new tests fail because the module does not exist.
- [ ] Implement deterministic validated-SPIN sample cycling and fail-closed schema validation.
- [ ] Add bounded provider scheduler with concurrency clamped to 3.
- [ ] Add a test that patches `socket.socket.connect` to raise and confirms replay still works.
- [ ] Run the complete CI suite and require zero failures.

### Task 2: Offline corpus CLI

**Files:**
- Create: `scripts/local_replay_soak.py`
- Extend: `tests/test_local_replay_soak.py`

**Interfaces:**
- `discover_sample_catalogs(root: Path) -> list[tuple[Path, dict]]`
- `run_corpus(input_root: Path, output_root: Path, *, iterations: int = 3000, concurrency: int = 3) -> dict`

- [ ] Write failing tests using a temporary corpus containing multiple providers and malformed input.
- [ ] Implement recursive discovery of `sample-catalog.json`, grouping by provider and running provider groups concurrently while each provider is limited to 3 games.
- [ ] Write one `replay-soak.json` per game plus top-level `summary.json`.
- [ ] Treat malformed catalogs and duplicate provider/game pairs as explicit fail-closed rows.
- [ ] Run the complete CI suite and require zero failures.

### Task 3: Verify against the captured RubyPlay rare-event regression

**Files:**
- Extend: `tests/test_rubyplay_natural_event_regression.py`

- [ ] Convert the existing captured 15-step RubyPlay sequence into a `sample-catalog/v2` replay input in the regression test.
- [ ] Assert the offline soak stops when that non-dominant sequence is replayed and preserves the source attempt/evidence.
- [ ] Assert the report labels the observation as replayed evidence rather than a newly discovered live event.
- [ ] Run all unit tests and pytest integration tests.
