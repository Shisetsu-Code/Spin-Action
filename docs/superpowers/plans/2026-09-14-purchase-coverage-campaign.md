# Purchase Coverage Campaign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate purchase actions across Pragmatic Play, RubyPlay, Red Tiger, Belatra, and 1Spin4Win with provider-specific wire evidence, execute every authoritative purchase option at least once, and fail closed instead of producing false positives.

**Architecture:** Add a provider-neutral purchase coverage envelope/aggregator, while keeping discovery and selector verification inside provider-local adapters. Existing `test_game()` transport implementations remain authoritative; the campaign runner reuses them and only promotes purchases whose exact provider selector is visible in a successful terminal attempt. Belatra and 1Spin4Win remain explicit `PURCHASE_UNKNOWN` until their own runtime evidence proves a purchase wire contract; no guessed payload is permitted.

**Tech Stack:** Python 3.12, existing provider adapters, `unittest`/`pytest`, GitHub Actions, Playwright only for provider bootstrap paths that already require it.

**Spec:** `docs/superpowers/specs/2026-09-14-purchase-coverage-campaign-design.md`

## Global Constraints

- Work only on `lab/actions-validation` in `Shisetsu-Code/Spin-Action`.
- Provider purchase wire formats stay isolated; no cross-provider inferred selector.
- A purchase is complete only with exact selector fidelity, protocol-valid response, demonstrated continuations, and a successful terminal attempt.
- HTTP 2xx, metadata, UI text, names, constants, multipliers, or a detected `buy` field alone are never sufficient.
- Missing authoritative absence evidence is `PURCHASE_UNKNOWN`, not `NO_PURCHASE_PROVEN`.
- One minimal execution per authoritative purchase option is sufficient for this campaign.
- Existing spin validation semantics must not be weakened.
- HAR capture remains disabled by default.
- Do not bypass Cloudflare/WAF or aggressively retry unavailable demos.
- BGaming remains outside the default campaign until its runtime module is trustworthy enough for this evidence standard.

---

### Task 1: Common purchase coverage contract and fail-closed aggregation

**Files:**
- Create: `tester_spin/purchase_coverage.py`
- Test: `tests/test_purchase_coverage.py`

**Interfaces:**
- Produces constants `PURCHASE_COMPLETE`, `PURCHASE_FAILED`, `PURCHASE_UNKNOWN`, `NO_PURCHASE_PROVEN`.
- Produces `make_purchase_option(...) -> dict[str, Any]`.
- Produces `finalize_purchase_coverage(result, *, options, inventory_state, authority, no_purchase_proven=False, reason="") -> dict[str, Any]`.
- Produces `aggregate_purchase_coverages(rows) -> dict[str, Any]`.
- Produces safe helpers for locating successful terminal attempts and reading request JSON only inside the run directory.

- [ ] Write failing tests for all four terminal coverage states and the ten false-positive defenses in the design.
- [ ] Verify the focused test file is RED in CI before implementation.
- [ ] Implement the minimal common module; it must not understand any provider selector semantics.
- [ ] Verify focused tests GREEN.

### Task 2: Provider-local purchase evidence adapters

**Files:**
- Create: `tester_spin/providers/pragmatic_purchase_coverage.py`
- Create: `tester_spin/providers/rubyplay/purchase_coverage.py`
- Create: `tester_spin/providers/redtiger/purchase_coverage.py`
- Create: `tester_spin/providers/belatra_purchase_coverage.py`
- Create: `tester_spin/providers/one_spin4win_purchase_coverage.py`
- Modify: `tester_spin/providers/base.py`
- Modify: `tester_spin/providers/pragmatic_farm_adapter.py`
- Modify: `tester_spin/providers/rubyplay/farm_adapter.py`
- Modify: `tester_spin/providers/redtiger/farm_adapter.py`
- Modify: `tester_spin/providers/redtiger/execution.py`
- Modify: `tester_spin/providers/belatra_farm_adapter.py`
- Modify: `tester_spin/providers/one_spin4win_farm_adapter.py`
- Test: `tests/test_purchase_coverage.py`

**Interfaces:**
- `ProviderAdapter.build_purchase_coverage(game, result) -> dict[str, Any]` defaults to `PURCHASE_UNKNOWN`.
- `ProviderAdapter.test_purchase_paths(...) -> GameTestResult` defaults to `test_game(..., spins=1)`.
- Each active provider overrides `build_purchase_coverage` with its own wire rules.
- Belatra overrides `test_purchase_paths` to use one base ENTER/START path rather than multiplying unrelated selector branches.

