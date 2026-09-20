from __future__ import annotations

from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.bgaming.farm_contract import (
    build_bgaming_farm_contract,
    validate_bgaming_farm_contract,
)
from tester_spin.providers.bgaming.feature_sessions import build_bgaming_feature_sessions
from tester_spin.providers.bgaming.profile import SWITCHABLE
from tester_spin.providers.bgaming_paths_v2 import BGamingProvider as _BGamingProvider
from tester_spin.providers.farm_structure import (
    apply_feature_session_gate,
    attach_execution_structure,
    select_domains,
)


def _close_switchable_variant_spin_contract(contract: dict) -> dict:
    """Treat proven switchable variants as the protocol's spin entry points.

    Switchable-container games do not expose a canonical ``SPIN`` mode. Each
    required VARIANT performs ``lobby_switch+init+spin`` and is independently
    proven terminal by the runtime. The generic BGaming farm builder therefore
    emits SPIN_CONTRACT_MISSING even when every concrete variant is already
    demonstrated. Remove only that synthetic blocker, and only when all
    required switchable variants have exact terminal proof. Any other unresolved
    item remains fail-closed and keeps the contract non-ready.
    """
    source = contract.get("source")
    family = str(source.get("protocol_family") or "") if isinstance(source, dict) else ""
    if family != SWITCHABLE:
        return contract

    unresolved = contract.get("unresolved")
    if not isinstance(unresolved, list) or "SPIN_CONTRACT_MISSING" not in unresolved:
        return contract

    modes = contract.get("modes")
    if not isinstance(modes, list):
        return contract
    variants = [
        mode
        for mode in modes
        if isinstance(mode, dict)
        and str(mode.get("kind") or "").upper() == "VARIANT"
        and bool(mode.get("required"))
    ]
    if not variants:
        return contract
    if any(
        str(mode.get("evidence") or "") != "DEMOSTRADO"
        or str(mode.get("executor") or "") != "lobby_switch+init+spin"
        for mode in variants
    ):
        return contract

    contract["unresolved"] = [
        reason for reason in unresolved if str(reason) != "SPIN_CONTRACT_MISSING"
    ]
    contract["ready"] = not contract["unresolved"]
    return contract


class BGamingProvider(_BGamingProvider):
    """Active BGaming provider with stable post-discovery farm export hooks."""

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def build_feature_sessions(self, result: GameTestResult) -> dict:
        return build_bgaming_feature_sessions(result)

    def build_farm_contract(
        self,
        game: Game,
        result: GameTestResult,
    ) -> dict:
        contract = build_bgaming_farm_contract(game, result, self.game_dir(game))
        contract = _close_switchable_variant_spin_contract(contract)
        contract = apply_feature_session_gate(contract, result)
        protocol = contract.get("protocol")
        profile = protocol.get("profile") if isinstance(protocol, dict) else {}
        domains = select_domains(
            profile,
            (
                "spin_option_choices",
                "effective_bet_selector",
                "effective_bet_multipliers",
                "purchase_features",
            ),
        )
        return attach_execution_structure(contract, provider_domains=domains)

    def validate_farm_contract(self, contract: dict) -> list[str]:
        return validate_bgaming_farm_contract(contract)


BGamingProvider.__module__ = "tester_spin.providers.bgaming"

__all__ = ["BGamingProvider"]
