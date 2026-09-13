from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult, SpinAttempt
from tester_spin.providers.base import Progress
from tester_spin.providers import pragmatic_har_states as _har_states
from tester_spin.providers.pragmatic_hybrid import PragmaticProvider as _PragmaticProvider
from tester_spin.providers.pragmatic_modes import PragmaticMode, discover_modes


MAX_FSO_DEPTH = 8
MAX_FSO_PREFIXES_PER_MODE = 128
_FORCE_LOCAL = threading.local()
_ORIGINAL_CHOOSE_FS_OPTION_INDEX = _har_states.choose_fs_option_index


@dataclass(slots=True)
class FSOBranchPoint:
    mode_id: str
    prefix: tuple[int, ...]
    required: set[int] = field(default_factory=set)
    covered: set[int] = field(default_factory=set)
    sample_counts: dict[int, int] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        head = "/".join(str(value) for value in self.prefix) if self.prefix else "ROOT"
        return f"{self.mode_id}:FSO:{head}"


def _forced_choose_fs_option_index(
    options: list[dict[str, Any]],
    *,
    repetition: int,
    preferred: int | None = None,
) -> int:
    state = getattr(_FORCE_LOCAL, "state", None)
    if not isinstance(state, dict):
        return _ORIGINAL_CHOOSE_FS_OPTION_INDEX(
            options,
            repetition=repetition,
            preferred=preferred,
        )

    indices = [int(item["index"]) for item in options]
    if not indices:
        raise ValueError("fs_opt no contiene opciones seleccionables")
    depth = int(state.get("depth") or 0)
    prefix = tuple(int(value) for value in state.get("prefix") or ())
    if depth < len(prefix):
        selected = int(prefix[depth])
        if selected not in indices:
            raise ValueError(
                f"ruta FSO esperaba ind={selected} en profundidad={depth}, "
                f"pero el provider anunció {indices}"
            )
    else:
        # Discovery beyond the forced prefix is deterministic. Siblings are queued
        # from the returned domain and replayed separately on fresh sessions.
        selected = indices[0]

    trace = state.setdefault("trace", [])
    trace.append({"required": list(indices), "selected": selected})
    state["depth"] = depth + 1
    return selected


if getattr(_har_states.choose_fs_option_index, "__name__", "") != "_forced_choose_fs_option_index":
    _har_states.choose_fs_option_index = _forced_choose_fs_option_index


