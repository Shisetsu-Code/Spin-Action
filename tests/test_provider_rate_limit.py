from __future__ import annotations

import threading
import unittest

from tester_spin.provider_rate_limit import ProviderRequestRateLimiter


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
            seconds = max(0.0, float(seconds))
            self.sleeps.append(seconds)
            self.value += seconds


class ProviderRequestRateLimiterTests(unittest.TestCase):
    def test_blocks_before_exceeding_window_ceiling(self) -> None:
        fake = _FakeTime()
        limiter = ProviderRequestRateLimiter(
            requests_per_minute=2,
            clock=fake.clock,
            sleep=fake.sleep,
        )

        limiter.acquire()
        limiter.acquire()
        limiter.acquire()

        self.assertEqual(limiter.total_acquired, 3)
        self.assertEqual(fake.sleeps, [60.0])
        self.assertLessEqual(limiter.requests_in_current_window, 2)

    def test_limit_is_shared_by_concurrent_game_workers(self) -> None:
        limiter = ProviderRequestRateLimiter(requests_per_minute=2000)
        barrier = threading.Barrier(4)

        def worker() -> None:
            barrier.wait()
            for _ in range(25):
                limiter.acquire()

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5.0)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(limiter.total_acquired, 75)
        self.assertEqual(limiter.requests_in_current_window, 75)

    def test_stop_event_cancels_wait_instead_of_sending_more(self) -> None:
        fake = _FakeTime()
        limiter = ProviderRequestRateLimiter(
            requests_per_minute=1,
            clock=fake.clock,
            sleep=fake.sleep,
        )
        limiter.acquire()
        stop = threading.Event()
        stop.set()

        acquired = limiter.acquire(stop_event=stop)

        self.assertFalse(acquired)
        self.assertEqual(limiter.total_acquired, 1)
        self.assertEqual(fake.sleeps, [])

    def test_stats_expose_configured_ceiling_and_total(self) -> None:
        limiter = ProviderRequestRateLimiter(requests_per_minute=1234)
        limiter.acquire()
        stats = limiter.snapshot()

        self.assertEqual(stats["requests_per_minute_limit"], 1234)
        self.assertEqual(stats["total_acquired"], 1)
        self.assertGreaterEqual(stats["requests_in_current_window"], 1)


if __name__ == "__main__":
    unittest.main()
