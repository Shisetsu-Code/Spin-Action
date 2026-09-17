from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from tester_spin.models import GameTestResult

SCHEMA = "tester-spin/feature-sessions/v1"
FEATURE_COMPLETE = "COMPLETE"
FEATURE_INCOMPLETE = "INCOMPLETE"
FEATURE_UNKNOWN = "UNKNOWN"
FEATURE_NOT_OBSERVED = "NOT_OBSERVED"

_VALID_FEATURE_STATES = {FEATURE_COMPLETE, FEATURE_INCOMPLETE, FEATURE_UNKNOWN}
_VALID_DOMAIN_STATES = {"PROVEN", "UNRESOLVED", "CONTRADICTED"}


def _clean(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _options(values: Iterable[Any] | None) -> list[str]:
    found: list[str] = []
    if values is None:
        return found
    for value in values:
        clean = _clean(value)
        if clean and clean not in found:
            found.append(clean)
    return found


def make_feature_round(
    ordinal: int,
    *,
    provider_action: str = "",
    wire_step: int | None = None,
    source: str = "",
    provider_state: Any = None,
    win: Any = None,
    balance_before: Any = None,
    balance_after: Any = None,
    stake_charged: bool | None = None,
    evidence: Any = None,
) -> dict[str, Any]:
    try:
        number = max(1, int(ordinal))
    except (TypeError, ValueError):
        number = 1
    step = None
    if wire_step is not None and not isinstance(wire_step, bool):
        try:
            step = max(0, int(wire_step))
        except (TypeError, ValueError):
            step = None
    return {
        "ordinal": number,
        "provider_action": str(provider_action or ""),
        "wire_step": step,
        "source": str(source or ""),
        "provider_state": provider_state,
        "win": win,
        "balance_before": balance_before,
        "balance_after": balance_after,
        "stake_charged": stake_charged if isinstance(stake_charged, bool) else None,
        "evidence": evidence,
    }


def make_feature_choice(
    *,
    command: str,
    prefix: Iterable[Any] | None = None,
    required_options: Iterable[Any] | None = None,
    covered_options: Iterable[Any] | None = None,
    selected: Any = None,
    domain_state: str = "",
    source: str = "",
    evidence: Any = None,
    required_samples: int = 1,
    sample_counts: dict[Any, Any] | None = None,
) -> dict[str, Any]:
    required = _options(required_options)
    covered = _options(covered_options)
    selected_value = _clean(selected)
    if selected_value and selected_value not in covered:
        covered.append(selected_value)

    state = str(domain_state or "").strip().upper()
    if state not in _VALID_DOMAIN_STATES:
        state = (
            "UNRESOLVED"
            if not required or "DOMAIN_UNRESOLVED" in required
            else "PROVEN"
        )

    try:
        target = max(1, int(required_samples))
    except (TypeError, ValueError):
        target = 1

    counts: dict[str, int] = {}
    if isinstance(sample_counts, dict):
        for key, value in sample_counts.items():
            clean_key = _clean(key)
            if not clean_key:
                continue
            try:
                counts[clean_key] = max(0, int(value))
            except (TypeError, ValueError):
                continue

    missing = [value for value in required if value not in covered]
    sample_deficits: dict[str, int] = {}
    if counts:
        sample_deficits = {
            value: max(0, target - int(counts.get(value, 0)))
            for value in required
        }
        missing = list(
            dict.fromkeys(
                missing
                + [value for value, deficit in sample_deficits.items() if deficit > 0]
            )
        )

    complete = bool(
        state == "PROVEN"
        and required
        and "DOMAIN_UNRESOLVED" not in required
        and not missing
    )
    return {
        "command": str(command or ""),
        "prefix": _options(prefix),
        "required_options": required,
        "covered_options": covered,
        "selected": selected_value,
        "domain_state": state,
        "source": str(source or ""),
        "evidence": evidence,
        "required_samples": target,
        "sample_counts": counts,
        "sample_deficits": sample_deficits,
        "missing_options": missing,
        "complete": complete,
    }


def make_feature_session(
    *,
    session_id: str,
    trigger: str,
    parent_mode: str,
    attempt_number: int,
    entry: dict[str, Any] | None = None,
    rounds: Iterable[dict[str, Any]] | None = None,
    choices: Iterable[dict[str, Any]] | None = None,
    transitions: Iterable[dict[str, Any]] | None = None,
    terminal_proven: bool = False,
    returned_to_base: bool = False,
    wire_steps: int = 0,
    round_classification_complete: bool = True,
    evidence_state: str = "",
    artifact_dir: str = "",
    reasons: Iterable[str] | None = None,
) -> dict[str, Any]:
    normalized_rounds = [dict(item) for item in (rounds or []) if isinstance(item, dict)]
    normalized_choices = [dict(item) for item in (choices or []) if isinstance(item, dict)]
    normalized_transitions = [
        dict(item) for item in (transitions or []) if isinstance(item, dict)
    ]
    blockers = [str(item).strip() for item in (reasons or []) if str(item).strip()]

    choice_complete = all(item.get("complete") is True for item in normalized_choices)
    if normalized_choices and not choice_complete:
        blockers.append("feature choice coverage is incomplete")
    if not bool(round_classification_complete):
        blockers.append("feature round classification is incomplete")
    if not (bool(terminal_proven) and bool(returned_to_base)):
        blockers.append("feature terminal/base return is not proven")
    blockers = list(dict.fromkeys(blockers))

    explicit_state = str(evidence_state or "").strip().upper()
    if explicit_state == FEATURE_UNKNOWN:
        state = FEATURE_UNKNOWN
    elif explicit_state in {FEATURE_COMPLETE, FEATURE_INCOMPLETE}:
        state = explicit_state
    else:
        state = FEATURE_INCOMPLETE if blockers else FEATURE_COMPLETE

    try:
        attempt = max(1, int(attempt_number))
    except (TypeError, ValueError):
        attempt = 1
    try:
        steps = max(0, int(wire_steps))
    except (TypeError, ValueError):
        steps = 0

    return {
        "session_id": str(session_id or f"{parent_mode}:{attempt}"),
        "trigger": str(trigger or "UNKNOWN").strip().upper(),
        "parent_mode": str(parent_mode or "UNKNOWN"),
        "attempt_number": attempt,
        "artifact_dir": str(artifact_dir or ""),
        "entry": dict(entry or {}),
        "rounds": normalized_rounds,
        "choices": normalized_choices,
        "transitions": normalized_transitions,
        "terminal": {
            "proven": bool(terminal_proven),
            "returned_to_base": bool(returned_to_base),
        },
        "totals": {
            "logical_rounds": len(normalized_rounds),
            "wire_steps": steps,
            "choices": len(normalized_choices),
        },
        "round_classification_complete": bool(round_classification_complete),
        "choice_coverage_complete": choice_complete,
        "state": state,
        "reasons": blockers,
    }


def _aggregate_state(states: list[str]) -> str:
    normalized = [str(value or "").upper() for value in states]
    if FEATURE_INCOMPLETE in normalized:
        return FEATURE_INCOMPLETE
    if FEATURE_UNKNOWN in normalized:
        return FEATURE_UNKNOWN
    if normalized and all(value == FEATURE_COMPLETE for value in normalized):
        return FEATURE_COMPLETE
    return FEATURE_UNKNOWN


def finalize_feature_session_report(
    result: GameTestResult,
    *,
    sessions: Iterable[dict[str, Any]],
    authority: str,
) -> dict[str, Any]:
    normalized = [dict(item) for item in sessions if isinstance(item, dict)]
    by_parent: dict[str, dict[str, Any]] = {}
    for session in normalized:
        parent = str(session.get("parent_mode") or "UNKNOWN")
        item = by_parent.setdefault(
            parent,
            {
                "state": FEATURE_COMPLETE,
                "session_count": 0,
                "logical_rounds": 0,
                "wire_steps": 0,
                "choices": 0,
                "session_ids": [],
                "_states": [],
            },
        )
        item["session_count"] += 1
        totals = session.get("totals") if isinstance(session.get("totals"), dict) else {}
        for key in ("logical_rounds", "wire_steps", "choices"):
            try:
                item[key] += max(0, int(totals.get(key) or 0))
            except (TypeError, ValueError):
                pass
        session_id = str(session.get("session_id") or "")
        if session_id:
            item["session_ids"].append(session_id)
        item["_states"].append(str(session.get("state") or FEATURE_UNKNOWN).upper())

    for item in by_parent.values():
        item["state"] = _aggregate_state(list(item.pop("_states", [])))

    return {
        "schema": SCHEMA,
        "provider": result.provider,
        "game": result.slug,
        "authority": str(authority or ""),
        "complete": all(
            str(session.get("state") or FEATURE_UNKNOWN).upper() == FEATURE_COMPLETE
            for session in normalized
        ),
        "session_count": len(normalized),
        "sessions": normalized,
        "by_parent_mode": by_parent,
    }


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": str(report.get("schema") or SCHEMA),
        "artifact": "feature-sessions.json",
        "complete": bool(report.get("complete")),
        "session_count": int(report.get("session_count") or 0),
        "authority": str(report.get("authority") or ""),
        "by_parent_mode": dict(report.get("by_parent_mode") or {}),
    }


