from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tester_spin.models import GameTestResult
from tester_spin.purchase_coverage import (
    attempts_for_mode,
    clean_terminal_attempt,
    finalize_purchase_coverage,
    inventory_state,
    make_purchase_option,
    matching_request,
)


def _valid_pur(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _enabled_purchase_modes(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    return [
        dict(mode)
        for mode in rows
        if isinstance(mode, dict)
        and str(mode.get("kind") or "").strip().upper() == "PURCHASE"
        and mode.get("enabled") is not False
    ]


def _artifact_purchase_modes(result: GameTestResult) -> tuple[str, list[dict[str, Any]]]:
    run_dir = str(result.run_dir or "").strip()
    if not run_dir:
        return "UNKNOWN", []
    path = Path(run_dir) / "discovery" / "modes.json"
    if not path.is_file():
        return "UNKNOWN", []
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "UNKNOWN", []
    if not isinstance(catalog, dict) or catalog.get("schema") != "tester-spin/pragmatic-mode-catalog/v2":
        return "UNKNOWN", []
    modes = catalog.get("modes")
    if not isinstance(modes, list):
        return "UNKNOWN", []
    # The current-run doInit-derived mode catalog is authoritative for root wager
    # actions. Its purchase inventory is independent of later FSO/bonus branch
    # expansion, which is audited by the general path-coverage gate.
    return "COMPLETE", _enabled_purchase_modes(modes)


def _purchase_inventory_state(
    result: GameTestResult,
    result_modes: list[dict[str, Any]],
) -> tuple[str, bool, str]:
    artifact_state, artifact_modes = _artifact_purchase_modes(result)

    if artifact_state == "COMPLETE":
        artifact_ids = [str(mode.get("id") or "") for mode in artifact_modes]
        result_ids = [str(mode.get("id") or "") for mode in result_modes]
        if sorted(artifact_ids) != sorted(result_ids):
            return (
                "INCOMPLETE",
                False,
                "Current doInit purchase roots contradict result.discovered_modes.",
            )
        candidate_modes = artifact_modes
        no_purchase_proven = not candidate_modes
    elif result_modes:
        # For synthetic/legacy results without the current-run artifact, explicit
        # provider-local purchase rows can still close *presence* if every selector
        # is self-consistent. This never proves absence.
        candidate_modes = result_modes
        no_purchase_proven = False
    else:
        fallback = inventory_state(result)
        return (
            fallback,
            fallback == "COMPLETE",
            "No current doInit mode catalog; falling back to the general root inventory only for authoritative absence.",
        )

    selectors: list[int] = []
    mode_ids: set[str] = set()
    for mode in candidate_modes:
        mode_id = str(mode.get("id") or "").strip()
        pur = _valid_pur(mode.get("provider_pur"))
        if not mode_id or mode_id in mode_ids or pur is None or mode.get("price_known") is not True:
            return (
                "INCOMPLETE",
                False,
                "Pragmatic purchase roots contain a duplicate/invalid id, selector, or unresolved price contract.",
            )
        if pur in selectors:
            return (
                "INCOMPLETE",
                False,
                "Pragmatic doInit maps multiple purchase roots to the same pur selector.",
            )
        mode_ids.add(mode_id)
        selectors.append(pur)

    return (
        "COMPLETE",
        no_purchase_proven,
        "Pragmatic purchase root inventory is closed from current doInit independently of continuation branch coverage.",
    )


def build_pragmatic_purchase_coverage(result: GameTestResult) -> dict[str, Any]:
    modes = _enabled_purchase_modes(result.discovered_modes)
    inventory, no_purchase_proven, inventory_reason = _purchase_inventory_state(result, modes)
    options: list[dict[str, Any]] = []

    for mode in modes:
        mode_id = str(mode.get("id") or "PURCHASE_UNKNOWN")
        pur = _valid_pur(mode.get("provider_pur"))
        price_known = mode.get("price_known") is True
        contract_proven = pur is not None and price_known
        exact_attempt = None
        attempted_with_contract = False

        if pur is not None:
            for attempt in attempts_for_mode(result, mode_id):
                request = matching_request(
                    result,
                    attempt,
                    lambda payload, expected=pur: (
                        str(payload.get("action") or "") == "doSpin"
                        and str(payload.get("pur") or "") == str(expected)
                    ),
                )
                if request is None:
                    continue
                attempted_with_contract = True
                if clean_terminal_attempt(attempt):
                    exact_attempt = attempt
                    break

        if exact_attempt is not None and contract_proven:
            execution_state = "COMPLETE"
            reason = "Exact Pragmatic pur selector observed on a clean terminal doSpin attempt."
            artifact_dir = exact_attempt.artifact_dir
            terminal = True
        elif attempted_with_contract and contract_proven:
            execution_state = "FAILED"
            reason = "Exact Pragmatic pur selector was sent, but no clean terminal purchase attempt completed."
            artifact_dir = next(
                (attempt.artifact_dir for attempt in attempts_for_mode(result, mode_id) if attempt.artifact_dir),
                "",
            )
            terminal = False
        else:
            execution_state = "NOT_ATTEMPTED"
            reason = (
                "Pragmatic purchase selector/price contract is incomplete or the stored request does not prove the exact pur value."
            )
            artifact_dir = ""
            terminal = False

        options.append(
            make_purchase_option(
                mode_id,
                provider_selector={"pur": pur} if pur is not None else None,
                display_name=mode_id,
                source_kind="pragmatic-doInit-purInit",
                source_evidence={
                    "source_field": mode.get("source_field"),
                    "source_value": mode.get("source_value"),
                    "provider_pur": pur,
                    "price_known": price_known,
                },
                price=mode.get("paid_cost"),
                price_multiplier=mode.get("price_x_base"),
                currency_or_stake_basis="base_bet",
                executable=contract_proven,
                wire_contract_state="PROVEN" if contract_proven else "UNKNOWN",
                execution_state=execution_state,
                terminal=terminal,
                artifact_dir=artifact_dir,
                reason=reason,
            )
        )

    return finalize_purchase_coverage(
        result,
        options=options,
        inventory_state=inventory,
        authority="pragmatic-doInit-purInit+runtime-wire",
        no_purchase_proven=(inventory == "COMPLETE" and no_purchase_proven and not modes),
        reason=inventory_reason,
    )


__all__ = ["build_pragmatic_purchase_coverage"]
