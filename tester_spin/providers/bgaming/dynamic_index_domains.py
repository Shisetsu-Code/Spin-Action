from __future__ import annotations

from collections.abc import Callable, Iterable
import json
from typing import Any


PROVEN = "PROVEN"
UNRESOLVED = "UNRESOLVED"
ACCEPTED = "ACCEPTED"
SEMANTIC_REJECTION = "SEMANTIC_REJECTION"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
PROTOCOL_ERROR = "PROTOCOL_ERROR"


def _probe_index(row: dict[str, Any]) -> int | None:
    raw = row.get("index")
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _covered_indices(rows: list[dict[str, Any]]) -> list[int]:
    values: list[int] = []
    for row in rows:
        index = _probe_index(row)
        outcome = str(row.get("outcome") or "").strip().upper()
        if index is None or outcome != ACCEPTED:
            continue
        if index not in values:
            values.append(index)
    return sorted(values)


def dynamic_index_option_label(
    literal_options: dict[str, Any],
    field: str,
    index: int,
) -> str:
    payload = dict(literal_options)
    payload[str(field)] = int(index)
    return "|".join(
        f"{key}={json.dumps(payload[key], ensure_ascii=False, sort_keys=True)}"
        for key in sorted(payload)
    )


def apply_index_domain_proof(
    point: dict[str, Any],
    variant: dict[str, Any],
    proof: dict[str, Any],
) -> bool:
    proofs = point.setdefault("dynamic_index_proofs", [])
    proofs.append(
        {
            **dict(proof),
            "variant": {
                "literal_options": dict(variant.get("literal_options") or {}),
                "unresolved_fields": [
                    str(item)
                    for item in variant.get("unresolved_fields") or []
                    if str(item)
                ],
                "source": str(variant.get("source") or ""),
            },
        }
    )
    if str(proof.get("state") or "") != PROVEN:
        return False

    unresolved_fields = [
        str(item)
        for item in variant.get("unresolved_fields") or []
        if str(item)
    ]
    if len(unresolved_fields) != 1:
        return False
    field = unresolved_fields[0]
    literal_options = dict(variant.get("literal_options") or {})
    raw_indices = proof.get("required_indices")
    if not isinstance(raw_indices, list) or not raw_indices:
        return False

    indices: list[int] = []
    for raw in raw_indices:
        if isinstance(raw, bool):
            return False
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return False
        if value < 0 or value in indices:
            return False
        indices.append(value)
    if indices != list(range(len(indices))):
        return False

    available = [
        str(value)
        for value in point.get("available") or ()
        if str(value)
    ]
    payloads = point.setdefault("dynamic_option_payloads", {})
    counts = point.setdefault("sample_counts", {})
    covered = point.setdefault("covered", set())
    if not isinstance(covered, set):
        covered = set(str(value) for value in covered or [])
        point["covered"] = covered

    for index in indices:
        label = dynamic_index_option_label(literal_options, field, index)
        payload = dict(literal_options)
        payload[field] = index
        if label not in available:
            available.append(label)
        payloads[label] = payload
        counts.setdefault(label, 0)

    original = point.get("unresolved_option_variants") or []
    point["unresolved_option_variants"] = [
        item
        for item in original
        if not (
            isinstance(item, dict)
            and dict(item.get("literal_options") or {}) == literal_options
            and [
                str(value)
                for value in item.get("unresolved_fields") or []
                if str(value)
            ] == unresolved_fields
            and str(item.get("source") or "") == str(variant.get("source") or "")
        )
    ]
    point["available"] = tuple(available)
    return True


def prove_contiguous_index_domain(
    probes: Iterable[dict[str, Any]],
    *,
    min_boundary_confirmations: int = 2,
) -> dict[str, Any]:
    rows = [dict(row) for row in probes if isinstance(row, dict)]
    covered = _covered_indices(rows)
    try:
        required_confirmations = max(2, int(min_boundary_confirmations))
    except (TypeError, ValueError):
        required_confirmations = 2

    unresolved = {
        "state": UNRESOLVED,
        "required_indices": [],
        "covered_indices": covered,
        "boundary_index": None,
        "boundary_confirmations": 0,
        "probes": rows,
    }
    if not rows:
        return unresolved

    by_index: dict[int, list[str]] = {}
    for row in rows:
        index = _probe_index(row)
        if index is None:
            return unresolved
        outcome = str(row.get("outcome") or "").strip().upper()
        if outcome not in {
            ACCEPTED,
            SEMANTIC_REJECTION,
            TRANSPORT_ERROR,
            PROTOCOL_ERROR,
        }:
            return unresolved
        by_index.setdefault(index, []).append(outcome)

    if any(len(set(outcomes)) != 1 for outcomes in by_index.values()):
        return unresolved

    rejection_indexes = sorted(
        index
        for index, outcomes in by_index.items()
        if outcomes and outcomes[0] == SEMANTIC_REJECTION
    )
    if len(rejection_indexes) != 1:
        return unresolved
    boundary = rejection_indexes[0]
    if boundary <= 0:
        return unresolved

    if sorted(by_index) != list(range(boundary + 1)):
        return unresolved

    for index in range(boundary):
        outcomes = by_index.get(index) or []
        if not outcomes or any(outcome != ACCEPTED for outcome in outcomes):
            return unresolved

    boundary_outcomes = by_index.get(boundary) or []
    if (
        len(boundary_outcomes) < required_confirmations
        or any(outcome != SEMANTIC_REJECTION for outcome in boundary_outcomes)
    ):
        return unresolved

    domain = list(range(boundary))
    return {
        "state": PROVEN,
        "required_indices": domain,
        "covered_indices": domain,
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
                "outcome": PROTOCOL_ERROR,
                "error": f"{type(exc).__name__}: {exc}",
            }
        row = dict(raw) if isinstance(raw, dict) else {}
        row["index"] = index
        outcome = str(row.get("outcome") or "").strip().upper()
        if not outcome:
            row["outcome"] = PROTOCOL_ERROR
            outcome = PROTOCOL_ERROR
        rows.append(row)

        if outcome == ACCEPTED:
            continue
        if outcome == SEMANTIC_REJECTION:
            for _ in range(confirmations - 1):
                try:
                    repeated_raw = probe_index(index)
                except BaseException as exc:
                    repeated_raw = {
                        "index": index,
                        "outcome": PROTOCOL_ERROR,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                repeated = dict(repeated_raw) if isinstance(repeated_raw, dict) else {}
                repeated["index"] = index
                repeated_outcome = str(repeated.get("outcome") or "").strip().upper()
                if not repeated_outcome:
                    repeated["outcome"] = PROTOCOL_ERROR
                    repeated_outcome = PROTOCOL_ERROR
                rows.append(repeated)
                if repeated_outcome != SEMANTIC_REJECTION:
                    break
        break

    return prove_contiguous_index_domain(
        rows,
        min_boundary_confirmations=confirmations,
    )


__all__ = [
    "ACCEPTED",
    "apply_index_domain_proof",
    "dynamic_index_option_label",
    "PROTOCOL_ERROR",
    "PROVEN",
    "SEMANTIC_REJECTION",
    "TRANSPORT_ERROR",
    "UNRESOLVED",
    "probe_contiguous_index_domain",
    "prove_contiguous_index_domain",
]
