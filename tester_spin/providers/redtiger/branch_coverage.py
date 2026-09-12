from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult, SpinAttempt
from tester_spin.providers.base import Progress
from tester_spin.providers.redtiger.evolution_launch import bootstrap_game as fresh_bootstrap
from tester_spin.providers.redtiger.execution import _merge_modes, _mode_id, _post_choice, _post_spin
from tester_spin.providers.redtiger.runtime import (
    ChoicePrompt,
    FeatureBuy,
    RedTigerRuntime,
    pending_choice_from_response,
    response_summary,
    sanitize_payload,
)


MAX_CHOICE_DEPTH = 8
MAX_BRANCH_PREFIXES_PER_MODE = 128


@dataclass(slots=True)
class ReplayOutcome:
    prompt: ChoicePrompt | None
    final_payload: dict[str, Any]
    summaries: list[dict[str, Any]]
    warnings: list[str]
    wire_steps: int
    status_code: int
    selected: tuple[str, ...]
    elapsed_ms: float
    artifact_dir: Path

    @property
    def terminal(self) -> bool:
        return bool(response_summary(self.final_payload).get("success")) and self.prompt is None


@dataclass(slots=True)
class BranchPoint:
    mode_id: str
    prefix: tuple[str, ...]
    required: list[str]
    covered: set[str]

    @property
    def signature(self) -> str:
        prefix = "/".join(self.prefix) if self.prefix else "ROOT"
        return f"{self.mode_id}:{prefix}"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _safe_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _base_mode_specs(result: GameTestResult) -> dict[str, tuple[str, FeatureBuy | None]]:
    specs: dict[str, tuple[str, FeatureBuy | None]] = {"SPIN": ("SPIN", None)}
    for item in result.discovered_modes:
        if not isinstance(item, dict):
            continue
        mode_id = str(item.get("id") or "").strip()
        if not mode_id or str(item.get("kind") or "").upper() != "PURCHASE":
            continue
        name = str(item.get("feature_buy") or "").strip()
        multiplier = _safe_decimal(item.get("feature_multiplier"))
        if name and multiplier is not None:
            specs[mode_id] = ("PURCHASE", FeatureBuy(name=name, multiplier=multiplier))
    return specs


