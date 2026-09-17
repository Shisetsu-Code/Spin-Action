from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tester_spin.feature_sessions import (
    finalize_feature_session_report,
    make_feature_choice,
    make_feature_session,
)
from tester_spin.models import GameTestResult, SpinAttempt

_KNOWN_BASE_NEXT = {"toPaid", "toIdle", ""}


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _gs(path: Path) -> dict[str, Any]:
    payload = _load(path)
    value = payload.get("gs") if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def _double_choice(result: GameTestResult) -> dict[str, Any] | None:
    for mode in result.discovered_modes:
        if not isinstance(mode, dict):
            continue
        if str(mode.get("id") or "") != "BELATRA_DOUBLE_DIALOG":
            continue
        required = mode.get("required_options")
        covered = mode.get("covered_options")
        required_list = list(required) if isinstance(required, list) else []
        return make_feature_choice(
            command="toDoubleDialog",
            prefix=[],
            required_options=required_list or ["DECLINE", "GAMBLE"],
            covered_options=covered if isinstance(covered, list) else [],
            domain_state="PROVEN",
            source="belatra-phaseNext+exhaustive-coverage",
            evidence=str(mode.get("branch_signature") or "BELATRA:toDoubleDialog"),
            required_samples=mode.get("required_samples", 1),
            sample_counts=(
                mode.get("sample_counts")
                if isinstance(mode.get("sample_counts"), dict)
                else None
            ),
        )
    return None


def _attempt_session(result: GameTestResult, attempt: SpinAttempt) -> dict[str, Any] | None:
    if "VARIANT" in str(attempt.mode_kind or "").upper():
        return None
    root = Path(str(attempt.artifact_dir or ""))
    if not root.is_dir():
        return None
    start = _gs(root / "start.response.json")
    if not start:
        return None
    phase_cur = str(start.get("phaseCur") or "")
    phase_next = str(start.get("phaseNext") or "")
    finish = _gs(root / "finish.response.json")
    terminal = bool(
        attempt.ok
        and attempt.terminal
        and str(finish.get("phaseCur") or "") == "finished"
        and str(finish.get("phaseNext") or "") == "toIdle"
    )

    if phase_next in _KNOWN_BASE_NEXT and phase_next != "toDoubleDialog":
        return None

    choices = []
    reasons: list[str] = []
    round_classification_complete = True
    if phase_next == "toDoubleDialog":
        choice = _double_choice(result)
        if choice is None:
            choice = make_feature_choice(
                command="toDoubleDialog",
                required_options=["DECLINE", "GAMBLE"],
                covered_options=[],
                domain_state="PROVEN",
                source="belatra-phaseNext",
                evidence="start.response.json",
            )
        choices.append(choice)
    else:
        round_classification_complete = False
        reasons.append(
            f"Belatra phaseNext={phase_next or '<empty>'} has no proven feature-round wire contract"
        )

    if not terminal:
        reasons.append(
            "Belatra feature/state path did not prove finished -> toIdle terminal return"
        )

    return make_feature_session(
        session_id=f"{attempt.mode_id}:{attempt.number}",
        trigger=(
            "PURCHASE"
            if str(attempt.mode_kind or "").upper() == "PURCHASE"
            else "NATURAL"
        ),
        parent_mode=str(attempt.mode_id or "SPIN"),
        attempt_number=int(attempt.number or 1),
        artifact_dir=str(attempt.artifact_dir or ""),
        entry={
            "command": "start",
            "phase_cur": phase_cur,
            "phase_next": phase_next,
            "history_id": start.get("historyId"),
            "evidence": "start.response.json",
        },
        rounds=[],
        choices=choices,
        transitions=[
            {
                "provider_action": phase_next,
                "phase_cur": phase_cur,
                "evidence": "start.response.json",
            }
        ],
        terminal_proven=terminal,
        returned_to_base=terminal,
        wire_steps=int(attempt.wire_steps or (2 if finish else 1)),
        round_classification_complete=round_classification_complete,
        reasons=reasons,
    )


def build_belatra_feature_sessions(result: GameTestResult) -> dict[str, Any]:
    sessions = []
    for attempt in result.attempts:
        session = _attempt_session(result, attempt)
        if session is not None:
            sessions.append(session)
    return finalize_feature_session_report(
        result,
        sessions=sessions,
        authority="belatra-phase-state+choice-coverage",
    )


__all__ = ["build_belatra_feature_sessions"]
