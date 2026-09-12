from __future__ import annotations

import itertools
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult, SpinAttempt
from tester_spin.providers.belatra import BelatraProvider as _BelatraProvider
from tester_spin.providers.base import Progress


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_error(result: GameTestResult, message: str) -> None:
    clean = str(message or "").strip()
    if not clean or clean in str(result.error or ""):
        return
    result.error = (str(result.error or "").strip() + " " + clean).strip()


def _read_enter(result: GameTestResult) -> dict[str, Any]:
    root = Path(str(result.run_dir or ""))
    path = root / "bootstrap" / "enter.response.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _math_type_domain(gs: dict[str, Any]) -> list[int]:
    values: list[int] = []
    current = gs.get("mathType")
    if isinstance(current, (bool, int, float)):
        values.append(int(current))
    analytical = gs.get("analInfo")
    if isinstance(analytical, dict):
        for key, value in analytical.items():
            if not re.fullmatch(r"mathType[A-Za-z0-9_]+", str(key)):
                continue
            if isinstance(value, (bool, int, float)):
                number = int(value)
                if number not in values:
                    values.append(number)
    return values if len(values) >= 2 else []


def _selector_dimensions(gs: dict[str, Any]) -> list[tuple[str, list[int]]]:
    dimensions: list[tuple[str, list[int]]] = []

    vip = gs.get("vipMode")
    if isinstance(vip, dict):
        try:
            vip_multiplier = float(vip.get("vipBetK") or 0)
        except (TypeError, ValueError):
            vip_multiplier = 0.0
        if vip_multiplier > 1:
            dimensions.append(("vipOn", [0, 1]))

    for key, value in gs.items():
        name = str(key)
        if not re.fullmatch(r"isMath[A-Za-z0-9_]*", name):
            continue
        if isinstance(value, bool) or (
            isinstance(value, (int, float)) and int(value) in {0, 1}
        ):
            dimensions.append((name, [0, 1]))

    math_types = _math_type_domain(gs)
    if math_types:
        dimensions.append(("mathType", math_types))

    deduped: list[tuple[str, list[int]]] = []
    seen: set[str] = set()
    for name, values in dimensions:
        if name in seen:
            continue
        seen.add(name)
        deduped.append((name, list(dict.fromkeys(values))))
    return deduped


def _buy_bonus_options(gs: dict[str, Any]) -> list[str]:
    buy = gs.get("buyBonus")
    if not isinstance(buy, dict):
        return []
    raw = buy.get("buyTotalBetK")
    options: list[str] = []
    if isinstance(raw, dict):
        for key in raw:
            clean = str(key).strip()
            if clean and clean not in options:
                options.append(clean)
    elif isinstance(raw, list):
        for index, item in enumerate(raw):
            if isinstance(item, dict):
                candidate = item.get("id")
                if candidate in (None, ""):
                    candidate = item.get("prefix2")
                if candidate in (None, ""):
                    candidate = index
            else:
                candidate = index
            clean = str(candidate).strip()
            if clean and clean not in options:
                options.append(clean)
    return options


def _combo_label(combo: dict[str, int]) -> str:
    if not combo:
        return "BASE"
    return "|".join(f"{key}={combo[key]}" for key in sorted(combo))


def _selector_matrix(
    dimensions: list[tuple[str, list[int]]],
) -> list[dict[str, int]]:
    if not dimensions:
        return [{}]
    names = [name for name, _values in dimensions]
    domains = [values for _name, values in dimensions]
    return [dict(zip(names, values)) for values in itertools.product(*domains)]


def _base_combo(provider: _BelatraProvider, enter: dict[str, Any], dimensions) -> dict[str, int]:
    request = provider._base_spin_request(enter)
    return {
        name: int(request[name])
        for name, _values in dimensions
        if isinstance(request.get(name), (bool, int, float))
    }


def _close_state(state: dict[str, Any] | None) -> None:
    if not isinstance(state, dict):
        return
    session = state.get("session")
    try:
        if session is not None:
            session.close()
    except Exception:
        pass


