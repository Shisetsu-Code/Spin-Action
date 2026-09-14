from __future__ import annotations

from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.result_farm_contract import (
    ProviderFarmSpec,
    build_result_farm_contract,
    validate_result_farm_contract,
)
from tester_spin.providers.rubyplay.exhaustive import RubyPlayProvider as _RubyPlayProvider


_SPEC = ProviderFarmSpec(
    provider="rubyplay",
    protocol_family="rubyplay-gameserver",
    bootstrap_strategy="rubyplay-public-demo-http",
    transport="http-json",
    terminal_contract={
        "type": "provider",
        "name": "rubyplay-next-action",
        "terminal_next_action": "spin",
    },
    identifier_metadata_keys=("identifier",),
    stable_metadata_keys=(
        "identifier",
        "runtime_transport",
        "state_authority",
        "state_carrier",
        "bet_profile",
        "client_profile",
    ),
    mode_option_keys=(
        "bet_policy",
        "allowed_bets",
        "default_bet",
        "wager",
        "effective_stake",
        "buy_feature_type",
        "pricing_basis",
        "feature_multiplier",
        "default_price",
        "observed_indices",
    ),
    runtime_outputs=("launcher_parameters", "gameserver_endpoint", "action_number"),
    protocol_static={
        "state_authority": "response.data.next_action",
        "base_action": "spin",
        "known_continuation_actions": ["respin"],
    },
)


class RubyPlayProvider(_RubyPlayProvider):
    """Active RubyPlay provider with post-discovery farm export hooks."""

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def build_farm_contract(self, game: Game, result: GameTestResult) -> dict:
        return build_result_farm_contract(game, result, self.game_dir(game), _SPEC)

    def validate_farm_contract(self, contract: dict) -> list[str]:
        return validate_result_farm_contract(contract, _SPEC)


RubyPlayProvider.__module__ = "tester_spin.providers.rubyplay.exhaustive"

__all__ = ["RubyPlayProvider"]
