from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.one_spin4win import OneSpin4WinProvider as _OneSpin4WinProvider


KNOWN_ACTIVE_STATES = {5, 6, 11, 12}
KNOWN_TERMINAL_STATES = {0}


def _decode_frame(provider: _OneSpin4WinProvider, frame: dict[str, Any]) -> dict[str, Any] | None:
    preview = frame.get("payload")
    if not isinstance(preview, dict):
        return None
    if preview.get("kind") == "text":
        return provider._decode_ws_json(str(preview.get("text") or ""))
    return None


def _observed_result_states(provider: _OneSpin4WinProvider, result: GameTestResult) -> set[int]:
    states: set[int] = set()
    for attempt in result.attempts:
        path = Path(str(attempt.artifact_dir or "")) / "ws-attempt.json"
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        frames = artifact.get("frames") if isinstance(artifact, dict) else None
        if not isinstance(frames, list):
            continue
        for frame in frames:
            if not isinstance(frame, dict) or frame.get("direction") != "received":
                continue
            payload = _decode_frame(provider, frame)
            if not isinstance(payload, dict):
                continue
            try:
                message_type = int(payload.get("type"))
            except (TypeError, ValueError):
                continue
            if message_type != 3 or payload.get("st") is None:
                continue
            try:
                states.add(int(payload.get("st")))
            except (TypeError, ValueError):
                continue
    return states


def apply_d1_path_audit(
    provider: _OneSpin4WinProvider,
    result: GameTestResult,
    *,
    progress: Progress,
) -> GameTestResult:
    if result.status in {"ERROR", "CANCELADO"} or not result.run_dir:
        return result

    states = _observed_result_states(provider, result)
    active = sorted(states & KNOWN_ACTIVE_STATES)
    unknown = sorted(states - KNOWN_ACTIVE_STATES - KNOWN_TERMINAL_STATES)

    if active:
        result.discovered_modes.append(
            {
                "id": "D1_FEATURE_CONTINUATIONS",
                "kind": "CONTINUATION",
                "observed": True,
                "executable": True,
                "wire_command": "A/u2 type=1",
                "states": active,
                "coverage_required": True,
                "branch_signature": "D1:feature-state-continuation",
                "required_options": [str(value) for value in active],
                "covered_options": [
                    str(value) for value in active
                    if any(attempt.ok and attempt.terminal for attempt in result.attempts)
                ],
            }
        )

    if unknown:
        result.discovered_modes.append(
            {
                "id": "D1_UNKNOWN_RESULT_STATES",
                "kind": "UNRESOLVED_STATE",
                "observed": True,
                "executable": False,
                "coverage_required": True,
                "branch_signature": "D1:type3-st-unclassified",
                "required_options": [str(value) for value in unknown],
                "covered_options": [],
                "reason": (
                    "st no pertenece a los estados activos HAR-confirmados {5,6,11,12} "
                    "ni al terminal 0; no se inventa una transición"
                ),
            }
        )
        if result.status == "OK":
            result.status = "PARCIAL"
        message = "D1 estados type=3 sin contrato: " + ", ".join(map(str, unknown)) + "."
        if message not in str(result.error or ""):
            result.error = (str(result.error or "").strip() + " " + message).strip()
        progress(message)

    try:
        Path(result.run_dir, "result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return result


class OneSpin4WinProvider(_OneSpin4WinProvider):
    """D1 executor that refuses OK for an unclassified result state."""

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
        return apply_d1_path_audit(self, result, progress=progress)


__all__ = ["OneSpin4WinProvider", "apply_d1_path_audit"]
