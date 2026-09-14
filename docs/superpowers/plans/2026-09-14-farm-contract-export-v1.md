# Farm Contract Export V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export a stable per-game `farm-contract.json` after discovery, with a candidate artifact for partial runs and a complete provider-specific BGaming contract when discovery is fully resolved.

**Architecture:** Add one provider-neutral contract exporter that owns schema validation, secret/ephemeral-data rejection, candidate writing, and promotion. `ProviderAdapter` exposes small build/validate hooks; unsupported providers return an explicit non-ready candidate without changing their existing discovery flow. BGaming fills its own `protocol` block from final `GameTestResult` evidence and stable persisted metadata, while generic code never interprets BGaming protocol fields.

**Tech Stack:** Python 3.12, stdlib `json`/`pathlib`, existing `ProviderAdapter`, existing BGaming result/evidence structures, unittest + pytest CI.

**Spec:** `docs/superpowers/specs/2026-09-14-discovery-contract-design.md`

## Global Constraints

- Schema is exactly `tester-spin/farm-contract/v1`.
- Discovery remains authoritative; contract export never changes `OK/PARCIAL/ERROR/SIN_DEMO`.
- A partial/invalid candidate never overwrites an existing published `farm-contract.json`.
- Persist no cookies, session tokens, CSRF values, authorization values, credentials, round IDs, signed temporary URLs, or equivalent secrets.
- Generic core never branches on provider name.
- No UI analysis, HAR analysis, browser discovery, or new protocol inference is added by this feature.
- BGaming is the only provider with a complete provider-specific protocol block in V1; other providers remain explicit `PROVIDER_CONTRACT_UNSUPPORTED` candidates until adapted.

---

### Task 1: Common contract envelope and safe persistence

**Files:**
- Create: `tester_spin/farm_contract.py`
- Test: `tests/test_farm_contract.py`

**Interfaces:**
- Produces: `SCHEMA = "tester-spin/farm-contract/v1"`
- Produces: `validate_common_contract(contract: dict[str, Any]) -> list[str]`
- Produces: `contains_forbidden_runtime_data(value: Any, path: str = "$") -> list[str]`
- Produces: `write_contract_candidate(game_dir: Path, contract: dict[str, Any]) -> Path`
- Produces: `promote_contract_if_ready(game_dir: Path, contract: dict[str, Any]) -> bool`

- [ ] **Step 1: Write failing envelope tests**

Add tests proving: valid envelope passes; wrong schema fails; non-empty `unresolved` prevents readiness; required non-`DEMOSTRADO` modes prevent readiness; forbidden keys such as `csrf_token`, `session_id`, `authorization`, `cookie`, `round_id`, `launch_token` are detected recursively; a non-ready candidate is written to `analysis/farm-contract-candidate.json` but does not overwrite an existing `farm-contract.json`.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:
```bash
python -m unittest tests.test_farm_contract -v
```
Expected: import/function failures because `tester_spin.farm_contract` does not yet exist.

- [ ] **Step 3: Implement the minimal common module**

Use deterministic JSON (`ensure_ascii=False`, `indent=2`, `sort_keys=True`) and atomic replacement via a sibling temporary file. Validation must require the common top-level fields from the spec and append explicit error codes such as `INVALID_SCHEMA`, `UNRESOLVED_ITEMS`, `REQUIRED_MODE_NOT_DEMONSTRATED`, and `FORBIDDEN_RUNTIME_DATA:<path>`.

Promotion logic:
```python
errors = validate_common_contract(contract)
ready = bool(contract.get("ready")) and not errors
write_contract_candidate(game_dir, contract)
if not ready:
    return False
atomic_write published farm-contract.json
return True
```

- [ ] **Step 4: Re-run focused tests**

Run:
```bash
python -m unittest tests.test_farm_contract -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tester_spin/farm_contract.py tests/test_farm_contract.py
git commit -m "feat: add common farm contract envelope"
```

---

### Task 2: Provider hooks and exporter integration

**Files:**
- Modify: `tester_spin/providers/base.py`
- Modify: `tester_spin/scheduler.py`
- Test: `tests/test_farm_contract.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- `ProviderAdapter.farm_contract_dir(game: Game) -> Path | None`
- `ProviderAdapter.build_farm_contract(game: Game, result: GameTestResult) -> dict[str, Any]`
- `ProviderAdapter.validate_farm_contract(contract: dict[str, Any]) -> list[str]`
- `export_farm_contract(provider: ProviderAdapter, game: Game, result: GameTestResult, progress: Progress) -> None`

- [ ] **Step 1: Write failing provider/export tests**

Create a synthetic provider with a temporary `farm_contract_dir`. Assert that the scheduler calls export only after `finalize_test_result()`. Assert unsupported provider output is a candidate with `ready=false` and `unresolved=["PROVIDER_CONTRACT_UNSUPPORTED"]`. Assert export exceptions are logged but do not mutate `GameTestResult.status`.

- [ ] **Step 2: Run focused tests and confirm RED**

Run:
```bash
python -m unittest tests.test_farm_contract tests.test_scheduler -v
```
Expected: missing hooks/export integration.

- [ ] **Step 3: Implement neutral default hooks**

Base defaults:
```python
def farm_contract_dir(self, game):
    return None

def build_farm_contract(self, game, result):
    return {
        "schema": SCHEMA,
        "provider": result.provider,
        "game": {"slug": result.slug, "name": result.game_name, "symbol": result.symbol},
        "ready": False,
        "source": {"run": result.finished_at, "protocol_family": "unsupported"},
        "bootstrap": {},
        "modes": [],
        "continuations": {"known": [], "unresolved": []},
        "terminal_contract": {},
        "protocol": {},
        "unresolved": ["PROVIDER_CONTRACT_UNSUPPORTED"],
    }

