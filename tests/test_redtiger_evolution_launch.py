from __future__ import annotations

import inspect
import threading
import unittest

from tester_spin.providers.redtiger import evolution_launch
from tester_spin.providers.redtiger import execution


class RedTigerEvolutionLaunchTests(unittest.TestCase):
    def test_execution_uses_live_evolution_launch_bootstrap(self) -> None:
        self.assertIs(execution.bootstrap_game, evolution_launch.bootstrap_game)

    def test_live_start_contract_does_not_require_button_or_loader(self) -> None:
        class FakePage:
            def evaluate(self, _script):
                return {
                    "id": "22914",
                    "form": True,
                    "button": False,
                    "api_host": "games.evolution.com",
                    "api_path": "/wp-json/",
                    "has_nonce": True,
                    "loader": True,
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

    def test_contract_wait_uses_wire_inputs_not_visible_button(self) -> None:
        source = inspect.getsource(evolution_launch._wait_for_live_start_contract)
        acceptance = source.split("if (", 1)[-1]
        self.assertIn("api_host", source)
        self.assertIn("api_path", source)
        self.assertIn("has_nonce", source)
        self.assertNotIn('and bool(state.get("button"))', source)
        self.assertNotIn('and bool(state.get("loader"))', source)

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
