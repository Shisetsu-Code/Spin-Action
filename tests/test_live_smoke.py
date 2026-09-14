from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from tester_spin.farm_contract import SCHEMA
from tester_spin.live_smoke import LiveSmokeError, run_provider_smoke
from tester_spin.models import Game, GameTestResult, SpinAttempt
from tester_spin.providers.base import ProviderAdapter


class _SmokeProvider(ProviderAdapter):
    key = "smoke"
    display_name = "Smoke"
    catalog_url = "https://example.invalid/games"

    def __init__(self, root: Path, *, ready: bool = True, games: list[Game] | None = None) -> None:
        self.root = root
        self.ready = ready
        self._games = games if games is not None else [
            Game(
                provider=self.key,
                slug="game-a",
                name="Game A",
                url="https://example.invalid/games/game-a",
                symbol="GAME_A",
            )
        ]

    def game_dir(self, game: Game) -> Path:
        path = self.root / game.slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    def farm_contract_dir(self, game: Game) -> Path | None:
        return self.game_dir(game)

    def crawl_catalog(self, *, stop_event, progress, max_pages=100, on_game=None):
        for game in self._games:
            if on_game is not None:
                on_game(game)
        return list(self._games)

    def test_game(self, game, *, spins, timeout_s, stop_event, progress):
        return GameTestResult(
            provider=self.key,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=1,
            successful_spins=1,
            failed_spins=0,
            status="OK",
            symbol=game.symbol,
            run_dir=str(self.game_dir(game) / "tests" / "run"),
            discovered_modes=[
                {
                    "id": "SPIN",
                    "kind": "SPIN",
                    "wire_command": "spin",
                    "observed": True,
                    "executable": True,
                }
            ],
            attempts=[
                SpinAttempt(
                    number=1,
                    ok=True,
                    mode_id="SPIN",
                    mode_kind="SPIN",
                    terminal=True,
                    artifact_dir=str(self.game_dir(game) / "tests" / "run" / "SPIN"),
                )
            ],
        )

    def finalize_test_result(self, result, *, progress):
        return result

    def build_farm_contract(self, game, result):
        unresolved = [] if self.ready else ["SYNTHETIC_NOT_READY"]
        return {
            "schema": SCHEMA,
            "provider": self.key,
            "game": {"slug": game.slug, "name": game.name, "symbol": game.symbol},
            "ready": self.ready,
            "source": {
                "run": result.finished_at,
                "result_status": result.status,
                "protocol_family": "smoke",
            },
            "bootstrap": {
                "strategy": "smoke",
                "inputs": {"public_game_url": game.url, "identifier": game.symbol},
                "runtime_outputs": [],
            },
            "modes": [
                {
                    "id": "SPIN",
                    "kind": "SPIN",
                    "required": True,
                    "evidence": "DEMOSTRADO",
                    "executor": "spin",
                    "options": {},
                }
            ],
            "continuations": {"known": [], "unresolved": []},
            "terminal_contract": {"type": "provider", "name": "smoke"},
            "protocol": {"family": "smoke"},
            "unresolved": unresolved,
        }


class LiveSmokeTests(unittest.TestCase):
    def test_real_flow_writes_candidate_and_reports_current_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = _SmokeProvider(Path(temp))
            report = run_provider_smoke(
                provider,
                slug="game-a",
                max_pages=1,
                timeout_s=5.0,
                progress=lambda _message: None,
            )
            self.assertEqual(report.provider, "smoke")
            self.assertEqual(report.slug, "game-a")
            self.assertEqual(report.result_status, "OK")
            self.assertTrue(report.candidate_exists)
            self.assertTrue(report.promoted_current_run)
            self.assertTrue(report.ready)

    def test_slug_selects_requested_catalog_game(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            provider = _SmokeProvider(
                root,
                games=[
                    Game("smoke", "a", "A", "https://example.invalid/a", symbol="A"),
                    Game("smoke", "b", "B", "https://example.invalid/b", symbol="B"),
                ],
            )
            report = run_provider_smoke(
                provider,
                slug="b",
                max_pages=1,
                timeout_s=5.0,
                progress=lambda _message: None,
            )
            self.assertEqual(report.slug, "b")

    def test_empty_catalog_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = _SmokeProvider(Path(temp), games=[])
            with self.assertRaisesRegex(LiveSmokeError, "catálogo vacío"):
                run_provider_smoke(
                    provider,
                    max_pages=1,
                    timeout_s=5.0,
                    progress=lambda _message: None,
                )

    def test_require_promoted_rejects_nonready_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = _SmokeProvider(Path(temp), ready=False)
            with self.assertRaisesRegex(LiveSmokeError, "no promovió"):
                run_provider_smoke(
                    provider,
                    max_pages=1,
                    timeout_s=5.0,
                    require_promoted=True,
                    progress=lambda _message: None,
                )


if __name__ == "__main__":
    unittest.main()
