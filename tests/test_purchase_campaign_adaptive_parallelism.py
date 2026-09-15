from __future__ import annotations

import inspect
import tempfile
import threading
import time
import unittest
from pathlib import Path

import scripts.purchase_campaign as purchase_campaign
from tester_spin.models import Game, GameTestResult
from tester_spin.provider_rate_limit import ProviderRequestRateLimiter
from tester_spin.purchase_coverage import PURCHASE_COMPLETE


class _FakeTime:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []
        self.lock = threading.Lock()

    def clock(self) -> float:
        with self.lock:
            return self.value

    def sleep(self, seconds: float) -> None:
        with self.lock:
            value = max(0.0, float(seconds))
            self.sleeps.append(value)
            self.value += value


class _ParallelProvider:
    key = "parallel"
    max_test_concurrency = None

    def __init__(self, cap: int | None) -> None:
        self.max_test_concurrency = cap
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def effective_test_concurrency(self, requested: int) -> int:
        requested = max(1, int(requested))
        if self.max_test_concurrency is None:
            return requested
        return min(requested, max(1, int(self.max_test_concurrency)))

    def test_purchase_paths(self, game, *, timeout_s, stop_event, progress):
        del timeout_s, stop_event, progress
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.04)
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
                run_dir="",
            )
        finally:
            with self._lock:
                self.active -= 1

    def build_purchase_coverage(self, game, result):
        del result
        return {
            "state": PURCHASE_COMPLETE,
            "provider": self.key,
            "game_slug": game.slug,
            "counts": {"total": 1, "complete": 1, "failed": 0, "unknown": 0},
            "options": [{"purchase_id": "P1"}],
        }


class AdaptiveParallelismTests(unittest.TestCase):
    def test_high_rpm_is_paced_instead_of_released_as_initial_burst(self) -> None:
        fake = _FakeTime()
        limiter = ProviderRequestRateLimiter(
            requests_per_minute=120,
            clock=fake.clock,
            sleep=fake.sleep,
        )

        limiter.acquire()
        limiter.acquire()
        limiter.acquire()

        self.assertEqual(limiter.total_acquired, 3)
        self.assertEqual(len(fake.sleeps), 2)
        self.assertAlmostEqual(fake.sleeps[0], 0.5, places=6)
        self.assertAlmostEqual(fake.sleeps[1], 0.5, places=6)

    def test_campaign_exposes_provider_capped_worker_resolution(self) -> None:
        resolver = getattr(purchase_campaign, "resolve_purchase_concurrency", None)
        self.assertTrue(callable(resolver), "purchase campaign must expose provider-capped concurrency")
        if resolver is None:
            return
        self.assertEqual(resolver(_ParallelProvider(cap=1), 8), 1)
        self.assertEqual(resolver(_ParallelProvider(cap=3), 8), 3)
        self.assertEqual(resolver(_ParallelProvider(cap=None), 8), 8)

    def test_runner_parallelizes_games_but_obeys_provider_cap(self) -> None:
        signature = inspect.signature(purchase_campaign.run_selected_games)
        self.assertIn("max_workers", signature.parameters)
        if "max_workers" not in signature.parameters:
            return

        games = [
            Game(provider="parallel", slug=f"g-{i}", name=f"Game {i}", url=f"https://example/{i}")
            for i in range(6)
        ]
        provider = _ParallelProvider(cap=2)
        with tempfile.TemporaryDirectory() as temp:
            rows = purchase_campaign.run_selected_games(
                provider,
                games,
                timeout_s=5.0,
                stop_event=threading.Event(),
                output_dir=Path(temp),
                progress=lambda _message: None,
                inter_game_delay_s=0.0,
                circuit_breaker_threshold=0,
                max_workers=8,
            )

        self.assertEqual(len(rows), len(games))
        self.assertEqual(provider.max_active, 2)


if __name__ == "__main__":
    unittest.main()
