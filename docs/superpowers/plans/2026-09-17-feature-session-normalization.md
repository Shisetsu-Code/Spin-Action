# Feature Session Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Every behavior change follows TDD.

**Goal:** Normalize purchased and natural multi-round features across all active providers, count logical feature rounds independently from network steps, preserve picker graphs, and prevent purchase/farm promotion while any observed feature session remains incomplete.

**Architecture:** Keep all provider wire executors isolated. Add one provider-neutral feature-session contract/gate plus provider-local artifact normalizers. Pragmatic, Red Tiger and BGaming keep their existing branch replay engines; RubyPlay, D1 and Belatra export equivalent session evidence without guessing unresolved domains.

**Tech Stack:** Python 3.12, existing provider adapters, `unittest`/`pytest`, JSON artifacts, GitHub Actions when hosted runners are available.

**Spec:** `docs/superpowers/specs/2026-09-17-feature-session-normalization-design.md`

## Global constraints

- Work only on `lab/actions-validation`.
- Do not weaken existing provider terminal checks or `path_coverage.py`.
- Do not create a cross-provider wire serializer.
- Do not use OCR/canvas/UI discovery to resolve picker domains.
- Unknown/unproven picker domains remain incomplete.
- Logical rounds are provider-evidenced outcomes, not request counts.
- Purchase campaign must finalize feature/path evidence before purchase coverage promotion.
- GitHub-hosted Actions are currently failing before runner assignment; do not interpret those infrastructure failures as test failures. Re-run full CI when runners are restored.

---

### Task 1: Common feature-session schema, persistence and gate

**Files:**
- Create: `tester_spin/feature_sessions.py`
- Modify: `tester_spin/providers/base.py`
- Test: `tests/test_feature_sessions.py`

**Interfaces:**
- `make_feature_round(...)`
- `make_feature_choice(...)`
- `make_feature_session(...)`
- `finalize_feature_session_report(result, *, sessions, authority)`
- `attach_feature_session_report(result, report)`
- `feature_session_state_for_mode(result, mode_id)`
- `enforce_complete_feature_sessions(result, report, *, progress=None)`
- `ProviderAdapter.build_feature_sessions(result)` defaults to an empty report.
- `ProviderAdapter.finalize_purchase_result(result, progress=...)` runs feature-session and path gates without natural sampling.

- [ ] Write RED tests for complete, incomplete and unknown sessions.
- [ ] Write RED test that one incomplete session downgrades an otherwise `OK` result to `PARCIAL`.
- [ ] Write RED test that parent-mode summary distinguishes two purchase modes.
- [ ] Implement minimal neutral schema and JSON persistence.
- [ ] Integrate normal finalization and purchase-only finalization.
- [ ] Verify focused tests GREEN.

### Task 2: Make purchase coverage respect feature-session state

**Files:**
- Modify: `tester_spin/purchase_coverage.py`
- Modify: `scripts/purchase_campaign.py`
- Test: `tests/test_purchase_coverage.py`
- Test: `tests/test_purchase_campaign.py`

- [ ] RED: purchase option with exact terminal root request plus `INCOMPLETE` feature session must be `PURCHASE_UNKNOWN`.
- [ ] RED: complete feature session preserves `PURCHASE_COMPLETE`.
- [ ] RED: purchase campaign invokes `finalize_purchase_result` before `build_purchase_coverage` but never the general sampling finalizer.
- [ ] Add common downgrade of otherwise-complete purchase options by matching `purchase_id`/parent mode.
- [ ] Call purchase-only finalizer in campaign after runtime execution and before coverage classification.
- [ ] Verify focused tests GREEN.

### Task 3: Pragmatic normalizer and purchase-path exhaustive FSO

**Files:**
- Create: `tester_spin/providers/pragmatic_feature_sessions.py`
- Modify: `tester_spin/providers/pragmatic_farm_adapter.py`
- Test: `tests/test_pragmatic_feature_sessions.py`
- Modify: `tests/test_pragmatic_purchase_execution.py`