def attach_feature_session_report(
    result: GameTestResult,
    report: dict[str, Any],
) -> GameTestResult:
    if not isinstance(result.structural_map, dict):
        result.structural_map = {}
    result.structural_map["feature_sessions"] = _summary(report)

    root = Path(str(result.run_dir or ""))
    if result.run_dir:
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / "feature-sessions.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
    return result


def _report_from_result(result: GameTestResult) -> dict[str, Any] | None:
    structural = result.structural_map if isinstance(result.structural_map, dict) else {}
    summary = structural.get("feature_sessions")
    if isinstance(summary, dict):
        return summary
    if not result.run_dir:
        return None
    try:
        value = json.loads(
            Path(result.run_dir, "feature-sessions.json").read_text(encoding="utf-8")
        )
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def feature_session_state_for_mode(result: GameTestResult, mode_id: str) -> str:
    report = _report_from_result(result)
    if not isinstance(report, dict):
        return FEATURE_NOT_OBSERVED
    by_parent = report.get("by_parent_mode")
    if not isinstance(by_parent, dict):
        return FEATURE_NOT_OBSERVED
    item = by_parent.get(str(mode_id or ""))
    if not isinstance(item, dict):
        return FEATURE_NOT_OBSERVED
    state = str(item.get("state") or FEATURE_UNKNOWN).upper()
    return state if state in _VALID_FEATURE_STATES else FEATURE_UNKNOWN


