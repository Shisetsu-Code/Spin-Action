from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from tester_spin.models import GameTestResult
from tester_spin.purchase_coverage import (
    attempts_for_mode,
    clean_terminal_attempt,
    finalize_purchase_coverage,
    make_purchase_option,
    matching_request,
)


def _load_game_metadata(game_dir: Path) -> dict[str, Any]:
    path = Path(game_dir) / "game.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _positive(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _same_number(left: Any, right: Any) -> bool:
    a = _positive(left)
    b = _positive(right)
    return a is not None and b is not None and math.isclose(a, b, rel_tol=0.0, abs_tol=1e-9)


def _request_matches(payload: dict[str, Any], *, name: str, cost: Any) -> bool:
    extras = payload.get("extras")
    if not isinstance(extras, dict):
        return False
    features = extras.get("features")
    if not isinstance(features, dict):
        return False
    if str(features.get("featureBuy") or "") != name:
        return False
    if cost not in (None, "") and not _same_number(features.get("featureBuyCost"), cost):
        return False
    return True


def build_redtiger_purchase_coverage(
    result: GameTestResult,
    game_dir: Path,
) -> dict[str, Any]:
    metadata = _load_game_metadata(Path(game_dir))
    has_feature_buy = metadata.get("has_feature_buy")
    authoritative_buys_raw = metadata.get("feature_buys")
    authoritative_buys: list[dict[str, Any]] = []
    if isinstance(authoritative_buys_raw, list):
        for item in authoritative_buys_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            multiplier = _positive(item.get("multiplier"))
            if name and multiplier is not None:
                authoritative_buys.append({"name": name, "multiplier": multiplier})

    modes = [
        mode
        for mode in result.discovered_modes
        if isinstance(mode, dict) and str(mode.get("kind") or "").upper() == "PURCHASE"
    ]
    mode_by_name = {
        str(mode.get("feature_buy") or "").strip(): mode
        for mode in modes
        if str(mode.get("feature_buy") or "").strip()
    }
    expected_names = {item["name"] for item in authoritative_buys}

    # A non-empty feature_buys list is itself parsed from the current SETTINGS
    # response and is authoritative proof of purchase presence. The boolean flag
    # is only needed to prove explicit absence or detect SETTINGS contradictions.
    if authoritative_buys:
        if has_feature_buy is False:
            inventory = "INCOMPLETE"
        else:
            inventory = "COMPLETE" if expected_names == set(mode_by_name) else "INCOMPLETE"
    elif has_feature_buy is True:
        inventory = "INCOMPLETE"
    elif has_feature_buy is False:
        inventory = "COMPLETE"
    else:
        inventory = "UNKNOWN"

    options: list[dict[str, Any]] = []
    for buy in authoritative_buys:
        name = str(buy["name"])
        multiplier = buy["multiplier"]
        mode = mode_by_name.get(name)
        mode_id = str((mode or {}).get("id") or f"PURCHASE_{name}")
        cost = (mode or {}).get("cost")
        contract_proven = bool(
            mode
            and mode.get("executable") is True
            and str(mode.get("wire_command") or "") == "platform/game/spin"
            and _positive(multiplier) is not None
            and _positive(cost) is not None
        )
        exact_attempt = None
        attempted_with_contract = False

        if contract_proven:
            for attempt in attempts_for_mode(result, mode_id):
                request = matching_request(
                    result,
                    attempt,
                    lambda payload, expected_name=name, expected_cost=cost: _request_matches(
                        payload,
                        name=expected_name,
                        cost=expected_cost,
                    ),
                )
                if request is None:
                    continue
                attempted_with_contract = True
                if clean_terminal_attempt(attempt):
                    exact_attempt = attempt
                    break

        if exact_attempt is not None:
            execution_state = "COMPLETE"
            terminal = True
            artifact_dir = exact_attempt.artifact_dir
            reason = "Exact Red Tiger featureBuy selector observed on a clean terminal platform/game/spin attempt."
        elif attempted_with_contract:
            execution_state = "FAILED"
            terminal = False
            artifact_dir = next(
                (attempt.artifact_dir for attempt in attempts_for_mode(result, mode_id) if attempt.artifact_dir),
                "",
            )
            reason = "Exact Red Tiger featureBuy request was sent, but its purchased round did not close cleanly."
        else:
            execution_state = "NOT_ATTEMPTED"
            terminal = False
            artifact_dir = ""
            reason = "Red Tiger SETTINGS purchase exists but exact executable request evidence is incomplete."

        options.append(
            make_purchase_option(
                mode_id,
                provider_selector={"featureBuy": name},
                display_name=name,
                source_kind="redtiger-current-settings",
                source_evidence={"name": name, "multiplier": multiplier},
                price=cost,
                price_multiplier=multiplier,
                currency_or_stake_basis="stake*feature_multiplier",
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
        authority="redtiger-current-settings+runtime-wire",
        no_purchase_proven=(has_feature_buy is False and inventory == "COMPLETE" and not options),
        reason=(
            "Red Tiger SETTINGS closes purchase presence from concrete feature_buys."
            if authoritative_buys and inventory == "COMPLETE"
            else "Red Tiger SETTINGS explicitly closes feature-buy absence."
            if has_feature_buy is False and inventory == "COMPLETE"
            else "Red Tiger SETTINGS feature-buy inventory is unavailable or contradictory."
        ),
    )


__all__ = ["build_redtiger_purchase_coverage"]
