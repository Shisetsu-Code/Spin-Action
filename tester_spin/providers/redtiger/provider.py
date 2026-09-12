from __future__ import annotations

import threading

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.redtiger.adapter import RedTigerProvider as RedTigerAdapter


class RedTigerProvider(RedTigerAdapter):
    """Public provider boundary preserving catalog launch and runtime identities.

    ``Game.symbol``/SQLite stores the public Evolution WordPress post id because
    that is the stable identifier consumed by the official ``games/v1/start``
    flow. The Red Tiger runtime ``gameId`` remains separate and is discovered from
    the live settings request, then stored only in runtime metadata/artifacts.
    """

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
        result = super().test_game(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        if launch_id:
            game.symbol = launch_id
            result.symbol = launch_id
        return result
