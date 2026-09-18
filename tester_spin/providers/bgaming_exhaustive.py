from __future__ import annotations

import itertools
import hashlib
import math
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.bgaming import BGamingProvider as _BGamingProvider
from tester_spin.providers.bgaming import execution as _execution
from tester_spin.providers.bgaming.flow_choices import (
    begin_flow_choice_run,
    end_flow_choice_run,
    install_flow_choice_adapter,
)


MAX_OPTION_COMBINATIONS = 128
MAX_FLOW_CHOICE_RUNS = 64

# The base executor owns profile discovery. Exhaustive replays only need a
# temporary, thread-local selector override; no per-game constants are stored.
_OVERRIDE_LOCAL = threading.local()
_ORIGINAL_DISCOVER_PROFILE = _execution.discover_profile


install_flow_choice_adapter()


def _same_option(left: Any, right: Any) -> bool:
    return (type(left) is type(right) and left == right) or str(left) == str(right)


def _apply_profile_override(profile):
    override = getattr(_OVERRIDE_LOCAL, "spin_options", None)
    if not isinstance(override, dict) or not override:
        return profile

    for field, selected in override.items():
        choices = profile.spin_option_choices.get(field)
        if not isinstance(choices, list) or not choices:
            raise ValueError(
                f"BGaming exhaustive: selector {field!r} sin dominio descubierto."
            )
        match = next(
            (value for value in choices if _same_option(value, selected)),
            None,
        )
        if match is None:
            raise ValueError(
                f"BGaming exhaustive: {field}={selected!r} no está en {choices!r}."
            )
        profile.spin_options[field] = match
        profile.evidence.append(f"exhaustive.override.{field}={match}")
    return profile


def _discover_profile_with_override(*args, **kwargs):
    return _apply_profile_override(_ORIGINAL_DISCOVER_PROFILE(*args, **kwargs))


if getattr(_execution.discover_profile, "__name__", "") != "_discover_profile_with_override":
    _execution.discover_profile = _discover_profile_with_override


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _safe_label(label: str) -> str:
    clean = [
        ch if ch.isalnum() or ch in {"-", "_", "."} else "_"
        for ch in str(label)
    ]
    digest = hashlib.sha256(str(label).encode("utf-8")).hexdigest()[:12]
    return ("".join(clean)[:150] or "BASE") + "-" + digest


def _load_profile(result: GameTestResult) -> dict[str, Any]:
    path = Path(str(result.run_dir or "")) / "profile.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _choice_domains(profile: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    raw = profile.get("spin_option_choices")
    if not isinstance(raw, dict):
        return []

    domains: list[tuple[str, list[Any]]] = []
    for field in sorted(raw):
        # These form one purchase contract, already enumerated by the executor.
        # Crossing them with base spin or another feature fabricates invalid requests.
        if field in {"purchased_feature", "purchased_feature_level"}:
            continue
        values = raw.get(field)
        if not isinstance(values, list):
            continue
        clean: list[Any] = []
        for value in values:
            if value in (None, ""):
                continue
            if not any(_same_option(value, seen) for seen in clean):
                clean.append(value)
        if len(clean) > 1:
            domains.append((str(field), clean))
    return domains