def _execute_selector_variant(
    provider: _BelatraProvider,
    game: Game,
    *,
    combo: dict[str, int],
    timeout_s: float,
    artifact_dir: Path,
) -> tuple[bool, int, str, str, bool]:
    state: dict[str, Any] | None = None
    started = time.monotonic()
    try:
        state = provider._open_direct_game(
            game,
            timeout_s=timeout_s,
            run_dir=artifact_dir / "bootstrap",
        )
        request = provider._base_spin_request(state["enter"])
        for key, value in combo.items():
            request[key] = int(value)
        start = provider._post_direct_game(
            state,
            request,
            timeout_s=timeout_s,
            artifact_dir=artifact_dir,
            label="start",
        )
        gs = start.get("gs")
        if not isinstance(gs, dict):
            raise RuntimeError("Belatra selector variant: start sin gs.")
        phase_cur = str(gs.get("phaseCur") or "")
        phase_next = str(gs.get("phaseNext") or "")
        history_id = gs.get("historyId")
        saw_double_dialog = phase_next == "toDoubleDialog"
        if history_id is None:
            raise RuntimeError("Belatra selector variant: start sin historyId.")
        if phase_next not in {"toPaid", "toDoubleDialog"}:
            return False, 1, phase_cur, phase_next, saw_double_dialog
        finish = provider._post_direct_game(
            state,
            {"q": "finish", "ghistId": history_id},
            timeout_s=timeout_s,
            artifact_dir=artifact_dir,
            label="finish",
        )
        finish_gs = finish.get("gs")
        if not isinstance(finish_gs, dict):
            raise RuntimeError("Belatra selector variant: finish sin gs.")
        final_cur = str(finish_gs.get("phaseCur") or "")
        final_next = str(finish_gs.get("phaseNext") or "")
        terminal = final_cur == "finished" and final_next == "toIdle"
        return terminal, 2, final_cur, final_next, saw_double_dialog
    finally:
        _close_state(state)
        _write_json(
            artifact_dir / "variant.json",
            {
                "selectors": combo,
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            },
        )


def _base_saw_double_dialog(result: GameTestResult) -> bool:
    for attempt in result.attempts:
        if not attempt.artifact_dir:
            continue
        path = Path(attempt.artifact_dir) / "start.response.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        gs = payload.get("gs") if isinstance(payload, dict) else None
        if isinstance(gs, dict) and str(gs.get("phaseNext") or "") == "toDoubleDialog":
            return True
    return False


