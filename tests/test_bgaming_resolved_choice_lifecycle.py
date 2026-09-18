from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from tester_spin.models import Game, GameTestResult
import tester_spin.providers.bgaming_exhaustive as bgaming_exhaustive


def test_exhaustive_provider_resets_resolved_dynamic_registry_per_game() -> None:
    with tempfile.TemporaryDirectory() as temp:
        provider = bgaming_exhaustive.BGamingProvider(Path(temp) / "data")
        game = Game(
            provider="bgaming",
            slug="synthetic",
            name="Synthetic",
            url="https://example.invalid/game",
        )
        result = GameTestResult(
            provider="bgaming",
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
        )
        with (
            patch.object(bgaming_exhaustive, "begin_resolved_dynamic_choice_run") as begin,
            patch.object(bgaming_exhaustive, "end_resolved_dynamic_choice_run") as end,
            patch.object(
                bgaming_exhaustive.BGamingProvider,
                "_test_game_exhaustive",
                return_value=result,
            ) as inner,
        ):
            observed = provider.test_game(
                game,
                spins=1,
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
            )

    assert observed is result
    begin.assert_called_once_with()
    end.assert_called_once_with()
    inner.assert_called_once()
