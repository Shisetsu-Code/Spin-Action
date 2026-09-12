from __future__ import annotations

import unittest

from tester_spin.providers.redtiger.bootstrap_browser import _bootstrap_timeouts


class RedTigerBootstrapTimingTests(unittest.TestCase):
    def test_default_provider_budget_no_longer_forces_sixty_seconds(self) -> None:
        navigation_ms, total_ms, post_json_ms = _bootstrap_timeouts(30.0)
        self.assertEqual(navigation_ms, 15_000)
        self.assertEqual(total_ms, 30_000)
        self.assertEqual(post_json_ms, 10_000)

    def test_large_user_budget_keeps_total_but_caps_individual_navigation(self) -> None:
        navigation_ms, total_ms, post_json_ms = _bootstrap_timeouts(90.0)
        self.assertEqual(navigation_ms, 15_000)
        self.assertEqual(total_ms, 90_000)
        self.assertEqual(post_json_ms, 10_000)

    def test_tiny_budget_keeps_safe_minimum(self) -> None:
        navigation_ms, total_ms, post_json_ms = _bootstrap_timeouts(2.0)
        self.assertEqual(navigation_ms, 15_000)
        self.assertEqual(total_ms, 15_000)
        self.assertEqual(post_json_ms, 10_000)


if __name__ == "__main__":
    unittest.main()
