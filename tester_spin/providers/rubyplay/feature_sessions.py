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

_ROUND_ACTIONS = {"freespin", "respin", "minispin"}
_CHOICE_ACTIONS = {"select", "pick"}
_STEP_REQUEST_RE = re.compile(r"^step-(\d+)-request\.json$")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _next_action(response: dict[str, Any]) -> str:
    body = response.get("data") if isinstance(response, dict) else None
    if not isinstance(body, dict):
        return ""
    return str(body.get("next_action") or "").strip().lower()


def _wire_rows(attempt: SpinAttempt) -> list[dict[str, Any]]:
    root = Path(str(attempt.artifact_dir or ""))
    if not root.is_dir():
        return []

    rows: list[dict[str, Any]] = []
    entry_request = root / "request.json"
    entry_response = root / "response.json"
    if entry_request.is_file():
        rows.append(
            {
                "wire_step": 1,
                "request_path": entry_request,
                "response_path": entry_response,
                "request": _load_json(entry_request),
                "response": _load_json(entry_response),
            }
        )

    numbered: list[tuple[int, Path]] = []
    for path in root.glob("step-*-request.json"):
        match = _STEP_REQUEST_RE.fullmatch(path.name)
        if match:
            numbered.append((int(match.group(1)), path))
    for step, request_path in sorted(numbered):
        response_path = request_path.with_name(f"step-{step:03d}-response.json")
        rows.append(
            {
                "wire_step": step,
                "request_path": request_path,
                "response_path": response_path,
                "request": _load_json(request_path),
                "response": _load_json(response_path),
            }
        )
    return rows


def _relative_evidence(result: GameTestResult, path: Path) -> str:
    if not result.run_dir:
        return str(path)
    try:
        return str(path.resolve().relative_to(Path(result.run_dir).resolve()))
    except (OSError, ValueError):
        return str(path)


