from __future__ import annotations

import threading
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.farm_structure import attach_execution_structure
from tester_spin.providers.one_spin4win_exhaustive import OneSpin4WinProvider as _OneSpin4WinProvider
from tester_spin.providers.one_spin4win_purchase_coverage import build_one_spin4win_purchase_coverage
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


class _RateLimitedWebSocket:
    """Transparent websocket proxy that accounts every outbound D1 frame."""

    def __init__(self, provider: "OneSpin4WinProvider", websocket) -> None:
        self._provider = provider
        self._websocket = websocket

    def send(self, value):
        if not self._provider.acquire_provider_request_slot():
            raise InterruptedError("D1 request cancelled by provider rate limiter")
        return self._websocket.send(value)

    def recv(self, *args, **kwargs):
        return self._websocket.recv(*args, **kwargs)

    def close(self, *args, **kwargs):
        return self._websocket.close(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._websocket, name)


class OneSpin4WinProvider(_OneSpin4WinProvider):
    """Active D1 provider with post-discovery farm export hooks."""

    def _open_websocket(self, spec: dict, timeout_s: float):
        websocket = super()._open_websocket(spec, timeout_s)
        return _RateLimitedWebSocket(self, websocket)

    def test_natural_spins(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        # D1 test_game already contains only natural playGame spins and their
        # server-directed feature continuations; it has no purchase/ante matrix.
        return self.test_game(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )

    def build_purchase_coverage(self, game: Game, result: GameTestResult) -> dict:
        del game
        return build_one_spin4win_purchase_coverage(result)

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def build_farm_contract(self, game: Game, result: GameTestResult) -> dict:
        contract = build_result_farm_contract(game, result, self.game_dir(game), _SPEC)
        return attach_execution_structure(contract)

    def validate_farm_contract(self, contract: dict) -> list[str]:
        return validate_result_farm_contract(contract, _SPEC)


OneSpin4WinProvider.__module__ = "tester_spin.providers.one_spin4win_exhaustive"

__all__ = ["OneSpin4WinProvider"]
