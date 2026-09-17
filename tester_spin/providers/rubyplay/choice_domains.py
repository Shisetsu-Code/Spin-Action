from __future__ import annotations

from collections.abc import Callable
from typing import Any, Iterable

import requests

_PROVEN = "PROVEN"
_UNRESOLVED = "UNRESOLVED"
_TERMINAL = "TERMINAL"
_SEMANTIC_REJECTION = "SEMANTIC_REJECTION"
_TRANSPORT_ERROR = "TRANSPORT_ERROR"
_PROTOCOL_ERROR = "PROTOCOL_ERROR"
_INDEX_ERROR_MARKERS = ("index", "choice", "option", "selection", "select", "pick")


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
    for key in ("error", "message", "reason", "detail", "description"):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                values.append(text)
    return " | ".join(values)


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
    error_lower = provider_error.casefold()
    index_specific = any(marker in error_lower for marker in _INDEX_ERROR_MARKERS)

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
) -> dict[str, Any]:
    """Prove a finite 0..N-1 RubyPlay index domain from isolated live probes.

    A domain closes only when every index below the boundary reached a clean
    terminal result and the immediately following index was explicitly rejected
    by the RubyPlay protocol in multiple fresh sessions. Network failures,
    malformed responses, nonterminal branches, gaps, contradictory outcomes, or
    rejection at index zero remain unresolved.
    """
    rows = [dict(row) for row in probes if isinstance(row, dict)]
    covered = _covered(rows)
    try:
        required_confirmations = max(2, int(min_boundary_confirmations))
    except (TypeError, ValueError):
        required_confirmations = 2
    unresolved = {
        "state": _UNRESOLVED,
        "required_options": ["DOMAIN_UNRESOLVED"],
        "covered_options": covered,
        "boundary_index": None,
        "boundary_confirmations": 0,
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

    # Contradictory repeats are never promoted.
    if any(len(set(outcomes)) != 1 for outcomes in by_index.values()):
        return unresolved

    rejection_indexes = sorted(
        index
        for index, outcomes in by_index.items()
        if outcomes and outcomes[0] == _SEMANTIC_REJECTION
    )
    if len(rejection_indexes) != 1:
        return unresolved
    boundary = rejection_indexes[0]
    if boundary <= 0:
        return unresolved

    expected_indexes = list(range(boundary + 1))
    if sorted(by_index) != expected_indexes:
        return unresolved

    for index in range(boundary):
        outcomes = by_index.get(index) or []
        if not outcomes or any(outcome != _TERMINAL for outcome in outcomes):
            return unresolved

    boundary_outcomes = by_index.get(boundary) or []
    if (
        len(boundary_outcomes) < required_confirmations
        or any(outcome != _SEMANTIC_REJECTION for outcome in boundary_outcomes)
    ):
        return unresolved

    domain = [str(index) for index in range(boundary)]
    return {
        "state": _PROVEN,
        "required_options": domain,
        "covered_options": domain,
        "boundary_index": boundary,
        "boundary_confirmations": len(boundary_outcomes),
        "probes": rows,
    }


def probe_contiguous_index_domain(
    probe_index: Callable[[int], dict[str, Any]],
    *,
    max_index: int = 32,
    boundary_confirmations: int = 2,
) -> dict[str, Any]:
    """Probe 0..N conservatively and confirm a boundary in fresh sessions.

    Each callback invocation must use an isolated logical round/session. Valid
    indices are sampled once. A semantic rejection is repeated at the same index
    to distinguish a stable provider boundary from a transient semantic failure.
    Any inconclusive result stops expansion immediately.
    """
    try:
        guard = max(0, int(max_index))
    except (TypeError, ValueError):
        guard = 32
    try:
        confirmations = max(2, int(boundary_confirmations))
    except (TypeError, ValueError):
        confirmations = 2

    rows: list[dict[str, Any]] = []
    for index in range(guard + 1):
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

        if outcome == _TERMINAL:
            continue
        if outcome == _SEMANTIC_REJECTION:
            for _ in range(confirmations - 1):
                try:
                    repeated_raw = probe_index(index)
                except BaseException as exc:
                    repeated_raw = {
                        "index": index,
                        "outcome": _PROTOCOL_ERROR,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                repeated = dict(repeated_raw) if isinstance(repeated_raw, dict) else {}
                repeated["index"] = index
                repeated_outcome = str(repeated.get("outcome") or "").strip().upper()
                if not repeated_outcome:
                    repeated["outcome"] = _PROTOCOL_ERROR
                    repeated_outcome = _PROTOCOL_ERROR
                rows.append(repeated)
                if repeated_outcome != _SEMANTIC_REJECTION:
                    break
        # Semantic rejection may prove the boundary; every other outcome makes
        # the run inconclusive. In all cases stop probing larger indices.
        break

    return prove_contiguous_index_domain(
        rows,
        min_boundary_confirmations=confirmations,
    )


__all__ = [
    "classify_probe_failure",
    "probe_contiguous_index_domain",
    "prove_contiguous_index_domain",
]
