from __future__ import annotations

import threading

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import Progress
from tester_spin.providers.redtiger.adapter import RedTigerProvider as RedTigerAdapter


class RedTigerProvider(RedTigerAdapter):
    """Public provider boundary preserving Red Tiger's two distinct identifiers.

    ``Game.symbol``/SQLite remains the CMS ``tableId`` because that is the stable
    input required to create the next fresh demo. The runtime ``gameId`` discovered
    from the official launcher is stored in game.json and per-attempt artifacts.
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
        table_id = self.table_id_for_game(game)
        result = super().test_game(
            game,
            spins=spins,
            timeout_s=timeout_s,
            stop_event=stop_event,
            progress=progress,
        )
        if table_id:
            game.symbol = table_id
            result.symbol = table_id
        return result
