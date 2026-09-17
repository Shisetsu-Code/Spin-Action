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


def classify_probe_failure(
    exc: BaseException,
    *,
    last_payload: dict[str, Any] | None,
    action: str,
) -> dict[str, Any]:
    """Classify one failed isolated index probe without inventing a boundary."""
    if isinstance(exc, requests.RequestException):
        return {
            "outcome": _TRANSPORT_ERROR,
            "error": f"{type(exc).__name__}: {exc}",
        }

    payload = last_payload if isinstance(last_payload, dict) else {}
    provider_status = str(payload.get("status") or "").strip().lower()
    if provider_status and provider_status != "ok":
        return {
            "outcome": _SEMANTIC_REJECTION,
            "provider_status": provider_status,
            "provider_error": str(
                payload.get("error")
                or payload.get("message")
                or payload.get("errorCode")
                or ""
            ),
            "topic": str(payload.get("topic") or f"gameserver/{action}"),
        }

    return {
        "outcome": _PROTOCOL_ERROR,
        "error": f"{type(exc).__name__}: {exc}",
    }


def prove_contiguous_index_domain(
    probes: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Prove a finite 0..N-1 RubyPlay index domain from isolated live probes.

    A domain closes only when every index below the boundary reached a clean
    terminal result and the immediately following index was explicitly rejected
    by the RubyPlay protocol. Network failures, malformed responses, nonterminal
    branches, gaps, duplicate contradictory outcomes, or rejection at index zero
    remain unresolved.
    """
    rows = [dict(row) for row in probes if isinstance(row, dict)]
    covered = _covered(rows)
    unresolved = {
        "state": _UNRESOLVED,
        "required_options": ["DOMAIN_UNRESOLVED"],
        "covered_options": covered,
        "boundary_index": None,
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
        if outcomes[0] == _SEMANTIC_REJECTION
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
        outcomes = by_index.get(index)
        if outcomes != [_TERMINAL]:
            return unresolved
    if by_index.get(boundary) != [_SEMANTIC_REJECTION]:
        return unresolved

    domain = [str(index) for index in range(boundary)]
    return {
        "state": _PROVEN,
        "required_options": domain,
        "covered_options": domain,
        "boundary_index": boundary,
        "probes": rows,
    }


def probe_contiguous_index_domain(
    probe_index: Callable[[int], dict[str, Any]],
    *,
    max_index: int = 32,
) -> dict[str, Any]:
    """Probe 0..N conservatively and stop at the first conclusive/inconclusive edge.

    Each callback invocation must use an isolated logical round/session. The loop
    never retries an index and never searches beyond the first non-terminal result.
    Reaching the defensive guard without a semantic rejection remains unresolved.
    """
    try:
        guard = max(0, int(max_index))
    except (TypeError, ValueError):
        guard = 32

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
        # Semantic rejection may prove the boundary; every other outcome makes
        # the run inconclusive. In both cases stop immediately.
        break

    return prove_contiguous_index_domain(rows)


__all__ = [
    "classify_probe_failure",
    "probe_contiguous_index_domain",
    "prove_contiguous_index_domain",
]
