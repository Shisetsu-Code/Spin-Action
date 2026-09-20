from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tester_spin.audit_wire_evidence import successful_wire_commands
from tester_spin.models import GameTestResult


_ACTIONABLE_KINDS = {
    "SPIN",
    "ANTE_BET",
    "PURCHASE",
    "PURCHASE_BRANCH",
    "FEATURE",
    "VARIANT",
    "CONTINUATION",
    "CHOICE_BRANCH",
    "CHOICE_CONTINUATION",
    "INDEXED_CHOICE",
    "FSO_BRANCH",
    "UNRESOLVED_STATE",
}

_PROVEN_STATES = {"PROVEN_TERMINAL", "DEMONSTRATED", "DEMOSTRADO"}
_PROVEN_EVIDENCE = {"REMOTE_EXECUTION", "DEMONSTRATED", "DEMOSTRADO"}


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    values: list[str] = []
    for item in value:
        if item is None or isinstance(item, bool):
            continue
        text = str(item).strip()
        if text and text not in values:
            values.append(text)
    return values


def _inventory(result: GameTestResult) -> tuple[str, dict[str, Any], list[str]]:
    structural = result.structural_map if isinstance(result.structural_map, dict) else {}
    raw = structural.get("action_inventory")
    if not isinstance(raw, dict):
        return (
            "UNKNOWN",
            {},
            ["Inventario de acciones sin cierre demostrable: falta structural_map.action_inventory."],
        )

    state = str(raw.get("state") or "").strip().upper()
    if state not in {"COMPLETE", "INCOMPLETE", "UNKNOWN"}:
        return (
            "UNKNOWN",
            raw,
            [f"Inventario de acciones con estado no verificable: {state or '<vacío>'}."],
        )

    source = str(raw.get("source") or "").strip()
    reason = str(raw.get("reason") or "").strip()
    if state == "COMPLETE" and not source:
        return (
            "UNKNOWN",
            raw,
            ["Inventario marcado COMPLETE sin fuente de autoridad explícita."],
        )
    if state == "UNKNOWN":
        return (
            state,
            raw,
            [reason or "El proveedor no pudo demostrar que el inventario de acciones esté cerrado."],
        )
    return state, raw, []


def _promoted_inventory_mode_ids(inventory: dict[str, Any]) -> set[str]:
    """Return modes proven by a validated/promoted farm contract.

    Farm-contract promotion occurs only after provider + common validation has
    established that every required mode is DEMOSTRADO and that no unresolved
    actions or continuations remain. Trust only that narrow inventory source;
    arbitrary COMPLETE inventories must still supply direct terminal/wire proof.
    """
    if str(inventory.get("state") or "").strip().upper() != "COMPLETE":
        return set()
    source = str(inventory.get("source") or "").strip()
    if not source.endswith(":promoted-farm-contract"):
        return set()
    return set(_clean_list(inventory.get("mode_ids")))