def expand_belatra_paths(
    provider: _BelatraProvider,
    game: Game,
    result: GameTestResult,
    *,
    repetitions: int,
    timeout_s: float,
    stop_event: threading.Event,
    progress: Progress,
) -> GameTestResult:
    if result.status in {"ERROR", "CANCELADO"} or not result.run_dir:
        return result

    enter = _read_enter(result)
    gs = enter.get("gs") if isinstance(enter, dict) else None
    if not isinstance(gs, dict):
        return result

    run_root = Path(result.run_dir)
    dimensions = _selector_dimensions(gs)
    matrix = _selector_matrix(dimensions)
    base_combo = _base_combo(provider, enter, dimensions)
    base_label = _combo_label(base_combo)
    required = [_combo_label(combo) for combo in matrix]
    covered: set[str] = set()

    base_ok = bool(result.attempts) and all(
        attempt.ok and attempt.terminal for attempt in result.attempts
        if attempt.mode_id == "SPIN"
    )
    if base_ok and base_label in required:
        covered.add(base_label)

    attempt_number = max((attempt.number for attempt in result.attempts), default=0)
    added_requested = 0
    added_successes = 0
    saw_double = _base_saw_double_dialog(result)

    for combo in matrix:
        if stop_event.is_set():
            break
        label = _combo_label(combo)
        if label == base_label:
            continue
        variant_all_ok = True
        for repetition in range(1, max(1, int(repetitions)) + 1):
            if stop_event.is_set():
                variant_all_ok = False
                break
            attempt_number += 1
            added_requested += 1
            attempt_dir = (
                run_root
                / "branch-coverage"
                / "belatra-start-selectors"
                / label.replace("|", "__").replace("=", "-")
                / f"attempt-{repetition:04d}"
            )
            started = time.monotonic()
            try:
                terminal, steps, phase_cur, phase_next, branch_double = _execute_selector_variant(
                    provider,
                    game,
                    combo=combo,
                    timeout_s=timeout_s,
                    artifact_dir=attempt_dir,
                )
                saw_double = saw_double or branch_double
                ok = bool(terminal)
                variant_all_ok = variant_all_ok and ok
                added_successes += int(ok)
                attempt = SpinAttempt(
                    number=attempt_number,
                    ok=ok,
                    mode_id=f"SPIN__{label}",
                    mode_kind="SPIN_VARIANT",
                    elapsed_ms=(time.monotonic() - started) * 1000.0,
                    symbol=result.symbol,
                    endpoint="/game",
                    na=phase_next,
                    terminal=terminal,
                    wire_steps=steps,
                    warning="" if ok else f"Belatra selector variant no terminal: {phase_cur}/{phase_next}",
                    artifact_dir=str(attempt_dir),
                )
            except Exception as exc:
                variant_all_ok = False
                attempt = SpinAttempt(
                    number=attempt_number,
                    ok=False,
                    mode_id=f"SPIN__{label}",
                    mode_kind="SPIN_VARIANT",
                    elapsed_ms=(time.monotonic() - started) * 1000.0,
                    symbol=result.symbol,
                    endpoint="/game",
                    terminal=False,
                    error=f"{type(exc).__name__}: {exc}",
                    artifact_dir=str(attempt_dir),
                )
            result.attempts.append(attempt)
            progress(
                f"[{game.name}] Belatra variante {label} {repetition}/{max(1, int(repetitions))}: "
                f"{'OK' if attempt.ok else 'PARCIAL'}"
            )
        if variant_all_ok:
            covered.add(label)

    if dimensions:
        result.discovered_modes.append(
            {
                "id": "BELATRA_START_SELECTOR_MATRIX",
                "kind": "CHOICE_BRANCH",
                "observed": True,
                "executable": True,
                "wire_command": "start",
                "coverage_required": True,
                "branch_signature": "BELATRA:start-selector-matrix",
                "dimensions": [
                    {"field": name, "values": values}
                    for name, values in dimensions
                ],
                "required_options": required,
                "covered_options": sorted(covered),
            }
        )

    buy_options = _buy_bonus_options(gs)
    if buy_options:
        result.discovered_modes.append(
            {
                "id": "BELATRA_BUY_BONUS",
                "kind": "PURCHASE_BRANCH",
                "observed": True,
                "executable": False,
                "coverage_required": True,
                "branch_signature": "BELATRA:buyBonus.buyTotalBetK",
                "required_options": buy_options,
                "covered_options": [],
                "reason": "buyTotalBetK está anunciado, pero no existe HAR de compra con wire contract autoritativo",
            }
        )

    if saw_double:
        result.discovered_modes.append(
            {
                "id": "BELATRA_DOUBLE_DIALOG",
                "kind": "CHOICE_BRANCH",
                "observed": True,
                "executable": False,
                "coverage_required": True,
                "branch_signature": "BELATRA:toDoubleDialog",
                "required_options": ["DECLINE", "GAMBLE"],
                "covered_options": ["DECLINE"],
                "reason": "finish/decline está HAR-confirmado; gamble todavía no tiene wire contract demostrado",
            }
        )

    result.requested_spins += added_requested
    result.successful_spins += added_successes
    result.failed_spins = max(0, result.requested_spins - result.successful_spins)
    missing_selector = [option for option in required if option not in covered]
    unresolved = []
    if missing_selector:
        unresolved.append("selectores start: " + ", ".join(missing_selector))
    if buy_options:
        unresolved.append("buy bonus: " + ", ".join(buy_options))
    if saw_double:
        unresolved.append("gamble de toDoubleDialog")
    if unresolved and result.status == "OK":
        result.status = "PARCIAL"
    if unresolved:
        _append_error(result, "Belatra cobertura exhaustiva pendiente: " + "; ".join(unresolved) + ".")

    _write_json(run_root / "result.json", result.to_dict())
    return result


class BelatraProvider(_BelatraProvider):
    """Belatra with HAR-grounded exhaustive selector coverage."""

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        result = super().test_game(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        return expand_belatra_paths(
            self,
            game,
            result,
            repetitions=max(1, int(spins)),
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )


__all__ = ["BelatraProvider", "expand_belatra_paths"]
