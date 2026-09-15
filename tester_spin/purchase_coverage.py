from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl

from tester_spin.models import GameTestResult, SpinAttempt

SCHEMA = "tester-spin/purchase-coverage/v1"
PURCHASE_COMPLETE = "PURCHASE_COMPLETE"
PURCHASE_FAILED = "PURCHASE_FAILED"
PURCHASE_UNKNOWN = "PURCHASE_UNKNOWN"
NO_PURCHASE_PROVEN = "NO_PURCHASE_PROVEN"

_VALID_WIRE_STATES = {"PROVEN", "UNKNOWN", "CONTRADICTED"}
_VALID_EXECUTION_STATES = {"COMPLETE", "FAILED", "NOT_ATTEMPTED", "UNKNOWN"}


def make_purchase_option(
    purchase_id: str,
    *,
    provider_selector: Any = None,
    display_name: str = "",
    source_kind: str = "",
    source_evidence: Any = None,
    price: Any = None,
    price_multiplier: Any = None,
    currency_or_stake_basis: Any = None,
    executable: bool = False,
    wire_contract_state: str = "UNKNOWN",
    execution_state: str = "UNKNOWN",
    terminal: bool = False,
    artifact_dir: str = "",
    reason: str = "",
) -> dict[str, Any]:
    wire_state = str(wire_contract_state or "UNKNOWN").upper()
    execution = str(execution_state or "UNKNOWN").upper()
    if wire_state not in _VALID_WIRE_STATES:
        wire_state = "UNKNOWN"
    if execution not in _VALID_EXECUTION_STATES:
        execution = "UNKNOWN"
    return {
        "purchase_id": str(purchase_id or "").strip() or "PURCHASE_UNKNOWN",
        "provider_selector": provider_selector,
        "display_name": str(display_name or ""),
        "source_kind": str(source_kind or ""),
        "source_evidence": source_evidence,
        "price": price,
        "price_multiplier": price_multiplier,
        "currency_or_stake_basis": currency_or_stake_basis,
        "executable": bool(executable),
        "wire_contract_state": wire_state,
        "execution_state": execution,
        "terminal": bool(terminal),
        "artifact_dir": str(artifact_dir or ""),
        "reason": str(reason or ""),
    }


def _option_bucket(option: dict[str, Any]) -> str:
    wire = str(option.get("wire_contract_state") or "UNKNOWN").upper()
    execution = str(option.get("execution_state") or "UNKNOWN").upper()
    executable = option.get("executable") is True
    terminal = option.get("terminal") is True
    if wire == "PROVEN" and execution == "FAILED":
        return "failed"
    if executable and wire == "PROVEN" and execution == "COMPLETE" and terminal:
        return "complete"
    return "unknown"