def validate_farm_contract(self, contract):
    return []
```

`export_farm_contract()` must: ask provider for output dir; skip silently if `None`; build candidate; merge common + provider validation errors into `unresolved`; force `ready=false` when errors exist; write candidate; promote only when ready; log one concise line; never change result status.

- [ ] **Step 4: Integrate after finalization**

In scheduler worker:
```python
result = provider.finalize_test_result(result, progress=game_progress)
export_farm_contract(provider, game, result, progress=game_progress)
return result
```

- [ ] **Step 5: Re-run focused tests**

Run:
```bash
python -m unittest tests.test_farm_contract tests.test_scheduler -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tester_spin/providers/base.py tester_spin/scheduler.py tester_spin/farm_contract.py tests/test_farm_contract.py tests/test_scheduler.py
git commit -m "feat: export farm contracts after finalized discovery"
```

---

### Task 3: BGaming stable contract builder

**Files:**
- Create: `tester_spin/providers/bgaming/farm_contract.py`
- Modify: `tester_spin/providers/bgaming_paths_v2.py`
- Test: `tests/test_bgaming_farm_contract.py`

**Interfaces:**
- Produces: `build_bgaming_farm_contract(game: Game, result: GameTestResult, game_dir: Path) -> dict[str, Any]`
- Produces: `validate_bgaming_farm_contract(contract: dict[str, Any]) -> list[str]`
- BGaming provider overrides the three neutral hooks using `self.game_dir(game)`.

- [ ] **Step 1: Write failing BGaming contract tests**

Synthetic `GameTestResult` cases:
1. `OK` with `SPIN` and purchase modes annotated `DEMOSTRADO` produces `ready=true`.
2. `SOLO_ANUNCIADO`, `CANDIDATO_WIRE`, or `NO_VALIDADO` required modes produce `ready=false` with exact unresolved reason.
3. Stable mode selectors/options are preserved, but secret-like keys are excluded/rejected.
4. `protocol.family` comes from stable persisted profile/game metadata when available; otherwise contract remains non-ready rather than guessing.
5. Known continuation/choice modes with complete coverage are represented; uncovered/unknown continuation keeps non-ready.

- [ ] **Step 2: Run focused tests and confirm RED**

Run:
```bash
python -m unittest tests.test_bgaming_farm_contract -v
```
Expected: module/functions missing.

- [ ] **Step 3: Implement BGaming builder**

Builder reads only final result plus stable files already under the game's folder (`game.json`, final run `profile.json`, coverage artifacts when present). It must never import or invoke HAR capture/analysis, browser code, or discovery functions.

Required BGaming `protocol` V1 shape:
```json
{
  "family": "api-v2",
  "identifier": "...",
  "modes": [
    {
      "id": "SPIN",
      "kind": "SPIN",
      "executor": "spin",
      "options": {},
      "evidence": "DEMOSTRADO"
    }
  ],
  "continuations": [
    {"command": "freespin", "coverage": "complete"}
  ]
}
```

Do not persist runtime endpoint/token/CSRF/round IDs. `bootstrap` contains only stable public game URL, stable identifier, and a named strategy such as `bgaming-api-v2`/`bgaming-hyperhive` when family is known.

- [ ] **Step 4: Override BGaming hooks**

In active `BGamingProvider` (`bgaming_paths_v2.py`):
```python
def farm_contract_dir(self, game):
    return self.game_dir(game)

def build_farm_contract(self, game, result):
    return build_bgaming_farm_contract(game, result, self.game_dir(game))

def validate_farm_contract(self, contract):
    return validate_bgaming_farm_contract(contract)
```

- [ ] **Step 5: Re-run BGaming + common tests**

Run:
```bash
python -m unittest tests.test_bgaming_farm_contract tests.test_farm_contract tests.test_bgaming_paths_v2_regressions -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tester_spin/providers/bgaming/farm_contract.py tester_spin/providers/bgaming_paths_v2.py tests/test_bgaming_farm_contract.py
git commit -m "feat: export BGaming farm contract"
```

---

### Task 4: Full regression and promotion semantics

**Files:**
- Modify only as required by failing regressions from Tasks 1-3.
- Test: existing full suite.

**Interfaces:** No new interface; this task verifies the feature does not alter current provider behavior.

- [ ] **Step 1: Run compile**

```bash
python -m compileall -q tester_spin tests run.py
```
Expected: exit 0.

- [ ] **Step 2: Run full unittest suite**

```bash
python -m unittest discover -s tests -v
```
Expected: all tests pass.

- [ ] **Step 3: Run pytest/integration suite**

```bash
python -m pytest -q
```
Expected: all tests pass.

- [ ] **Step 4: Verify diff scope**

Confirm no provider protocol other than BGaming changed and no GUI/HAR/browser discovery logic was added.

- [ ] **Step 5: Commit any regression-only fixes**

```bash
git add <only files required by failing regressions>
git commit -m "test: verify farm contract export regressions"
```

## Acceptance Check

A finalized BGaming discovery writes `analysis/farm-contract-candidate.json` every time contract export is supported. A fully resolved BGaming result additionally publishes `farm-contract.json`. Partial discovery preserves an older published contract untouched. The published/candidate files contain no ephemeral authentication/session values. Other providers retain existing discovery behavior and do not gain invented protocol knowledge.
