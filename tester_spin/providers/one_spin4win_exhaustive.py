from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.one_spin4win import OneSpin4WinProvider as _OneSpin4WinProvider
from tester_spin.providers.one_spin4win_client_evidence import build_client_action_evidence


KNOWN_ACTIVE_STATES = {5, 6, 11, 12}
KNOWN_TERMINAL_STATES = {0}


def _decode_frame(provider: _OneSpin4WinProvider, frame: dict[str, Any]) -> dict[str, Any] | None:
    preview = frame.get("payload")
    if not isinstance(preview, dict):
        return None
    if preview.get("kind") == "text":
        return provider._decode_ws_json(str(preview.get("text") or ""))
    return None


def _load_attempt_artifact(attempt) -> dict[str, Any]:
    path = Path(str(attempt.artifact_dir or "")) / "ws-attempt.json"
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return artifact if isinstance(artifact, dict) else {}


def _safe_script_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value or ""))
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _runtime_script_urls(result: GameTestResult) -> list[str]:
    for attempt in result.attempts:
        artifact_dir = Path(str(attempt.artifact_dir or ""))
        path = artifact_dir / "runtime-spec.json"
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw_urls = spec.get("scripts_scanned") if isinstance(spec, dict) else None
        if not isinstance(raw_urls, list):
            continue
        urls: list[str] = []
        for raw in raw_urls:
            value = str(raw or "").strip()
            if value and value not in urls:
                urls.append(value)
        if urls:
            return urls
    return []


def attach_d1_client_action_evidence(
    provider: _OneSpin4WinProvider,
    result: GameTestResult,
    *,
    timeout_s: float,
    progress: Progress,
) -> GameTestResult:
    """Attach compact structural evidence from the already-discovered D1 assets.

    This deliberately does not construct ``action_inventory``.  It only records
    which ``gameController.<method>()`` calls are present in the official client,
    with source hashes, so inventory closure can be decided later from evidence
    instead of from repeated spins or game-specific assumptions.
    """

    if result.status in {"ERROR", "CANCELADO"} or not result.run_dir:
        return result

    urls = _runtime_script_urls(result)
    sources: list[tuple[str, str]] = []
    failures: list[dict[str, str]] = []
    session = provider._worker_session()
    request_timeout = min(20.0, max(2.0, float(timeout_s)))

    for url in urls:
        stored_url = _safe_script_url(url)
        try:
            response = session.get(url, timeout=request_timeout, allow_redirects=True)
            response.raise_for_status()
            if len(response.content) > 8 * 1024 * 1024:
                failures.append({"url": stored_url, "error": "asset_too_large"})
                continue
            sources.append((stored_url, response.text or ""))
        except Exception as exc:
            failures.append(
                {
                    "url": stored_url,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    evidence = build_client_action_evidence(sources)
    evidence["requested_script_count"] = len(urls)
    evidence["fetched_script_count"] = len(sources)
    evidence["fetch_failures"] = failures
    evidence["complete"] = bool(urls) and len(sources) == len(urls) and not failures

    target = Path(result.run_dir) / "client-action-evidence.json"
    try:
        target.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        structural = dict(result.structural_map or {})
        structural["client_action_evidence"] = {
            "schema": evidence["schema"],
            "artifact": target.name,
            "complete": evidence["complete"],
            "requested_script_count": evidence["requested_script_count"],
            "fetched_script_count": evidence["fetched_script_count"],
            "aggregate": evidence["aggregate"],
        }
        result.structural_map = structural
        progress(
            "D1 evidencia de cliente: métodos gameController="
            + repr(evidence["aggregate"]["game_controller_methods"])
            + f"; assets={len(sources)}/{len(urls)}; cierre de inventario aún no inferido."
        )
    except Exception as exc:
        progress(f"D1 evidencia de cliente no persistida: {type(exc).__name__}: {exc}")
    return result


def _observed_result_states(provider: _OneSpin4WinProvider, result: GameTestResult) -> set[int]:
    states: set[int] = set()
    for attempt in result.attempts:
        artifact = _load_attempt_artifact(attempt)
        frames = artifact.get("frames")
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


def _proven_terminal_result_states(
    provider: _OneSpin4WinProvider,
    result: GameTestResult,
) -> set[int]:
    """Return type=3 states proven terminal by the executor artifact itself.

    A state is not promoted merely because it was observed.  The same attempt
    must have completed successfully, its artifact must declare terminal=true,
    and final_result must name that type=3 state.  We also require the exact
    state to be present in a received frame so a stale or fabricated summary
    cannot close the audit on its own.
    """

    proven: set[int] = set()
    for attempt in result.attempts:
        if not (attempt.ok and attempt.terminal):
            continue
        artifact = _load_attempt_artifact(attempt)
        if artifact.get("terminal") is not True:
            continue
        final_result = artifact.get("final_result")
        if not isinstance(final_result, dict):
            continue
        try:
            message_type = int(final_result.get("type"))
            state = int(final_result.get("st"))
        except (TypeError, ValueError):
            continue
        if message_type != 3:
            continue

        observed_in_frames = False
        frames = artifact.get("frames")
        if isinstance(frames, list):
            for frame in frames:
                if not isinstance(frame, dict) or frame.get("direction") != "received":
                    continue
                payload = _decode_frame(provider, frame)
                if not isinstance(payload, dict):
                    continue
                try:
                    frame_type = int(payload.get("type"))
                    frame_state = int(payload.get("st"))
                except (TypeError, ValueError):
                    continue
                if frame_type == 3 and frame_state == state:
                    observed_in_frames = True
                    break
        if observed_in_frames:
            proven.add(state)
    return proven


def apply_d1_path_audit(
    provider: _OneSpin4WinProvider,
    result: GameTestResult,
    *,
    progress: Progress,
) -> GameTestResult:
    if result.status in {"ERROR", "CANCELADO"} or not result.run_dir:
        return result

    states = _observed_result_states(provider, result)
    proven_terminal = _proven_terminal_result_states(provider, result)
    active = sorted(states & KNOWN_ACTIVE_STATES)
    terminal = sorted((states & KNOWN_TERMINAL_STATES) | proven_terminal)
    unknown = sorted(states - KNOWN_ACTIVE_STATES - KNOWN_TERMINAL_STATES - proven_terminal)

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
                    "st no pertenece a los estados activos HAR-confirmados {5,6,11,12}, "
                    "al terminal conocido 0, ni fue demostrado como final_result terminal "
                    "por una ejecución remota exitosa; no se inventa una transición"
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
        Path(result.run_dir, "d1-state-coverage.json").write_text(
            json.dumps(
                {
                    "observed_states": sorted(states),
                    "active_states": active,
                    "terminal_states": terminal,
                    "evidence_backed_terminal_states": sorted(proven_terminal),
                    "unknown_states": unknown,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
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
        result = attach_d1_client_action_evidence(
            self,
            result,
            timeout_s=timeout_s,
            progress=progress,
        )
        return apply_d1_path_audit(self, result, progress=progress)


__all__ = [
    "OneSpin4WinProvider",
    "apply_d1_path_audit",
    "attach_d1_client_action_evidence",
]
