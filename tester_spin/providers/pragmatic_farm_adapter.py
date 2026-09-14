from __future__ import annotations

from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.pragmatic_exhaustive import PragmaticProvider as _PragmaticProvider
from tester_spin.providers.result_farm_contract import (
    ProviderFarmSpec,
    build_result_farm_contract,
    validate_result_farm_contract,
)


_SPEC = ProviderFarmSpec(
    provider="pragmatic",
    protocol_family="pragmatic-game-service",
    bootstrap_strategy="pragmatic-public-demo-http",
    transport="http-form-urlencoded",
    terminal_contract={
        "type": "provider",
        "name": "pragmatic-na-terminal",
        "terminal_states": ["", "s"],
        "requires_feature_inactive": True,
    },
    identifier_metadata_keys=("provider_internal_id",),
    stable_metadata_keys=("provider_internal_id",),
    mode_option_keys=(
        "provider_bl",
        "provider_pur",
        "ordinal",
        "price_x_base",
        "paid_cost",
        "price_known",
        "source_field",
        "source_value",
    ),
    runtime_outputs=("mgckey", "game_service_endpoint", "index", "counter"),
    protocol_static={
        "entry_action": "doSpin",
        "known_continuation_actions": [
            "doBonus",
            "doCollectBonus",
            "doCollect",
            "doSpin",
            "doFSOption",
            "doMysteryScatter",
        ],
    },
)


class PragmaticProvider(_PragmaticProvider):
    """Active Pragmatic provider with post-discovery farm export hooks."""

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def build_farm_contract(self, game: Game, result: GameTestResult) -> dict:
        return build_result_farm_contract(game, result, self.game_dir(game), _SPEC)

    def validate_farm_contract(self, contract: dict) -> list[str]:
        return validate_result_farm_contract(contract, _SPEC)


PragmaticProvider.__module__ = "tester_spin.providers.pragmatic_hybrid"

__all__ = ["PragmaticProvider"]
