# GitHub Actions Provider Validation Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a GitHub Actions laboratory that validates Tester-Spin provider/game discovery fail-closed, records every discovered gameplay action with evidence, and never treats an unproven inventory as complete.

**Architecture:** Keep provider protocols autonomous. Add a provider-neutral audit layer that reads `GameTestResult`, attempts, discovered modes, branch-coverage artifacts, and provider artifacts without inventing wire requests. Add a headless Actions runner that crawls/tests one provider at a time, persists machine-readable reports, and fails the workflow unless the audit verdict is `COMPLETE`.

**Tech Stack:** Python 3.12, unittest/pytest, existing Tester-Spin provider adapters, GitHub Actions, Playwright only when the selected provider actually requires browser bootstrap.

**Spec:** Existing provider contracts in `docs/PROTOCOLS.md`, `docs/SAMPLING_AND_PROTOCOL_LEARNING.md`, and `docs/EXHAUSTIVE_PATH_COVERAGE.md`.

## Global Constraints

- Work only in `Shisetsu-Code/Spin-Action`; do not modify `Tester-Spin`.
- Provider protocol logic remains isolated by provider.
- Fail closed: unknown inventory/evidence is `UNKNOWN`, known missing coverage is `INCOMPLETE`, and only demonstrably closed coverage is `COMPLETE`.
- A provider/game `status=OK` is not sufficient evidence by itself.
- Do not download/capture HAR by default. HAR capture is an explicit fallback only when normal wire/bootstrap evidence cannot resolve the game.
- Do not execute invented requests or infer actions from game names/slugs.
- Preserve diagnostics and action evidence as workflow artifacts.
- Start with one provider and one game; expand only after the gate proves trustworthy.

---

### Task 1: Provider-neutral action audit

**Files:**
- Create: `tester_spin/action_audit.py`
- Test: `tests/test_action_audit.py`

**Interfaces:**
- Consumes: `GameTestResult`, `SpinAttempt`, and optional `path-coverage.json`.
- Produces: `build_action_audit(result) -> dict` with `verdict`, `actions`, `unknown_reasons`, `missing_reasons`, and evidence counts.

- [ ] Write failing tests covering: terminal demonstrated action => complete candidate; actionable discovered mode without terminal evidence => incomplete; no demonstrable inventory closure => unknown; missing branch options => incomplete; ERROR/CANCELADO => error/cancelled verdict.
- [ ] Run the focused tests in Actions and verify the expected RED failure.
- [ ] Implement the smallest audit module that satisfies the tests without provider-specific wire logic.
- [ ] Run focused tests and the full suite.

### Task 2: Headless provider probe

**Files:**
- Create: `scripts/actions_provider_probe.py`
- Test: `tests/test_actions_provider_probe.py`

**Interfaces:**
- Inputs: provider key, game limit, repetitions, timeout, max catalog pages, output directory, explicit `--allow-har-fallback` flag.
- Outputs: `summary.json`, one audit JSON per game, provider logs, and non-zero exit unless every selected game is `COMPLETE`.

- [ ] Write failing tests for provider selection, deterministic game limiting, verdict aggregation, and HAR fallback defaulting to disabled.
- [ ] Implement provider construction for Pragmatic, BGaming, RubyPlay, Red Tiger, Belatra, and 1spin4win.
- [ ] Crawl once per provider invocation, test games sequentially, run provider finalization, and audit each result.
- [ ] Make `UNKNOWN` and `INCOMPLETE` fail the command while preserving artifacts.

### Task 3: GitHub Actions laboratory

**Files:**
- Create: `.github/workflows/provider-validation.yml`

**Interfaces:**
- Manual inputs: provider, game limit, repetitions, timeout, catalog page limit, HAR fallback boolean.
- Push-on-lab defaults: Pragmatic, one game, one repetition, no HAR fallback.

- [ ] Install Python dependencies.
- [ ] Install Chromium only for runs/providers that need browser bootstrap.
- [ ] Execute `scripts/actions_provider_probe.py`.
- [ ] Upload `action-results/` even on failure.
- [ ] Ensure the workflow fails on `UNKNOWN`, `INCOMPLETE`, `ERROR`, or `CANCELLED`.

### Task 4: Provider-by-provider expansion

**Files:**
- Modify only provider-specific code/tests when evidence proves a concrete gap.

**Interfaces:**
- Input: failed audit report and preserved provider artifacts.
- Output: exact regression test plus minimal provider-local fix.

- [ ] Run Pragmatic first, one game at a time until every advertised mode/branch is demonstrated or explicitly classified unknown.
- [ ] Repeat for Belatra, 1spin4win, RubyPlay, Red Tiger, then BGaming.
- [ ] For each failure, inspect normal wire/bootstrap artifacts first. Enable HAR fallback only when those artifacts cannot identify the missing contract.
- [ ] Never promote an action to demonstrated from static text, advertisement, HTTP success, or a non-terminal response alone.
