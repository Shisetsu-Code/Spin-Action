from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from tester_spin.models import Game, GameTestResult, SpinAttempt
from tester_spin.providers.rubyplay import availability


GAME = Game(
    provider="rubyplay",
    slug="offline-demo",
    name="Offline Demo",
    url="https://rubyplay.com/games/offline-demo/",
)


def _result(error: str, *, attempts=None) -> GameTestResult:
    return GameTestResult(
        provider="rubyplay",
        slug=GAME.slug,
        game_name=GAME.name,
        game_url=GAME.url,
        requested_spins=1,
        successful_spins=0,
        failed_spins=1,
        status="ERROR",
        error=error,
        attempts=list(attempts or []),
    )


class RubyPlayAvailabilityTests(unittest.TestCase):
    def _call(self, result: GameTestResult) -> GameTestResult:
        logs: list[str] = []
        with patch.object(availability, "_ORIGINAL_TEST_GAME", return_value=result):
            returned = availability.test_game(
                object(),
                GAME,
                spins=1,
                timeout_s=5.0,
                stop_event=threading.Event(),
                progress=logs.append,
            )
        self.logs = logs
        return returned

    def test_bootstrap_404_is_sin_demo_not_protocol_error(self) -> None:
        result = self._call(
            _result(
                "HTTPError: 404 Client Error: Not Found for url: "
                "https://prrpeu3.com/launcher?..."
            )
        )
        self.assertEqual(result.status, "SIN_DEMO")
        self.assertEqual(result.requested_spins, 0)
        self.assertEqual(result.failed_spins, 0)
        self.assertIn("fuera de servicio", result.error)
        self.assertTrue(any("SIN_DEMO" in line for line in self.logs))

    def test_non_404_error_remains_error(self) -> None:
        result = self._call(_result("HTTPError: 500 Server Error"))
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.requested_spins, 1)

    def test_404_after_executable_attempt_is_not_hidden(self) -> None:
        attempt = SpinAttempt(number=1, ok=False, error="HTTP 404")
        result = self._call(
            _result(
                "HTTPError: 404 Client Error: Not Found for gameserver",
                attempts=[attempt],
            )
        )
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.requested_spins, 1)


if __name__ == "__main__":
    unittest.main()
