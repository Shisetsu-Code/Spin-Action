from __future__ import annotations

import unittest

from tester_spin.providers import BGamingProvider, PragmaticProvider


class ProviderWiringTests(unittest.TestCase):
    def test_active_pragmatic_provider_is_hybrid(self) -> None:
        self.assertEqual(
            PragmaticProvider.__module__,
            "tester_spin.providers.pragmatic_hybrid",
        )

    def test_active_bgaming_provider_uses_package_artifact_layer(self) -> None:
        self.assertEqual(
            BGamingProvider.__module__,
            "tester_spin.providers.bgaming",
        )


if __name__ == "__main__":
    unittest.main()
