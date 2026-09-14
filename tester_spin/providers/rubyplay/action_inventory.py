from __future__ import annotations

import re
from typing import Any

from tester_spin.models import GameTestResult

_SOURCE = "rubyplay-active-client+init-session"
_ROOT_KINDS = {"SPIN", "PURCHASE"}


def _mode_ids(rows: Any) -> list[str]:
    if not isinstance(rows, list):
        return []
    values: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("enabled") is False:
            continue
        if str(row.get("kind") or "").strip().upper() not in _ROOT_KINDS:
            continue
        mode_id = str(row.get("id") or "").strip()
        if mode_id and mode_id not in values:
            values.append(mode_id)
    return sorted(values)


def _purchase_mode_id(feature_type: str) -> str:
    suffix = re.sub(r"[^A-Z0-9]+", "_", str(feature_type or "").upper()).strip("_")
    return f"PURCHASE_{suffix or 'FEATURE'}"


def _inventory(
    *,
    state: str,
    reason: str,
    roots: list[str],
    missing: list[str] | None = None,
    unexpected: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "source": _SOURCE,
        "reason": reason,
        "root_actions": sorted(roots),
        "missing_root_actions": sorted(missing or []),
        "unexpected_root_actions": sorted(unexpected or []),
    }


def annotate_rubyplay_action_inventory(result: GameTestResult, runtime: Any) -> GameTestResult:
    """Close RubyPlay root actions only from the active client and current init.

    Continuations are response-driven state transitions and remain independently
    audited from observed wire evidence. This inventory covers only root wager
    actions: base spin and an optional Buy Feature action.
    """
    if not isinstance(result.structural_map, dict):
        result.structural_map = {}

    profile = getattr(runtime, "client_profile", None)
    capability = getattr(profile, "buy_feature_client_supported", None)
    init_data = getattr(runtime, "init_data", None)
    body = init_data.get("data") if isinstance(init_data, dict) else None
    available = body.get("buy_feature_available") if isinstance(body, dict) else None
    result_roots = _mode_ids(result.discovered_modes)

    if capability is None:
        result.structural_map["action_inventory"] = _inventory(
            state="UNKNOWN",
            reason="RubyPlay client Buy Feature capability is unresolved; root inventory cannot be closed.",
            roots=result_roots,
        )
        return result

    if not isinstance(available, bool):
        result.structural_map["action_inventory"] = _inventory(
            state="UNKNOWN",
            reason="RubyPlay init does not expose a boolean buy_feature_available value.",
            roots=result_roots,
        )
        return result

    expected = ["SPIN"]
    if capability is False and available is True:
        result.structural_map["action_inventory"] = _inventory(
            state="INCOMPLETE",
            reason="RubyPlay client proves Buy Feature absent but current init still advertises it.",
            roots=result_roots,
            unexpected=[mode for mode in result_roots if mode != "SPIN"],
        )
        return result

    if capability is True and available is True:
        feature_type = str(getattr(profile, "buy_feature_type", "") or "").strip()
        multiplier = getattr(profile, "buy_feature_multiplier", None)
        wager = getattr(profile, "wager", None)
        if (
            not feature_type
            or not isinstance(multiplier, (int, float))
            or isinstance(multiplier, bool)
            or multiplier <= 0
            or not isinstance(wager, (int, float))
            or isinstance(wager, bool)
            or wager <= 0
        ):
            result.structural_map["action_inventory"] = _inventory(
                state="INCOMPLETE",
                reason="RubyPlay Buy Feature is available but its client purchase contract is incomplete.",
                roots=result_roots,
                missing=["BUY_FEATURE_CONTRACT"],
            )
            return result
        expected.append(_purchase_mode_id(feature_type))

    expected = sorted(expected)
    missing = sorted(set(expected) - set(result_roots))
    unexpected = sorted(set(result_roots) - set(expected))
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append(f"missing root actions={missing}")
        if unexpected:
            details.append(f"unexpected root actions={unexpected}")
        result.structural_map["action_inventory"] = _inventory(
            state="INCOMPLETE",
            reason="RubyPlay root inventory contradicts active client/init: " + "; ".join(details) + ".",
            roots=expected,
            missing=missing,
            unexpected=unexpected,
        )
        return result

    if str(result.status or "").strip().upper() != "OK":
        result.structural_map["action_inventory"] = _inventory(
            state="UNKNOWN",
            reason=f"RubyPlay root actions match, but runtime status is {result.status!r}; inventory is not promoted.",
            roots=expected,
        )
        return result

    result.structural_map["action_inventory"] = _inventory(
        state="COMPLETE",
        reason="Active RubyPlay client capability and current init close the root wager actions.",
        roots=expected,
    )
    return result


__all__ = ["annotate_rubyplay_action_inventory"]
