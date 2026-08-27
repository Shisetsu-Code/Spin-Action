from __future__ import annotations

import unittest

from tester_spin.providers import PragmaticProvider


class ProviderWiringTests(unittest.TestCase):
    def test_active_pragmatic_provider_is_hybrid(self) -> None:
        self.assertEqual(
            PragmaticProvider.__module__,
            "tester_spin.providers.pragmatic_hybrid",
        )


if __name__ == "__main__":
    unittest.main()
