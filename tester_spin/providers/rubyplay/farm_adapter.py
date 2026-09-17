from __future__ import annotations

from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.farm_structure import attach_execution_structure, select_domains
from tester_spin.providers.result_farm_contract import (
    ProviderFarmSpec,
    build_result_farm_contract,
    validate_result_farm_contract,
)
from tester_spin.providers.rubyplay.choice_exhaustive import RubyPlayProvider as _RubyPlayProvider
from tester_spin.providers.rubyplay.feature_sessions import build_rubyplay_feature_sessions
from tester_spin.providers.rubyplay.purchase_coverage import build_rubyplay_purchase_coverage


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
        "boundary_index",
        "domain_authority",
    ),
    runtime_outputs=("launcher_parameters", "gameserver_endpoint", "action_number"),
    protocol_static={
        "state_authority": "response.data.next_action",
        "base_action": "spin",
        "known_continuation_actions": ["freespin", "respin", "minispin", "select", "pick"],
    },
)


class RubyPlayProvider(_RubyPlayProvider):
    """Active RubyPlay provider with post-discovery farm export hooks."""

    # Keep one launcher/runtime at a time until RubyPlay multi-session behavior is
    # explicitly validated. This avoids shared launcher/session state collisions.
    max_test_concurrency = 1

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def build_feature_sessions(self, result: GameTestResult) -> dict:
        return build_rubyplay_feature_sessions(result)

    def build_purchase_coverage(self, game: Game, result: GameTestResult) -> dict:
        del game
        return build_rubyplay_purchase_coverage(result)

    def build_farm_contract(self, game: Game, result: GameTestResult) -> dict:
        contract = build_result_farm_contract(game, result, self.game_dir(game), _SPEC)
        protocol = contract.get("protocol")
        stable = protocol.get("stable_metadata") if isinstance(protocol, dict) else {}
        domains = select_domains(stable, ("bet_profile", "client_profile"))
        return attach_execution_structure(contract, provider_domains=domains)

    def validate_farm_contract(self, contract: dict) -> list[str]:
        return validate_result_farm_contract(contract, _SPEC)


RubyPlayProvider.__module__ = "tester_spin.providers.rubyplay.exhaustive"

__all__ = ["RubyPlayProvider"]
