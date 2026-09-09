from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from tester_spin.models import Game
from tester_spin.providers.pragmatic import HttpBootstrap, PragmaticProvider


class _EmptyCatalog:
    base_bet = 0.20

    def enabled(self):
        return []

    def to_dict(self):
        return {}


class PragmaticKnownSymbolTests(unittest.TestCase):
    def test_known_symbol_bootstraps_without_public_game_page_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = PragmaticProvider(Path(temp))
            game = Game(
                provider="pragmatic",
                slug="known-game",
                name="Known Game",
                url="https://www.pragmaticplay.com/en/games/known-game/",
                symbol="vs20known",
            )
            session = requests.Session()
            bootstrap = HttpBootstrap(
                session=session,
                symbol="vs20known",
                mgckey="stylename@test~SESSION@test",
                cver="123456",
                endpoint="https://example.test/gameService",
                launch_url="https://example.test/game",
                spin_template={},
                init_request_raw="action=doInit",
                init_response_raw=b"",
                init_response={},
                calibration_request_raw="action=doSpin",
                calibration_response_raw=b"",
                calibration_response={},
            )

            with (
                patch.object(provider, "_http_bootstrap", return_value=bootstrap) as direct,
                patch.object(
                    provider,
                    "_browser_bootstrap",
                    side_effect=AssertionError("public-page resolver must not be used"),
                ) as resolver,
                patch.object(provider, "_write_discovery"),
                patch.object(provider, "_update_game_protocol_metadata"),
                patch.object(provider, "_record_last_test_in_game_json"),
                patch(
                    "tester_spin.providers.pragmatic.discover_modes",
                    return_value=_EmptyCatalog(),
                ),
            ):
                result = provider.test_game(
                    game,
                    spins=1,
                    timeout_s=5.0,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

            self.assertEqual(result.status, "OK")
            self.assertEqual(result.symbol, "vs20known")
            resolver.assert_not_called()
            direct.assert_called_once_with(
                game.url,
                "vs20known",
                None,
                provider.base_bet,
                60.0,
            )


if __name__ == "__main__":
    unittest.main()
