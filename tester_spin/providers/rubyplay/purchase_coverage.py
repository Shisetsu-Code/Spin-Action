from __future__ import annotations

import math
from typing import Any

from tester_spin.models import GameTestResult, SpinAttempt
from tester_spin.providers.rubyplay.choice_domains import rubyplay_choice_domain_is_proven
from tester_spin.purchase_coverage import (
    attempts_for_mode,
    clean_terminal_attempt,
    finalize_purchase_coverage,
    inventory_state,
    make_purchase_option,
    matching_request,
    request_payloads,
)


_INDEXED_BRANCH_ACTIONS = {"select", "pick"}


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _same_number(left: Any, right: Any) -> bool:
    a = _positive_number(left)
    b = _positive_number(right)
    return a is not None and b is not None and math.isclose(a, b, rel_tol=0.0, abs_tol=1e-9)


def _indexed_domain_closed(
    result: GameTestResult,
    parent_mode: str,
    action: str,
) -> bool:
    candidates = [
        mode
        for mode in result.discovered_modes
        if isinstance(mode, dict)
        and str(mode.get("kind") or "").upper() == "INDEXED_CHOICE"
        and str(mode.get("parent") or "") == str(parent_mode or "")
        and str(mode.get("wire_command") or "").strip().lower() == action
    ]
    return bool(candidates) and all(
        rubyplay_choice_domain_is_proven(mode)
        for mode in candidates
    )


def _unresolved_indexed_actions(
    result: GameTestResult,
    attempt: SpinAttempt,
) -> list[str]:
    observed: set[str] = set()
    for payload in request_payloads(result, attempt):
        action = str(payload.get("action") or "").strip().lower()
        if action in _INDEXED_BRANCH_ACTIONS:
            observed.add(action)
    return sorted(
        action
        for action in observed
        if not _indexed_domain_closed(result, str(attempt.mode_id or ""), action)
    )


def build_rubyplay_purchase_coverage(result: GameTestResult) -> dict[str, Any]:
    inventory = inventory_state(result)
    modes = [
        mode
        for mode in result.discovered_modes
        if isinstance(mode, dict)
        and str(mode.get("kind") or "").upper() == "PURCHASE"
    ]
    options: list[dict[str, Any]] = []

    for mode in modes:
        mode_id = str(mode.get("id") or "PURCHASE_UNKNOWN")
        feature_type = str(mode.get("buy_feature_type") or "").strip().lower()
        price = _positive_number(mode.get("default_price"))
        multiplier = _positive_number(mode.get("feature_multiplier"))
        contract_proven = bool(
            mode.get("executable") is True
            and str(mode.get("wire_command") or "").lower() == "buy_feature"
            and feature_type
            and price is not None
            and multiplier is not None
        )
        exact_attempt = None
        attempted_with_contract = False
        unresolved_branch_actions: list[str] = []
        unresolved_branch_artifact = ""

        if contract_proven:
            for attempt in attempts_for_mode(result, mode_id):
                request = matching_request(
                    result,
                    attempt,
                    lambda payload, expected_type=feature_type, expected_price=price: (
                        str(payload.get("action") or "").lower() == "buy_feature"
                        and str(payload.get("buy_feature_type") or "").strip().lower() == expected_type
                        and _same_number(payload.get("buy_feature_price"), expected_price)
                    ),
                )
                if request is None:
                    continue
                attempted_with_contract = True
                if clean_terminal_attempt(attempt):
                    unresolved = _unresolved_indexed_actions(result, attempt)
                    if unresolved:
                        unresolved_branch_actions = unresolved
                        unresolved_branch_artifact = attempt.artifact_dir
                        break
                    exact_attempt = attempt
                    break

        if unresolved_branch_actions:
            execution_state = "UNKNOWN"
            terminal = True
            artifact_dir = unresolved_branch_artifact
            reason = (
                "Exact RubyPlay buy_feature request reached terminal state, but indexed "
                "choice domain coverage remains unresolved for this purchase mode: "
                + ", ".join(unresolved_branch_actions)
                + "."
            )
        elif exact_attempt is not None:
            execution_state = "COMPLETE"
            terminal = True
            artifact_dir = exact_attempt.artifact_dir
            reason = "Exact RubyPlay buy_feature type and price observed on a clean terminal purchase attempt."
        elif attempted_with_contract:
            execution_state = "FAILED"
            terminal = False
            artifact_dir = next(
                (attempt.artifact_dir for attempt in attempts_for_mode(result, mode_id) if attempt.artifact_dir),
                "",
            )
            reason = "RubyPlay buy_feature request matched the contract, but no clean terminal attempt completed."
        else:
            execution_state = "NOT_ATTEMPTED"
            terminal = False
            artifact_dir = ""
            reason = "RubyPlay purchase contract or exact stored buy_feature request is incomplete."

        options.append(
            make_purchase_option(
                mode_id,
                provider_selector={"buy_feature_type": feature_type} if feature_type else None,
                display_name=feature_type or mode_id,
                source_kind="rubyplay-active-client+init-session",
                source_evidence={
                    "wire_command": mode.get("wire_command"),
                    "client_observed": mode.get("client_observed"),
                    "feature_multiplier": mode.get("feature_multiplier"),
                    "default_price": mode.get("default_price"),
                },
                price=mode.get("default_price"),
                price_multiplier=mode.get("feature_multiplier"),
                currency_or_stake_basis="bet*wager*feature_multiplier",
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
        authority="rubyplay-active-client+init-session+runtime-wire",
        no_purchase_proven=(inventory == "COMPLETE" and not modes),
        reason=(
            "RubyPlay purchase inventory is closed by active-client capability and current init."
            if inventory == "COMPLETE"
            else "RubyPlay purchase inventory is not closed."
        ),
    )


__all__ = ["build_rubyplay_purchase_coverage"]