def enforce_complete_feature_sessions(
    result: GameTestResult,
    report: dict[str, Any],
    *,
    progress=None,
) -> GameTestResult:
    attach_feature_session_report(result, report)
    sessions = report.get("sessions") if isinstance(report, dict) else None
    incomplete = [
        item
        for item in (sessions or [])
        if isinstance(item, dict)
        and str(item.get("state") or FEATURE_UNKNOWN).upper() != FEATURE_COMPLETE
    ]
    if not incomplete:
        return result

    if str(result.status or "").upper() == "OK":
        result.status = "PARCIAL"
    if str(result.status or "").upper() not in {"ERROR", "CANCELADO", "CANCELLED"}:
        preview = ", ".join(
            f"{item.get('parent_mode') or 'UNKNOWN'}={item.get('state') or FEATURE_UNKNOWN}"
            for item in incomplete[:8]
        )
        message = f"Feature sessions pendientes: {preview}."
        if message not in str(result.error or ""):
            result.error = (str(result.error or "").strip() + " " + message).strip()
        if progress is not None:
            progress(message)
    return result


__all__ = [
    "SCHEMA",
    "FEATURE_COMPLETE",
    "FEATURE_INCOMPLETE",
    "FEATURE_UNKNOWN",
    "FEATURE_NOT_OBSERVED",
    "make_feature_round",
    "make_feature_choice",
    "make_feature_session",
    "finalize_feature_session_report",
    "attach_feature_session_report",
    "feature_session_state_for_mode",
    "enforce_complete_feature_sessions",
]
