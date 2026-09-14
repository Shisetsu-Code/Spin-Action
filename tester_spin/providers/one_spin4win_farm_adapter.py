from __future__ import annotations

from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.farm_structure import attach_execution_structure
from tester_spin.providers.one_spin4win_exhaustive import OneSpin4WinProvider as _OneSpin4WinProvider
from tester_spin.providers.result_farm_contract import (
    ProviderFarmSpec,
    build_result_farm_contract,
    validate_result_farm_contract,
)


_SPEC = ProviderFarmSpec(
    provider="1spin4win",
    protocol_family="d1-websocket",
    bootstrap_strategy="1spin4win-public-demo-websocket",
    transport="websocket-json",
    terminal_contract={
        "type": "provider",
        "name": "d1-type3-state",
        "terminal_result_states": [0],
        "known_active_result_states": [5, 6, 11, 12],
    },
    mode_option_keys=("states",),
    runtime_outputs=("websocket_endpoint", "game_version", "wallet", "currency"),
    protocol_static={
        "initial_envelope": "A/u2 type=0",
        "spin_envelope": "A/u2 type=1",
        "result_message_type": 3,
    },
)


class OneSpin4WinProvider(_OneSpin4WinProvider):
    """Active D1 provider with post-discovery farm export hooks."""

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def build_farm_contract(self, game: Game, result: GameTestResult) -> dict:
        contract = build_result_farm_contract(game, result, self.game_dir(game), _SPEC)
        return attach_execution_structure(contract)

    def validate_farm_contract(self, contract: dict) -> list[str]:
        return validate_result_farm_contract(contract, _SPEC)


OneSpin4WinProvider.__module__ = "tester_spin.providers.one_spin4win_exhaustive"

__all__ = ["OneSpin4WinProvider"]
