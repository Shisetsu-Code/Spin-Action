from __future__ import annotations

import inspect
import threading
import unittest

from tester_spin.providers.redtiger import evolution_launch


class RedTigerEvolutionLaunchTests(unittest.TestCase):
    def test_loader_wait_does_not_require_external_loader_constructor(self) -> None:
        class FakePage:
            def evaluate(self, _script):
                return {
                    "id": "22914",
                    "form": True,
                    "button": True,
                    "api_host": "games.evolution.com",
                    "api_path": "/wp-json/",
                    "has_nonce": True,
                    "loader": False,
                }

            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        resolved = evolution_launch._wait_for_live_start_contract(
            FakePage(),
            expected_launch_id="22914",
            timeout_ms=3000,
            stop_event=threading.Event(),
        )
        self.assertEqual(resolved, "22914")

    def test_start_request_uses_live_page_nonce_and_wp_endpoint(self) -> None:
        source = inspect.getsource(evolution_launch._submit_live_start)
        self.assertIn("window.game_api_settings", source)
        self.assertIn("window.evo_casino_config", source)
        self.assertIn("games/v1/start", source)
        self.assertIn("X-WP-Nonce", source)
        self.assertIn("mobile', 'false", source)
        self.assertIn("document.createElement('iframe')", source)
        self.assertNotIn("fa4f88a22f", source)
        self.assertNotIn("22914", source)

    def test_start_wait_is_cooperatively_cancellable(self) -> None:
        class NeverCalledPage:
            def evaluate(self, _script):
                raise AssertionError("page should not be touched after cancellation")

        stop = threading.Event()
        stop.set()
        with self.assertRaises(InterruptedError):
            evolution_launch._wait_for_live_start_contract(
                NeverCalledPage(),
                expected_launch_id="opaque",
                timeout_ms=3000,
                stop_event=stop,
            )


if __name__ == "__main__":
    unittest.main()
