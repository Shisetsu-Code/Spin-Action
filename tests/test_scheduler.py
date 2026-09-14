from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from tester_spin.farm_contract import SCHEMA
from tester_spin.models import Game, GameTestResult
from tester_spin.providers import BGamingProvider
from tester_spin.providers.base import ProviderAdapter
from tester_spin.scheduler import run_game_tests


class _LimitedProvider(ProviderAdapter):
    key = "limited"
    display_name = "Limited Provider"
    catalog_url = "https://example.test"
    max_test_concurrency = 1

    def __init__(self, *, fail_prepare: bool = False) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.prepared: list[str] = []
        self.fail_prepare = fail_prepare

    def crawl_catalog(self, **_kwargs):
        return []

    def prepare_test_artifacts(
        self,
        game: Game,
        *,
        timeout_s: float,
        stop_event: threading.Event,
        progress,
    ) -> None:
        self.prepared.append(game.slug)
        progress(f"prepared:{game.slug}")
        if self.fail_prepare:
            raise RuntimeError("artifact capture failed")

    def test_game(
        self,
        game: Game,
        *,
        spins: int,
        timeout_s: float,
        stop_event: threading.Event,
        progress,
    ) -> GameTestResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.03)
            return GameTestResult(
                provider=game.provider,
                slug=game.slug,
                game_name=game.name,
                game_url=game.url,
                requested_spins=spins,
                successful_spins=spins,
                failed_spins=0,
                status="OK",
                symbol=game.symbol,
            )
        finally:
            with self._lock:
                self.active -= 1


class _OrderingProvider(_LimitedProvider):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.events: list[str] = []

    def test_game(self, game, *, spins, timeout_s, stop_event, progress):
        self.events.append("test")
        return GameTestResult(
            provider=game.provider,
            slug=game.slug,
            game_name=game.name,
            game_url=game.url,
            requested_spins=spins,
            successful_spins=spins,
            failed_spins=0,
            status="OK",
            symbol=game.symbol,
        )

    def finalize_test_result(self, result, *, progress):
        self.events.append("finalize")
        return result

    def farm_contract_dir(self, game):
        return self.root

    def build_farm_contract(self, game, result):
        self.events.append("build")
        return {
            "schema": SCHEMA,
            "provider": result.provider,
            "game": {
                "slug": result.slug,
                "name": result.game_name,
                "symbol": result.symbol,
            },
            "ready": False,
            "source": {
                "run": result.finished_at,
                "protocol_family": "synthetic",
            },
            "bootstrap": {},
            "modes": [],
            "continuations": {"known": [], "unresolved": []},
            "terminal_contract": {},
            "protocol": {},
            "unresolved": ["TEST_NOT_READY"],
        }


class SchedulerConcurrencyTests(unittest.TestCase):
    def test_provider_effective_concurrency_applies_cap(self) -> None:
        provider = _LimitedProvider()
        self.assertEqual(provider.effective_test_concurrency(1), 1)
        self.assertEqual(provider.effective_test_concurrency(3), 1)

    def test_bgaming_respects_requested_concurrency(self) -> None:
        provider = BGamingProvider.__new__(BGamingProvider)
        self.assertEqual(provider.effective_test_concurrency(3), 3)

    def test_scheduler_enforces_provider_cap_even_if_three_requested(self) -> None:
        provider = _LimitedProvider()
        games = [
            Game(
                provider=provider.key,
                slug=f"game-{index}",
                name=f"Game {index}",
                url=f"https://example.test/game-{index}",
            )
            for index in range(3)
        ]
        results: list[GameTestResult] = []
        logs: list[str] = []

        run_game_tests(
            provider,
            games,
            concurrency=3,
            spins_per_game=1,
            delay_between_starts_s=0.0,
            timeout_s=5.0,
            stop_event=threading.Event(),
            progress=logs.append,
            on_result=results.append,
        )

        self.assertEqual(len(results), 3)
        self.assertEqual(provider.max_active, 1)
        self.assertEqual(
            provider.prepared,
            ["game-0", "game-1", "game-2"],
        )
        self.assertTrue(
            any("concurrencia solicitada=3" in line for line in logs),
            logs,
        )
        self.assertEqual(
            sum("prepared:" in line for line in logs),
            3,
        )

    def test_artifact_preparation_failure_does_not_block_game_test(self) -> None:
        provider = _LimitedProvider(fail_prepare=True)
        game = Game(
            provider=provider.key,
            slug="game",
            name="Game",
            url="https://example.test/game",
        )
        results: list[GameTestResult] = []
        logs: list[str] = []

        run_game_tests(
            provider,
            [game],
            concurrency=1,
            spins_per_game=1,
            delay_between_starts_s=0.0,
            timeout_s=5.0,
            stop_event=threading.Event(),
            progress=logs.append,
            on_result=results.append,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "OK")
        self.assertTrue(
            any("preparación de artefactos ERROR" in line for line in logs),
            logs,
        )

    def test_farm_contract_build_runs_after_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = _OrderingProvider(Path(temp))
            game = Game(
                provider=provider.key,
                slug="game",
                name="Game",
                url="https://example.test/game",
            )
            results: list[GameTestResult] = []

            run_game_tests(
                provider,
                [game],
                concurrency=1,
                spins_per_game=1,
                delay_between_starts_s=0.0,
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=lambda _message: None,
                on_result=results.append,
            )

            self.assertEqual(provider.events, ["test", "finalize", "build"])
            self.assertEqual(results[0].status, "OK")
            self.assertTrue(
                (Path(temp) / "analysis" / "farm-contract-candidate.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
