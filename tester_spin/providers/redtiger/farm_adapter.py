from __future__ import annotations

from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.farm_structure import attach_execution_structure, select_domains
from tester_spin.providers.redtiger.provider import RedTigerProvider as _RedTigerProvider
from tester_spin.providers.redtiger.purchase_coverage import build_redtiger_purchase_coverage
from tester_spin.providers.result_farm_contract import (
    ProviderFarmSpec,
    build_result_farm_contract,
    validate_result_farm_contract,
)


_SPEC = ProviderFarmSpec(
    provider="redtiger",
    protocol_family="redtiger-platform-game",
    bootstrap_strategy="evolution-public-start-http",
    transport="http-json",
    terminal_contract={
        "type": "provider",
        "name": "redtiger-success-no-pending-choice",
        "requires_success": True,
        "requires_no_pending_choice": True,
    },
    identifier_metadata_keys=("launch_id", "wp_post_id"),
    stable_metadata_keys=(
        "launch_id",
        "wp_post_id",
        "catalog_game_id",
        "runtime_game_id",
        "runtime_transport",
        "runtime_bootstrap",
        "stakes",
        "default_stake",
        "feature_buys",
        "has_feature_buy",
        "game_modes",
        "math_modes",
    ),
    mode_option_keys=(
        "stake",
        "stakes",
        "feature_buy",
        "feature_multiplier",
        "cost",
        "available",
        "selected_for_validation",
        "values",
    ),
    runtime_outputs=("runtime_game_id", "settings_endpoint", "spin_endpoint", "response_token"),
    protocol_static={
        "spin_action": "platform/game/spin",
        "choice_action": "platform/game/choice",
        "choice_domains_must_be_observed": True,
    },
)


class RedTigerProvider(_RedTigerProvider):
    """Active Red Tiger provider with post-discovery farm export hooks."""

    # Evolution/Red Tiger bootstrap currently depends on one browser/runtime flow
    # at a time. Keep it serial until multi-session bootstrap is proven safe.
    max_test_concurrency = 1

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def build_purchase_coverage(self, game: Game, result: GameTestResult) -> dict:
        return build_redtiger_purchase_coverage(result, self.game_dir(game))

    def build_farm_contract(self, game: Game, result: GameTestResult) -> dict:
        contract = build_result_farm_contract(game, result, self.game_dir(game), _SPEC)
        protocol = contract.get("protocol")
        stable = protocol.get("stable_metadata") if isinstance(protocol, dict) else {}
        domains = select_domains(
            stable,
            ("stakes", "default_stake", "feature_buys", "game_modes", "math_modes"),
        )
        return attach_execution_structure(contract, provider_domains=domains)

    def validate_farm_contract(self, contract: dict) -> list[str]:
        return validate_result_farm_contract(contract, _SPEC)


RedTigerProvider.__module__ = "tester_spin.providers.redtiger.provider"

__all__ = ["RedTigerProvider"]