def _read_trace(attempt: SpinAttempt) -> list[tuple[tuple[int, ...], int]]:
    root = Path(str(attempt.artifact_dir or ""))
    if not root.is_dir():
        return []
    rows: list[tuple[tuple[int, ...], int]] = []
    for path in sorted(root.glob("fso-selection-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            required = tuple(int(value) for value in payload.get("option_indices") or [])
            selected = int(payload.get("selected_index"))
        except Exception:
            continue
        if required and selected in required:
            rows.append((required, selected))
    return rows


def _ingest_attempt(
    points: dict[tuple[str, tuple[int, ...]], FSOBranchPoint],
    attempt: SpinAttempt,
) -> None:
    trace = _read_trace(attempt)
    prefix: list[int] = []
    completed = bool(attempt.ok and attempt.terminal and not attempt.warning)
    for required, selected in trace:
        key = (attempt.mode_id, tuple(prefix))
        point = points.get(key)
        if point is None:
            point = FSOBranchPoint(mode_id=attempt.mode_id, prefix=tuple(prefix))
            points[key] = point
        point.required.update(required)
        if completed:
            point.covered.add(selected)
            point.sample_counts[selected] = point.sample_counts.get(selected, 0) + 1
        prefix.append(selected)


def _missing_prefixes(
    points: dict[tuple[str, tuple[int, ...]], FSOBranchPoint],
    repetitions: int = 1,
) -> list[tuple[str, tuple[int, ...]]]:
    missing: list[tuple[str, tuple[int, ...]]] = []
    for point in points.values():
        for option in sorted(point.required):
            if point.sample_counts.get(option, 0) >= repetitions:
                continue
            missing.append((point.mode_id, point.prefix + (option,)))
    return sorted(missing, key=lambda item: (item[0], len(item[1]), item[1]))


def _next_repetition(run_root: Path, mode_id: str) -> int:
    values: list[int] = []
    mode_root = run_root / mode_id
    if mode_root.is_dir():
        for path in mode_root.glob("attempt-*"):
            try:
                values.append(int(path.name.split("-", 1)[1]))
            except Exception:
                continue
    return max(values, default=0) + 1


def _upsert_metadata(
    result: GameTestResult,
    points: dict[tuple[str, tuple[int, ...]], FSOBranchPoint],
    repetitions: int = 1,
) -> None:
    result.discovered_modes = [
        item
        for item in result.discovered_modes
        if not (
            isinstance(item, dict)
            and str(item.get("kind") or "").upper() == "FSO_BRANCH"
        )
    ]
    for point in sorted(points.values(), key=lambda value: (value.mode_id, value.prefix)):
        prefix = list(point.prefix)
        label = "ROOT" if not prefix else "_".join(map(str, prefix))
        result.discovered_modes.append(
            {
                "id": f"{point.mode_id}__FSO_BRANCH_{label}",
                "kind": "FSO_BRANCH",
                "parent": point.mode_id,
                "prefix": prefix,
                "observed": True,
                "executable": True,
                "wire_command": "doFSOption",
                "coverage_required": True,
                "branch_signature": point.signature,
                "required_options": [str(value) for value in sorted(point.required)],
                "covered_options": [str(value) for value in sorted(point.covered)],
                "required_samples": repetitions,
                "sample_counts": {str(k): v for k, v in point.sample_counts.items()},
            }
        )


def expand_pragmatic_fso_paths(
    provider: _PragmaticProvider,
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

    points: dict[tuple[str, tuple[int, ...]], FSOBranchPoint] = {}
    for attempt in result.attempts:
        _ingest_attempt(points, attempt)
    if not points:
        return result

    run_root = Path(result.run_dir)
    repetitions = max(1, int(repetitions))
    queue = _missing_prefixes(points, repetitions)
    queued = set(queue)
    attempted_prefixes: set[tuple[str, tuple[int, ...]]] = set()
    replay_counts: dict[tuple[str, tuple[int, ...]], int] = {}
    extra_errors: list[str] = []
    added_requested = 0
    added_successful = 0
    added_failed = 0

    if queue:
        try:
            discovery = provider._browser_bootstrap(
                game.url,
                timeout_s=max(60.0, timeout_s),
                progress=progress,
            )
            catalog = discover_modes(
                discovery.init_response,
                requested_base_bet=provider.base_bet,
            )
            modes: dict[str, PragmaticMode] = {
                mode.id: mode for mode in catalog.enabled()
            }
            next_rep = {
                mode_id: _next_repetition(run_root, mode_id)
                for mode_id, _prefix in queue
            }
            attempt_number = max((attempt.number for attempt in result.attempts), default=0)

            while queue and not stop_event.is_set():
                mode_id, prefix = queue.pop(0)
                queued.discard((mode_id, prefix))
                key = (mode_id, prefix)
                if replay_counts.get(key, 0) >= repetitions:
                    continue
                # Queued ancestors may already have enough samples after a leaf replay.
                if key not in _missing_prefixes(points, repetitions):
                    continue
                attempted_prefixes.add(key)
                replay_counts[key] = replay_counts.get(key, 0) + 1
                if len(prefix) > MAX_FSO_DEPTH:
                    extra_errors.append(
                        f"{mode_id}/{prefix}: excede profundidad FSO {MAX_FSO_DEPTH}"
                    )
                    continue
                mode_count = sum(1 for candidate, _path in attempted_prefixes if candidate == mode_id)
                if mode_count > MAX_FSO_PREFIXES_PER_MODE:
                    extra_errors.append(
                        f"{mode_id}: excede {MAX_FSO_PREFIXES_PER_MODE} rutas FSO"
                    )
                    continue

                mode = modes.get(mode_id)
                if mode is None:
                    extra_errors.append(f"{mode_id}: modo ya no aparece en doInit")
                    continue

                repetition = next_rep.setdefault(mode_id, _next_repetition(run_root, mode_id))
                next_rep[mode_id] = repetition + 1
                attempt_number += 1
                progress(
                    f"{mode_id}: recorriendo ruta FSO {list(prefix)} "
                    f"en sesión fresca."
                )
                _FORCE_LOCAL.state = {"prefix": tuple(prefix), "depth": 0, "trace": []}
                try:
                    attempt = provider._test_mode_once(
                        game,
                        symbol=discovery.symbol,
                        cver=discovery.cver,
                        mode=mode,
                        catalog=catalog,
                        attempt_number=attempt_number,
                        repetition=repetition,
                        run_root=run_root,
                        timeout_s=timeout_s,
                        fs_option_index=None,
                    )
                finally:
                    _FORCE_LOCAL.state = None

                result.attempts.append(attempt)
                added_requested += 1
                if attempt.ok and attempt.terminal and not attempt.warning:
                    added_successful += 1
                elif not attempt.ok:
                    added_failed += 1
                    extra_errors.append(
                        f"{mode_id}/{list(prefix)}: {attempt.error or 'falló'}"
                    )
                else:
                    extra_errors.append(
                        f"{mode_id}/{list(prefix)}: no terminal {attempt.warning or attempt.na}"
                    )

                _ingest_attempt(points, attempt)
                for candidate in _missing_prefixes(points, repetitions):
                    if replay_counts.get(candidate, 0) < repetitions and candidate not in queued:
                        queue.append(candidate)
                        queued.add(candidate)
        except Exception as exc:
            extra_errors.append(f"expansión FSO: {type(exc).__name__}: {exc}")

    _upsert_metadata(result, points, repetitions)
    result.requested_spins += added_requested
    result.successful_spins += added_successful
    result.failed_spins += added_failed

    missing = _missing_prefixes(points, repetitions)
    if stop_event.is_set():
        result.status = "CANCELADO"
        extra_errors.append("detenido durante expansión FSO")
    if missing or extra_errors:
        if result.status == "OK":
            result.status = "PARCIAL"
        detail: list[str] = []
        if missing:
            detail.append(
                "rutas FSO pendientes="
                + ", ".join(f"{mode}:{list(prefix)}" for mode, prefix in missing[:12])
            )
        if extra_errors:
            detail.append("errores=" + " | ".join(extra_errors[:8]))
        message = "Pragmatic cobertura FSO exhaustiva incompleta: " + "; ".join(detail) + "."
        if message not in str(result.error or ""):
            result.error = (str(result.error or "").strip() + " " + message).strip()
    else:
        total_options = sum(len(point.required) for point in points.values())
        progress(
            f"Pragmatic cobertura FSO por ruta completa: {total_options}/{total_options} opciones."
        )

    try:
        (run_root / "result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        provider._record_last_test_in_game_json(game, result)
    except Exception:
        pass
    return result


class PragmaticProvider(_PragmaticProvider):
    """Pragmatic hybrid plus exhaustive path-sensitive FSO traversal."""

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
        return expand_pragmatic_fso_paths(
            self,
            game,
            result,
            repetitions=max(1, int(spins)),
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )


PragmaticProvider.__module__ = "tester_spin.providers.pragmatic_hybrid"

__all__ = ["PragmaticProvider", "expand_pragmatic_fso_paths"]
