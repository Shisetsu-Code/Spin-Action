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

    def test_without_bls_uses_line_and_default_coin_fields(self) -> None:
        init = {
            "l": "20",
            "defc": "0.1",
            "sc": "0.05,0.1,0.2",
        }
        catalog = discover_modes(init, requested_base_bet=2.0)
        enabled = {mode.id for mode in catalog.enabled()}
        self.assertEqual(enabled, {"SPIN"})
        self.assertEqual(catalog.base_scale, 20.0)
        self.assertEqual(catalog.base_coin, 0.1)
        self.assertEqual(catalog.base_bet, 2.0)
        self.assertEqual(catalog.mode_evidence["base_scale_source"], "l")
        self.assertEqual(catalog.mode_evidence["base_coin_source"], "defc")

    def test_without_bls_can_use_unit_scale_with_coin_list(self) -> None:
        init = {"sc": "0.5,1,2,5"}
        catalog = discover_modes(init, requested_base_bet=2.0)
        self.assertEqual(catalog.base_scale, 1.0)
        self.assertEqual(catalog.base_coin, 2.0)
        self.assertEqual(catalog.base_bet, 2.0)
        self.assertEqual(catalog.mode_evidence["base_scale_source"], "unit-scale-fallback")

    def test_without_any_wager_evidence_still_fails_explicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "escala de apuesta alternativa"):
            discover_modes({"foo": "bar"}, requested_base_bet=2.0)


if __name__ == "__main__":
    unittest.main()
