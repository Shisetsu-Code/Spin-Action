# Local Provider Campaign Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local multi-provider campaign runner with per-provider session counts, hard campaign configuration, checkpoint/resume, explicit-action coverage, and evidence-backed natural-spin soak handling.

**Architecture:** Add a small campaign subsystem around the existing provider registry instead of modifying the GUI scheduler. The campaign runner owns provider-level concurrency and checkpoints; each provider retains protocol autonomy and must explicitly implement `test_natural_spins()` before it can enter the soak. Existing `ProviderRequestRateLimiter`, catalog enumeration, action audit, and campaign aggregate logic are reused and extended rather than duplicated.

**Tech Stack:** Python 3.12+, `concurrent.futures`, `threading`, JSON, pathlib, existing `requests`/Playwright provider adapters, unittest/pytest, GitHub Actions CI.

**Spec:** `docs/superpowers/specs/2026-09-14-local-provider-campaign-design.md`

## Global Constraints

- Natural soak default is exactly 3000 spins per game.
- Natural batch default is 100 spins.
- Provider request ceiling default is exactly 2000 requests/minute.
- Providers run concurrently; games inside one provider use a configurable session count.
- Effective sessions must respect `provider.effective_test_concurrency()`.
- Unsupported natural-spin execution fails closed; never substitute `test_game(spins=3000)`.
- HAR fallback remains disabled unless the provider itself already has reusable evidence; campaign code never enables capture implicitly.
- Coverage and natural results are checkpointed atomically and resumable.
- Event-based early stop requires persisted evidence, never a bare boolean.

---

### Task 1: Campaign configuration

**Files:**
- Create: `tester_spin/campaign_config.py`
- Create: `campaign.config.json`
- Test: `tests/test_campaign_config.py`

**Interfaces:**
- Produces: `CampaignConfig.load(path: Path) -> CampaignConfig`
- Produces: `CampaignConfig.with_session_overrides(values: list[str]) -> CampaignConfig`
- Produces: `ProviderCampaignConfig.effective_*` scalar settings.

- [ ] **Step 1: Write failing tests** for defaults, provider session overrides, invalid provider keys, sessions < 1, RPM <= 0, and per-provider overrides.
- [ ] **Step 2: Run unit test** and verify RED because the module does not exist.
- [ ] **Step 3: Implement immutable dataclasses and JSON validation** with canonical provider aliases (`one_spin4win` -> `1spin4win`).
- [ ] **Step 4: Run unit test** and verify GREEN.
- [ ] **Step 5: Commit** configuration and tests.

### Task 2: Evidence-backed natural event classifier and aggregate semantics

**Files:**
- Create: `tester_spin/campaign_events.py`
- Modify: `scripts/provider_campaign_aggregate.py`
- Test: `tests/test_campaign_events.py`
- Modify/Test: `tests/test_provider_campaign_aggregate.py`

**Interfaces:**
- Produces: `detect_structural_event(result: GameTestResult) -> dict | None`
- Event dictionaries include `kind`, `evidence`, `attempt`, and `wire_steps` where applicable.
- Aggregate natural row accepts early completion only when `event_observed=true` and `event_evidence` is a non-empty list.

- [ ] **Step 1: Write failing tests** for multi-step attempts, observed continuation modes, sample-catalog variants, and rejection of evidence-free event booleans.
- [ ] **Step 2: Run focused tests** and verify RED.
- [ ] **Step 3: Implement classifier** using attempts, discovered modes, and `sample-catalog.json` only.
- [ ] **Step 4: Extend aggregate semantics** so either quota completion or evidence-backed early event completion is accepted.
- [ ] **Step 5: Run focused tests** and verify GREEN.
- [ ] **Step 6: Commit** classifier/aggregate changes.

### Task 3: Provider campaign scheduler and checkpoint/resume

**Files:**
- Create: `tester_spin/campaign_runner.py`
- Create: `scripts/provider_campaign.py`
- Test: `tests/test_campaign_runner.py`

**Interfaces:**
- Consumes: `CampaignConfig`, provider classes, `enumerate_provider_targets`, `build_action_audit`, `ProviderRequestRateLimiter`, `detect_structural_event`.
- Produces: `run_campaign(config, *, provider_factory=None, progress=print, stop_event=None) -> dict`.
- Produces: provider artifacts under `<output_dir>/<provider>/`.

- [ ] **Step 1: Write fake-provider tests** proving all providers can run concurrently while each provider respects its own worker/session count.
- [ ] **Step 2: Add resume tests** proving accepted slugs are skipped and partial slugs resume.
- [ ] **Step 3: Add natural batching tests** proving quota accumulation and early event stop.
- [ ] **Step 4: Run focused tests** and verify RED.
- [ ] **Step 5: Implement scheduler** with one provider future per enabled provider and one provider-local `ThreadPoolExecutor`.
- [ ] **Step 6: Implement atomic JSON checkpoints** using temp-file + replace.
- [ ] **Step 7: Implement CLI** with `--config`, repeated `--sessions provider=N`, and `--output-dir` override.
- [ ] **Step 8: Run focused tests** and verify GREEN.
- [ ] **Step 9: Commit** scheduler/CLI.

### Task 4: Pragmatic natural-spin path

**Files:**
- Modify: `tester_spin/providers/pragmatic_farm_adapter.py`
- Test: `tests/test_natural_spin_contract.py`

**Interfaces:**
- Implements: `PragmaticProvider.test_natural_spins(...)`.
- Must execute only the root `SPIN` mode while preserving existing continuation handling and artifact generation.