def _choice_contract(
    result: GameTestResult,
    *,
    parent_mode: str,
    command: str,
    prefix: list[str],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for mode in result.discovered_modes:
        if not isinstance(mode, dict):
            continue
        if str(mode.get("kind") or "").upper() != "INDEXED_CHOICE":
            continue
        if str(mode.get("parent") or "") != parent_mode:
            continue
        if str(mode.get("wire_command") or "").strip().lower() != command:
            continue
        candidates.append(mode)

    exact = [
        mode
        for mode in candidates
        if isinstance(mode.get("prefix"), list)
        and [str(value) for value in mode.get("prefix") or []] == prefix
    ]
    if len(exact) == 1:
        return exact[0]
    no_prefix = [mode for mode in candidates if mode.get("prefix") is None]
    if len(candidates) == 1 and len(no_prefix) == 1:
        return no_prefix[0]
    return None


def _choice_from_row(
    result: GameTestResult,
    attempt: SpinAttempt,
    row: dict[str, Any],
    *,
    prefix: list[str],
) -> dict[str, Any]:
    request = row.get("request") if isinstance(row.get("request"), dict) else {}
    command = str(request.get("action") or "").strip().lower()
    raw_index = request.get("index")
    selected = ""
    if not isinstance(raw_index, bool) and raw_index is not None:
        selected = str(raw_index).strip()

    contract = _choice_contract(
        result,
        parent_mode=str(attempt.mode_id or ""),
        command=command,
        prefix=prefix,
    )
    evidence = _relative_evidence(result, Path(row["request_path"]))
    if contract is None:
        return make_feature_choice(
            command=command,
            prefix=prefix,
            required_options=["DOMAIN_UNRESOLVED"],
            covered_options=[selected] if selected else [],
            selected=selected,
            domain_state="UNRESOLVED",
            source="rubyplay-runtime-index",
            evidence=evidence,
        )

    required = contract.get("required_options")
    covered = contract.get("covered_options")
    domain_state = (
        "UNRESOLVED"
        if not isinstance(required, list) or not required or "DOMAIN_UNRESOLVED" in required
        else "PROVEN"
    )
    return make_feature_choice(
        command=command,
        prefix=prefix,
        required_options=required if isinstance(required, list) else ["DOMAIN_UNRESOLVED"],
        covered_options=covered if isinstance(covered, list) else [],
        selected=selected,
        domain_state=domain_state,
        source="rubyplay-parent-scoped-index-domain",
        evidence=evidence,
        required_samples=contract.get("required_samples", 1),
        sample_counts=(
            contract.get("sample_counts")
            if isinstance(contract.get("sample_counts"), dict)
            else None
        ),
    )


def _attempt_session(
    result: GameTestResult,
    attempt: SpinAttempt,
) -> dict[str, Any] | None:
    rows = _wire_rows(attempt)
    if not rows:
        return None

    entry = rows[0]
    entry_request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    entry_response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
    entry_action = str(entry_request.get("action") or "").strip().lower()

    rounds: list[dict[str, Any]] = []
    choices: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    unknown_actions: list[str] = []
    prefix: list[str] = []
    pick_domain_recorded = False

    for row in rows[1:]:
        request = row.get("request") if isinstance(row.get("request"), dict) else {}
        response = row.get("response") if isinstance(row.get("response"), dict) else {}
        action = str(request.get("action") or "").strip().lower()
        evidence = _relative_evidence(result, Path(row["request_path"]))
        if action in _ROUND_ACTIONS:
            rounds.append(
                make_feature_round(
                    len(rounds) + 1,
                    provider_action=action,
                    wire_step=int(row.get("wire_step") or 0),
                    source="rubyplay-gameserver-response",
                    provider_state={"next_action": _next_action(response)},
                    stake_charged=False,
                    evidence=evidence,
                )
            )
            continue
        if action in _CHOICE_ACTIONS:
            selected_raw = request.get("index")
            selected = (
                str(selected_raw).strip()
                if selected_raw is not None and not isinstance(selected_raw, bool)
                else ""
            )
            choice_prefix = [] if action == "pick" else list(prefix)
            if action != "pick" or not pick_domain_recorded:
                choice = _choice_from_row(
                    result,
                    attempt,
                    row,
                    prefix=choice_prefix,
                )
                choices.append(choice)
                if action == "pick":
                    pick_domain_recorded = True
            transitions.append(
                {
                    "wire_step": int(row.get("wire_step") or 0),
                    "provider_action": action,
                    "selected": selected,
                    "next_action": _next_action(response),
                    "evidence": evidence,
                }
            )
            if selected:
                prefix.append(f"{action}={selected}")
            continue
        if action:
            transitions.append(
                {
                    "wire_step": int(row.get("wire_step") or 0),
                    "provider_action": action,
                    "next_action": _next_action(response),
                    "evidence": evidence,
                }
            )
            unknown_actions.append(action)

    entry_next = _next_action(entry_response)
    unknown_entry_state = bool(
        entry_next
        and entry_next != "spin"
        and entry_next not in (_ROUND_ACTIONS | _CHOICE_ACTIONS)
    )
    feature_observed = bool(
        rounds
        or choices
        or len(rows) > 1
        or (entry_next and entry_next != "spin")
    )
    if not feature_observed:
        return None

    final_response = rows[-1].get("response") if isinstance(rows[-1].get("response"), dict) else {}
    final_next = _next_action(final_response)
    returned_to_base = bool(
        final_next == "spin" or str(attempt.na or "").strip().lower() == "spin"
    )
    terminal = bool(attempt.terminal and returned_to_base)
    trigger = "PURCHASE" if str(attempt.mode_kind or "").upper() == "PURCHASE" else "NATURAL"
    reasons = []
    if unknown_entry_state:
        reasons.append(
            f"RubyPlay entry next_action={entry_next!r} has no classified round/choice contract"
        )
    if unknown_actions:
        reasons.append(
            "RubyPlay continuation actions are not classified as round/choice: "
            + ", ".join(sorted(set(unknown_actions)))
        )
    if attempt.warning:
        reasons.append(str(attempt.warning))

    return make_feature_session(
        session_id=f"{attempt.mode_id}:{attempt.number}",
        trigger=trigger,
        parent_mode=str(attempt.mode_id or "UNKNOWN"),
        attempt_number=int(attempt.number or 1),
        artifact_dir=str(attempt.artifact_dir or ""),
        entry={
            "command": entry_action,
            "next_action": entry_next,
            "evidence": _relative_evidence(result, Path(entry["request_path"])),
        },
        rounds=rounds,
        choices=choices,
        transitions=transitions,
        terminal_proven=terminal,
        returned_to_base=returned_to_base,
        wire_steps=int(attempt.wire_steps or len(rows)),
        round_classification_complete=(not unknown_entry_state and not unknown_actions),
        reasons=reasons,
    )


def build_rubyplay_feature_sessions(result: GameTestResult) -> dict[str, Any]:
    sessions = []
    for attempt in result.attempts:
        session = _attempt_session(result, attempt)
        if session is not None:
            sessions.append(session)
    return finalize_feature_session_report(
        result,
        sessions=sessions,
        authority="rubyplay-next_action+runtime-wire+parent-scoped-choice-domain",
    )


__all__ = ["build_rubyplay_feature_sessions"]
