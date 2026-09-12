from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.models import Game, GameTestResult
from tester_spin.providers.base import ProviderAdapter
from tester_spin.scheduler import run_game_tests


class BranchingProvider(ProviderAdapter):
    key = "branching"
    display_name = "Branching"
    catalog_url = "https://example.invalid"

    def __init__(self, root: Path) -> None:
        self.root = root

    def crawl_catalog(self, **_kwargs):
        return []

    def test_game(self, game, *, spins, timeout_s, stop_event, progress):
        run_root = self.root / "run"
        attempt = run_root / "PURCHASE" / "attempt-00001"
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / "response.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "result": {
                        "game": {
                            "choices": {
                                "selected": None,
                                "available": ["LEFT", "RIGHT"],
                            }
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (attempt / "choice-request.json").write_text(
            json.dumps({"choice": "LEFT"}),
            encoding="utf-8",
        )
        return GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            run_dir=str(run_root),
        )


class SchedulerPathCoverageTests(unittest.TestCase):
    def test_scheduler_never_accepts_ok_with_uncovered_branch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = BranchingProvider(Path(temp))
            game = Game(
                provider=provider.key,
                slug="game",
                name="Game",
                url="https://example.invalid/game",
            )
            results: list[GameTestResult] = []
            run_game_tests(
                provider,
                [game],
                concurrency=1,
                spins_per_game=1,
                delay_between_starts_s=0,
                timeout_s=5,
                stop_event=threading.Event(),
                progress=lambda _message: None,
                on_result=results.append,
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].status, "PARCIAL")
            self.assertIn("RIGHT", results[0].error)
            report = json.loads(
                (Path(results[0].run_dir) / "path-coverage.json").read_text(encoding="utf-8")
            )
            self.assertFalse(report["complete"])


if __name__ == "__main__":
    unittest.main()