**Rules:**
- Repeated feature-active `doSpin` continuations are logical rounds.
- `doFSOption` is a choice; its finite domain comes from `fso-selection-*.json`.
- `doBonus`, `doCollectBonus`, `doCollect`, `doMysteryScatter` are transitions unless independent outcome evidence proves otherwise.
- Purchase campaign must use the exhaustive Pragmatic wrapper so FSO siblings are replayed.

- [ ] RED synthetic artifact with ten continuation spins => ten feature rounds, regardless of additional transition requests.
- [ ] RED FSO branch with missing sibling => session incomplete.
- [ ] RED fully covered prefix-sensitive FSO => session complete.
- [ ] Change `test_purchase_paths` to use the exhaustive provider path once per mode instead of bypassing it.
- [ ] Verify existing FSO traversal tests remain valid.

### Task 4: Red Tiger normalizer

**Files:**
- Create: `tester_spin/providers/redtiger/feature_sessions.py`
- Modify: `tester_spin/providers/redtiger/farm_adapter.py`
- Test: `tests/test_redtiger_feature_sessions.py`

**Rules:**
- Use `result_tree` nodes as response-tree logical-round evidence.
- Use `choice_continuations` and final `CHOICE_BRANCH` modes for picker evidence.
- Provider `game.choices.available` remains authoritative domain.
- Branch replay remains in `branch_coverage.py`.

- [ ] RED response containing multiple result nodes maps to multiple logical rounds while `wire_steps` stays independent.
- [ ] RED uncovered choice sibling => incomplete session.
- [ ] RED closed recursive choice graph => complete session.
- [ ] Wire provider hook and verify no executor behavior changes.

### Task 5: BGaming normalizer

**Files:**
- Create: `tester_spin/providers/bgaming/feature_sessions.py`
- Modify: `tester_spin/providers/bgaming_farm_adapter.py`
- Test: `tests/test_bgaming_feature_sessions.py`

**Rules:**
- Parse ordered `step-NNN-request/response/proof` artifacts.
- Proven `freespin`/`respin` continuation requests are logical rounds.
- Known setup/state commands remain transitions.
- Unknown continuation semantics inside an observed feature make `round_classification_complete=false`.
- Choice domains/paths come from `BGAMING_FLOW_CHOICE_*` discovered modes and `flow-choice-coverage.json`.

- [ ] RED feature with ten `freespin` steps => ten logical rounds.
- [ ] RED unknown continuation command => incomplete/unknown classification, not silently counted.
- [ ] RED missing flow-choice branch => incomplete.
- [ ] Verify existing BGaming exhaustive branch tests unaffected.

### Task 6: RubyPlay normalizer and parent-scoped picker audit

**Files:**
- Create: `tester_spin/providers/rubyplay/feature_sessions.py`
- Modify: `tester_spin/providers/rubyplay/exhaustive.py`
- Modify: `tester_spin/providers/rubyplay/farm_adapter.py`
- Modify: `tester_spin/providers/rubyplay/purchase_coverage.py`
- Test: `tests/test_rubyplay_feature_sessions.py`
- Modify: `tests/test_exhaustive_provider_paths.py`
- Modify: `tests/test_rubyplay_purchase_branch_coverage.py`

**Rules:**
- `freespin`, `respin`, `minispin` are logical rounds.
- `select`, `pick` are choices.
- Return to `next_action=spin` proves terminal.
- Picker evidence must be scoped by parent mode/attempt.
- Current finite domain stays unresolved unless provider/client evidence proves it.

- [ ] RED purchase with 12 round continuations => 12 rounds.
- [ ] RED natural spin entering a 15-step feature => `trigger=NATURAL` session.
- [ ] RED two purchase modes with select/pick in only one: unresolved domain must not contaminate the other mode.
- [ ] Refactor path audit to emit parent-scoped `INDEXED_CHOICE` records.
- [ ] Update purchase coverage to consume parent-scoped branch evidence.
- [ ] Verify unresolved domains still block completion.

