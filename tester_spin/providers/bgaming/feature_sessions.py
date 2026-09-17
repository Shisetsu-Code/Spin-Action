from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tester_spin.feature_sessions import (
    finalize_feature_session_report,
    make_feature_choice,
    make_feature_round,
    make_feature_session,
)
from tester_spin.models import GameTestResult, SpinAttempt

_ROUND_COMMANDS = {"freespin", "respin", "minispin"}
_KNOWN_TRANSITIONS = {
    "preselection_game",
    "preselection",
    "collect",
    "finish",
    "bonus",
    "pick",
    "select",
}
_STEP_RE = re.compile(r"^step-(\d+)-request\.json$")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _rows(attempt: SpinAttempt) -> list[dict[str, Any]]:
    root = Path(str(attempt.artifact_dir or ""))
    if not root.is_dir():
        return []
    rows = []
    for path in root.glob("step-*-request.json"):
        match = _STEP_RE.fullmatch(path.name)
        if not match:
            continue
        step = int(match.group(1))
        rows.append(
            {
                "step": step,
                "request_path": path,
                "request": _load_json(path),
                "response": _load_json(root / f"step-{step:03d}-response.json"),
                "proof": _load_json(root / f"step-{step:03d}-proof.json"),
            }
        )
    return sorted(rows, key=lambda row: int(row["step"]))


def _flow_state(payload: dict[str, Any]) -> str:
    flow = payload.get("flow") if isinstance(payload, dict) else None
    if not isinstance(flow, dict):
        return ""
    return str(flow.get("state") or "").strip().lower()


def _choices_for_scope(result: GameTestResult, scope: str) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    prefix = f"BGAMING:flow-choice:{scope}:"
    for mode in result.discovered_modes:
        if not isinstance(mode, dict):
            continue
        if str(mode.get("kind") or "").upper() != "CHOICE_CONTINUATION":
            continue
        signature = str(mode.get("branch_signature") or "")
        if not signature.startswith(prefix):
            continue
        required = mode.get("required_options")
        covered = mode.get("covered_options")
        required_list = list(required) if isinstance(required, list) else []
        choices.append(
            make_feature_choice(
                command=str(mode.get("wire_command") or ""),
                prefix=(
                    mode.get("path_prefix")
                    if isinstance(mode.get("path_prefix"), list)
                    else []
                ),
                required_options=required_list or ["DOMAIN_UNRESOLVED"],
                covered_options=covered if isinstance(covered, list) else [],
                domain_state=(
                    "PROVEN"
                    if required_list and "DOMAIN_UNRESOLVED" not in required_list
                    else "UNRESOLVED"
                ),
                source=str(mode.get("source") or "bgaming-flow-choice-domain"),
                evidence=signature,
                required_samples=mode.get("required_samples", 1),
                sample_counts=(
                    mode.get("sample_counts")
                    if isinstance(mode.get("sample_counts"), dict)
                    else None
                ),
            )
        )
    return choices


def _attempt_session(result: GameTestResult, attempt: SpinAttempt) -> dict[str, Any] | None:
    if "CHOICE_VARIANT" in str(attempt.mode_kind or "").upper():
        return None
    rows = _rows(attempt)
    if not rows:
        return None

    parent_mode = str(attempt.mode_id or "SPIN")
    choices = _choices_for_scope(result, parent_mode)
    choice_commands = {str(choice.get("command") or "").lower() for choice in choices}
    rounds = []
    transitions = []
    unknown: list[str] = []

    for row in rows[1:]:
        request = row.get("request") if isinstance(row.get("request"), dict) else {}
        response = row.get("response") if isinstance(row.get("response"), dict) else {}
        proof = row.get("proof") if isinstance(row.get("proof"), dict) else {}
        command = str(request.get("command") or request.get("action") or "").strip().lower()
        if command in _ROUND_COMMANDS:
            rounds.append(
                make_feature_round(
                    len(rounds) + 1,
                    provider_action=command,
                    wire_step=int(row.get("step") or 0),
                    source="bgaming-api-continuation",
                    provider_state={
                        "flow_state": _flow_state(response),
                        "round_id": proof.get("round_id"),
                        "last_action_id": proof.get("last_action_id"),
                    },
                    win=proof.get("win"),
                    stake_charged=False,
                    evidence=str(Path(row["request_path"]).name),
                )
            )
            continue
        if command in _KNOWN_TRANSITIONS or command in choice_commands:
            transitions.append(
                {
                    "provider_action": command,
                    "wire_step": int(row.get("step") or 0),
                    "flow_state": _flow_state(response),
                    "evidence": str(Path(row["request_path"]).name),
                }
            )
            continue
        if command:
            unknown.append(command)

    entry = rows[0]
    entry_request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    entry_response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
    entry_state = _flow_state(entry_response)
    feature_observed = bool(
        rounds
        or choices
        or len(rows) > 1
        or (entry_state and entry_state != "closed")
    )
    if not feature_observed:
        return None

    reasons: list[str] = []
    if unknown:
        reasons.append(
            "BGaming continuation commands are not classified as round/transition: "
            + ", ".join(sorted(set(unknown)))
        )
    if attempt.warning:
        reasons.append(str(attempt.warning))

    trigger = "PURCHASE" if str(attempt.mode_kind or "").upper() == "PURCHASE" else "NATURAL"
    terminal = bool(attempt.ok and attempt.terminal and not attempt.warning and not attempt.error)
    return make_feature_session(
        session_id=f"{parent_mode}:{attempt.number}",
        trigger=trigger,
        parent_mode=parent_mode,
        attempt_number=int(attempt.number or 1),
        artifact_dir=str(attempt.artifact_dir or ""),
        entry={
            "command": str(entry_request.get("command") or "spin"),
            "flow_state": entry_state,
            "round_id": (
                (entry_response.get("flow") or {}).get("round_id")
                if isinstance(entry_response.get("flow"), dict)
                else None
            ),
            "evidence": str(Path(entry["request_path"]).name),
        },
        rounds=rounds,
        choices=choices,
        transitions=transitions,
        terminal_proven=terminal,
        returned_to_base=terminal,
        wire_steps=int(attempt.wire_steps or len(rows)),
        round_classification_complete=not unknown,
        reasons=reasons,
    )


def build_bgaming_feature_sessions(result: GameTestResult) -> dict[str, Any]:
    sessions = []
    for attempt in result.attempts:
        session = _attempt_session(result, attempt)
        if session is not None:
            sessions.append(session)
    return finalize_feature_session_report(
        result,
        sessions=sessions,
        authority="bgaming-flow-state+continuation-wire+choice-graph",
    )


__all__ = ["build_bgaming_feature_sessions"]
