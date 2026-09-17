from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from tester_spin.feature_sessions import (
    finalize_feature_session_report,
    make_feature_choice,
    make_feature_round,
    make_feature_session,
)
from tester_spin.models import GameTestResult, SpinAttempt

_STEP_RE = re.compile(r"^step-(\d+)-(.+)\.request\.txt$")
_KNOWN_TRANSITIONS = {
    "bonus",
    "collect-bonus",
    "collect",
    "mystery-scatter",
}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _load_request(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return {str(key): str(value) for key, value in parse_qsl(text, keep_blank_values=True)}


def _wire_rows(attempt: SpinAttempt) -> list[dict[str, Any]]:
    root = Path(str(attempt.artifact_dir or ""))
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.glob("step-*.request.txt"):
        match = _STEP_RE.fullmatch(path.name)
        if not match:
            continue
        step = int(match.group(1))
        label = match.group(2)
        response_path = root / f"step-{step:03d}-{label}.response.json"
        rows.append(
            {
                "wire_step": step + 1,
                "label": label,
                "request_path": path,
                "request": _load_request(path),
                "response": _load_json(response_path),
            }
        )
    return sorted(rows, key=lambda row: int(row["wire_step"]))


def _choices_for_mode(result: GameTestResult, parent_mode: str) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    for mode in result.discovered_modes:
        if not isinstance(mode, dict):
            continue
        if str(mode.get("kind") or "").upper() != "FSO_BRANCH":
            continue
        if str(mode.get("parent") or "") != parent_mode:
            continue
        required = mode.get("required_options")
        covered = mode.get("covered_options")
        required_list = list(required) if isinstance(required, list) else []
        domain_state = (
            "PROVEN"
            if required_list and "DOMAIN_UNRESOLVED" not in required_list
            else "UNRESOLVED"
        )
        choices.append(
            make_feature_choice(
                command="doFSOption",
                prefix=mode.get("prefix") if isinstance(mode.get("prefix"), list) else [],
                required_options=required_list or ["DOMAIN_UNRESOLVED"],
                covered_options=covered if isinstance(covered, list) else [],
                domain_state=domain_state,
                source="pragmatic-fs_opt+exhaustive-prefix-coverage",
                evidence=str(mode.get("branch_signature") or mode.get("id") or ""),
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
    rows = _wire_rows(attempt)
    if not rows:
        return None
    rounds: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    unknown_labels: list[str] = []

    for row in rows[1:]:
        label = str(row.get("label") or "")
        request = row.get("request") if isinstance(row.get("request"), dict) else {}
        response = row.get("response") if isinstance(row.get("response"), dict) else {}
        if label == "continuation-spin":
            rounds.append(
                make_feature_round(
                    len(rounds) + 1,
                    provider_action="doSpin",
                    wire_step=int(row.get("wire_step") or 0),
                    source="pragmatic-gameService-response",
                    provider_state={
                        "na": response.get("na"),
                        "fs": response.get("fs"),
                        "rs": response.get("rs"),
                    },
                    stake_charged=False,
                    evidence=str(Path(row["request_path"]).name),
                )
            )
            continue
        if label.startswith("fs-option-"):
            transitions.append(
                {
                    "provider_action": "doFSOption",
                    "wire_step": int(row.get("wire_step") or 0),
                    "selected": request.get("ind"),
                    "evidence": str(Path(row["request_path"]).name),
                }
            )
            continue
        if label in _KNOWN_TRANSITIONS:
            transitions.append(
                {
                    "provider_action": str(request.get("action") or label),
                    "wire_step": int(row.get("wire_step") or 0),
                    "evidence": str(Path(row["request_path"]).name),
                }
            )
            continue
        if label != "entry":
            unknown_labels.append(label)

    parent_mode = str(attempt.mode_id or "UNKNOWN")
    choices = _choices_for_mode(result, parent_mode)
    labels = {str(row.get("label") or "") for row in rows}
    feature_observed = bool(
        rounds
        or choices
        or labels.intersection(
            {"bonus", "collect-bonus", "mystery-scatter"}
        )
        or any(label.startswith("fs-option-") for label in labels)
    )
    if not feature_observed:
        return None

    entry = rows[0]
    entry_request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    entry_response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
    reasons: list[str] = []
    if unknown_labels:
        reasons.append(
            "Pragmatic wire labels inside feature are unclassified: "
            + ", ".join(sorted(set(unknown_labels)))
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
            "command": str(entry_request.get("action") or "doSpin"),
            "provider_pur": entry_request.get("pur"),
            "na": entry_response.get("na"),
            "evidence": str(Path(entry["request_path"]).name),
        },
        rounds=rounds,
        choices=choices,
        transitions=transitions,
        terminal_proven=terminal,
        returned_to_base=terminal,
        wire_steps=int(attempt.wire_steps or len(rows)),
        round_classification_complete=not unknown_labels,
        reasons=reasons,
    )


def build_pragmatic_feature_sessions(result: GameTestResult) -> dict[str, Any]:
    sessions = []
    for attempt in result.attempts:
        session = _attempt_session(result, attempt)
        if session is not None:
            sessions.append(session)
    return finalize_feature_session_report(
        result,
        sessions=sessions,
        authority="pragmatic-gameService-feature-state+FSO-branches",
    )


__all__ = ["build_pragmatic_feature_sessions"]
