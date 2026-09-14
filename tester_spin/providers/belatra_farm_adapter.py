from __future__ import annotations

from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.belatra_exhaustive import BelatraProvider as _BelatraProvider
from tester_spin.providers.farm_structure import attach_execution_structure
from tester_spin.providers.result_farm_contract import (
    ProviderFarmSpec,
    build_result_farm_contract,
    validate_result_farm_contract,
)


_SPEC = ProviderFarmSpec(
    provider="belatra",
    protocol_family="belatra-encrypted-http",
    bootstrap_strategy="belatra-public-demo-http",
    transport="http-encrypted-json",
    terminal_contract={
        "type": "provider",
        "name": "belatra-finish-idle",
        "requires_terminal_attempt": True,
    },
    mode_option_keys=(
        "selector",
        "selector_value",
        "selectors",
        "mathType",
        "vipOn",
        "buy_bonus_option",
        "line_bet",
        "bet",
    ),
    runtime_outputs=("demo_endpoint", "enter_state", "crypto_context"),
    protocol_static={
        "flow": ["enter", "start", "finish"],
        "selectors_are_provider_discovered": True,
    },
)


class BelatraProvider(_BelatraProvider):
    """Active Belatra provider with post-discovery farm export hooks."""

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def build_farm_contract(self, game: Game, result: GameTestResult) -> dict:
        contract = build_result_farm_contract(game, result, self.game_dir(game), _SPEC)
        return attach_execution_structure(contract)

    def validate_farm_contract(self, contract: dict) -> list[str]:
        return validate_result_farm_contract(contract, _SPEC)


BelatraProvider.__module__ = "tester_spin.providers.belatra_exhaustive"

__all__ = ["BelatraProvider"]