def _terminal_attempts(result: GameTestResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for attempt in result.attempts:
        if not (
            bool(attempt.ok)
            and bool(attempt.terminal)
            and not str(attempt.error or "").strip()
            and not str(attempt.warning or "").strip()
        ):
            continue
        mode_id = str(attempt.mode_id or "").strip()
        if not mode_id:
            continue
        counts[mode_id] = counts.get(mode_id, 0) + 1
    return counts


def _is_actionable(mode: dict[str, Any]) -> bool:
    kind = str(mode.get("kind") or "").strip().upper()
    if kind in _ACTIONABLE_KINDS:
        return True
    return mode.get("coverage_required") is True or mode.get("executable") is True


def _audit_mode(
    mode: dict[str, Any],
    terminal_counts: dict[str, int],
    wire_counts: dict[str, int],
    promoted_mode_ids: set[str],
) -> dict[str, Any]:
    mode_id = str(mode.get("id") or "UNKNOWN").strip() or "UNKNOWN"
    kind = str(mode.get("kind") or "UNKNOWN").strip().upper() or "UNKNOWN"
    required = _clean_list(
        mode.get("required_options")
        if mode.get("required_options") is not None
        else mode.get("available")
    )
    covered = _clean_list(
        mode.get("covered_options")
        if mode.get("covered_options") is not None
        else mode.get("selected_options")
    )

    # Explicit branch coverage remains authoritative even after farm promotion:
    # a promoted parent mode must never mask a missing required branch option.
    if mode.get("coverage_required") is True:
        if not required:
            return {
                "id": mode_id,
                "kind": kind,
                "state": "UNKNOWN",
                "required_options": [],
                "covered_options": covered,
                "missing_options": [],
                "evidence": "coverage_required without a resolved option domain",
            }
        missing = [option for option in required if option not in covered]
        return {
            "id": mode_id,
            "kind": kind,
            "state": "INCOMPLETE" if missing else "DEMONSTRATED",
            "required_options": required,
            "covered_options": covered,
            "missing_options": missing,
            "evidence": "explicit branch coverage",
        }

    direct_count = int(terminal_counts.get(mode_id, 0))
    wire_command = str(mode.get("wire_command") or "").strip()
    wire_count = int(wire_counts.get(wire_command, 0)) if wire_command else 0
    parent = str(mode.get("parent") or "").strip()
    parent_count = int(terminal_counts.get(parent, 0)) if parent else 0
    provider_marked_proven = (
        mode.get("validated") is True
        and (
            str(mode.get("execution_state") or "").strip().upper() in _PROVEN_STATES
            or str(mode.get("evidence_level") or "").strip().upper() in _PROVEN_EVIDENCE
        )
    )
    promoted_contract_proven = mode_id in promoted_mode_ids
    if direct_count > 0 or wire_count > 0 or provider_marked_proven or promoted_contract_proven:
        if direct_count:
            evidence = f"terminal remote attempts={direct_count}"
        elif wire_count:
            evidence = f"remote wire executions={wire_count}"
        elif provider_marked_proven:
            evidence = "provider terminal remote proof"
        else:
            evidence = "validated promoted farm contract proof"
        return {
            "id": mode_id,
            "kind": kind,
            "state": "DEMONSTRATED",
            "required_options": required,
            "covered_options": covered,
            "missing_options": [],
            "evidence": evidence,
        }

    if parent_count > 0 and kind in {"CONTINUATION", "FEATURE"} and mode.get("observed") is True:
        return {
            "id": mode_id,
            "kind": kind,
            "state": "UNKNOWN",
            "required_options": required,
            "covered_options": covered,
            "missing_options": [],
            "evidence": "parent completed but this action has no independent terminal proof",
        }

    return {
        "id": mode_id,
        "kind": kind,
        "state": "INCOMPLETE",
        "required_options": required,
        "covered_options": covered,
        "missing_options": required,
        "evidence": "no successful terminal remote execution",
    }


def _path_coverage_reason(result: GameTestResult) -> str:
    run_dir = str(result.run_dir or "").strip()
    if not run_dir:
        return ""
    path = Path(run_dir) / "path-coverage.json"
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "path-coverage.json existe pero no puede validarse."
    if payload.get("complete") is not False:
        return ""
    missing: list[str] = []
    for point in payload.get("branch_points") or []:
        if not isinstance(point, dict):
            continue
        values = _clean_list(point.get("missing"))
        if values:
            mode_id = str(point.get("mode_id") or "UNKNOWN")
            missing.append(f"{mode_id}:{','.join(values)}")
    suffix = "; ".join(missing[:8])
    return "path-coverage.json incompleto" + (f" ({suffix})" if suffix else "") + "."


def build_action_audit(result: GameTestResult) -> dict[str, Any]:
    """Build a fail-closed, provider-neutral evidence audit.

    This function does not discover or execute wire requests. It only decides
    whether the evidence already produced by a provider is sufficient to trust a
    completeness claim. `OK` from the runtime is therefore necessary but never
    sufficient for a `COMPLETE` audit verdict.
    """
    runtime_status = str(result.status or "").strip().upper()
    if runtime_status == "UNAVAILABLE":
        reason = str(result.error or "").strip() or "Provider catalog marks this target unavailable."
        return {
            "schema": "tester-spin/action-audit/v1",
            "provider": result.provider,
            "game": result.slug,
            "runtime_status": result.status,
            "verdict": "UNAVAILABLE",
            "inventory": {},
            "actions": [],
            "unknown_reasons": [],
            "missing_reasons": [],
            "unavailable_reason": reason,
            "counts": {"actions": 0, "demonstrated": 0, "incomplete": 0, "unknown": 0},
        }
    if runtime_status == "ERROR":
        return {
            "schema": "tester-spin/action-audit/v1",
            "provider": result.provider,
            "game": result.slug,
            "runtime_status": result.status,
            "verdict": "ERROR",
            "inventory": {},
            "actions": [],
            "unknown_reasons": [],
            "missing_reasons": [str(result.error or "runtime error").strip() or "runtime error"],
            "counts": {"actions": 0, "demonstrated": 0, "incomplete": 0, "unknown": 0},
        }
    if runtime_status == "CANCELADO":
        return {
            "schema": "tester-spin/action-audit/v1",
            "provider": result.provider,
            "game": result.slug,
            "runtime_status": result.status,
            "verdict": "CANCELLED",
            "inventory": {},
            "actions": [],
            "unknown_reasons": [],
            "missing_reasons": ["Ejecución cancelada antes de cerrar la evidencia."],
            "counts": {"actions": 0, "demonstrated": 0, "incomplete": 0, "unknown": 0},
        }

    inventory_state, inventory, unknown_reasons = _inventory(result)
    promoted_mode_ids = _promoted_inventory_mode_ids(inventory)
    missing_reasons: list[str] = []
    terminal_counts = _terminal_attempts(result)
    wire_counts = successful_wire_commands(result)
    modes = [
        mode
        for mode in result.discovered_modes
        if isinstance(mode, dict) and _is_actionable(mode)
    ]
    actions = [
        _audit_mode(mode, terminal_counts, wire_counts, promoted_mode_ids)
        for mode in modes
    ]

    if not actions:
        unknown_reasons.append("No hay ninguna acción jugable registrada con evidencia auditable.")

    for action in actions:
        if action["state"] == "INCOMPLETE":
            missing = action.get("missing_options") or []
            suffix = f"; opciones faltantes={missing}" if missing else ""
            missing_reasons.append(
                f"Acción {action['id']} sin evidencia completa{suffix}."
            )
        elif action["state"] == "UNKNOWN":
            unknown_reasons.append(
                f"Acción {action['id']} no puede clasificarse como demostrada ni faltante con la evidencia actual."
            )

    path_reason = _path_coverage_reason(result)
    if path_reason:
        missing_reasons.append(path_reason)

    if inventory_state == "INCOMPLETE":
        reason = str(inventory.get("reason") or "Inventario declarado incompleto.").strip()
        missing_reasons.append(reason)

    if runtime_status == "PARCIAL" and not missing_reasons:
        unknown_reasons.append(
            "El runtime devolvió PARCIAL pero no existe una causa faltante demostrada; no se acepta un parcial opaco."
        )
    elif runtime_status not in {"OK", "PARCIAL"}:
        unknown_reasons.append(f"Estado runtime no clasificable de forma segura: {result.status!r}.")

    if missing_reasons:
        verdict = "INCOMPLETE"
    elif unknown_reasons or inventory_state != "COMPLETE" or runtime_status != "OK":
        verdict = "UNKNOWN"
    else:
        verdict = "COMPLETE"

    demonstrated = sum(action["state"] == "DEMONSTRATED" for action in actions)
    incomplete = sum(action["state"] == "INCOMPLETE" for action in actions)
    unknown = sum(action["state"] == "UNKNOWN" for action in actions)
    return {
        "schema": "tester-spin/action-audit/v1",
        "provider": result.provider,
        "game": result.slug,
        "runtime_status": result.status,
        "verdict": verdict,
        "inventory": inventory,
        "actions": actions,
        "unknown_reasons": list(dict.fromkeys(unknown_reasons)),
        "missing_reasons": list(dict.fromkeys(missing_reasons)),
        "counts": {
            "actions": len(actions),
            "demonstrated": demonstrated,
            "incomplete": incomplete,
            "unknown": unknown,
        },
    }