def _matrix(domains: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:
    if not domains:
        return [{}]
    names = [name for name, _values in domains]
    return [
        dict(zip(names, values))
        for values in itertools.islice(
            itertools.product(*(values for _name, values in domains)),
            MAX_OPTION_COMBINATIONS + 1,
        )
    ]


def _label(combo: dict[str, Any]) -> str:
    if not combo:
        return "BASE"
    return "|".join(
        f"{field}={json.dumps(combo[field], ensure_ascii=False, sort_keys=True)}"
        for field in sorted(combo)
    )


def _base_combo(
    profile: dict[str, Any],
    domains: list[tuple[str, list[Any]]],
) -> dict[str, Any]:
    selected = profile.get("spin_options")
    if not isinstance(selected, dict):
        selected = {}

    combo: dict[str, Any] = {}
    for field, values in domains:
        current = selected.get(field)
        match = next(
            (value for value in values if _same_option(value, current)),
            None,
        )
        combo[field] = values[0] if match is None else match
    return combo


def _complete(result: GameTestResult) -> bool:
    return (
        result.status == "OK"
        and result.requested_spins > 0
        and result.successful_spins == result.requested_spins
        and result.failed_spins == 0
        and bool(result.attempts)
        and all(attempt.ok and attempt.terminal and not attempt.warning and not attempt.error for attempt in result.attempts)
    )


def _remap_attempt_paths(result: GameTestResult, old_root: Path, new_root: Path) -> None:
    for attempt in result.attempts:
        if not attempt.artifact_dir:
            continue
        path = Path(attempt.artifact_dir)
        try:
            rel = path.relative_to(old_root)
        except ValueError:
            continue
        attempt.artifact_dir = str(new_root / rel)


def _move_run(result: GameTestResult, target: Path) -> None:
    if not result.run_dir:
        return
    source = Path(str(result.run_dir or ""))
    if not source.is_dir():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"No se sobrescribe evidencia existente: {target}")
    if source.resolve() == target.resolve() or source.resolve() in target.resolve().parents:
        raise ValueError("El destino no puede estar dentro de la corrida de origen")
    shutil.move(str(source), str(target))
    _remap_attempt_paths(result, source, target)
    result.run_dir = str(target)


def _merge_modes(target: GameTestResult, source: GameTestResult) -> None:
    signatures = {
        (
            str(item.get("id") or ""),
            str(item.get("branch_signature") or ""),
        )
        for item in target.discovered_modes
        if isinstance(item, dict)
    }
    for item in source.discovered_modes:
        if not isinstance(item, dict):
            continue
        signature = (
            str(item.get("id") or ""),
            str(item.get("branch_signature") or ""),
        )
        if signature in signatures:
            continue
        target.discovered_modes.append(dict(item))
        signatures.add(signature)


def _trace_confirms(
    trace: list[dict[str, Any]],
    scope: str,
    command: str,
    path: tuple[str, ...],
) -> bool:
    return any(
        str(item.get("scope") or "") == scope
        and str(item.get("command") or item.get("wire_command") or "") == command
        and tuple(str(value) for value in item.get("path_after") or []) == path
        for item in trace
        if isinstance(item, dict)
    )


def _merge_choice_trace(
    graph: dict[tuple[str, str, tuple[str, ...], tuple[str, ...]], dict[str, Any]],
    trace: list[dict[str, Any]],
    *,
    complete: bool,
) -> None:
    for item in trace:
        if not isinstance(item, dict):
            continue
        scope = str(item.get("scope") or "SPIN")
        command = str(item.get("command") or item.get("wire_command") or "")
        prefix = tuple(str(value) for value in item.get("prefix") or [])
        available = tuple(
            str(value) for value in item.get("available") or [] if str(value)
        )
        if not command or not available:
            continue

        key = (scope, command, prefix, available)
        point = graph.setdefault(
            key,
            {
                "scope": scope,
                "command": command,
                "option_field": str(item.get("option_field") or ""),
                "source": str(item.get("source") or "runtime"),
                "prefix": prefix,
                "available": available,
                "covered": set(),
                "sample_counts": {},
            },
        )
        selected = str(item.get("selected") or "")
        if complete and selected in available:
            point["covered"].add(selected)
            point["sample_counts"][selected] = point["sample_counts"].get(selected, 0) + 1


