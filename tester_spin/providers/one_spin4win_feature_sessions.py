from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tester_spin.feature_sessions import (
    finalize_feature_session_report,
    make_feature_round,
    make_feature_session,
)
from tester_spin.models import GameTestResult, SpinAttempt

_ACTIVE_STATES = {5, 6, 11, 12}
_TERMINAL_STATES = {0}


def _decode_payload(frame: dict[str, Any]) -> dict[str, Any] | None:
    preview = frame.get("payload")
    if not isinstance(preview, dict) or preview.get("kind") != "text":
        return None
    text = str(preview.get("text") or "").strip()
    if not text:
        return None
    candidates = [text]
    starts = [index for index, char in enumerate(text[:64]) if char in "[{"]
    candidates.extend(text[index:] for index in starts if index > 0)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _load_artifact(attempt: SpinAttempt) -> dict[str, Any]:
    path = Path(str(attempt.artifact_dir or "")) / "ws-attempt.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _play_result_pairs(frames: list[Any]) -> list[tuple[dict[str, Any], dict[str, Any], int]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    pending: dict[str, Any] | None = None
    sent_index = 0
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        payload = _decode_payload(frame)
        if not isinstance(payload, dict):
            continue
        direction = str(frame.get("direction") or "")
        try:
            message_type = int(payload.get("type"))
        except (TypeError, ValueError):
            continue
        if direction == "sent" and message_type == 1:
            pending = payload
            sent_index += 1
            continue
        if direction != "received" or message_type != 3 or pending is None:
            continue
        pairs.append((pending, payload, sent_index))
        pending = None
    return pairs


def _attempt_session(result: GameTestResult, attempt: SpinAttempt) -> dict[str, Any] | None:
    artifact = _load_artifact(attempt)
    frames = artifact.get("frames")
    if not isinstance(frames, list):
        return None
    pairs = _play_result_pairs(frames)
    if not pairs:
        return None

    root_result = pairs[0][1]
    try:
        root_state = int(root_result.get("st"))
    except (TypeError, ValueError):
        return None
    if root_state not in _ACTIVE_STATES:
        return None

    rounds = []
    reasons: list[str] = []
    known = True
    final_state = root_state
    for ordinal, (_request, response, wire_step) in enumerate(pairs[1:], start=1):
        try:
            state = int(response.get("st"))
        except (TypeError, ValueError):
            known = False
            reasons.append("D1 continuation result has no integer st state")
            state = None
        if state is not None and state not in _ACTIVE_STATES and state not in _TERMINAL_STATES:
            known = False
            reasons.append(f"D1 continuation result state {state} is unclassified")
        rounds.append(
            make_feature_round(
                ordinal,
                provider_action="A/u2 type=1",
                wire_step=wire_step,
                source="d1-websocket-type3-result",
                provider_state={"type": 3, "st": state},
                stake_charged=False,
                evidence=f"{attempt.mode_id}/attempt-{attempt.number}:frame-pair-{wire_step}",
            )
        )
        if state is not None:
            final_state = state

    artifact_terminal = artifact.get("terminal") is True
    terminal_state = final_state in _TERMINAL_STATES
    terminal = bool(attempt.terminal and artifact_terminal and terminal_state)
    returned_to_base = terminal_state
    if not terminal:
        reasons.append(f"D1 feature terminal state is not proven (last st={final_state})")

    return make_feature_session(
        session_id=f"{attempt.mode_id}:{attempt.number}",
        trigger="NATURAL",
        parent_mode=str(attempt.mode_id or "SPIN"),
        attempt_number=int(attempt.number or 1),
        artifact_dir=str(attempt.artifact_dir or ""),
        entry={
            "command": "A/u2 type=1",
            "result_state": root_state,
            "source": "d1-websocket-type3-result",
        },
        rounds=rounds,
        choices=[],
        transitions=[],
        terminal_proven=terminal,
        returned_to_base=returned_to_base,
        wire_steps=int(attempt.wire_steps or artifact.get("wire_steps") or len(pairs)),
        round_classification_complete=known,
        reasons=reasons,
    )


def build_one_spin4win_feature_sessions(result: GameTestResult) -> dict[str, Any]:
    sessions = []
    for attempt in result.attempts:
        session = _attempt_session(result, attempt)
        if session is not None:
            sessions.append(session)
    return finalize_feature_session_report(
        result,
        sessions=sessions,
        authority="d1-websocket-type1-type3-state-machine",
    )


__all__ = ["build_one_spin4win_feature_sessions"]
