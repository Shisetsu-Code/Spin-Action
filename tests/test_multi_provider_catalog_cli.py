from __future__ import annotations

import unittest

from scripts.multi_provider_catalog import parse_provider_keys


class MultiProviderCatalogCliTests(unittest.TestCase):
    def test_all_expands_to_approved_default_set(self) -> None:
        self.assertEqual(
            parse_provider_keys("all"),
            ["pragmatic", "1spin4win", "belatra", "rubyplay", "redtiger"],
        )

    def test_csv_selection_is_deduplicated_and_preserves_order(self) -> None:
        self.assertEqual(
            parse_provider_keys("rubyplay,redtiger,rubyplay"),
            ["rubyplay", "redtiger"],
        )

    def test_bgaming_is_rejected_from_batch_runner(self) -> None:
        with self.assertRaisesRegex(ValueError, "bgaming"):
            parse_provider_keys("pragmatic,bgaming")


if __name__ == "__main__":
    unittest.main()
