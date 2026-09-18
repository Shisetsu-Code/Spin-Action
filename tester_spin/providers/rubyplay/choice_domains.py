from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any, Iterable

import requests

_PROVEN = "PROVEN"
_UNRESOLVED = "UNRESOLVED"
_TERMINAL = "TERMINAL"
_SEMANTIC_REJECTION = "SEMANTIC_REJECTION"
_TRANSPORT_ERROR = "TRANSPORT_ERROR"
_PROTOCOL_ERROR = "PROTOCOL_ERROR"
_INDEX_ARGUMENT_NOUN = r"(?:index|choice|option|selection)"
_INDEX_ARGUMENT_ERROR_PATTERNS = (
    re.compile(rf"\\b(?:invalid|unknown|unsupported|bad|illegal)\\s+{_INDEX_ARGUMENT_NOUN}\b", re.I),
    re.compile(
        rf"\\b{_INDEX_ARGUMENT_NOUN}\\b.{{0,40}}\\b(?:invalid|unknown|unsupported|bad|illegal|out\\s+of\\s+range)\b",
        re.I,
    ),
    re.compile(rf"\\bno\\s+such\\s+{_INDEX_ARGUMENT_NOUN}\b", re.I),
)


def _index(row: dict[str, Any]) -> int | None:
    raw = row.get("index")
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _covered(rows: list[dict[str, Any]]) -> list[str]:
    values: list[int] = []
    for row in rows:
        index = _index(row)
        if index is None or str(row.get("outcome") or "").upper() != _TERMINAL:
            continue
        if index not in values:
            values.append(index)
    return [str(value) for value in sorted(values)]


def _provider_error_text(payload: dict[str, Any]) -> str:
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text and text not in values:
                values.append(text)
            return
        if isinstance(value, dict):
            for key in ("error", "message", "reason", "detail", "description", "code"):
                if key in value:
                    collect(value.get(key))
            return
        if isinstance(value, list):
            for item in value:
                collect(item)

    for key in ("error", "message", "reason", "detail", "description", "errors"):
        if key in payload:
            collect(payload.get(key))
    return " | ".join(values)


def rubyplay_choice_domain_is_authoritative(mode: dict[str, Any]) -> bool:
    if not isinstance(mode, dict):
        return False
    if str(mode.get("kind") or "").upper() != "INDEXED_CHOICE":
        return False
    if str(mode.get("domain_authority") or "") != "isolated-live-server-rejection-window":
        return False

    try:
        boundary = int(mode.get("boundary_index"))
        confirmations = int(mode.get("boundary_confirmations"))
        rejection_span = int(mode.get("rejection_span"))
    except (TypeError, ValueError):
        return False
    if boundary <= 0 or confirmations < 2 or rejection_span < 2:
        return False

    required_raw = mode.get("required_options")
    covered_raw = mode.get("covered_options")
    if not isinstance(required_raw, list) or not isinstance(covered_raw, list):
        return False

    required: list[int] = []
    for raw in required_raw:
        if isinstance(raw, bool):
            return False
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return False
        if value < 0 or value in required:
            return False
        required.append(value)
    if required != list(range(boundary)):
        return False

    covered: set[int] = set()
    for raw in covered_raw:
        if isinstance(raw, bool):
            return False
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return False
        if value < 0 or value not in required:
            return False
        covered.add(value)

    observed_raw = mode.get("observed_indices")
    if isinstance(observed_raw, list):
        for raw in observed_raw:
            if isinstance(raw, bool):
                return False
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return False
            if value < 0 or value >= boundary:
                return False
    return True


def rubyplay_choice_domain_is_proven(mode: dict[str, Any]) -> bool:
    if not rubyplay_choice_domain_is_authoritative(mode):
        return False
    required = {str(value) for value in mode.get("required_options") or []}
    covered = {str(value) for value in mode.get("covered_options") or []}
    return covered == required


def classify_probe_failure(
    exc: BaseException,
    *,
    last_payload: dict[str, Any] | None,
    action: str,
) -> dict[str, Any]:
    """Classify one failed isolated index probe without inventing a boundary.

    Only an action-matched RubyPlay error whose provider detail explicitly refers
    to an index/choice/option can establish a candidate domain boundary. Generic
    server errors remain protocol errors even when ``status`` is non-OK.
    """
    if isinstance(exc, requests.RequestException):
        return {
            "outcome": _TRANSPORT_ERROR,
            "error": f"{type(exc).__name__}: {exc}",
        }

    payload = last_payload if isinstance(last_payload, dict) else {}
    provider_status = str(payload.get("status") or "").strip().lower()
    topic = str(payload.get("topic") or "").strip().lower()
    expected_topic = f"gameserver/{str(action or '').strip().lower()}"
    provider_error = _provider_error_text(payload)
    index_specific = any(
        pattern.search(provider_error)
        for pattern in _INDEX_ARGUMENT_ERROR_PATTERNS
    )

    if (
        provider_status
        and provider_status != "ok"
        and topic == expected_topic
        and index_specific
    ):
        return {
            "outcome": _SEMANTIC_REJECTION,
            "provider_status": provider_status,
            "provider_error": provider_error,
            "topic": topic,
        }

    return {
        "outcome": _PROTOCOL_ERROR,
        "provider_status": provider_status,
        "provider_error": provider_error,
        "topic": topic,
        "error": f"{type(exc).__name__}: {exc}",
    }


