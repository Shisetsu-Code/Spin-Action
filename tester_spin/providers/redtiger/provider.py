from __future__ import annotations

import threading

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.redtiger import execution as redtiger_execution
from tester_spin.providers.redtiger.adapter import RedTigerProvider as RedTigerAdapter
from tester_spin.providers.redtiger.evolution_launch import bootstrap_game as evolution_bootstrap_game


class RedTigerProvider(RedTigerAdapter):
    """Public provider boundary preserving catalog launch and runtime identities."""

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress: Progress,
    ) -> GameTestResult:
        launch_id = self.launch_id_for_game(game)
        original_bootstrap = redtiger_execution.bootstrap_game
        redtiger_execution.bootstrap_game = evolution_bootstrap_game
        try:
            result = super().test_game(
                game,
                spins=spins,
                timeout_s=timeout_s,
                stop_event=stop_event,
                progress=progress,
            )
        finally:
            redtiger_execution.bootstrap_game = original_bootstrap

        if launch_id:
            game.symbol = launch_id
            result.symbol = launch_id
        return result