def finalize_purchase_coverage(
    result: GameTestResult,
    *,
    options: Iterable[dict[str, Any]],
    inventory_state: str,
    authority: str,
    no_purchase_proven: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    normalized = [dict(item) for item in options if isinstance(item, dict)]
    inventory = str(inventory_state or "UNKNOWN").upper()
    buckets = [_option_bucket(option) for option in normalized]
    counts = {
        "total": len(normalized),
        "complete": buckets.count("complete"),
        "failed": buckets.count("failed"),
        "unknown": buckets.count("unknown"),
    }

    if inventory != "COMPLETE":
        state = PURCHASE_UNKNOWN
    elif not normalized:
        state = NO_PURCHASE_PROVEN if no_purchase_proven else PURCHASE_UNKNOWN
    elif counts["failed"]:
        state = PURCHASE_FAILED
    elif counts["unknown"]:
        state = PURCHASE_UNKNOWN
    else:
        state = PURCHASE_COMPLETE

    return {
        "schema": SCHEMA,
        "provider": result.provider,
        "game_slug": result.slug,
        "game_name": result.game_name,
        "game_identifier": result.symbol,
        "state": state,
        "inventory_state": inventory,
        "inventory_closed": inventory == "COMPLETE",
        "authority": str(authority or ""),
        "no_purchase_proven": bool(no_purchase_proven and inventory == "COMPLETE" and not normalized),
        "counts": counts,
        "options": normalized,
        "result_status": result.status,
        "run_dir": result.run_dir,
        "reason": str(reason or ""),
    }


def aggregate_purchase_coverages(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = [dict(row) for row in rows if isinstance(row, dict)]
    state_counts = {
        PURCHASE_COMPLETE: 0,
        PURCHASE_FAILED: 0,
        PURCHASE_UNKNOWN: 0,
        NO_PURCHASE_PROVEN: 0,
    }
    option_counts = {"total": 0, "complete": 0, "failed": 0, "unknown": 0}
    for row in values:
        state = str(row.get("state") or PURCHASE_UNKNOWN)
        if state not in state_counts:
            state = PURCHASE_UNKNOWN
        state_counts[state] += 1
        counts = row.get("counts")
        if isinstance(counts, dict):
            for key in option_counts:
                try:
                    option_counts[key] += int(counts.get(key) or 0)
                except (TypeError, ValueError):
                    continue

    if state_counts[PURCHASE_FAILED]:
        overall = PURCHASE_FAILED
    elif state_counts[PURCHASE_UNKNOWN]:
        overall = PURCHASE_UNKNOWN
    else:
        overall = PURCHASE_COMPLETE

    return {
        "schema": "tester-spin/purchase-campaign-aggregate/v1",
        "overall_state": overall,
        "games": len(values),
        "counts": state_counts,
        "option_counts": option_counts,
        "closed": overall == PURCHASE_COMPLETE,
    }


def inventory_state(result: GameTestResult) -> str:
    structural = result.structural_map if isinstance(result.structural_map, dict) else {}
    inventory = structural.get("action_inventory")
    if not isinstance(inventory, dict):
        return "UNKNOWN"
    state = str(inventory.get("state") or "UNKNOWN").upper()
    return state if state in {"COMPLETE", "INCOMPLETE", "UNKNOWN"} else "UNKNOWN"


def attempts_for_mode(result: GameTestResult, mode_id: str) -> list[SpinAttempt]:
    target = str(mode_id or "")
    return [attempt for attempt in result.attempts if str(attempt.mode_id or "") == target]


def clean_terminal_attempt(attempt: SpinAttempt) -> bool:
    return bool(
        attempt.ok
        and attempt.terminal
        and not str(attempt.warning or "").strip()
        and not str(attempt.error or "").strip()
    )


def _safe_artifact_root(result: GameTestResult, attempt: SpinAttempt) -> Path | None:
    raw = str(attempt.artifact_dir or "").strip()
    if not raw:
        return None
    try:
        root = Path(raw).resolve()
    except OSError:
        return None
    run_raw = str(result.run_dir or "").strip()
    if run_raw:
        try:
            root.relative_to(Path(run_raw).resolve())
        except (OSError, ValueError):
            return None
    return root if root.is_dir() else None


def request_payloads(result: GameTestResult, attempt: SpinAttempt) -> list[dict[str, Any]]:
    root = _safe_artifact_root(result, attempt)
    if root is None:
        return []
    paths: list[Path] = []
    for pattern in ("*request.json", "*.request.json", "*.request.txt"):
        for path in root.rglob(pattern):
            if path.is_file() and path not in paths:
                paths.append(path)
    payloads: list[dict[str, Any]] = []
    for path in sorted(paths):
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.suffix.lower() == ".json":
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                payloads.append(value)
            continue
        parsed = {str(key): str(value) for key, value in parse_qsl(text, keep_blank_values=True)}
        if parsed:
            payloads.append(parsed)
    return payloads


def matching_request(
    result: GameTestResult,
    attempt: SpinAttempt,
    predicate,
) -> dict[str, Any] | None:
    for payload in request_payloads(result, attempt):
        try:
            if predicate(payload):
                return payload
        except Exception:
            continue
    return None


__all__ = [
    "SCHEMA",
    "PURCHASE_COMPLETE",
    "PURCHASE_FAILED",
    "PURCHASE_UNKNOWN",
    "NO_PURCHASE_PROVEN",
    "make_purchase_option",
    "finalize_purchase_coverage",
    "aggregate_purchase_coverages",
    "inventory_state",
    "attempts_for_mode",
    "clean_terminal_attempt",
    "request_payloads",
    "matching_request",
]
