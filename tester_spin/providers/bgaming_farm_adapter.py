from __future__ import annotations

from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.bgaming.farm_contract import (
    build_bgaming_farm_contract,
    validate_bgaming_farm_contract,
)
from tester_spin.providers.bgaming_paths_v2 import BGamingProvider as _BGamingProvider
from tester_spin.providers.farm_structure import attach_execution_structure, select_domains


class BGamingProvider(_BGamingProvider):
    """Active BGaming provider with stable post-discovery farm export hooks."""

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def build_farm_contract(
        self,
        game: Game,
        result: GameTestResult,
    ) -> dict:
        contract = build_bgaming_farm_contract(game, result, self.game_dir(game))
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


# Preserve the public provider identity used by existing wiring/regression tests.
BGamingProvider.__module__ = "tester_spin.providers.bgaming"

__all__ = ["BGamingProvider"]
