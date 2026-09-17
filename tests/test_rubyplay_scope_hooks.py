from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.rubyplay.farm_adapter import RubyPlayProvider


def _result(game: Game) -> GameTestResult:
    return GameTestResult(
        provider="rubyplay",
        slug=game.slug,
        game_name=game.name,
        game_url=game.url,
        requested_spins=1,
        successful_spins=1,
        failed_spins=0,
        status="OK",
    )


class RubyPlayScopeHookTests(unittest.TestCase):
    def test_natural_spin_entry_activates_natural_only_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = RubyPlayProvider(Path(temp))
            game = Game(provider="rubyplay", slug="g", name="G", url="https://example.invalid/g")
            seen = []

            def execute(self, target, *, spins, timeout_s, stop_event, progress):
                seen.append((self.rubyplay_execution_scope(), spins))
                return _result(target)

            with patch.object(RubyPlayProvider, "test_game", autospec=True, side_effect=execute):
                provider.test_natural_spins(
                    game,
                    spins=50,
                    timeout_s=5.0,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

        self.assertEqual(seen, [("NATURAL_ONLY", 50)])
        self.assertEqual(provider.rubyplay_execution_scope(), "ALL")

    def test_purchase_entry_activates_purchase_only_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = RubyPlayProvider(Path(temp))
            game = Game(provider="rubyplay", slug="g", name="G", url="https://example.invalid/g")
            seen = []

            def execute(self, target, *, spins, timeout_s, stop_event, progress):
                seen.append((self.rubyplay_execution_scope(), spins))
                return _result(target)

            with patch.object(RubyPlayProvider, "test_game", autospec=True, side_effect=execute):
                provider.test_purchase_paths(
                    game,
                    timeout_s=5.0,
                    stop_event=threading.Event(),
                    progress=lambda _message: None,
                )

        self.assertEqual(seen, [("PURCHASE_ONLY", 1)])
        self.assertEqual(provider.rubyplay_execution_scope(), "ALL")


if __name__ == "__main__":
    unittest.main()
