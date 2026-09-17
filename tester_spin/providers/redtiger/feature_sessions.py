from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tester_spin.feature_sessions import (
    finalize_feature_session_report,
    make_feature_choice,
    make_feature_round,
    make_feature_session,
)
from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.redtiger.result_tree import ResultNode, result_nodes


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _leaf_nodes(payload: dict[str, Any]) -> list[ResultNode]:
    nodes = result_nodes(payload)
    leaves: list[ResultNode] = []
    for node in nodes:
        prefix_dot = node.path + "."
        prefix_list = node.path + "["
        if any(
            other.path != node.path
            and (other.path.startswith(prefix_dot) or other.path.startswith(prefix_list))
            for other in nodes
        ):
            continue
        leaves.append(node)
    return leaves


def _response_files(attempt: SpinAttempt) -> list[Path]:
    root = Path(str(attempt.artifact_dir or ""))
    if not root.is_dir():
        return []
    files = [path for path in root.rglob("response.json") if path.is_file()]
    return sorted(files, key=lambda path: str(path.relative_to(root)))


def _choices_for_mode(result: GameTestResult, parent_mode: str) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    for mode in result.discovered_modes:
        if not isinstance(mode, dict):
            continue
        if str(mode.get("kind") or "").upper() != "CHOICE_BRANCH":
            continue
        if str(mode.get("parent") or "") != parent_mode:
            continue
        required = mode.get("required_options")
        covered = mode.get("covered_options")
        required_list = list(required) if isinstance(required, list) else []
        choices.append(
            make_feature_choice(
                command=str(mode.get("wire_command") or "platform/game/choice"),
                prefix=mode.get("prefix") if isinstance(mode.get("prefix"), list) else [],
                required_options=required_list or ["DOMAIN_UNRESOLVED"],
                covered_options=covered if isinstance(covered, list) else [],
                domain_state=(
                    "PROVEN"
                    if required_list and "DOMAIN_UNRESOLVED" not in required_list
                    else "UNRESOLVED"
                ),
                source="redtiger-game.choices.available+branch-replay",
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
    if str(attempt.mode_kind or "").upper() == "CHOICE_PATH":
        return None

    response_files = _response_files(attempt)
    rounds: list[dict[str, Any]] = []
    any_feature_container = False
    for response_index, path in enumerate(response_files, start=1):
        payload = _load_json(path)
        all_nodes = result_nodes(payload)
        any_feature_container = any_feature_container or any(
            node.feature_count > 0 for node in all_nodes
        )
        for node in _leaf_nodes(payload):
            rounds.append(
                make_feature_round(
                    len(rounds) + 1,
                    provider_action=node.spin_mode or "result-state",
                    wire_step=response_index,
                    source="redtiger-result-tree-leaf",
                    provider_state={
                        "path": node.path,
                        "spin_mode": node.spin_mode,
                        "game_mode": node.game_mode,
                        "has_state": node.has_state,
                    },
                    stake_charged=False,
                    evidence=str(path.relative_to(Path(attempt.artifact_dir))),
                )
            )

    parent_mode = str(attempt.mode_id or "UNKNOWN")
    choices = _choices_for_mode(result, parent_mode)
    trigger = "PURCHASE" if str(attempt.mode_kind or "").upper() == "PURCHASE" else "NATURAL"

    feature_observed = bool(
        choices
        or len(rounds) > 1
        or any_feature_container
        or (trigger == "PURCHASE" and rounds)
    )
    if not feature_observed:
        return None

    terminal = bool(attempt.ok and attempt.terminal and not attempt.warning and not attempt.error)
    request_path = Path(str(attempt.artifact_dir or "")) / "request.json"
    entry_request = _load_json(request_path)
    return make_feature_session(
        session_id=f"{parent_mode}:{attempt.number}",
        trigger=trigger,
        parent_mode=parent_mode,
        attempt_number=int(attempt.number or 1),
        artifact_dir=str(attempt.artifact_dir or ""),
        entry={
            "command": "platform/game/spin",
            "feature_buy": (
                ((entry_request.get("extras") or {}).get("features") or {}).get("featureBuy")
                if isinstance(entry_request.get("extras"), dict)
                else None
            ),
            "evidence": "request.json",
        },
        rounds=rounds,
        choices=choices,
        transitions=[],
        terminal_proven=terminal,
        returned_to_base=terminal,
        wire_steps=int(attempt.wire_steps or max(1, len(response_files))),
        round_classification_complete=True,
        reasons=[str(attempt.warning)] if attempt.warning else [],
    )


def build_redtiger_feature_sessions(result: GameTestResult) -> dict[str, Any]:
    sessions = []
    for attempt in result.attempts:
        session = _attempt_session(result, attempt)
        if session is not None:
            sessions.append(session)
    return finalize_feature_session_report(
        result,
        sessions=sessions,
        authority="redtiger-result-tree+choice-branch-coverage",
    )


__all__ = ["build_redtiger_feature_sessions"]