### Task 7: D1 / 1spin4win normalizer

**Files:**
- Create: `tester_spin/providers/one_spin4win_feature_sessions.py`
- Modify: `tester_spin/providers/one_spin4win_farm_adapter.py`
- Test: `tests/test_one_spin4win_feature_sessions.py`

**Rules:**
- Pair each sent type=1 play with the next received type=3 result.
- First pair is root spin; subsequent pairs while previous result state is active `{5,6,11,12}` are feature rounds.
- Known/proven terminal result ends the session.
- Unknown type=3 state remains fail-closed.

- [ ] RED WS artifact with root + ten continuation pairs => ten feature rounds.
- [ ] RED unknown intermediate/final state => incomplete.
- [ ] RED known terminal state => complete.
- [ ] Preserve existing D1 state audit behavior.

### Task 8: Belatra normalizer

**Files:**
- Create: `tester_spin/providers/belatra_feature_sessions.py`
- Modify: `tester_spin/providers/belatra_farm_adapter.py`
- Test: `tests/test_belatra_feature_sessions.py`

**Rules:**
- Known `start -> finish -> toIdle` base path is not fabricated into a feature.
- Non-terminal provider phases are preserved as incomplete sessions/state flows.
- `toDoubleDialog` maps to choice coverage using `BELATRA_DOUBLE_DIALOG`.
- `buyBonus` remains unresolved until exact wire contract exists.

- [ ] RED toDoubleDialog with only DECLINE covered => incomplete choice session.
- [ ] RED unknown/non-terminal phase => incomplete, raw evidence referenced.
- [ ] Base terminal spin with no feature => no feature session.

### Task 9: Farm contract feature-session surface

**Files:**
- Modify: `tester_spin/providers/farm_structure.py`
- Modify: `tester_spin/providers/result_farm_contract.py`
- Test: `tests/test_farm_contract_structure.py`

- [ ] RED execution structure exposes normalized `feature_sessions` summary.
- [ ] RED unresolved feature session contributes a farm unresolved reason and prevents `ready=true`.
- [ ] Preserve provider-local protocol blocks unchanged.
- [ ] Verify current wager/choice structure tests remain GREEN.

### Task 10: Cross-provider regression and verification

**Files:**
- Modify: `tests/test_exhaustive_provider_paths.py`
- Modify/add focused provider tests as above.

- [ ] Build one synthetic cross-provider matrix asserting that every provider can represent: no feature, natural feature, purchased feature, unresolved choice, closed choice.
- [ ] Run focused feature-session tests.
- [ ] Run purchase coverage/campaign tests.
- [ ] Run exhaustive path/farm tests.
- [ ] Run full test suite when an executable runner is available.
- [ ] Inspect fresh GitHub Actions job metadata. If `runner_id=0` and no steps again, report infrastructure blocker separately and do not claim CI success.
- [ ] Run one low-traffic live validation per available provider after runners return, then a RubyPlay official-catalog purchase campaign with HAR disabled.

## Completion definition

The objective is reached when:

- all six provider adapters emit the common feature-session report from their existing evidence;
- purchased/natural multi-round sessions expose logical round counts independently of wire steps;
- Pragmatic/Red Tiger/BGaming pickers remain exhaustively replayed from authoritative domains;
- RubyPlay unknown index domains stay scoped and fail closed rather than poisoning unrelated modes;
- D1 bonus continuations are counted as rounds;
- Belatra unresolved buy/gamble semantics remain explicit;
- purchase coverage cannot close while its observed feature session is incomplete;
- farm contracts cannot promote unresolved feature sessions;
- focused deterministic tests pass and full CI/live validation is rerun once runner provisioning is available.
