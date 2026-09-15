from __future__ import annotations

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


def build_pragmatic_purchase_coverage(result: GameTestResult) -> dict[str, Any]:
    inventory = inventory_state(result)
    modes = [
        mode
        for mode in result.discovered_modes
        if isinstance(mode, dict)
        and str(mode.get("kind") or "").upper() == "PURCHASE"
        and mode.get("enabled") is not False
    ]
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
        no_purchase_proven=(inventory == "COMPLETE" and not modes),
        reason=(
            "Pragmatic purchase inventory is closed from current doInit and exact pur wire evidence."
            if inventory == "COMPLETE"
            else "Pragmatic root action inventory is not closed."
        ),
    )


__all__ = ["build_pragmatic_purchase_coverage"]
