from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.pragmatic import PragmaticProvider as BasePragmaticProvider
from tester_spin.providers.pragmatic_exhaustive import PragmaticProvider as ExhaustivePragmaticProvider
from tester_spin.providers.pragmatic_farm_adapter import PragmaticProvider


class PragmaticPurchaseExecutionTests(unittest.TestCase):
    def test_purchase_campaign_uses_exhaustive_fso_wrapper(self) -> None:
        game = Game(
            provider="pragmatic",
            slug="synthetic",
            name="Synthetic",
            url="https://example.invalid/game",
            symbol="vsSynthetic",
        )
        sentinel = GameTestResult(
            provider="pragmatic",
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            symbol=game.symbol,
        )
        with tempfile.TemporaryDirectory() as temp:
            provider = PragmaticProvider(Path(temp))
            with (
                patch.object(
                    BasePragmaticProvider,
                    "test_game",
                    side_effect=AssertionError("purchase path must enter exhaustive Pragmatic wrapper"),
                ) as base_test,
                patch.object(
                    ExhaustivePragmaticProvider,
                    "test_game",
                    return_value=sentinel,
                ) as exhaustive_test,
            ):
                result = provider.test_purchase_paths(
                    game,
                    timeout_s=5.0,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

        self.assertIs(result, sentinel)
        exhaustive_test.assert_called_once()
        base_test.assert_not_called()


if __name__ == "__main__":
    unittest.main()