def _choice_parent_modes(result: GameTestResult) -> set[str]:
    parents: set[str] = set()
    for item in result.discovered_modes:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").upper() != "CHOICE_CONTINUATION":
            continue
        parent = str(item.get("parent") or "").strip()
        if parent:
            parents.add(parent)

    root = Path(str(result.run_dir or ""))
    if root.is_dir():
        for path in root.glob("*/attempt-*/response.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if pending_choice_from_response(payload) is not None:
                parents.add(path.relative_to(root).parts[0])
    return parents


def _path_label(prefix: tuple[str, ...]) -> str:
    if not prefix:
        return "ROOT"
    return "__".join(_mode_id(value) for value in prefix)


def _branch_mode_id(base_mode: str, prefix: tuple[str, ...]) -> str:
    if not prefix:
        return base_mode
    return base_mode + "__" + "__".join(f"CHOICE_{_mode_id(value)}" for value in prefix)


def _replay_prefix(
    provider,
    game: Game,
    *,
    launch_id: str,
    base_mode: str,
    feature: FeatureBuy | None,
    repetition: int,
    prefix: tuple[str, ...],
    timeout_s: float,
    stop_event,
    run_root: Path,
) -> ReplayOutcome:
    if stop_event.is_set():
        raise InterruptedError("Detención solicitada durante expansión de ramas Red Tiger.")

    branch_root = (
        run_root
        / base_mode
        / "branch-coverage"
        / f"rep-{repetition:05d}"
        / f"path-{_path_label(prefix)}"
    )
    branch_root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    runtime: RedTigerRuntime | None = None

    try:
        runtime = fresh_bootstrap(
            game.url,
            launch_id,
            timeout_s=max(15.0, float(timeout_s)),
            artifact_dir=branch_root / "bootstrap",
            endpoints=provider.bootstrap_endpoints,
            progress=None,
            stop_event=stop_event,
        )
        status_code, entry_request, final_payload, warnings = _post_spin(
            runtime,
            stake=runtime.default_stake,
            feature_buy=feature,
            timeout_s=timeout_s,
            stop_event=stop_event,
        )
        summaries = [response_summary(final_payload)]
        _write_json(branch_root / "entry-request.json", sanitize_payload(entry_request))
        _write_json(branch_root / "entry-response.json", sanitize_payload(final_payload))
        wire_steps = 1
        consumed: list[str] = []

        for depth, selected in enumerate(prefix, start=1):
            prompt = pending_choice_from_response(final_payload)
            if prompt is None:
                raise RuntimeError(
                    f"{base_mode}: la ruta {list(prefix)!r} esperaba selector en profundidad {depth}, "
                    "pero la ronda terminó antes."
                )
            if selected not in prompt.available:
                raise RuntimeError(
                    f"{base_mode}: choice {selected!r} ya no está entre {list(prompt.available)!r}."
                )
            choice_status, endpoint, request, response, choice_warnings = _post_choice(
                runtime,
                prompt=prompt,
                choice=selected,
                timeout_s=timeout_s,
                stop_event=stop_event,
            )
            status_code = choice_status
            warnings.extend(choice_warnings)
            wire_steps += 1
            consumed.append(selected)
            summary = response_summary(response)
            summaries.append(summary)
            choice_root = branch_root / f"choice-{depth:02d}-{_mode_id(selected)}"
            _write_json(choice_root / "request.json", sanitize_payload(request))
            _write_json(choice_root / "response.json", sanitize_payload(response))
            _write_json(choice_root / "summary.json", summary)
            _write_json(
                choice_root / "transition.json",
                {
                    "round_id": prompt.round_id,
                    "available": list(prompt.available),
                    "selected": selected,
                    "endpoint": endpoint,
                    "status_code": choice_status,
                },
            )
            final_payload = response

        prompt = pending_choice_from_response(final_payload)
        combined = response_summary(final_payload)
        combined["spin_modes"] = _merge_modes(*summaries)
        combined["wire_steps"] = wire_steps
        combined["branch_prefix"] = list(prefix)
        combined["pending_choice"] = (
            {
                "round_id": prompt.round_id,
                "available": list(prompt.available),
            }
            if prompt is not None
            else None
        )
        _write_json(branch_root / "summary.json", combined)
        return ReplayOutcome(
            prompt=prompt,
            final_payload=final_payload,
            summaries=summaries,
            warnings=warnings,
            wire_steps=wire_steps,
            status_code=status_code,
            selected=tuple(consumed),
            elapsed_ms=(time.monotonic() - started) * 1000.0,
            artifact_dir=branch_root,
        )
    finally:
        if runtime is not None:
            runtime.session.close()


def _upsert_branch_metadata(
    result: GameTestResult,
    branch_points: dict[tuple[str, tuple[str, ...]], BranchPoint],
) -> None:
    # Remove the older diagnostic-only continuation rows. They record only the
    # first selected option and would otherwise make coverage look ambiguous.
    result.discovered_modes = [
        item
        for item in result.discovered_modes
        if not (
            isinstance(item, dict)
            and str(item.get("kind") or "").upper() == "CHOICE_CONTINUATION"
        )
    ]
    for point in sorted(branch_points.values(), key=lambda item: (item.mode_id, item.prefix)):
        result.discovered_modes.append(
            {
                "id": f"{point.mode_id}__BRANCH_{_path_label(point.prefix)}",
                "kind": "CHOICE_BRANCH",
                "parent": point.mode_id,
                "prefix": list(point.prefix),
                "observed": True,
                "executable": True,
                "wire_command": "platform/game/choice",
                "coverage_required": True,
                "branch_signature": point.signature,
                "required_options": list(point.required),
                "covered_options": sorted(point.covered),
            }
        )


def _append_error(result: GameTestResult, message: str) -> None:
    clean = str(message or "").strip()
    if not clean or clean in str(result.error or ""):
        return
    result.error = (str(result.error or "").strip() + " " + clean).strip()


def expand_all_choice_branches(
    provider,
    game: Game,
    result: GameTestResult,
    *,
    launch_id: str,
    repetitions: int,
    timeout_s: float,
    stop_event,
    progress: Progress,
) -> GameTestResult:
    """Execute every observed Red Tiger choice path using fresh rounds.

    A choice consumes the provider round id, so sibling options cannot be tested on
    the same transaction. Each queued prefix is replayed from a fresh Evolution
    demo session, and every discovered option is expanded. Unknown/unstable paths
    never become OK silently: they are recorded as failed coverage.
    """
    if result.status in {"ERROR", "CANCELADO"} or not result.run_dir or not launch_id:
        return result

    parent_modes = _choice_parent_modes(result)
    if not parent_modes:
        return result

    specs = _base_mode_specs(result)
    run_root = Path(result.run_dir)
    branch_points: dict[tuple[str, tuple[str, ...]], BranchPoint] = {}
    added_requested = 0
    added_successes = 0
    branch_errors: list[str] = []
    attempt_number = max((attempt.number for attempt in result.attempts), default=0)

    for base_mode in sorted(parent_modes):
        spec = specs.get(base_mode)
        if spec is None:
            branch_errors.append(f"{base_mode}: no se pudo reconstruir el contrato de entrada")
            continue
        mode_kind, feature = spec

        for repetition in range(1, max(1, int(repetitions)) + 1):
            queue: list[tuple[str, ...]] = [()]
            seen: set[tuple[str, ...]] = set()
            scheduled = 0

            while queue and not stop_event.is_set():
                prefix = queue.pop(0)
                if prefix in seen:
                    continue
                seen.add(prefix)
                scheduled += 1
                if scheduled > MAX_BRANCH_PREFIXES_PER_MODE:
                    branch_errors.append(
                        f"{base_mode}: excedió {MAX_BRANCH_PREFIXES_PER_MODE} rutas de selector"
                    )
                    break
                if len(prefix) > MAX_CHOICE_DEPTH:
                    branch_errors.append(
                        f"{base_mode}: excedió profundidad máxima {MAX_CHOICE_DEPTH}"
                    )
                    continue

                progress(
                    f"[{game.name}] {base_mode}: recorriendo rama "
                    f"{list(prefix) if prefix else ['<descubrir>']} "
                    f"(rep={repetition})."
                )
                try:
                    outcome = _replay_prefix(
                        provider,
                        game,
                        launch_id=launch_id,
                        base_mode=base_mode,
                        feature=feature,
                        repetition=repetition,
                        prefix=prefix,
                        timeout_s=timeout_s,
                        stop_event=stop_event,
                        run_root=run_root,
                    )
                except InterruptedError:
                    return result
                except Exception as exc:
                    if prefix:
                        attempt_number += 1
                        added_requested += 1
                        result.attempts.append(
                            SpinAttempt(
                                number=attempt_number,
                                ok=False,
                                mode_id=_branch_mode_id(base_mode, prefix),
                                mode_kind="CHOICE_PATH",
                                symbol=result.symbol,
                                terminal=False,
                                wire_steps=0,
                                error=f"{type(exc).__name__}: {exc}",
                                artifact_dir=str(
                                    run_root
                                    / base_mode
                                    / "branch-coverage"
                                    / f"rep-{repetition:05d}"
                                    / f"path-{_path_label(prefix)}"
                                ),
                            )
                        )
                    branch_errors.append(
                        f"{base_mode}/{_path_label(prefix)}: {type(exc).__name__}: {exc}"
                    )
                    continue

                if prefix:
                    parent_prefix = prefix[:-1]
                    parent = branch_points.get((base_mode, parent_prefix))
                    if parent is not None:
                        parent.covered.add(prefix[-1])

                if outcome.prompt is not None:
                    key = (base_mode, prefix)
                    point = branch_points.get(key)
                    if point is None:
                        point = BranchPoint(
                            mode_id=base_mode,
                            prefix=prefix,
                            required=list(outcome.prompt.available),
                            covered=set(),
                        )
                        branch_points[key] = point
                    else:
                        for option in outcome.prompt.available:
                            if option not in point.required:
                                point.required.append(option)

                    if len(prefix) >= MAX_CHOICE_DEPTH:
                        branch_errors.append(
                            f"{base_mode}/{_path_label(prefix)}: selector adicional supera profundidad máxima"
                        )
                        continue
                    for option in outcome.prompt.available:
                        child = prefix + (option,)
                        if child not in seen and child not in queue:
                            queue.append(child)
                    continue

                if not prefix:
                    # A branch was seen in the original run but a fresh replay no
                    # longer exposed it. Keep the run incomplete rather than guess.
                    branch_errors.append(
                        f"{base_mode}: la rama observada no reapareció en una sesión fresca"
                    )
                    continue

                added_requested += 1
                attempt_number += 1
                warnings = list(dict.fromkeys(outcome.warnings))
                terminal = outcome.terminal
                ok = terminal and not warnings
                added_successes += int(ok)
                merged_modes = _merge_modes(*outcome.summaries)
                result.attempts.append(
                    SpinAttempt(
                        number=attempt_number,
                        ok=ok,
                        mode_id=_branch_mode_id(base_mode, prefix),
                        mode_kind="CHOICE_PATH",
                        status_code=outcome.status_code,
                        elapsed_ms=outcome.elapsed_ms,
                        symbol=result.symbol,
                        endpoint="platform/game/choice",
                        na=",".join(merged_modes),
                        terminal=terminal,
                        wire_steps=outcome.wire_steps,
                        warning="; ".join(warnings),
                        artifact_dir=str(outcome.artifact_dir),
                    )
                )
                if not ok:
                    branch_errors.append(
                        f"{base_mode}/{_path_label(prefix)}: "
                        + ("; ".join(warnings) if warnings else "ruta no terminal")
                    )

    _upsert_branch_metadata(result, branch_points)
    result.requested_spins += added_requested
    result.successful_spins += added_successes
    result.failed_spins = max(0, result.requested_spins - result.successful_spins)

    missing = [
        (point.signature, [option for option in point.required if option not in point.covered])
        for point in branch_points.values()
        if any(option not in point.covered for option in point.required)
    ]
    if missing:
        branch_errors.extend(
            f"{signature}: faltan {options}" for signature, options in missing
        )

    if stop_event.is_set():
        result.status = "CANCELADO"
        _append_error(result, "Detención solicitada durante expansión de ramificaciones Red Tiger.")
    elif branch_errors or result.failed_spins:
        if result.status != "ERROR":
            result.status = "PARCIAL"
        _append_error(
            result,
            "Cobertura de choices Red Tiger incompleta: "
            + " | ".join(list(dict.fromkeys(branch_errors))[:8]),
        )
    elif added_requested:
        progress(
            f"[{game.name}] Red Tiger ramas: {added_successes}/{added_requested} "
            "rutas terminales adicionales validadas."
        )

    _write_json(run_root / "result.json", result.to_dict())
    return result
