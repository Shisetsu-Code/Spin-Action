from __future__ import annotations

import unittest

from tester_spin.providers.pragmatic_modes import discover_modes


class PragmaticModeDiscoveryTests(unittest.TestCase):
    def test_discovers_base_ante_bets_and_purchases(self) -> None:
        init = {
            "bls": "15,22.5,300",
            "sc": "0.01,0.02,0.05,0.1,0.2",
            "purInit": '[{"bet":1500},{"bet":3000}]',
            "purInit_e": "1,1",
            "bonusFeature": "future-evidence",
        }
        catalog = discover_modes(init, requested_base_bet=1.5)
        modes = {mode.id: mode for mode in catalog.enabled()}

        self.assertIn("SPIN", modes)
        self.assertIn("ANTE_BET_1", modes)
        self.assertIn("ANTE_BET_2", modes)
        self.assertIn("PURCHASE_1", modes)
        self.assertIn("PURCHASE_2", modes)
        self.assertEqual(modes["ANTE_BET_1"].provider_bl, 1)
        self.assertEqual(modes["PURCHASE_1"].provider_pur, 0)
        self.assertIn("bonusFeature", catalog.mode_evidence)

    def test_disabled_purchase_is_not_returned_by_enabled(self) -> None:
        init = {
            "bls": "15",
            "sc": "0.1",
            "purInit": '[{"bet":1500},{"bet":3000}]',
            "purInit_e": "1,0",
        }
        catalog = discover_modes(init, requested_base_bet=1.5)
        enabled = {mode.id for mode in catalog.enabled()}
        self.assertIn("PURCHASE_1", enabled)
        self.assertNotIn("PURCHASE_2", enabled)


if __name__ == "__main__":
    unittest.main()
