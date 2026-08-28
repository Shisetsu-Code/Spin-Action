from __future__ import annotations

import unittest

from tester_spin.providers.pragmatic_endpoint import MAX_WIRE_STEPS, PragmaticProvider


class PragmaticEndpointStateTests(unittest.TestCase):
    def test_purchase_bookkeeping_is_not_feature_activity(self) -> None:
        self.assertFalse(
            PragmaticProvider._feature_active(
                {
                    "na": "s",
                    "puri": "0",
                    "purtr": "1",
                }
            )
        )

    def test_zero_feature_counters_are_not_active(self) -> None:
        self.assertFalse(
            PragmaticProvider._feature_active(
                {
                    "na": "s",
                    "fs": "0",
                    "rs_c": "0",
                    "rs_p": "0",
                }
            )
        )

    def test_long_hold_and_spin_fields_are_active(self) -> None:
        # Representative Mighty Munching Melons state observed near the old
        # 128-step cutoff: the round is still progressing mechanically.
        self.assertTrue(
            PragmaticProvider._feature_active(
                {
                    "na": "s",
                    "rs": "hs",
                    "rs_p": "61",
                    "rs_c": "3",
                    "rs_m": "3",
                    "puri": "0",
                    "purtr": "1",
                }
            )
        )

    def test_wire_guard_allows_long_nested_features(self) -> None:
        self.assertGreaterEqual(MAX_WIRE_STEPS, 1024)


if __name__ == "__main__":
    unittest.main()