def prove_contiguous_index_domain(
    probes: Iterable[dict[str, Any]],
    *,
    min_boundary_confirmations: int = 2,
    min_rejection_span: int = 2,
) -> dict[str, Any]:
    """Prove a finite 0..N-1 domain from a confirmed rejection boundary window.

    Every index below N must terminate cleanly. N and the immediately following
    rejection-window indices must be explicitly rejected by the RubyPlay
    protocol in multiple fresh sessions. A valid successor therefore disproves
    the contiguous-boundary hypothesis instead of being silently ignored.
    """
    rows = [dict(row) for row in probes if isinstance(row, dict)]
    covered = _covered(rows)
    try:
        required_confirmations = max(2, int(min_boundary_confirmations))
    except (TypeError, ValueError):
        required_confirmations = 2
    try:
        required_span = max(2, int(min_rejection_span))
    except (TypeError, ValueError):
        required_span = 2

    unresolved = {
        "state": _UNRESOLVED,
        "required_options": ["DOMAIN_UNRESOLVED"],
        "covered_options": covered,
        "boundary_index": None,
        "boundary_confirmations": 0,
        "rejection_span": 0,
        "probes": rows,
    }
    if not rows:
        return unresolved

    by_index: dict[int, list[str]] = {}
    for row in rows:
        index = _index(row)
        if index is None:
            return unresolved
        outcome = str(row.get("outcome") or "").strip().upper()
        if not outcome:
            return unresolved
        by_index.setdefault(index, []).append(outcome)

    if any(len(set(outcomes)) != 1 for outcomes in by_index.values()):
        return unresolved

    rejection_indexes = sorted(
        index
        for index, outcomes in by_index.items()
        if outcomes and outcomes[0] == _SEMANTIC_REJECTION
    )
    if not rejection_indexes:
        return unresolved
    boundary = rejection_indexes[0]
    if boundary <= 0:
        return unresolved

    expected_rejections = list(range(boundary, boundary + required_span))
    if rejection_indexes != expected_rejections:
        return unresolved

    expected_indexes = list(range(boundary + required_span))
    if sorted(by_index) != expected_indexes:
        return unresolved

    for index in range(boundary):
        outcomes = by_index.get(index) or []
        if not outcomes or any(outcome != _TERMINAL for outcome in outcomes):
            return unresolved

    confirmations: list[int] = []
    for index in expected_rejections:
        outcomes = by_index.get(index) or []
        if (
            len(outcomes) < required_confirmations
            or any(outcome != _SEMANTIC_REJECTION for outcome in outcomes)
        ):
            return unresolved
        confirmations.append(len(outcomes))

    domain = [str(index) for index in range(boundary)]
    return {
        "state": _PROVEN,
        "required_options": domain,
        "covered_options": domain,
        "boundary_index": boundary,
        "boundary_confirmations": min(confirmations),
        "rejection_span": required_span,
        "probes": rows,
    }


def probe_contiguous_index_domain(
    probe_index: Callable[[int], dict[str, Any]],
    *,
    max_index: int = 32,
    boundary_confirmations: int = 2,
    rejection_span: int = 2,
) -> dict[str, Any]:
    """Probe 0..N and confirm a contiguous rejection window in fresh sessions."""
    try:
        guard = max(0, int(max_index))
    except (TypeError, ValueError):
        guard = 32
    try:
        confirmations = max(2, int(boundary_confirmations))
    except (TypeError, ValueError):
        confirmations = 2
    try:
        span = max(2, int(rejection_span))
    except (TypeError, ValueError):
        span = 2

    rows: list[dict[str, Any]] = []

    def one(index: int) -> str:
        try:
            raw = probe_index(index)
        except BaseException as exc:
            raw = {
                "index": index,
                "outcome": _PROTOCOL_ERROR,
                "error": f"{type(exc).__name__}: {exc}",
            }
        row = dict(raw) if isinstance(raw, dict) else {}
        row["index"] = index
        outcome = str(row.get("outcome") or "").strip().upper()
        if not outcome:
            row["outcome"] = _PROTOCOL_ERROR
            outcome = _PROTOCOL_ERROR
        rows.append(row)
        return outcome

    boundary: int | None = None
    for index in range(guard + 1):
        outcome = one(index)
        if outcome == _TERMINAL:
            continue
        if outcome != _SEMANTIC_REJECTION:
            break

        boundary = index
        for _ in range(confirmations - 1):
            if one(index) != _SEMANTIC_REJECTION:
                break
        if any(
            str(row.get("outcome") or "").upper() != _SEMANTIC_REJECTION
            for row in rows
            if _index(row) == index
        ):
            break

        for successor in range(index + 1, index + span):
            if one(successor) != _SEMANTIC_REJECTION:
                break
            for _ in range(confirmations - 1):
                if one(successor) != _SEMANTIC_REJECTION:
                    break
            if any(
                str(row.get("outcome") or "").upper() != _SEMANTIC_REJECTION
                for row in rows
                if _index(row) == successor
            ):
                break
        break

    return prove_contiguous_index_domain(
        rows,
        min_boundary_confirmations=confirmations,
        min_rejection_span=span,
    )


__all__ = [
    "classify_probe_failure",
    "rubyplay_choice_domain_is_proven",
    "probe_contiguous_index_domain",
    "prove_contiguous_index_domain",
]