def _choice_mode_from_point(
    point: dict[str, Any],
    *,
    repetitions: int = 1,
) -> dict[str, Any]:
    scope = str(point.get("scope") or "SPIN")
    command = str(point.get("command") or "")
    prefix = tuple(str(value) for value in point.get("prefix") or ())
    required = [str(value) for value in point.get("available") or [] if str(value)]
    try:
        samples = max(1, int(repetitions))
    except (TypeError, ValueError):
        samples = 1
    counts = point.get("sample_counts")
    if not isinstance(counts, dict):
        counts = {}

    def count(value: str) -> int:
        try:
            return max(0, int(counts.get(value, 0) or 0))
        except (TypeError, ValueError):
            return 0

    covered = [value for value in required if count(value) >= samples]
    mode_id = "BGAMING_FLOW_CHOICE_" + _safe_label(
        scope
        + "__"
        + command
        + "__"
        + "__".join(prefix or ("ROOT",))
    ).upper()
    return {
        "id": mode_id,
        "kind": "CHOICE_CONTINUATION",
        "scope": scope,
        "parent": scope,
        "observed": True,
        "executable": True,
        "wire_command": command,
        "option_field": str(point.get("option_field") or ""),
        "coverage_required": True,
        "branch_signature": (
            "BGAMING:flow-choice:"
            + scope
            + ":"
            + command
            + ":"
            + json.dumps(
                list(prefix),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        ),
        "path_prefix": list(prefix),
        "required_options": required,
        "covered_options": covered,
        "required_samples": samples,
        "sample_counts": {
            value: count(value)
            for value in required
        },
        "source": str(point.get("source") or "runtime"),
    }


def _next_missing_choice(
    graph: dict[tuple[str, str, tuple[str, ...], tuple[str, ...]], dict[str, Any]],
    attempted: set[tuple[str, str, tuple[str, ...]]],
    repetitions: int = 1,
) -> tuple[str, str, tuple[str, ...]] | None:
    ordered = sorted(
        graph.values(),
        key=lambda item: (
            str(item["scope"]),
            str(item["command"]),
            len(item["prefix"]),
            tuple(item["prefix"]),
        ),
    )
    for point in ordered:
        for option in point["available"]:
            if point["sample_counts"].get(option, 0) >= repetitions:
                continue
            target = (
                str(point["scope"]),
                str(point["command"]),
                (*point["prefix"], str(option)),
            )
            if target not in attempted:
                return target
    return None


class BGamingProvider(_BGamingProvider):
    """BGaming with exhaustive traversal of client-proven finite choices."""

    def _raw_test(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
        forced_scope: str = "",
        forced_command: str = "",
        forced_path: tuple[str, ...] = (),
    ) -> tuple[GameTestResult, list[dict[str, Any]]]:
        from tester_spin.providers.bgaming.structural_map import reset_capture
        reset_capture()
        begin_flow_choice_run(
            forced_scope=forced_scope,
            forced_command=forced_command,
            forced_path=forced_path,
        )
        try:
            result = _BGamingProvider.test_game(
                self,
                game,
                spins=spins,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
        finally:
            trace = end_flow_choice_run()
        return result, trace

    def _run_with_flow_choice_coverage(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        result, base_trace = self._raw_test(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        if result.status in {"ERROR", "CANCELADO", "SIN_DEMO"} or not result.run_dir:
            return result
        if not base_trace:
            return result

        graph: dict[
            tuple[str, str, tuple[str, ...], tuple[str, ...]],
            dict[str, Any],
        ] = {}
        _merge_choice_trace(graph, base_trace, complete=_complete(result))

        initial_root = Path(result.run_dir)
        master_root = initial_root.with_name(
            initial_root.name + f"-flow-choices-{time.time_ns() % 1_000_000_000:09d}"
        )
        _move_run(result, master_root)

        added_requested = 0
        added_successes = 0
        branch_errors: list[str] = []
        attempted: set[tuple[str, str, tuple[str, ...]]] = set()
        executed = 0

        while not stop_event.is_set():
            target = _next_missing_choice(graph, attempted, max(1, int(spins)))
            if target is None:
                break
            scope, command, forced_path = target
            attempted.add(target)
            if executed >= MAX_FLOW_CHOICE_RUNS:
                branch_errors.append(
                    f"elecciones de flujo exceden guard de {MAX_FLOW_CHOICE_RUNS} replays"
                )
                break
            executed += 1
            path_label = " → ".join(forced_path)
            progress(
                f"[{game.name}] BGaming {command}: reproduciendo "
                f"{scope} → {path_label}."
            )

            try:
                sub, trace = self._raw_test(
                    game,
                    spins=max(1, int(spins)),
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=progress,
                    forced_scope=scope,
                    forced_command=command,
                    forced_path=forced_path,
                )
            except Exception as exc:
                branch_errors.append(
                    f"{scope}/{command}/{path_label}: {type(exc).__name__}: {exc}"
                )
                continue

            target_dir = (
                master_root
                / "flow-choice-runs"
                / _safe_label(scope)
                / _safe_label(command)
                / _safe_label("__".join(forced_path))
            )
            if Path(str(sub.run_dir or "")).is_dir():
                _move_run(sub, target_dir)

            added_requested += sub.requested_spins
            added_successes += sub.successful_spins
            suffix = _safe_label(
                scope + "__" + command + "__" + "__".join(forced_path)
            ).upper()
            for attempt in sub.attempts:
                attempt.mode_id = f"{attempt.mode_id}__FLOW_{suffix}"
                attempt.mode_kind = f"{attempt.mode_kind}_CHOICE_VARIANT"
                result.attempts.append(attempt)
            _merge_modes(result, sub)

            sub_complete = _complete(sub)
            _merge_choice_trace(graph, trace, complete=sub_complete)
            if not _trace_confirms(trace, scope, command, forced_path):
                branch_errors.append(
                    f"{scope}/{command}/{path_label}: la ronda fresca no volvió a alcanzar esa rama"
                )
            elif not sub_complete:
                branch_errors.append(
                    f"{scope}/{command}/{path_label}: {sub.status} {sub.error}".strip()
                )

        result.requested_spins += added_requested
        result.successful_spins += added_successes
        result.failed_spins = max(
            0,
            result.requested_spins - result.successful_spins,
        )

        missing_labels: list[str] = []
        for point in sorted(
            graph.values(),
            key=lambda item: (
                str(item["scope"]),
                str(item["command"]),
                len(item["prefix"]),
                tuple(item["prefix"]),
            ),
        ):
            scope = str(point["scope"])
            command = str(point["command"])
            prefix = tuple(str(value) for value in point["prefix"])
            prefix_text = "ROOT" if not prefix else " → ".join(prefix)
            mode = _choice_mode_from_point(
                point,
                repetitions=max(1, int(spins)),
            )
            required = list(mode["required_options"])
            covered = list(mode["covered_options"])
            result.discovered_modes.append(mode)
            for value in required:
                if value not in covered:
                    missing_labels.append(
                        f"{scope}/{command}/{prefix_text} → {value}"
                    )

        if stop_event.is_set():
            result.status = "CANCELADO"
        elif (missing_labels or branch_errors) and result.status == "OK":
            result.status = "PARCIAL"
        if missing_labels or branch_errors:
            details: list[str] = []
            if missing_labels:
                details.append("faltan=" + ", ".join(missing_labels[:20]))
            if branch_errors:
                details.append("errores=" + " | ".join(branch_errors[:8]))
            message = (
                "BGaming cobertura de elecciones de flujo incompleta: "
                + "; ".join(details)
                + "."
            )
            if message not in str(result.error or ""):
                result.error = (
                    str(result.error or "").strip() + " " + message
                ).strip()

        _write_json(
            master_root / "flow-choice-coverage.json",
            {
                "schema": "tester-spin/bgaming-flow-choice-coverage/v1",
                "branch_points": [
                    {
                        "scope": str(point["scope"]),
                        "command": str(point["command"]),
                        "option_field": str(point.get("option_field") or ""),
                        "prefix": list(point["prefix"]),
                        "available": list(point["available"]),
                        "covered": [
                            value
                            for value in point["available"]
                            if value in point["covered"]
                        ],
                        "source": str(point.get("source") or "runtime"),
                    }
                    for point in graph.values()
                ],
                "complete": not missing_labels and not branch_errors and not stop_event.is_set(),
                "replays": executed,
            },
        )
        _write_json(master_root / "result.json", result.to_dict())
        return result

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        result = self._run_with_flow_choice_coverage(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        if result.status in {"ERROR", "CANCELADO", "SIN_DEMO"} or not result.run_dir:
            return result

        profile = _load_profile(result)
        domains = _choice_domains(profile)
        if not domains:
            return result

        combinations = _matrix(domains)
        total_combinations = math.prod(len(values) for _, values in domains)
        base_combo = _base_combo(profile, domains)
        base_label = _label(base_combo)
        required = [_label(combo) for combo in combinations]
        if total_combinations > len(combinations):
            required.append("MATRIX_LIMIT_EXCEEDED")
        covered: set[str] = {base_label} if _complete(result) else set()

        original_root = Path(result.run_dir)
        master_root = original_root.with_name(
            original_root.name + f"-exhaustive-{time.time_ns() % 1_000_000_000:09d}"
        )
        _move_run(result, master_root)

        added_requested = 0
        added_successes = 0
        branch_errors: list[str] = []
        executed = 1  # The original combination has already consumed a session.

        for combo in combinations:
            label = _label(combo)
            if label == base_label:
                continue
            if stop_event.is_set():
                branch_errors.append("detenido por el usuario")
                break
            if executed >= MAX_OPTION_COMBINATIONS:
                branch_errors.append(
                    f"matriz excede guard de {MAX_OPTION_COMBINATIONS} combinaciones"
                )
                break
            executed += 1
            progress(
                f"[{game.name}] BGaming opciones dinámicas: probando {label}."
            )
            _OVERRIDE_LOCAL.spin_options = dict(combo)
            try:
                sub = self._run_with_flow_choice_coverage(
                    game,
                    spins=max(1, int(spins)),
                    timeout_s=timeout_s,
                    stop_event=stop_event,
                    progress=progress,
                )
            except Exception as exc:
                branch_errors.append(
                    f"{label}: {type(exc).__name__}: {exc}"
                )
                continue
            finally:
                _OVERRIDE_LOCAL.spin_options = None

            sub_root = Path(str(sub.run_dir or ""))
            target = master_root / "branch-runs" / _safe_label(label)
            if sub_root.is_dir():
                _move_run(sub, target)
            added_requested += sub.requested_spins
            added_successes += sub.successful_spins
            suffix = _safe_label(label).upper()
            for attempt in sub.attempts:
                attempt.mode_id = f"{attempt.mode_id}__OPTIONS_{suffix}"
                attempt.mode_kind = f"{attempt.mode_kind}_OPTION_VARIANT"
                result.attempts.append(attempt)
            _merge_modes(result, sub)
            if _complete(sub):
                covered.add(label)
            else:
                branch_errors.append(
                    f"{label}: {sub.status} {sub.error}".strip()
                )

        result.requested_spins += added_requested
        result.successful_spins += added_successes
        result.failed_spins = max(
            0,
            result.requested_spins - result.successful_spins,
        )
        result.discovered_modes.append(
            {
                "id": "BGAMING_SPIN_OPTION_MATRIX",
                "kind": "CHOICE_BRANCH",
                "observed": True,
                "executable": True,
                "wire_command": "additionalSpinOptions",
                "coverage_required": True,
                "branch_signature": "BGAMING:additionalSpinOptions:matrix",
                "dimensions": [
                    {"field": name, "values": values}
                    for name, values in domains
                ],
                "total_combinations": total_combinations,
                "required_options": required,
                "covered_options": sorted(covered),
            }
        )

        missing = [value for value in required if value not in covered]
        if stop_event.is_set():
            result.status = "CANCELADO"
        elif (missing or branch_errors) and result.status == "OK":
            result.status = "PARCIAL"
        if missing or branch_errors:
            details: list[str] = []
            if missing:
                details.append("faltan=" + ", ".join(missing[:20]))
            if branch_errors:
                details.append("errores=" + " | ".join(branch_errors[:8]))
            message = (
                "BGaming cobertura de additionalSpinOptions incompleta: "
                + "; ".join(details)
                + "."
            )
            if message not in str(result.error or ""):
                result.error = (
                    str(result.error or "").strip() + " " + message
                ).strip()

        _write_json(master_root / "result.json", result.to_dict())
        return result


# Preserve the public provider boundary used by registry/wiring tests.
BGamingProvider.__module__ = "tester_spin.providers.bgaming"

__all__ = ["BGamingProvider"]
