from __future__ import annotations

from typing import Any

from tester_spin.models import GameTestResult
from tester_spin.purchase_coverage import finalize_purchase_coverage, make_purchase_option


def build_one_spin4win_purchase_coverage(result: GameTestResult) -> dict[str, Any]:
    options: list[dict[str, Any]] = []

    for mode in result.discovered_modes:
        if not isinstance(mode, dict):
            continue
        kind = str(mode.get("kind") or "").upper()
        mode_id = str(mode.get("id") or "")
        if "PURCHASE" not in kind and "PURCHASE" not in mode_id.upper() and "BUY" not in mode_id.upper():
            continue
        options.append(
            make_purchase_option(
                mode_id or "D1_PURCHASE_CANDIDATE",
                provider_selector=None,
                display_name=mode_id,
                source_kind="1spin4win-client/runtime-candidate",
                source_evidence={
                    "kind": kind,
                    "wire_command": mode.get("wire_command"),
                    "required_options": mode.get("required_options"),
                },
                executable=False,
                wire_contract_state="UNKNOWN",
                execution_state="NOT_ATTEMPTED",
                terminal=False,
                reason=(
                    "1Spin4Win root purchase semantics are not proven: exact WebSocket message type, arguments, and option mapping are required before execution."
                ),
            )
        )

    structural = result.structural_map if isinstance(result.structural_map, dict) else {}
    evidence = structural.get("client_action_evidence")
    evidence_summary = evidence if isinstance(evidence, dict) else {}

    return finalize_purchase_coverage(
        result,
        options=options,
        inventory_state="UNKNOWN",
        authority="1spin4win-official-client/runtime-evidence",
        no_purchase_proven=False,
        reason=(
            "Current D1 evidence proves base play/continuations but not an authoritative root purchase contract or authoritative absence. "
            f"client_evidence_complete={bool(evidence_summary.get('complete'))}."
        ),
    )


__all__ = ["build_one_spin4win_purchase_coverage"]