- [ ] Add synthetic failing tests for exact Pragmatic `pur`, RubyPlay `buy_feature_type` + price, Red Tiger `extras.features.featureBuy`, Belatra metadata-only unknown, and 1Spin4Win unresolved root purchase semantics.
- [ ] Pragmatic: require an enabled PURCHASE mode, exact `provider_pur`, a terminal clean attempt for that mode, and a stored `doSpin` request whose `pur` equals the discovered selector. Closed `doInit` inventory with no purchase modes may prove no-purchase.
- [ ] RubyPlay: require active-client/init inventory closure, executable purchase contract, exact `buy_feature` type/price in the request, and a clean terminal return to `spin`. Closed inventory with no purchase mode may prove no-purchase.
- [ ] Red Tiger: persist `has_feature_buy` from SETTINGS alongside parsed `feature_buys`; require exact `featureBuy` in request extras and terminal success. `hasFeatureBuy=false` plus zero parsed buys may prove no-purchase; `true` plus zero parsed buys remains unknown.
- [ ] Belatra: expose every `buyTotalBetK` advertised option as unresolved evidence while `buyBonus` wire mapping is unproven. Never execute a guessed selector.
- [ ] 1Spin4Win: preserve client action evidence but keep purchase coverage unknown unless exact root message type/arguments/option mapping are proven by official runtime evidence.
- [ ] Run provider-specific and full purchase tests GREEN.

### Task 3: Headless purchase campaign runner

**Files:**
- Create: `scripts/purchase_campaign.py`
- Test: `tests/test_purchase_campaign.py`

**Interfaces:**
- Inputs: `--provider`, optional direct target (`--slug`, `--game-url`, `--game-name`, `--symbol`), `--game-offset`, `--game-limit`, `--timeout`, `--max-pages`, `--data-root`, `--output-dir`.
- Outputs per game: finalized `result.json` plus `purchase-coverage.json`.
- Output aggregate: `purchase-campaign.json` with counts by purchase state and option state.
- Exit 0 only when every selected game is `PURCHASE_COMPLETE` or `NO_PURCHASE_PROVEN`; failures/unknowns remain visible and exit non-zero after all games were attempted.

- [ ] Write failing runner tests for default provider set, BGaming exclusion, deterministic selection, continuation after individual errors, and fail-closed aggregate exit semantics.
- [ ] Reuse `provider_class_for`, low-traffic catalog enumeration, direct target construction, provider rate limits, and provider-local `test_purchase_paths`.
- [ ] Persist evidence even when a game throws during bootstrap/runtime.
- [ ] Run focused runner tests and full unit suite.

### Task 4: GitHub Actions purchase laboratory

**Files:**
- Create: `.github/purchase-campaign.json`
- Create: `.github/workflows/purchase-campaign.yml`

**Interfaces:**
- Push configuration supports provider, direct target or offset/limit, timeout, max pages, Chromium toggle, and monotonically increasing run sequence.
- Workflow uploads `purchase-results/` on success or failure.
- Workflow never enables HAR fallback automatically.

- [ ] Add config-triggered workflow on `lab/actions-validation`.
- [ ] Install Chromium only when configured/required.
- [ ] Run `python -m scripts.purchase_campaign ...`.
- [ ] Upload evidence with `if: always()`.
- [ ] Ensure `PURCHASE_UNKNOWN` and `PURCHASE_FAILED` fail the workflow without aborting earlier game iteration inside the runner.

### Task 5: Verification and live provider campaign

**Files:**
- Modify provider-specific code only for reproducible systemic gaps found during live validation.
- Do not weaken the coverage classifier to make runs green.

**Interfaces:**
- Evidence source: GitHub Actions logs + uploaded purchase artifacts.

- [ ] Run compileall and the complete unittest/pytest suite in CI.
- [ ] Run a known Pragmatic purchase-capable title and verify exact `pur` fidelity.
- [ ] Run RubyPlay catalog targets; execute at least one demonstrated buy on every game whose active client/init proves a Buy Feature, while unavailable launchers remain unknown.
- [ ] Run Red Tiger targets; attempt the existing feature-buy path per game when SETTINGS is reachable; record Cloudflare/launcher blocks without bypass.
- [ ] Run Belatra across the authoritative catalog; enumerate advertised buy options and only execute them if a provider-local wire mapping is proven from normal runtime/client evidence.
- [ ] Run 1Spin4Win across the authoritative catalog; search its already-fetched official client/runtime evidence for a root purchase contract and execute only if exact message semantics are proven.
- [ ] Process catalogs in bounded offsets so an isolated game failure does not stop the provider.
- [ ] Before claiming completion, verify exact CI run IDs, purchase counts, complete/failed/unknown counts, and representative selector artifacts.

## Acceptance Check

A green purchase result must mean a real provider purchase was executed, not merely detected. Every purchase-capable game that can be authoritatively identified is attempted at least once per authoritative purchase option. Unavailable demos, unproven purchase protocols, and ambiguous inventories remain visible as unknown rather than being converted into success. Existing normal-spin coverage remains unchanged.