- [ ] **Step 1: Add failing test** proving a purchase-capable mode catalog still invokes only SPIN in natural mode.
- [ ] **Step 2: Run focused test** and verify RED.
- [ ] **Step 3: Implement natural method** by reusing the existing `_browser_bootstrap`, `discover_modes`, and `_test_mode_once` primitives with the enabled root SPIN only.
- [ ] **Step 4: Finalize result/sample artifacts** without exhaustive FSO replay expansion.
- [ ] **Step 5: Run focused/provider regressions** and verify GREEN.
- [ ] **Step 6: Commit** Pragmatic support.

### Task 5: RubyPlay natural-spin path

**Files:**
- Modify: `tester_spin/providers/rubyplay/farm_adapter.py`
- Test: `tests/test_natural_spin_contract.py`

**Interfaces:**
- Implements: `RubyPlayProvider.test_natural_spins(...)`.
- Uses existing RubyPlay bootstrap/post/continuation primitives; never schedules `buy_feature`.

- [ ] **Step 1: Add failing test** where init advertises buy feature but natural method emits only `spin` plus server-directed continuations.
- [ ] **Step 2: Run focused test** and verify RED.
- [ ] **Step 3: Implement provider-local natural executor** with the same artifact shape used by normal RubyPlay SPIN attempts.
- [ ] **Step 4: Annotate action inventory and return a normal `GameTestResult`**.
- [ ] **Step 5: Run focused/provider regressions** and verify GREEN.
- [ ] **Step 6: Commit** RubyPlay support.

### Task 6: Red Tiger natural-spin path

**Files:**
- Modify: `tester_spin/providers/redtiger/farm_adapter.py`
- Test: `tests/test_natural_spin_contract.py`

**Interfaces:**
- Implements: `RedTigerProvider.test_natural_spins(...)`.
- Executes only `platform/game/spin` without `featureBuy`, while following required `platform/game/choice` continuations.

- [ ] **Step 1: Add failing test** proving advertised feature buys are not scheduled in natural mode.
- [ ] **Step 2: Run focused test** and verify RED.
- [ ] **Step 3: Implement natural executor** by reusing Evolution bootstrap and Red Tiger spin/choice helpers.
- [ ] **Step 4: Preserve existing runtime metadata/artifact semantics**.
- [ ] **Step 5: Run focused/provider regressions** and verify GREEN.
- [ ] **Step 6: Commit** Red Tiger support.

### Task 7: BGaming natural-spin path

**Files:**
- Modify: `tester_spin/providers/bgaming_farm_adapter.py`
- Modify only if required: `tester_spin/providers/bgaming_paths_v2.py`
- Test: `tests/test_natural_spin_contract.py`

**Interfaces:**
- Implements: `BGamingProvider.test_natural_spins(...)`.
- Must retain BGaming policy/HAR/server-guided diagnostics but schedule only the root natural SPIN/play command and server-directed continuations.

- [ ] **Step 1: Add failing test** proving purchase modes are excluded even when advertised.
- [ ] **Step 2: Run focused test** and verify RED.
- [ ] **Step 3: Implement a scoped natural-only policy flag** in the existing thread-local BGaming policy so concurrent games cannot leak state.
- [ ] **Step 4: Ensure normal exhaustive `test_game()` behavior is unchanged outside the natural scope**.
- [ ] **Step 5: Run BGaming focused/regression tests** and verify GREEN.
- [ ] **Step 6: Commit** BGaming support.

### Task 8: Request-rate enforcement for campaign providers

**Files:**
- Modify provider-local active adapters/helpers as needed.
- Test: `tests/test_provider_wire_rate_limit.py`

**Interfaces:**
- All campaign natural/coverage protocol sends consume the shared `ProviderRequestRateLimiter` attached to the provider instance.

- [ ] **Step 1: Extend existing rate-limit tests** to Pragmatic, RubyPlay, Red Tiger, and BGaming protocol sends.
- [ ] **Step 2: Run focused tests** and verify RED only on missing provider hooks.
- [ ] **Step 3: Add provider-local hooks** at the actual HTTP/WebSocket send points; avoid catalog asset counting.
- [ ] **Step 4: Run focused tests** and verify GREEN.
- [ ] **Step 5: Commit** rate-limit wiring.

### Task 9: Local operator UX and packaging

**Files:**
- Create: `INICIAR-CAMPANA.cmd`
- Modify: `README.md`
- Modify: `.github/workflows/package-lab.yml`
- Test: CLI smoke in CI.

**Interfaces:**
- `INICIAR-CAMPANA.cmd` creates/uses `.venv`, installs requirements, then runs `scripts/provider_campaign.py --config campaign.config.json`.

- [ ] **Step 1: Add CLI smoke test** using fake providers/config and no external network.
- [ ] **Step 2: Add Windows launcher and README instructions** including per-provider session editing/CLI overrides.
- [ ] **Step 3: Include campaign config/launcher/tests in the package workflow**.
- [ ] **Step 4: Run compile + full unittest + pytest**.
- [ ] **Step 5: Run packaged CLI `--help` and config validation in CI**.
- [ ] **Step 6: Commit** UX/package changes.

### Task 10: Verification and branch handoff

**Files:** none unless verification exposes a defect.

- [ ] **Step 1: Run the complete CI suite** on the campaign branch and confirm every step is green.
- [ ] **Step 2: Run campaign fake-provider smoke** and inspect generated checkpoints/summaries.
- [ ] **Step 3: Compare branch against `lab/actions-validation`** and confirm only planned files changed.
- [ ] **Step 4: Fast-forward `lab/actions-validation`** to the verified campaign commit.
- [ ] **Step 5: Re-run CI/package workflow on `lab/actions-validation`** and report exact commit/run IDs and local command.
